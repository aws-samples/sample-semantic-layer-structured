"""CloudFormation custom-resource handler that bootstraps authorization state.

Turning authorization on is only safe if the deployment already contains the
state an authorized request needs. Three things have to exist before the first
request, and none of them can be created *by* a request:

1. **The ``default`` namespace.** Every ontology needs a namespace to be
   authorized against, and a create that names no namespace has to land
   somewhere. Its id is fixed (see ``DefaultNamespaceId``) rather than a
   generated UUID, so the REST API can carry it as a synth-time constant instead
   of waiting on this resource's output.
2. **Platform-admin grants for named operators** (optional). The role resolver
   also maps Cognito groups (``Admin`` → ``platform-admin``) as a bootstrap, so
   this is for deployments whose user pool has no such group, and for putting the
   grant in DynamoDB where it is auditable and revocable.
3. **A ``namespaceId`` on pre-existing ontology rows.** Ontologies created before
   namespaces existed have no namespace attribute; an authorization check against
   them would have no resource to name. They are backfilled onto ``default``.

Every step is idempotent and adoptive: it runs on each Create *and* Update, and
adopts state that is already correct rather than failing or overwriting it. That
matters because a stack update re-invokes this handler, and because a
from-scratch rebuild after ``cdk destroy`` must recover the default namespace and
admin access without manual repair.

Delete is deliberately a no-op — see :func:`on_event`.

Item shapes are mirrored from ``lambda/rest-api/services/namespace_service.py``
rather than imported: this handler is a plain zip Lambda with no access to that
tree or to pydantic. ``tests/unit/test_authz_bootstrap.py`` asserts the mirrored
constants and the principal-key encoder still agree with the canonical
implementations, because a writer that disagrees produces rows the reader can
never find — which presents as "the grant had no effect", with no error anywhere.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Mirrored from services/namespace_service.py ───────────────────────
_METADATA_SK = 'METADATA'
_RESERVATION_SK = 'RESERVATION'
_NS_PK_PREFIX = 'NS#'
_NAME_PK_PREFIX = 'NS_NAME#'
_GRANT_PK_PREFIX = 'RES#'

# ── Mirrored from authz/cedar_schema.py and agents/shared/grant_profile.py ──
_GLOBAL_RESOURCE_ID = 'GLOBAL'
_PLATFORM_ADMIN_ROLE = 'platform-admin'
_STATUS_ACTIVE = 'ACTIVE'

# Recorded as the granter on seeded grants so an audit of the grants table can
# tell operator intent apart from deployment bootstrap.
_BOOTSTRAP_GRANTED_BY = 'authz-bootstrap'


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        The timestamp, e.g. ``2026-08-24T21:15:00+00:00``.
    """
    return datetime.now(timezone.utc).isoformat()


def _sanitize_principal_key(*, value: str) -> str:
    """URL-encode a principal identifier for use in a grants-table key.

    Mirrors ``agents.shared.grant_profile.sanitize_principal_key`` exactly:
    values containing ``@`` are treated as email addresses and stripped and
    lower-cased first (IdPs emit email case inconsistently), then everything is
    percent-encoded with ``@`` and ``.`` left readable.

    Args:
        value: The raw principal identifier (a Cognito ``sub``, an email address,
            or a group name).

    Returns:
        The encoded key component.
    """
    if '@' in value:
        value = value.strip().lower()
    return urllib.parse.quote(value, safe='@.')


def _build_principal_key(*, principal_type: str, principal_id: str) -> str:
    """Build the grants-table principal key for one identity.

    Mirrors ``services.namespace_service.build_principal_key``.

    Args:
        principal_type: ``User`` or ``Group``.
        principal_id: A ``sub``, email address, or Cognito group name.

    Returns:
        The ``<type>::<encoded id>`` key.

    Raises:
        ValueError: If the type is not User/Group or the id is blank. Both are
            operator-supplied stack parameters, so a typo must fail the deploy
            rather than write a grant nothing will ever match.
    """
    normalized = principal_type.strip().capitalize()
    if normalized not in ('User', 'Group'):
        raise ValueError(
            f'principalType must be User or Group, got {principal_type!r}'
        )
    if not principal_id.strip():
        raise ValueError('principalId must not be empty')
    return f'{normalized}::{_sanitize_principal_key(value=principal_id)}'


def _ensure_workgroup(
    *,
    athena: Any,
    workgroup: str,
    result_location: str,
    namespace_id: str,
    name: str,
) -> None:
    """Create the namespace's Athena workgroup, adopting one that already exists.

    ``EnforceWorkGroupConfiguration`` is what makes the per-namespace S3 prefix an
    isolation boundary rather than a default a caller can override, so it is set
    here exactly as ``namespace_service._provision_workgroup`` sets it.

    Args:
        athena: A boto3 Athena client.
        workgroup: The workgroup name to create or adopt.
        result_location: The ``s3://`` prefix the workgroup must write to.
        namespace_id: The namespace id, recorded as a tag and in the description.
        name: The namespace name, recorded as a tag and in the description.

    Returns:
        None.
    """
    try:
        athena.get_work_group(WorkGroup=workgroup)
        logger.info('workgroup_exists workgroup=%s', workgroup)
        return
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        # Athena reports a missing workgroup as InvalidRequestException in some
        # regions and ResourceNotFoundException in others; anything else is a
        # real failure and must not be mistaken for "absent".
        if code not in ('InvalidRequestException', 'ResourceNotFoundException'):
            raise

    athena.create_work_group(
        Name=workgroup,
        Description=f'Semantic Layer namespace {name} ({namespace_id})',
        Configuration={
            'ResultConfiguration': {'OutputLocation': result_location},
            'EnforceWorkGroupConfiguration': True,
            'PublishCloudWatchMetricsEnabled': True,
        },
        Tags=[
            {'Key': 'SemanticLayerNamespaceId', 'Value': namespace_id},
            {'Key': 'SemanticLayerNamespaceName', 'Value': name},
        ],
    )
    logger.info('workgroup_created workgroup=%s location=%s', workgroup, result_location)


def _ensure_default_namespace(
    *,
    namespaces: Any,
    athena: Any,
    namespace_id: str,
    name: str,
    description: str,
    resource_prefix: str,
    results_bucket: str,
) -> Dict[str, str]:
    """Create the default namespace if it is not already registered.

    An existing metadata row is adopted untouched: it may carry an operator's
    edited description or an ARCHIVED status, and a deploy must not silently
    revert either.

    Args:
        namespaces: The namespaces DynamoDB ``Table`` resource.
        athena: A boto3 Athena client.
        namespace_id: The fixed id to register the namespace under.
        name: The namespace name (also the workgroup name suffix).
        description: Description for the row, used only on first create.
        resource_prefix: Prefix for the workgroup name; must match the prefix the
            REST API's ``RESOURCE_PREFIX`` produces or the API will create a
            *second*, differently-named workgroup for the same namespace.
        results_bucket: Bucket for the workgroup's enforced output location.

    Returns:
        ``{'workgroup': …, 'resultLocation': …, 'adopted': 'true'|'false'}``.

    Raises:
        RuntimeError: If ``results_bucket`` is blank, or if the name is already
            reserved by a *different* namespace id. Provisioning a workgroup with
            no enforced output location would provide no isolation, and silently
            re-pointing an existing name would detach live grants from the
            namespace they were written for.
    """
    if not results_bucket:
        raise RuntimeError(
            'AthenaResultsBucket is required: a workgroup with no enforced '
            'output location gives the namespace no isolation at all'
        )

    existing = namespaces.get_item(
        Key={'pk': f'{_NS_PK_PREFIX}{namespace_id}', 'sk': _METADATA_SK}
    ).get('Item')
    if existing:
        logger.info(
            'default_namespace_adopted id=%s name=%s status=%s workgroup=%s',
            namespace_id,
            existing.get('name', ''),
            existing.get('status', ''),
            existing.get('workgroup', ''),
        )
        return {
            'workgroup': str(existing.get('workgroup', '')),
            'resultLocation': str(existing.get('resultLocation', '')),
            'adopted': 'true',
        }

    workgroup = f'{resource_prefix}-ns-{name}'
    result_location = f's3://{results_bucket}/namespaces/{namespace_id}/'
    _ensure_workgroup(
        athena=athena,
        workgroup=workgroup,
        result_location=result_location,
        namespace_id=namespace_id,
        name=name,
    )

    # The name reservation is written without a `ttl`: the TTL in the API's create
    # path only exists to free a name when a create dies midway, and this handler
    # writes the metadata row immediately below. A reservation carrying a live TTL
    # would expire and let a user re-claim `default` out from under us.
    try:
        namespaces.put_item(
            Item={
                'pk': f'{_NAME_PK_PREFIX}{name}',
                'sk': _RESERVATION_SK,
                'namespaceId': namespace_id,
            },
            ConditionExpression='attribute_not_exists(pk)',
        )
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != (
            'ConditionalCheckFailedException'
        ):
            raise
        reservation = namespaces.get_item(
            Key={'pk': f'{_NAME_PK_PREFIX}{name}', 'sk': _RESERVATION_SK}
        ).get('Item') or {}
        claimed_by = str(reservation.get('namespaceId', ''))
        if claimed_by != namespace_id:
            raise RuntimeError(
                f'namespace name {name!r} is already reserved by namespace '
                f'{claimed_by!r}; refusing to re-point it at {namespace_id!r}'
            ) from exc
        # Ours already, but possibly still carrying the create path's TTL.
        namespaces.update_item(
            Key={'pk': f'{_NAME_PK_PREFIX}{name}', 'sk': _RESERVATION_SK},
            UpdateExpression='REMOVE #ttl',
            ExpressionAttributeNames={'#ttl': 'ttl'},
        )

    timestamp = _now_iso()
    namespaces.put_item(
        Item={
            'pk': f'{_NS_PK_PREFIX}{namespace_id}',
            'sk': _METADATA_SK,
            'id': namespace_id,
            'name': name,
            'description': description,
            'status': _STATUS_ACTIVE,
            'workgroup': workgroup,
            'resultLocation': result_location,
            'createdBy': _BOOTSTRAP_GRANTED_BY,
            'createdAt': timestamp,
            'updatedAt': timestamp,
        }
    )
    logger.info(
        'default_namespace_created id=%s name=%s workgroup=%s',
        namespace_id,
        name,
        workgroup,
    )
    return {
        'workgroup': workgroup,
        'resultLocation': result_location,
        'adopted': 'false',
    }


def _seed_platform_admins(*, grants: Any, principals: List[str]) -> int:
    """Grant ``platform-admin`` at GLOBAL scope to each named principal.

    Written conditionally so a re-deploy does not rewrite an existing grant's
    ``grantedAt``. Note the flip side: a platform-admin grant that an operator
    revoked *is* re-created by the next deploy, which is intended — this is the
    documented way back in from a lockout — but it means revoking access to a
    bootstrap principal requires removing it from the stack configuration too.

    Args:
        grants: The grants DynamoDB ``Table`` resource.
        principals: Cognito ``sub`` values or email addresses. An entry
            containing ``@`` is normalised as an email by the shared encoder.

    Returns:
        The number of grants newly written (existing grants are not counted).
    """
    written = 0
    for principal_id in principals:
        principal_key = _build_principal_key(
            principal_type='User', principal_id=principal_id
        )
        try:
            grants.put_item(
                Item={
                    'pk': f'{_GRANT_PK_PREFIX}{_GLOBAL_RESOURCE_ID}',
                    'sk': f'{principal_key}#{_PLATFORM_ADMIN_ROLE}',
                    'principalKey': principal_key,
                    'resourceId': _GLOBAL_RESOURCE_ID,
                    'role': _PLATFORM_ADMIN_ROLE,
                    'grantedBy': _BOOTSTRAP_GRANTED_BY,
                    'grantedAt': _now_iso(),
                },
                # Restriction attributes (tableAllowlist / columnDenylist /
                # allowedMetrics) are deliberately absent, not empty: absent
                # means "no constraint", whereas an empty list would deny
                # everything and make the seeded admin useless.
                ConditionExpression='attribute_not_exists(pk)',
            )
            written += 1
            logger.info('platform_admin_granted principal=%s', principal_key)
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') != (
                'ConditionalCheckFailedException'
            ):
                raise
            logger.info('platform_admin_exists principal=%s', principal_key)
    return written


def _backfill_namespace_id(*, metadata: Any, namespace_id: str) -> int:
    """Stamp ``namespaceId`` onto ontology metadata rows that have none.

    Args:
        metadata: The ontology metadata DynamoDB ``Table`` resource (key
            ``id`` + ``version``).
        namespace_id: The namespace id to bind un-namespaced rows to.

    Returns:
        The number of rows updated.
    """
    updated = 0
    scan_kwargs: Dict[str, Any] = {
        # `version` is a DynamoDB reserved word, so both key attributes go
        # through ExpressionAttributeNames.
        'ProjectionExpression': '#i, #v',
        'ExpressionAttributeNames': {'#i': 'id', '#v': 'version'},
        'FilterExpression': 'attribute_not_exists(namespaceId)',
    }
    while True:
        response = metadata.scan(**scan_kwargs)
        for item in response.get('Items', []):
            try:
                metadata.update_item(
                    Key={'id': item['id'], 'version': item['version']},
                    UpdateExpression='SET namespaceId = :ns',
                    ExpressionAttributeValues={':ns': namespace_id},
                    # Loses the race to a concurrent write rather than
                    # overwriting a namespace someone just assigned.
                    ConditionExpression='attribute_not_exists(namespaceId)',
                )
                updated += 1
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != (
                    'ConditionalCheckFailedException'
                ):
                    raise
        token = response.get('LastEvaluatedKey')
        if not token:
            break
        scan_kwargs['ExclusiveStartKey'] = token

    logger.info('namespace_backfill_complete rows=%s namespace=%s', updated, namespace_id)
    return updated


def _string_list(*, properties: Dict[str, Any], key: str) -> List[str]:
    """Read a list-valued custom-resource property, tolerating CFN's shapes.

    CloudFormation delivers a list property as a JSON array of strings, but an
    empty list is often delivered as ``''`` or omitted entirely.

    Args:
        properties: The ``ResourceProperties`` block.
        key: The property name.

    Returns:
        The non-blank entries, stripped.

    Raises:
        TypeError: If the property is neither a list nor a string. Guessing at an
            unexpected shape here would seed the wrong principals.
    """
    raw = properties.get(key, [])
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    if not isinstance(raw, list):
        raise TypeError(f'{key} must be a list of strings, got {type(raw).__name__}')
    return [str(entry).strip() for entry in raw if str(entry).strip()]


def on_event(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle the custom-resource lifecycle event.

    Create and Update run the identical, idempotent seeding path. Delete is a
    no-op: the tables themselves are removed with the stack, and deleting grants
    on a *rollback* — which arrives as a Delete for the newly-created resource —
    would strip administrator access at exactly the moment someone needs it to
    diagnose the failed deploy.

    Args:
        event: The CloudFormation custom-resource event. ``ResourceProperties``
            carries ``NamespacesTable``, ``GrantsTable``, ``MetadataTable``,
            ``DefaultNamespaceId``, ``DefaultNamespaceName``,
            ``DefaultNamespaceDescription``, ``ResourcePrefix``,
            ``AthenaResultsBucket`` and the optional ``PlatformAdminUsers`` list.
        context: The Lambda context object (unused).

    Returns:
        ``{'PhysicalResourceId': …, 'Data': {…}}`` — ``Data`` reports what the
        run actually did so the stack event log shows whether state was created
        or adopted.

    Raises:
        RuntimeError: If a required property is missing.
    """
    request_type = event.get('RequestType', '')
    properties: Dict[str, Any] = event.get('ResourceProperties', {})
    logger.info('authz_bootstrap request=%s', request_type)

    namespaces_table = str(properties.get('NamespacesTable', ''))
    physical_id = f'authz-bootstrap-{namespaces_table or "unconfigured"}'

    if request_type == 'Delete':
        logger.info('authz_bootstrap_delete_noop id=%s', physical_id)
        return {'PhysicalResourceId': physical_id, 'Data': {}}

    grants_table = str(properties.get('GrantsTable', ''))
    metadata_table = str(properties.get('MetadataTable', ''))
    namespace_id = str(properties.get('DefaultNamespaceId', ''))
    namespace_name = str(properties.get('DefaultNamespaceName', ''))
    for label, value in (
        ('NamespacesTable', namespaces_table),
        ('GrantsTable', grants_table),
        ('MetadataTable', metadata_table),
        ('DefaultNamespaceId', namespace_id),
        ('DefaultNamespaceName', namespace_name),
    ):
        if not value:
            raise RuntimeError(f'{label} is required')

    region = os.environ['AWS_REGION']
    dynamodb = boto3.resource('dynamodb', region_name=region)
    athena = boto3.client('athena', region_name=region)

    provisioned = _ensure_default_namespace(
        namespaces=dynamodb.Table(namespaces_table),
        athena=athena,
        namespace_id=namespace_id,
        name=namespace_name,
        description=str(properties.get('DefaultNamespaceDescription', '')),
        resource_prefix=str(properties.get('ResourcePrefix', '')),
        results_bucket=str(properties.get('AthenaResultsBucket', '')),
    )
    admins_written = _seed_platform_admins(
        grants=dynamodb.Table(grants_table),
        principals=_string_list(properties=properties, key='PlatformAdminUsers'),
    )
    rows_backfilled = _backfill_namespace_id(
        metadata=dynamodb.Table(metadata_table), namespace_id=namespace_id
    )

    return {
        'PhysicalResourceId': physical_id,
        'Data': {
            'DefaultNamespaceId': namespace_id,
            'DefaultNamespaceName': namespace_name,
            'Workgroup': provisioned['workgroup'],
            'ResultLocation': provisioned['resultLocation'],
            'Adopted': provisioned['adopted'],
            'PlatformAdminGrantsWritten': str(admins_written),
            'OntologyRowsBackfilled': str(rows_backfilled),
        },
    }
