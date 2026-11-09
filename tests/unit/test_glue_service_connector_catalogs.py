"""Unit tests for GlueService's Athena LAMBDA connector-catalog support.

Why this exists: a federated connector (e.g. Snowflake) is registered as an Athena
DataCatalog of type LAMBDA, which is not a Glue object at all. glue:GetCatalogs returns
only 's3tablescatalog', so before this support the admin data-source picker could never
show a connector's databases no matter which IAM permissions the caller held — it silently
rendered a Glue-only list. These tests pin the Athena-based code path that fixes it.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


def _paginator(pages: list) -> MagicMock:
    """Build a mock boto3 paginator that yields the given pages.

    :param pages: list of response-page dicts to yield from paginate().
    :returns: a MagicMock whose paginate() returns the pages.
    """
    pag = MagicMock()
    pag.paginate.return_value = pages
    return pag


def _make_service() -> tuple:
    """Construct a GlueService with all three boto3 clients mocked.

    GlueService creates glue, s3tables and athena clients in __init__; each is handed
    back as a distinct MagicMock keyed by service name so tests can assert which API
    was used for which catalog type.

    :returns: tuple of (GlueService instance, dict of service-name → mock client).
    """
    clients: dict = {}

    def _client(service_name: str, **kwargs) -> MagicMock:
        clients.setdefault(service_name, MagicMock())
        return clients[service_name]

    with patch('services.glue_service.boto3') as mock_boto:
        mock_boto.client.side_effect = _client
        from services.glue_service import GlueService
        svc = GlueService()
    return svc, clients


def _set_catalogs(clients: dict, summaries: list) -> None:
    """Point the athena client's list_data_catalogs paginator at the given summaries.

    :param clients: the mock-client dict from _make_service().
    :param summaries: list of DataCatalogsSummary entries.
    :returns: None
    """
    clients['athena'].get_paginator.side_effect = lambda op: {
        'list_data_catalogs': _paginator([{'DataCatalogsSummary': summaries}]),
    }[op]


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------

def test_discovery_keeps_only_lambda_catalogs():
    """GLUE-type catalogs are covered by the Glue pass and must not be duplicated."""
    svc, clients = _make_service()
    _set_catalogs(clients, [
        {'CatalogName': 'AwsDataCatalog', 'Type': 'GLUE'},
        {'CatalogName': 'snowflake_fhir_db', 'Type': 'LAMBDA'},
        {'CatalogName': 'some_federated', 'Type': 'FEDERATED'},
    ])
    with patch.dict(os.environ, {'ATHENA_CONNECTOR_CATALOG_DENYLIST': ''}):
        assert svc._connector_catalog_names() == ['snowflake_fhir_db']


def test_discovery_applies_default_denylist_to_dynamodb_catalog():
    """dynamodb_catalog is excluded by default: its tables already arrive via Glue.

    The DynamoDB connector reads schemas from the Glue database `<project>_dynamodb`,
    which the AWSDataCatalog pass returns with curated descriptions. Surfacing its Athena
    catalog too would add a bare duplicate `default` database for the same tables.
    """
    svc, clients = _make_service()
    _set_catalogs(clients, [
        {'CatalogName': 'dynamodb_catalog', 'Type': 'LAMBDA'},
        {'CatalogName': 'snowflake_fhir_db', 'Type': 'LAMBDA'},
    ])
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('ATHENA_CONNECTOR_CATALOG_DENYLIST', None)
        assert svc._connector_catalog_names() == ['snowflake_fhir_db']


def test_discovery_denylist_is_configurable():
    """An explicit denylist overrides the default, and is comma-separated."""
    svc, clients = _make_service()
    _set_catalogs(clients, [
        {'CatalogName': 'dynamodb_catalog', 'Type': 'LAMBDA'},
        {'CatalogName': 'snowflake_fhir_db', 'Type': 'LAMBDA'},
    ])
    with patch.dict(os.environ, {'ATHENA_CONNECTOR_CATALOG_DENYLIST': 'snowflake_fhir_db'}):
        assert svc._connector_catalog_names() == ['dynamodb_catalog']


def test_discovery_result_is_cached():
    """Discovery runs once per instance — list_databases and list_tables both consult it."""
    svc, clients = _make_service()
    _set_catalogs(clients, [{'CatalogName': 'snowflake_fhir_db', 'Type': 'LAMBDA'}])
    with patch.dict(os.environ, {'ATHENA_CONNECTOR_CATALOG_DENYLIST': ''}):
        svc._connector_catalog_names()
        svc._connector_catalog_names()
    assert clients['athena'].get_paginator.call_count == 1


def test_discovery_failure_degrades_to_no_connectors():
    """A missing athena:ListDataCatalogs grant must not break the whole picker.

    The Glue-backed databases still render; only connector catalogs go missing, and the
    cause is logged as a warning so it is diagnosable in CloudWatch.
    """
    svc, clients = _make_service()
    clients['athena'].get_paginator.side_effect = Exception('AccessDeniedException')
    assert svc._connector_catalog_names() == []


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('catalog_id,expected', [
    (None, False),
    ('AWSDataCatalog', False),
    ('s3tablescatalog/my-bucket', False),
    ('snowflake_fhir_db', True),
    ('not_registered', False),
])
def test_is_connector_catalog(catalog_id, expected):
    """Only a registered LAMBDA catalog routes to the Athena metadata APIs."""
    svc, clients = _make_service()
    _set_catalogs(clients, [{'CatalogName': 'snowflake_fhir_db', 'Type': 'LAMBDA'}])
    with patch.dict(os.environ, {'ATHENA_CONNECTOR_CATALOG_DENYLIST': ''}):
        assert svc._is_connector_catalog(catalog_id) is expected


def test_list_tables_uses_athena_for_connector_and_never_glue():
    """A connector database is read via Athena — Glue's get_tables would raise EntityNotFound."""
    svc, clients = _make_service()
    svc._connector_catalogs = ['snowflake_fhir_db']
    clients['athena'].get_paginator.side_effect = lambda op: {
        'list_table_metadata': _paginator([{'TableMetadataList': [
            {'Name': 'PATIENTS', 'TableType': 'EXTERNAL',
             'Columns': [{'Name': 'ID', 'Type': 'varchar'},
                         {'Name': 'GENDER', 'Type': 'varchar'}]},
        ]}]),
    }[op]

    tables = svc.list_tables('FHIR_R4', catalog_id='snowflake_fhir_db')

    assert len(tables) == 1
    assert tables[0]['name'] == 'PATIENTS'
    assert tables[0]['columns'] == 2
    assert tables[0]['catalogId'] == 'snowflake_fhir_db'
    # The connector catalog IS the Athena data source — this is the string that later
    # routes queries to Snowflake instead of the default Glue catalog.
    assert tables[0]['dataSource'] == 'snowflake_fhir_db'
    clients['glue'].get_paginator.assert_not_called()


def test_list_tables_still_uses_glue_for_default_catalog():
    """The Glue path is untouched: no Athena call for AWSDataCatalog."""
    svc, clients = _make_service()
    svc._connector_catalogs = ['snowflake_fhir_db']
    clients['glue'].get_paginator.return_value = _paginator([{'TableList': [
        {'Name': 'claims', 'StorageDescriptor': {'Columns': [{'Name': 'id', 'Type': 'string'}]}},
    ]}])

    tables = svc.list_tables('insurance', catalog_id='AWSDataCatalog')

    assert [t['name'] for t in tables] == ['claims']
    assert tables[0]['dataSource'] == 'AwsDataCatalog'
    clients['athena'].get_paginator.assert_not_called()


def test_get_table_metadata_maps_connector_shape():
    """Connector metadata is returned in the same shape as the Glue path.

    Storage fields are empty by nature, not by omission: a federated table has no S3
    storage descriptor because its data lives in the remote engine.
    """
    svc, clients = _make_service()
    svc._connector_catalogs = ['snowflake_fhir_db']
    clients['athena'].get_table_metadata.return_value = {'TableMetadata': {
        'Name': 'PATIENTS',
        'TableType': 'EXTERNAL',
        'Columns': [{'Name': 'ID', 'Type': 'varchar'},
                    {'Name': 'BIRTH_DATE', 'Type': 'date'}],
        'PartitionKeys': [],
        'Parameters': {'comment': 'One row per patient'},
    }}

    md = svc.get_table_metadata('FHIR_R4', 'PATIENTS', catalog_id='snowflake_fhir_db')

    assert md['name'] == 'PATIENTS'
    assert md['catalogId'] == 'snowflake_fhir_db'
    assert md['dataSource'] == 'snowflake_fhir_db'
    assert md['description'] == 'One row per patient'
    assert [c['name'] for c in md['columns']] == ['ID', 'BIRTH_DATE']
    assert md['partitionKeys'] == []
    assert md['location'] == ''
    assert md['serdeInfo'] == {}
    clients['glue'].get_table.assert_not_called()


# ---------------------------------------------------------------------------
# Database listing
# ---------------------------------------------------------------------------

def test_list_connector_databases_skips_system_schemas():
    """INFORMATION_SCHEMA is never a user-facing data source.

    Snowflake exposes it in every database; enumerating it costs a connector Lambda
    round-trip and returns hundreds of system views.
    """
    svc, clients = _make_service()
    clients['athena'].get_paginator.side_effect = lambda op: {
        'list_databases': _paginator([{'DatabaseList': [
            {'Name': 'FHIR_R4'},
            {'Name': 'INFORMATION_SCHEMA'},
            {'Name': 'PUBLIC'},
        ]}]),
    }[op]

    dbs = svc._list_connector_databases('snowflake_fhir_db')

    assert [d['name'] for d in dbs] == ['FHIR_R4', 'PUBLIC']
    assert all(d['catalogId'] == 'snowflake_fhir_db' for d in dbs)
    assert all(d['dataSource'] == 'snowflake_fhir_db' for d in dbs)


def test_list_databases_appends_connector_databases_in_order():
    """Connector databases join the Glue ones, and ordering stays deterministic.

    Ordering matters because the non-empty filter runs concurrently; the results are
    zipped back onto the original list rather than collected as they complete.
    """
    svc, clients = _make_service()
    clients['glue'].get_paginator.return_value = _paginator([{'DatabaseList': [
        {'Name': 'bedrock_cur_db'},
        {'Name': 'bench_stage'},
    ]}])

    with patch.object(svc, '_discover_catalog_ids', return_value=[]), \
         patch.object(svc, '_connector_catalog_names', return_value=['snowflake_fhir_db']), \
         patch.object(svc, '_list_connector_databases', return_value=[
             {'name': 'FHIR_R4', 'catalogId': 'snowflake_fhir_db',
              'dataSource': 'snowflake_fhir_db', 'description': '',
              'location': '', 'createTime': None},
         ]), \
         patch.object(svc, 'list_tables', return_value=[{'name': 'anything'}]):
        dbs = svc.list_databases()

    assert [(d['catalogId'], d['name']) for d in dbs] == [
        ('AWSDataCatalog', 'bedrock_cur_db'),
        ('AWSDataCatalog', 'bench_stage'),
        ('snowflake_fhir_db', 'FHIR_R4'),
    ]


def test_connector_databases_are_never_emptiness_verified():
    """A connector schema is kept without a round-trip — the check would breach the timeout.

    Verifying one connector schema means invoking the connector Lambda, which queries the
    remote engine: measured at 4-9s and highly variable. With three Snowflake schemas the
    endpoint ran 12.7-27.3s warm against API Gateway's hard 30s integration timeout, so the
    picker would have failed intermittently. Concurrency did not fix it because tail latency,
    not the sum, breaches the timeout. This pins the short-circuit.
    """
    svc, clients = _make_service()
    connector_db = {'name': 'FHIR_R4', 'catalogId': 'snowflake_fhir_db',
                    'dataSource': 'snowflake_fhir_db', 'description': '',
                    'location': '', 'createTime': None}
    clients['glue'].get_paginator.return_value = _paginator([{'DatabaseList': [
        {'Name': 'glue_db'},
    ]}])

    verified: list = []

    def _tables(database_name, catalog_id=None, enrich=True):
        verified.append((catalog_id, database_name))
        return [{'name': 't1'}]

    with patch.object(svc, '_discover_catalog_ids', return_value=[]), \
         patch.object(svc, '_connector_catalog_names', return_value=['snowflake_fhir_db']), \
         patch.object(svc, '_list_connector_databases', return_value=[connector_db]), \
         patch.object(svc, 'list_tables', side_effect=_tables):
        dbs = svc.list_databases()

    # The connector schema survives...
    assert ('snowflake_fhir_db', 'FHIR_R4') in [(d['catalogId'], d['name']) for d in dbs]
    # ...but was never probed, while the Glue database still was.
    assert verified == [('AWSDataCatalog', 'glue_db')]


def test_list_databases_drops_confirmed_empty_but_keeps_unverifiable():
    """Exclude on confirmed-empty, include on error — the pre-existing policy, preserved.

    A connector that errors on one schema (Snowflake will, for a schema holding a view the
    connector cannot resolve) must not make that schema vanish from the picker.
    """
    svc, clients = _make_service()
    clients['glue'].get_paginator.return_value = _paginator([{'DatabaseList': [
        {'Name': 'empty_db'},
        {'Name': 'full_db'},
        {'Name': 'broken_db'},
    ]}])

    def _tables(database_name, catalog_id=None, enrich=True):
        if database_name == 'empty_db':
            return []
        if database_name == 'broken_db':
            raise Exception('MetadataException: Could not find table')
        return [{'name': 't1'}]

    with patch.object(svc, '_discover_catalog_ids', return_value=[]), \
         patch.object(svc, '_connector_catalog_names', return_value=[]), \
         patch.object(svc, 'list_tables', side_effect=_tables):
        dbs = svc.list_databases()

    assert [d['name'] for d in dbs] == ['full_db', 'broken_db']
