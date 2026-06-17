"""
AWS Glue Service for Data Source Management

This service provides methods to interact with AWS Glue for:
- Listing databases and tables across all catalogs (default + federated)
- Getting table metadata and schema information
- Managing Glue crawlers
- Extracting metadata for semantic metadata generation
"""

import json
import logging
import os
import time
import boto3
from typing import List, Dict, Any, Optional
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class GlueService:
    """Service class for AWS Glue operations"""

    def __init__(self):
        """Initialize AWS Glue and S3 Tables clients"""
        config = Config(
            retries={'max_attempts': 3, 'mode': 'standard'}
        )
        self.glue_client = boto3.client('glue', config=config)
        self._s3t_client = boto3.client('s3tables', config=config)
        # Cache: bucket-name → ARN, populated lazily by _bucket_arn()
        self._bucket_arn_cache: Dict[str, str] = {}
        # Cache: bucket-ARN → PyIceberg RestCatalog instance
        self._iceberg_catalog_cache: Dict[str, Any] = {}
        logger.info("GlueService initialized")

    def _data_source_for_catalog(self, catalog_id: Optional[str]) -> str:
        """
        Derive the Athena data source name from a catalog ID.

        - None or 'AWSDataCatalog' → 'AwsDataCatalog'  (built-in Glue catalog)
        - starts with 's3tablescatalog/' → 'AwsDataCatalog'  (S3 Tables is a sub-catalog)
        - any other value (e.g. 'dynamodb_catalog') → catalog_id itself
          (registered federated connectors are their own data source)

        Args:
            catalog_id: The catalog ID string (e.g., 'AWSDataCatalog', 's3tablescatalog/<bucket>',
                        or a registered federated connector name like 'dynamodb_catalog')

        Returns:
            Athena data source identifier string
        """
        if not catalog_id or catalog_id == 'AWSDataCatalog':
            return 'AwsDataCatalog'
        if catalog_id.startswith('s3tablescatalog/'):
            return 'AwsDataCatalog'
        return catalog_id

    def _bucket_arn(self, bucket_name: str) -> Optional[str]:
        """
        Return the ARN of an S3 table bucket by name, using a local cache to
        avoid repeated list_table_buckets calls within the same request.
        """
        if bucket_name in self._bucket_arn_cache:
            return self._bucket_arn_cache[bucket_name]
        try:
            paginator = self._s3t_client.get_paginator('list_table_buckets')
            for page in paginator.paginate():
                for bucket in page.get('tableBuckets', []):
                    self._bucket_arn_cache[bucket['name']] = bucket['arn']
            return self._bucket_arn_cache.get(bucket_name)
        except Exception as e:
            logger.warning(f"Could not look up S3 table bucket ARN for '{bucket_name}': {e}")
            return None

    def _get_region(self) -> str:
        """Return the current AWS region, preferring Lambda environment variables."""
        return (
            os.environ.get('AWS_REGION')
            or os.environ.get('AWS_DEFAULT_REGION')
            or self.glue_client.meta.region_name
            or 'us-east-1'
        )

    def _enrich_from_metadata_location(self, metadata_location: str) -> Optional[Dict[str, Any]]:
        """
        Read an Iceberg metadata JSON file directly from S3 and return the
        table-level description and per-column doc strings.

        This is faster and more reliable than PyIceberg RestCatalog because it
        avoids SigV4 re-negotiation and works whenever the Lambda has s3:GetObject
        on the table bucket.  Called when 'metadata_location' is present in the
        Glue table Parameters dict (always the case for S3 Tables).

        Args:
            metadata_location: s3://… URI from Glue table Parameters.

        Returns:
            ``{'description': str, 'col_docs': {name: doc}}`` or ``None`` on error.
        """
        if not metadata_location or not metadata_location.startswith('s3://'):
            return None
        try:
            without_prefix = metadata_location[5:]
            bucket, key = without_prefix.split('/', 1)
            s3 = boto3.client('s3')
            resp = s3.get_object(Bucket=bucket, Key=key)
            data = json.loads(resp['Body'].read())

            description = data.get('properties', {}).get('description', '')

            # Resolve the current schema
            current_schema_id = data.get('current-schema-id', 0)
            schemas = data.get('schemas', [])
            current_schema = next(
                (s for s in schemas if s.get('schema-id') == current_schema_id),
                schemas[-1] if schemas else {}
            )
            col_docs: Dict[str, str] = {
                field['name'].lower(): field['doc']
                for field in current_schema.get('fields', [])
                if field.get('doc')
            }

            logger.debug(
                f"Direct S3 enrichment from {metadata_location}: "
                f"description={'yes' if description else 'no'}, "
                f"{len(col_docs)} column doc(s)"
            )
            return {'description': description, 'col_docs': col_docs}

        except Exception as e:
            logger.warning(f"Could not read Iceberg metadata from '{metadata_location}': {e}")
            return None

    def _enrich_with_iceberg(
        self, catalog_id: str, database_name: str, table_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Load the PyIceberg table via the S3 Tables REST catalog and return
        Iceberg-native metadata that Glue federation does not expose:

        - ``description``: from ``table.properties['description']``
        - ``col_docs``:    mapping of column-name → ``field.doc`` for every
                           field that carries a non-empty doc string

        The method is intentionally forgiving: any error (import failure,
        network, auth, table not found) is logged as a warning and returns
        ``None`` so callers can degrade gracefully to Glue-only metadata.

        Args:
            catalog_id:     Catalog ID in the form ``s3tablescatalog/<bucket>``.
            database_name:  S3 Tables namespace (= Glue database name).
            table_name:     Iceberg table name.

        Returns:
            ``{'description': str, 'col_docs': {name: doc}}`` or ``None``.
        """
        try:
            from pyiceberg.catalog.rest import RestCatalog  # lazy import
        except ImportError:
            logger.debug("pyiceberg not installed — skipping Iceberg enrichment")
            return None

        try:
            bucket_name = catalog_id.split('/', 1)[1]
            bucket_arn = self._bucket_arn(bucket_name)
            if not bucket_arn:
                logger.warning(
                    f"Cannot enrich {database_name}.{table_name}: "
                    f"no ARN found for bucket '{bucket_name}'"
                )
                return None

            region = self._get_region()

            # Reuse catalog instance per bucket to avoid re-negotiating auth
            if bucket_arn not in self._iceberg_catalog_cache:
                self._iceberg_catalog_cache[bucket_arn] = RestCatalog(
                    name="s3tables",
                    **{
                        "uri": f"https://s3tables.{region}.amazonaws.com/iceberg",
                        "warehouse": bucket_arn,
                        "rest.sigv4-enabled": "true",
                        "rest.signing-name": "s3tables",
                        "rest.signing-region": region,
                    },
                )

            catalog = self._iceberg_catalog_cache[bucket_arn]
            table = catalog.load_table((database_name, table_name))

            # Table-level description stored as an Iceberg table property
            description = table.properties.get('description', '')

            # Per-column doc strings from the Iceberg schema.
            # Key by lowercase name so the caller can match against Glue column names
            # (which are always lowercase) even when the Iceberg schema uses PascalCase
            # (e.g. "CodeValue" from a DynamoDB backfill).
            col_docs: Dict[str, str] = {
                field.name.lower(): field.doc
                for field in table.schema().fields
                if field.doc
            }

            logger.debug(
                f"Iceberg enrichment OK for {catalog_id}.{database_name}.{table_name}: "
                f"description={'yes' if description else 'no'}, "
                f"{len(col_docs)} column doc(s)"
            )
            return {'description': description, 'col_docs': col_docs}

        except Exception as e:
            logger.warning(
                f"Iceberg enrichment failed for {catalog_id}.{database_name}.{table_name}: {e}"
            )
            return None

    def _list_s3tables(self, database_name: str, catalog_id: str, enrich: bool = True) -> List[Dict[str, Any]]:
        """
        List tables in an S3 Tables namespace using the dedicated s3tables API.

        Glue's get_tables cannot enumerate S3 Tables — the s3tables client is required.

        Args:
            database_name: Glue database name, which maps to the S3 Tables namespace.
            catalog_id: Catalog ID in the form 's3tablescatalog/<bucket-name>'.
            enrich: When True (default), fetches per-table Iceberg metadata (description,
                    column docs) via Glue get_table + S3 read.  Pass False for lightweight
                    existence checks (e.g. non-empty filter in list_databases) to avoid
                    N×M API calls across many namespaces.

        Returns:
            List of table dicts in the same shape as list_tables().
        """
        bucket_name = catalog_id.split('/', 1)[1]
        bucket_arn = self._bucket_arn(bucket_name)
        if not bucket_arn:
            logger.warning(f"No table bucket ARN found for '{bucket_name}', returning empty list")
            return []

        tables: List[Dict[str, Any]] = []
        try:
            paginator = self._s3t_client.get_paginator('list_tables')
            for page in paginator.paginate(tableBucketARN=bucket_arn, namespace=database_name):
                for table in page.get('tables', []):
                    tables.append({
                        'name': table['name'],
                        'databaseName': database_name,
                        'catalogId': catalog_id,
                        'dataSource': self._data_source_for_catalog(catalog_id),
                        'description': '',
                        'location': '',
                        'columns': 0,
                        'createTime': table['createdAt'].isoformat() if table.get('createdAt') else None,
                        'updateTime': table['modifiedAt'].isoformat() if table.get('modifiedAt') else None,
                        'tableType': 'ICEBERG',
                    })
        except Exception as e:
            logger.warning(f"Error listing S3 Tables in '{catalog_id}.{database_name}': {e}", exc_info=True)
            raise

        # Enrich each table's description from Iceberg table properties.
        # s3tables.list_tables() returns no description — call Glue get_table to
        # obtain the metadata_location from table Parameters, then read the
        # Iceberg metadata JSON directly from S3 (faster and more reliable than
        # the PyIceberg RestCatalog path).  Fall back to PyIceberg if S3 read fails.
        # Skip when enrich=False (lightweight existence check — avoids N×M API calls).
        if enrich:
            for tbl in tables:
                try:
                    glue_resp = self.glue_client.get_table(
                        CatalogId=catalog_id,
                        DatabaseName=database_name,
                        Name=tbl['name'],
                    )
                    metadata_location = glue_resp['Table'].get('Parameters', {}).get('metadata_location', '')
                except Exception as e:
                    logger.debug(f"get_table failed for {tbl['name']} during list enrichment: {e}")
                    metadata_location = ''

                enrichment = self._enrich_from_metadata_location(metadata_location)
                if not enrichment:
                    enrichment = self._enrich_with_iceberg(catalog_id, database_name, tbl['name'])
                if enrichment and enrichment.get('description'):
                    tbl['description'] = enrichment['description']

        logger.info(f"Listed {len(tables)} S3 Tables in '{catalog_id}.{database_name}'")
        return tables

    def _discover_catalog_ids(self) -> List[str]:
        """
        Dynamically discover all Glue catalog IDs beyond the default AWSDataCatalog.

        Calls GetCatalogs to enumerate top-level catalogs (e.g. 's3tablescatalog'),
        then recurses one level to find sub-catalogs (e.g. individual S3 table bucket
        names), building paths like 's3tablescatalog/<bucket-name>'.

        Returns:
            List of catalog ID paths for federated catalogs only (AWSDataCatalog excluded).
        """
        catalog_ids: List[str] = []
        try:
            kwargs: Dict[str, Any] = {}
            while True:
                response = self.glue_client.get_catalogs(**kwargs)
                for catalog in response.get('CatalogList', []):
                    top_name: str = catalog['Name']
                    # Attempt to list sub-catalogs (e.g. bucket names under s3tablescatalog)
                    try:
                        sub_kwargs: Dict[str, Any] = {'ParentCatalogId': top_name}
                        found_sub = False
                        while True:
                            sub_response = self.glue_client.get_catalogs(**sub_kwargs)
                            for sub in sub_response.get('CatalogList', []):
                                catalog_ids.append(f"{top_name}/{sub['Name']}")
                                found_sub = True
                            if 'NextToken' in sub_response:
                                sub_kwargs['NextToken'] = sub_response['NextToken']
                            else:
                                break
                        if not found_sub:
                            # Top-level catalog has no sub-catalogs — query it directly
                            catalog_ids.append(top_name)
                    except ClientError:
                        # No sub-catalogs or access denied — treat as directly queryable
                        catalog_ids.append(top_name)
                if 'NextToken' in response:
                    kwargs['NextToken'] = response['NextToken']
                else:
                    break
        except Exception as e:
            logger.warning(f"Dynamic catalog discovery unavailable (get_catalogs): {e}")
        logger.info(f"Discovered federated catalog IDs: {catalog_ids}")
        return catalog_ids

    def list_databases(self) -> List[Dict[str, Any]]:
        """
        List all non-empty Glue databases across all catalogs.

        Always queries the default AWSDataCatalog first, then dynamically discovers
        and queries any additional federated catalogs (e.g. S3 Tables via s3tablescatalog,
        DynamoDB federated connector).

        A database is excluded only when list_tables confirms it is empty (returns an empty
        list without raising). If list_tables raises for a database it is included rather than
        silently dropped — this prevents S3 Tables / federated namespaces from disappearing
        when the tables check itself encounters a transient error.

        Returns:
            List of database dicts, each including a 'catalogId' field.
        """
        try:
            all_databases = []
            paginator = self.glue_client.get_paginator('get_databases')

            # Default AWSDataCatalog
            for page in paginator.paginate():
                for db in page.get('DatabaseList', []):
                    all_databases.append({
                        'name': db['Name'],
                        'catalogId': 'AWSDataCatalog',
                        'dataSource': self._data_source_for_catalog('AWSDataCatalog'),
                        'description': db.get('Description', ''),
                        'location': db.get('LocationUri', ''),
                        'createTime': db.get('CreateTime').isoformat() if db.get('CreateTime') else None,
                    })

            # Dynamically discovered federated catalogs (e.g. s3tablescatalog/<bucket>)
            for catalog_id in self._discover_catalog_ids():
                try:
                    for page in paginator.paginate(CatalogId=catalog_id):
                        for db in page.get('DatabaseList', []):
                            db_name = db['Name']
                            # Skip zetl_* namespaces — internal Zero-ETL replication staging
                            # namespaces that are never user-facing data sources.  Including
                            # them causes N×M API call explosion (20 namespaces × 12 tables)
                            # and makes the databases list unusable for metadata selection.
                            if db_name.startswith('zetl_'):
                                logger.debug(f"Skipping internal Zero-ETL namespace '{db_name}'")
                                continue
                            all_databases.append({
                                'name': db_name,
                                'catalogId': catalog_id,
                                'dataSource': self._data_source_for_catalog(catalog_id),
                                'description': db.get('Description', ''),
                                'location': db.get('LocationUri', ''),
                                'createTime': db.get('CreateTime').isoformat() if db.get('CreateTime') else None,
                            })
                except ClientError as e:
                    logger.warning(f"Could not list databases for catalog '{catalog_id}': {e}")

            # Filter: keep only databases that are confirmed non-empty.
            # Rule: exclude on confirmed-empty (empty list, no error).
            #       include on error (can't verify — safe default avoids false negatives).
            # Use enrich=False to skip per-table Iceberg metadata API calls during this
            # existence check — descriptions are not needed here and each enrichment call
            # adds 2 API round-trips per table, which causes request timeouts at scale.
            databases = []
            for db in all_databases:
                try:
                    tables = self.list_tables(db['name'], catalog_id=db['catalogId'], enrich=False)
                    if tables:
                        databases.append(db)
                    else:
                        logger.debug(f"Skipping confirmed-empty database '{db['catalogId']}.{db['name']}'")
                except Exception as e:
                    logger.warning(
                        f"Could not verify tables for '{db['catalogId']}.{db['name']}', "
                        f"including to avoid false-negative: {e}"
                    )
                    databases.append(db)

            logger.info(
                f"Listed {len(databases)} non-empty databases "
                f"({len(all_databases) - len(databases)} empty skipped) across all catalogs"
            )
            return databases

        except ClientError as e:
            logger.warning(f"Error listing Glue databases: {e}", exc_info=True)
            raise

    def list_tables(self, database_name: str, catalog_id: Optional[str] = None, enrich: bool = True) -> List[Dict[str, Any]]:
        """
        List all tables in a specific Glue database or S3 Tables namespace.

        Dispatches to the correct API based on the catalog type:
        - S3 Tables (catalog_id starts with 's3tablescatalog/') → boto3 s3tables client
          (Glue's get_tables cannot enumerate S3 Tables tables)
        - All other catalogs → Glue get_tables paginator

        Args:
            database_name: Glue database name (maps to S3 Tables namespace for S3 Tables).
            catalog_id: Optional catalog ID (e.g. 's3tablescatalog/<bucket>').
                        Defaults to AWSDataCatalog when omitted or 'AWSDataCatalog'.
            enrich: Passed through to _list_s3tables; set False for lightweight
                    existence checks to skip per-table Iceberg metadata API calls.

        Returns:
            List of table dicts including a 'catalogId' field.
        """
        # S3 Tables: Glue get_tables cannot list these — use the dedicated s3tables API
        if catalog_id and catalog_id.startswith('s3tablescatalog/'):
            return self._list_s3tables(database_name, catalog_id, enrich=enrich)

        try:
            tables = []
            paginator = self.glue_client.get_paginator('get_tables')
            paginate_kwargs: Dict[str, Any] = {'DatabaseName': database_name}
            if catalog_id and catalog_id != 'AWSDataCatalog':
                paginate_kwargs['CatalogId'] = catalog_id

            for page in paginator.paginate(**paginate_kwargs):
                for table in page.get('TableList', []):
                    tables.append({
                        'name': table['Name'],
                        'databaseName': database_name,
                        'catalogId': catalog_id or 'AWSDataCatalog',
                        'dataSource': self._data_source_for_catalog(catalog_id or 'AWSDataCatalog'),
                        'description': table.get('Description', ''),
                        'location': table.get('StorageDescriptor', {}).get('Location', ''),
                        'columns': len(table.get('StorageDescriptor', {}).get('Columns', [])),
                        'createTime': table.get('CreateTime').isoformat() if table.get('CreateTime') else None,
                        'updateTime': table.get('UpdateTime').isoformat() if table.get('UpdateTime') else None,
                        'tableType': table.get('TableType', 'EXTERNAL_TABLE'),
                    })

            logger.info(f"Listed {len(tables)} tables in {catalog_id or 'AWSDataCatalog'}.{database_name}")
            return tables

        except ClientError as e:
            logger.warning(f"Error listing tables in database {database_name}: {e}", exc_info=True)
            raise

    def get_table_metadata(self, database_name: str, table_name: str, catalog_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get detailed metadata for a specific table.

        Args:
            database_name: Name of the Glue database
            table_name: Name of the table
            catalog_id: Optional catalog ID. Defaults to AWSDataCatalog when omitted.

        Returns:
            Dictionary containing table metadata including schema, partitions, and statistics
        """
        try:
            get_kwargs: Dict[str, Any] = {
                'DatabaseName': database_name,
                'Name': table_name,
            }
            if catalog_id and catalog_id != 'AWSDataCatalog':
                get_kwargs['CatalogId'] = catalog_id

            response = self.glue_client.get_table(**get_kwargs)

            table = response['Table']
            storage_desc = table.get('StorageDescriptor', {})

            # Extract column information
            columns = []
            for col in storage_desc.get('Columns', []):
                columns.append({
                    'name': col['Name'],
                    'type': col['Type'],
                    'comment': col.get('Comment', '')
                })

            # Extract partition keys
            partition_keys = []
            for pk in table.get('PartitionKeys', []):
                partition_keys.append({
                    'name': pk['Name'],
                    'type': pk['Type'],
                    'comment': pk.get('Comment', '')
                })

            metadata = {
                'name': table['Name'],
                'databaseName': database_name,
                'catalogId': catalog_id or 'AWSDataCatalog',
                'dataSource': self._data_source_for_catalog(catalog_id or 'AWSDataCatalog'),
                'description': table.get('Description', ''),
                'owner': table.get('Owner', ''),
                'createTime': table.get('CreateTime').isoformat() if table.get('CreateTime') else None,
                'updateTime': table.get('UpdateTime').isoformat() if table.get('UpdateTime') else None,
                'lastAccessTime': table.get('LastAccessTime').isoformat() if table.get('LastAccessTime') else None,
                'retention': table.get('Retention', 0),
                'tableType': table.get('TableType', 'EXTERNAL_TABLE'),
                'parameters': table.get('Parameters', {}),
                'location': storage_desc.get('Location', ''),
                'inputFormat': storage_desc.get('InputFormat', ''),
                'outputFormat': storage_desc.get('OutputFormat', ''),
                'compressed': storage_desc.get('Compressed', False),
                'numberOfBuckets': storage_desc.get('NumberOfBuckets', 0),
                'serdeInfo': storage_desc.get('SerdeInfo', {}),
                'columns': columns,
                'partitionKeys': partition_keys,
            }

            # For S3 Tables: enrich with Iceberg-native metadata.
            # Glue federation returns column names/types correctly but leaves
            # Description and column Comment empty — those live in Iceberg
            # metadata JSON files in S3.  Try a direct S3 read of the
            # metadata JSON (fastest, no extra auth) and fall back to the
            # PyIceberg RestCatalog if that fails.
            if catalog_id and catalog_id.startswith('s3tablescatalog/'):
                metadata_location = metadata.get('parameters', {}).get('metadata_location', '')
                enrichment = self._enrich_from_metadata_location(metadata_location)
                if not enrichment:
                    enrichment = self._enrich_with_iceberg(catalog_id, database_name, table_name)
                if enrichment:
                    if enrichment['description']:
                        metadata['description'] = enrichment['description']
                    col_docs = enrichment['col_docs']
                    if col_docs:
                        for col in metadata['columns']:
                            doc = col_docs.get(col['name'].lower())
                            if doc:
                                col['comment'] = doc

            logger.info(f"Retrieved metadata for {catalog_id or 'AWSDataCatalog'}.{database_name}.{table_name}")
            return metadata

        except ClientError as e:
            logger.warning(f"Error getting table metadata for {database_name}.{table_name}: {e}", exc_info=True)
            raise

    def extract_metadata_for_semantic_metadata(self, data_sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Extract metadata from multiple data sources for semantic metadata generation.

        Args:
            data_sources: List of data sources. Each entry must have 'databaseName' and
                          optional 'tableName'; an optional 'catalogId' selects the catalog
                          (defaults to AWSDataCatalog).
                          Format: [{"catalogId": "...", "databaseName": "db1", "tableName": "table1"}, ...]
                          If tableName is None or absent, all tables in the database are selected.

        Returns:
            Dictionary containing aggregated metadata for all selected data sources
        """
        try:
            extracted_metadata = {
                'dataSources': [],
                'totalTables': 0,
                'totalColumns': 0,
                'relationships': []
            }

            for source in data_sources:
                database_name = source.get('databaseName')
                table_name = source.get('tableName')  # May be None for database-level entry
                catalog_id = source.get('catalogId')

                if not database_name:
                    logger.warning(f"Skipping invalid data source: {source}")
                    continue

                # Resolve table list: explicit table or all tables in database
                if table_name:
                    tables_to_process = [table_name]
                else:
                    all_tables = self.list_tables(database_name, catalog_id=catalog_id)
                    tables_to_process = [t['name'] for t in all_tables]

                for tbl_name in tables_to_process:
                    metadata = None
                    last_error = None
                    for attempt in range(3):
                        try:
                            metadata = self.get_table_metadata(database_name, tbl_name, catalog_id=catalog_id)
                            break
                        except ClientError as e:
                            last_error = e
                            # S3 Tables federation can return transient ValidationException;
                            # retry with backoff before giving up on this table.
                            if e.response['Error']['Code'] == 'ValidationException' and attempt < 2:
                                delay = 2 ** attempt  # 1s, 2s
                                logger.warning(
                                    f"Transient error for {database_name}.{tbl_name} (attempt {attempt + 1}), "
                                    f"retrying in {delay}s: {e}"
                                )
                                time.sleep(delay)
                            else:
                                break
                        except Exception as e:
                            last_error = e
                            break
                    if metadata is None:
                        logger.warning(
                            f"Skipping table {database_name}.{tbl_name} (catalog={catalog_id}) "
                            f"after retries: {last_error}"
                        )
                        continue

                    extracted_metadata['dataSources'].append({
                        'dataSource': source.get('dataSource', 'AwsDataCatalog'),
                        'catalogId': catalog_id or 'AWSDataCatalog',
                        'database': database_name,
                        'table': tbl_name,
                        'metadata': metadata
                    })

                    extracted_metadata['totalTables'] += 1
                    extracted_metadata['totalColumns'] += len(metadata.get('columns', []))

                    # Detect potential relationships based on column names
                    # (foreign key patterns like user_id, customer_id, etc.)
                    for col in metadata.get('columns', []):
                        col_name = col['name'].lower()
                        if col_name.endswith('_id') and col_name != 'id':
                            related_table = col_name[:-3]  # Remove '_id' suffix
                            extracted_metadata['relationships'].append({
                                'sourceTable': f"{database_name}.{tbl_name}",
                                'sourceColumn': col['name'],
                                'targetTable': related_table,
                                'relationship': 'foreign_key_candidate'
                            })

            logger.info(f"Extracted metadata for {len(data_sources)} data sources")
            return extracted_metadata

        except Exception as e:
            logger.warning(f"Error extracting metadata for metadata: {e}", exc_info=True)
            raise

    def start_crawler(self, crawler_name: str) -> Dict[str, Any]:
        """
        Start a Glue crawler

        Args:
            crawler_name: Name of the crawler to start

        Returns:
            Dictionary with crawler start status
        """
        try:
            self.glue_client.start_crawler(Name=crawler_name)
            logger.info(f"Started crawler: {crawler_name}")
            return {
                'crawlerName': crawler_name,
                'status': 'STARTING'
            }

        except ClientError as e:
            if e.response['Error']['Code'] == 'CrawlerRunningException':
                logger.warning(f"Crawler {crawler_name} is already running")
                return {
                    'crawlerName': crawler_name,
                    'status': 'RUNNING',
                    'message': 'Crawler is already running'
                }
            else:
                logger.warning(f"Error starting crawler {crawler_name}: {e}", exc_info=True)
                raise

    def get_crawler_status(self, crawler_name: str) -> Dict[str, Any]:
        """
        Get the status of a Glue crawler

        Args:
            crawler_name: Name of the crawler

        Returns:
            Dictionary with crawler status and metrics
        """
        try:
            response = self.glue_client.get_crawler(Name=crawler_name)
            crawler = response['Crawler']

            status = {
                'crawlerName': crawler_name,
                'state': crawler.get('State', 'UNKNOWN'),
                'lastCrawl': None,
                'tablesCreated': 0,
                'tablesUpdated': 0,
                'tablesDeleted': 0
            }

            # Get last crawl information
            last_crawl = crawler.get('LastCrawl', {})
            if last_crawl:
                status['lastCrawl'] = {
                    'status': last_crawl.get('Status', 'UNKNOWN'),
                    'errorMessage': last_crawl.get('ErrorMessage', ''),
                    'logGroup': last_crawl.get('LogGroup', ''),
                    'logStream': last_crawl.get('LogStream', ''),
                    'messagePrefix': last_crawl.get('MessagePrefix', ''),
                    'startTime': last_crawl.get('StartTime').isoformat() if last_crawl.get('StartTime') else None
                }

            if 'CrawlElapsedTime' in crawler:
                status['crawlElapsedTime'] = crawler['CrawlElapsedTime']

            if 'LastCrawl' in crawler and 'TablesCreated' in crawler['LastCrawl']:
                status['tablesCreated'] = crawler['LastCrawl'].get('TablesCreated', 0)
                status['tablesUpdated'] = crawler['LastCrawl'].get('TablesUpdated', 0)
                status['tablesDeleted'] = crawler['LastCrawl'].get('TablesDeleted', 0)

            logger.info(f"Retrieved status for crawler {crawler_name}: {status['state']}")
            return status

        except ClientError as e:
            logger.warning(f"Error getting crawler status for {crawler_name}: {e}", exc_info=True)
            raise
