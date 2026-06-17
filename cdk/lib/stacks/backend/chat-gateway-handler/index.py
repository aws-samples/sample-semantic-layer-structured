"""Chat gateway CloudFormation custom-resource handlers.

This module is bundled as a Lambda asset (with boto3>=1.38.0 pinned in
requirements.txt) so the control-plane create_gateway call can OMIT the now
optional protocolType keyword. The Lambda runtime's bundled boto3 (~1.34) still
marks protocolType as REQUIRED on bedrock-agentcore-control.create_gateway, so
relying on it raises ParamValidationError. Bundling a newer boto3 (>=1.38) fixes
that — newer botocore treats protocolType as optional, which is what makes this
a non-MCP gateway that AgentCore Runtime targets can attach to.

Two entry points (referenced from the CDK Lambda `handler:` props):

  * on_event_handler   — index.on_event_handler
  * is_complete_handler — index.is_complete_handler
"""

import time
import uuid

import boto3
from botocore.exceptions import ClientError

# Control-plane client — supports optional protocolType + runtime targets,
# unlike CloudFormation's AWS::BedrockAgentCore::Gateway.
client = boto3.client('bedrock-agentcore-control')

# ── Gateway-create retry tuning ──────────────────────────────────────────────
# The AgentCore gateway control-plane is still a preview API and has been
# observed to flip a freshly-created gateway to FAILED transiently even when the
# IDENTICAL config reaches READY on a manual retry. We therefore self-heal a
# FAILED create by deleting the FAILED gateway and creating a fresh one, up to
# MAX_CREATE_ATTEMPTS total attempts.
#
# WHY this loop lives in onEvent (not isComplete):
#   * The CDK custom-resource Provider invokes isComplete REPEATEDLY but it is
#     STATELESS between polls in the way we need — the only state threaded into
#     the next poll is the Data returned by onEvent (NOT the Data returned by a
#     prior isComplete poll). Relying on isComplete to thread its own attempt
#     counter forward is therefore not something we can be confident about, so
#     we avoid that design entirely.
#   * onEvent has a 5-minute Lambda timeout (see mcp-server-stack.ts). Gateway
#     READY took ~20s in manual testing, so create + poll-to-READY + (delete +
#     recreate) up to 3 times fits comfortably under 5 minutes. We therefore do
#     the whole create→READY→retry loop synchronously INSIDE onEvent and only
#     return once the gateway is READY (or raise with the last statusReasons
#     once attempts are exhausted). No Lambda-timeout bump was required.
#   * Targets were NOT the failure point and target-READY took ~20s, so target
#     creation + readiness polling stays in isComplete (gated on gateway READY).
MAX_CREATE_ATTEMPTS = 3
# Per-attempt budget for polling a new gateway from CREATING to READY/FAILED.
# Generous vs the observed ~20s; the onEvent Lambda's 5-min timeout is the hard
# ceiling and 3 * GATEWAY_READY_TIMEOUT_SECS stays under it.
GATEWAY_READY_TIMEOUT_SECS = 75
GATEWAY_POLL_INTERVAL_SECS = 5


# ── onEvent ────────────────────────────────────────────────────────────────


def _delete_gateway(gateway_id):
    """Best-effort tear down a gateway and all its targets.

    Lists and deletes every target first (a gateway with live targets cannot
    be deleted), then the gateway. Swallows ResourceNotFoundException so a
    stack delete never hangs on an already-gone resource.

    :param gateway_id: the gatewayId to delete.
    :type gateway_id: str
    :returns: None
    """
    if not gateway_id:
        return
    try:
        targets = client.list_gateway_targets(gatewayIdentifier=gateway_id).get('items', [])
        for t in targets:
            target_id = t.get('targetId')
            if not target_id:
                continue
            try:
                client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    print(f"Non-fatal error deleting target {target_id}: {e}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            print(f"Non-fatal error listing targets for {gateway_id}: {e}")
    try:
        client.delete_gateway(gatewayIdentifier=gateway_id)
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            print(f"Non-fatal error deleting gateway {gateway_id}: {e}")


def _unique_gateway_name(base_name):
    """Build a unique gateway name from a stable base + a short random token.

    The AgentCore gateway NAME does not need to be globally stable — the
    frontend reads the gateway URL/id (both carry their own unique suffix) from
    the CFN output, never the name. Making each create_gateway call use a UNIQUE
    name guarantees it can never collide with a lingering/deleting gateway of
    the same base name (an async delete leaves the old one in DELETING for a
    while, and a prior rolled-back deploy can leave a FAILED orphan). Both would
    otherwise raise ConflictException on a same-name recreate.

    The token is uuid4's first 8 hex chars. AgentCore gateway names are limited
    to ~100 chars of ``[a-zA-Z0-9_-]``; the base (e.g.
    ``semantic-layer-dev-chat-gateway`` = 31 chars) plus ``-`` + 8 hex = 40
    chars stays well within both the charset and length limits.

    :param base_name: the stable base gateway name from ResourceProperties.
    :type base_name: str
    :returns: ``"{base_name}-{8 hex chars}"``.
    :rtype: str
    """
    return f"{base_name}-{uuid.uuid4().hex[:8]}"


def _create_gateway_once(props):
    """Issue a single create_gateway call for a non-MCP CUSTOM_JWT gateway.

    IMPORTANT: protocolType is OMITTED entirely — that is what makes this a
    non-MCP gateway that AgentCore Runtime targets can attach to. Targets are
    deliberately NOT created here: create_gateway returns the gateway in
    status=CREATING and create_gateway_target raises ConflictException (HTTP
    409) until the gateway reaches READY, so target creation is driven by the
    isComplete poller (see the isComplete handler).

    The gateway is created with a UNIQUE name per call (see
    _unique_gateway_name) so a same-base-name gateway that is still DELETING
    (delete_gateway is async) or a FAILED orphan from a prior deploy can never
    cause a ConflictException on (re)create.

    :param props: the ResourceProperties dict from the CFN event.
    :type props: dict
    :returns: the create_gateway response dict.
    :rtype: dict
    """
    gateway_name = _unique_gateway_name(props['GatewayName'])
    print(f"Creating gateway with unique name {gateway_name} "
          f"(base={props['GatewayName']})")
    resp = client.create_gateway(
        name=gateway_name,
        roleArn=props['RoleArn'],
        authorizerType='CUSTOM_JWT',
        authorizerConfiguration={
            'customJWTAuthorizer': {
                'discoveryUrl': props['DiscoveryUrl'],
                'allowedClients': [props['AllowedClientId']],
            }
        },
    )
    return resp


def _wait_for_gateway_ready(gateway_id):
    """Poll get_gateway until the gateway is READY or FAILED (or times out).

    :param gateway_id: the gatewayId to poll.
    :type gateway_id: str
    :returns: tuple of (status, status_reasons) where status is the terminal
        status observed ('READY', 'FAILED', or the last non-terminal status if
        the poll timed out) and status_reasons is the statusReasons list from
        the final get_gateway response (may be None/empty).
    :rtype: tuple[str, list | None]
    """
    deadline = time.time() + GATEWAY_READY_TIMEOUT_SECS
    status = None
    resp = None
    while time.time() < deadline:
        resp = client.get_gateway(gatewayIdentifier=gateway_id)
        status = resp.get('status')
        # Surface the FULL response at INFO whenever the gateway is not in a
        # benign in-progress state, so a future failure is debuggable from
        # CloudWatch (the preview API does not always populate statusReasons).
        if status not in ('READY', 'CREATING'):
            print(f"get_gateway({gateway_id}) full response: {resp}")
        print(f"Gateway {gateway_id} status: {status}")
        if status in ('READY', 'FAILED'):
            return status, resp.get('statusReasons')
        time.sleep(GATEWAY_POLL_INTERVAL_SECS)  # nosemgrep: arbitrary-sleep — intentional AgentCore gateway status poll
    # Timed out without reaching a terminal state — treat as a transient failure
    # so the retry loop recreates the gateway.
    reasons = resp.get('statusReasons') if resp else None
    print(f"Gateway {gateway_id} did not reach READY within "
          f"{GATEWAY_READY_TIMEOUT_SECS}s (last status={status})")
    return status, reasons


def _reap_failed_orphans(base_name):
    """Best-effort delete FAILED gateways left over by prior rolled-back deploys.

    A gateway that went FAILED in a previously rolled-back deploy lingers in the
    account and accumulates over time. Since create now uses a unique name (see
    _unique_gateway_name) such an orphan no longer BLOCKS a new create, but
    reaping it keeps the account clean. We page through list_gateways, match any
    gateway whose name STARTS WITH base_name AND is in FAILED status, and
    best-effort delete each.

    Only FAILED gateways are reaped — never READY ones — so a live gateway
    (e.g. from a concurrent stack) is never touched. The whole routine is
    best-effort: every failure (including a missing ListGateways permission) is
    swallowed and logged so cleanup hygiene can never block gateway creation
    (the unique name already prevents the conflict this used to cause).

    :param base_name: the stable base gateway name to match orphans against.
    :type base_name: str
    :returns: None
    """
    try:
        next_token = None
        while True:
            # list_gateways response shape:
            #   {'items': [{'gatewayId':.., 'name':.., 'status':..}], 'nextToken':..}
            kwargs = {'nextToken': next_token} if next_token else {}
            resp = client.list_gateways(**kwargs)
            for gw in resp.get('items', []):
                name = gw.get('name') or ''
                status = gw.get('status')
                gateway_id = gw.get('gatewayId')
                if status == 'FAILED' and name.startswith(base_name) and gateway_id:
                    print(f"Reaping FAILED orphan gateway {gateway_id} "
                          f"(name={name})")
                    # _delete_gateway is itself best-effort (swallows
                    # ResourceNotFoundException and logs other ClientErrors).
                    _delete_gateway(gateway_id)
            next_token = resp.get('nextToken')
            if not next_token:
                break
    except Exception as e:  # noqa: BLE001 — cleanup must never block creation.
        # Includes AccessDenied if ListGateways is not granted. Swallow + log.
        print(f"Non-fatal error reaping FAILED orphan gateways "
              f"(base={base_name}): {e}")


def _create_gateway(props):
    """Create a gateway and drive it to READY, retrying transient FAILEDs.

    The AgentCore preview gateway control-plane occasionally flips a brand-new
    gateway to FAILED transiently (the IDENTICAL config reaches READY on a
    manual retry). To self-heal, this loops up to MAX_CREATE_ATTEMPTS times:
    create_gateway → poll to READY/FAILED; on FAILED (or a CREATING timeout),
    best-effort delete the FAILED gateway and create a fresh one. The
    statusReasons are logged on every failed attempt and, if all attempts are
    exhausted, are included in the raised exception so the real reason is
    visible in CloudFormation + CloudWatch.

    Runs entirely in onEvent (5-min Lambda timeout) so onEvent only returns once
    the gateway is READY — see the module-level comment for why the retry lives
    here and not in the stateless isComplete poller.

    :param props: the ResourceProperties dict from the CFN event.
    :type props: dict
    :returns: dict with PhysicalResourceId (gatewayId) and Data attributes.
    :rtype: dict
    :raises Exception: if every creation attempt ends in FAILED, with the last
        observed statusReasons in the message.
    """
    # Defensive hygiene: reap any FAILED orphans of this base name left by prior
    # rolled-back deploys before creating. Best-effort — never blocks create.
    _reap_failed_orphans(props['GatewayName'])

    last_reasons = None
    for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
        resp = _create_gateway_once(props)
        gateway_id = resp['gatewayId']
        print(f"Created gateway {gateway_id} (attempt {attempt}/"
              f"{MAX_CREATE_ATTEMPTS}, status={resp.get('status')}); "
              f"polling for READY")

        status, last_reasons = _wait_for_gateway_ready(gateway_id)
        if status == 'READY':
            # Re-read so Data carries the final url/arn (CFN populates
            # getAttString values from this Data).
            final = client.get_gateway(gatewayIdentifier=gateway_id)
            print(f"Gateway {gateway_id} READY after attempt {attempt}; "
                  f"targets will be created by isComplete")
            return {
                'PhysicalResourceId': gateway_id,
                'Data': {
                    'GatewayId': gateway_id,
                    'GatewayUrl': final['gatewayUrl'],
                    'GatewayArn': final['gatewayArn'],
                },
            }

        # Transient FAILED (or CREATING timeout) — log the reason and tear the
        # FAILED gateway down (best effort, NON-blocking: delete_gateway is
        # async and we do NOT wait for it to finish). The next attempt uses a
        # brand-new UNIQUE name, so it can never collide with this one even
        # while it is still DELETING — hence no wait is needed here.
        print(f"Gateway {gateway_id} did not reach READY on attempt "
              f"{attempt}/{MAX_CREATE_ATTEMPTS} (status={status}); "
              f"statusReasons={last_reasons}; deleting (best-effort) and "
              f"retrying with a new unique name")
        _delete_gateway(gateway_id)

    raise Exception(
        f"Gateway creation FAILED after {MAX_CREATE_ATTEMPTS} attempts: "
        f"statusReasons={last_reasons}"
    )


def on_event_handler(event, context):
    """CloudFormation custom-resource onEvent handler for the chat gateway.

    Create  : create_gateway + drive it to READY, retrying transient FAILEDs
              up to MAX_CREATE_ATTEMPTS (no targets yet) — see _create_gateway.
    Update  : best-effort delete of the OLD gateway+targets (from the prior
              PhysicalResourceId) then create+ready a fresh one. The isComplete
              poller then creates + drives the new gateway's targets to READY.
    Delete  : best-effort tear down of the gateway and its targets.

    :param event: the CFN custom-resource event.
    :type event: dict
    :param context: the Lambda context (unused).
    :returns: dict with PhysicalResourceId and Data (Create/Update), or just
        PhysicalResourceId (Delete).
    :rtype: dict
    """
    print(f"Event: {event}")
    request_type = event['RequestType']
    props = event['ResourceProperties']

    if request_type == 'Delete':
        _delete_gateway(event.get('PhysicalResourceId'))
        return {'PhysicalResourceId': event.get('PhysicalResourceId', 'chat-gateway')}

    if request_type == 'Update':
        # If the existing gateway is still healthy, REUSE it (keep the same
        # gatewayId + URL) and let isComplete reconcile the targets in place
        # (e.g. migrate the outbound credential mode via _ensure_targets). This
        # avoids minting a new gateway URL on every property change — the
        # frontend bakes the gateway URL at build time, so a new URL would break
        # chat until the frontend is redeployed. Only fall back to
        # delete-then-recreate when the old gateway is gone/unhealthy.
        old_id = event.get('PhysicalResourceId')
        if old_id:
            try:
                cur = client.get_gateway(gatewayIdentifier=old_id)
                if cur.get('status') in ('READY', 'CREATING', 'UPDATING'):
                    print(f"Update: reusing existing gateway {old_id} "
                          f"(status={cur.get('status')}); targets reconciled by isComplete")
                    return {
                        'PhysicalResourceId': old_id,
                        'Data': {
                            'GatewayId': old_id,
                            'GatewayUrl': cur['gatewayUrl'],
                            'GatewayArn': cur['gatewayArn'],
                        },
                    }
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    print(f"Update: get_gateway({old_id}) error {e}; recreating")
        # Old gateway missing/unhealthy — delete-then-recreate (best-effort).
        _delete_gateway(old_id)

    return _create_gateway(props)


# ── isComplete ───────────────────────────────────────────────────────────────

# The two runtime targets to create on the chat gateway. The (name, props-key)
# tuples map the stable target name to the ResourceProperties key that carries
# the runtime ARN. These names are matched against existing targets so the
# create step is idempotent across repeated isComplete polls.
TARGET_SPECS = (
    ('metadata-query', 'MetadataQueryRuntimeArn'),
    ('ontology-query', 'OntologyQueryRuntimeArn'),
)


# Outbound credential mode for the runtime targets. JWT_PASSTHROUGH forwards
# the gateway's validated inbound Cognito access token to the runtime target so
# the runtime can re-validate it and decode the end-user `sub` (used for
# chat-session ownership). The runtime targets are configured with a matching
# Cognito JWT inbound authorizer in agentcore-stack.ts. (Was GATEWAY_IAM_ROLE,
# which invoked the runtime with the gateway's service role and dropped the
# user's identity — chat history then persisted under 'anonymous'.)
TARGET_CREDENTIAL_TYPE = 'JWT_PASSTHROUGH'


def _ensure_targets(gateway_id, props):
    """Idempotently create the runtime targets with the desired credential type.

    isComplete may be polled many times, so this MUST be safe to re-run. It
    lists existing targets by stable name and, for each expected target:
      * creates it if absent, or
      * if it exists but with the WRONG credentialProviderType (e.g. a target
        left over from the GATEWAY_IAM_ROLE era), deletes + recreates it with
        TARGET_CREDENTIAL_TYPE. This migrates the credential mode in place
        without requiring a full gateway recreate.

    :param gateway_id: the gatewayId the targets belong to.
    :type gateway_id: str
    :param props: the ResourceProperties dict (carries the runtime ARNs).
    :type props: dict
    :returns: None
    """
    existing = client.list_gateway_targets(gatewayIdentifier=gateway_id).get('items', [])
    by_name = {t.get('name'): t for t in existing}
    for name, arn_key in TARGET_SPECS:
        runtime_arn = props[arn_key]
        cur = by_name.get(name)
        if cur is not None:
            # A target that is still CREATING does not yet populate
            # credentialProviderConfigurations — treating that empty list as a
            # "mismatch" would delete+recreate the target we JUST created, an
            # infinite churn. Only evaluate the credential type once the target
            # is READY; otherwise leave it alone this poll (it's converging).
            if cur.get('status') != 'READY':
                continue
            # IMPORTANT: list_gateway_targets does NOT return
            # credentialProviderConfigurations for runtime targets (it comes back
            # empty even for a READY target), whereas get_gateway_target DOES.
            # Trusting the list result here read the cred types as [] on every
            # poll → perpetual "mismatch" → infinite delete+recreate → CFN
            # custom-resource timeout. Re-fetch the single target so the cred
            # type is actually populated before deciding it's wrong.
            try:
                detail = client.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=cur.get('targetId')
                )
            except ClientError as e:
                if e.response['Error']['Code'] == 'ResourceNotFoundException':
                    # Raced with a delete from a prior poll — fall through to create.
                    detail = {}
                else:
                    raise
            # Detect a credential-type mismatch so a redeploy migrates the mode.
            cur_types = [
                c.get('credentialProviderType')
                for c in (detail.get('credentialProviderConfigurations') or [])
            ]
            if TARGET_CREDENTIAL_TYPE in cur_types:
                continue  # already correct
            # Defensive: if get_gateway_target STILL returns no cred types on a
            # READY target (API shape drift), do NOT churn — assume correct and
            # move on rather than delete+recreate forever.
            if not cur_types:
                print(f"Target {name} READY but get_gateway_target returned no "
                      f"credential types; assuming correct (avoid churn)")
                continue
            # Wrong credential mode (e.g. a GATEWAY_IAM_ROLE target from before).
            # delete_gateway_target is ASYNC, so we must NOT create in the same
            # invocation (CreateGatewayTarget raises ConflictException while the
            # old target is still DELETING). Issue the delete and SKIP create
            # this poll — _targets_ready then returns False (target absent) and
            # the next isComplete poll recreates it once the delete has settled.
            target_id = cur.get('targetId')
            print(f"Target {name} has credential types {cur_types} "
                  f"!= {TARGET_CREDENTIAL_TYPE}; deleting (recreate next poll)")
            try:
                client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    raise
            continue  # recreate on a subsequent poll after the async delete
        # Absent (or just deleted on a prior poll) — create with the right mode.
        # A ConflictException here means a prior poll's async delete hasn't fully
        # settled yet; swallow it and let the next poll retry (idempotent).
        try:
            client.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=name,
                credentialProviderConfigurations=[{'credentialProviderType': TARGET_CREDENTIAL_TYPE}],
                targetConfiguration={
                    'http': {'agentcoreRuntime': {'arn': runtime_arn, 'qualifier': 'DEFAULT'}}
                },
            )
            print(f"Created target {name} ({TARGET_CREDENTIAL_TYPE}) -> {runtime_arn}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConflictException':
                print(f"Target {name} create conflict (prior delete still settling); "
                      f"will retry next poll")
            else:
                raise


def _targets_ready(gateway_id):
    """Return True only when both expected targets exist AND are READY.

    Re-lists the targets (a target created in this same poll starts in
    CREATING) and polls each one via get_gateway_target. Raises if any target
    is FAILED.

    :param gateway_id: the gatewayId whose targets to check.
    :type gateway_id: str
    :returns: True if both targets exist and are READY, else False.
    :rtype: bool
    """
    expected_names = {name for name, _ in TARGET_SPECS}
    items = client.list_gateway_targets(gatewayIdentifier=gateway_id).get('items', [])
    by_name = {t.get('name'): t for t in items}
    if not expected_names.issubset(by_name.keys()):
        print(f"Targets not all present yet: have {set(by_name.keys())}, want {expected_names}")
        return False
    for name in expected_names:
        target_id = by_name[name].get('targetId')
        resp = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = resp.get('status')
        # Surface the full response at INFO whenever the target is not in a
        # benign in-progress state, so a future failure is debuggable from
        # CloudWatch.
        if status not in ('READY', 'CREATING'):
            print(f"get_gateway_target({target_id}) full response: {resp}")
        print(f"Target {name} ({target_id}) status: {status}")
        if status == 'FAILED':
            # Include statusReasons so the real failure is visible in
            # CloudFormation + CloudWatch rather than a bare FAILED.
            raise Exception(
                f"Target {name} ({target_id}) FAILED: "
                f"statusReasons={resp.get('statusReasons')}"
            )
        if status != 'READY':
            return False
    return True


def is_complete_handler(event, context):
    """CloudFormation custom-resource isComplete handler.

    Drives the FULL readiness sequence for the chat gateway, since target
    creation cannot happen until the gateway is READY (create_gateway_target
    raises ConflictException while the gateway is CREATING):

      1. get_gateway — if FAILED raise; if not READY return IsComplete False.
      2. Once gateway READY, idempotently create any missing targets
         (_ensure_targets) using the runtime ARNs from ResourceProperties.
      3. Poll each target via get_gateway_target — if any FAILED raise; if any
         not yet READY return IsComplete False.
      4. When gateway + both targets are READY, re-read gatewayUrl/Arn via
         get_gateway and return IsComplete True with the Data attributes (CFN
         populates getAttString from THIS Data, so the PascalCase keys must be
         present here).

    The provider passes ResourceProperties to BOTH onEvent and isComplete, so
    the runtime ARNs are read from event['ResourceProperties'] here too.
    Delete is always complete (onEvent already swallowed not-found).

    :param event: the CFN custom-resource event (carries Data + ResourceProperties).
    :type event: dict
    :param context: the Lambda context (unused).
    :returns: dict with IsComplete (and Data once complete).
    :rtype: dict
    """
    print(f"Event: {event}")
    request_type = event['RequestType']

    if request_type == 'Delete':
        return {'IsComplete': True}

    data = event.get('Data', {})
    gateway_id = data.get('GatewayId')
    if not gateway_id:
        # Fall back to the PhysicalResourceId (the onEvent sets it to gatewayId).
        gateway_id = event.get('PhysicalResourceId')
    if not gateway_id:
        raise Exception("GatewayId not found in event data or PhysicalResourceId")

    props = event['ResourceProperties']

    resp = client.get_gateway(gatewayIdentifier=gateway_id)
    status = resp.get('status')
    # Surface the full response at INFO whenever the gateway is not in a benign
    # in-progress state, so a future failure is debuggable from CloudWatch.
    if status not in ('READY', 'CREATING'):
        print(f"get_gateway({gateway_id}) full response: {resp}")
    print(f"Gateway {gateway_id} status: {status}")

    if status == 'FAILED':
        # onEvent already drives the gateway to READY (retrying transient
        # FAILEDs), so reaching here means a post-READY failure — surface the
        # statusReasons rather than a bare FAILED.
        raise Exception(
            f"Gateway {gateway_id} FAILED: statusReasons={resp.get('statusReasons')}"
        )
    if status != 'READY':
        return {'IsComplete': False}

    # Gateway is READY — gate target creation on this. Idempotent across polls.
    _ensure_targets(gateway_id, props)

    if not _targets_ready(gateway_id):
        return {'IsComplete': False}

    # Everything READY. Re-read the gateway so Data is populated on the poll
    # that returns IsComplete True (CFN takes getAttString values from here).
    final = client.get_gateway(gatewayIdentifier=gateway_id)
    return {
        'IsComplete': True,
        'Data': {
            'GatewayId': gateway_id,
            'GatewayUrl': final['gatewayUrl'],
            'GatewayArn': final['gatewayArn'],
        },
    }
