import json

import boto3


# ── Built-in evaluators every online config gets ──────────────────────────────
# NOTE: Builtin.ToolSelectionAccuracy / Builtin.ToolParameterAccuracy were
# removed (2026-06-06). They are AWS-managed ReAct-trajectory judges that dock
# the deterministic Tier 2 graph's SliceSufficiency span as a "wrong/misparam
# tool call" — SliceSufficiency is the Phase 3 judge's designed output, not a
# model-selected tool, so no prompt change can satisfy them. They produced
# systematic 0.0 noise that buried the genuinely prompt-addressable signals.
_BUILTIN_EVALUATORS = [
    {'evaluatorId': 'Builtin.GoalSuccessRate'},
    {'evaluatorId': 'Builtin.Correctness'},
]


# ══════════════════════════════════════════════════════════════════════════════
# TWO CUSTOM-RESOURCE STRATEGIES (they behave differently because of a lock)
# ══════════════════════════════════════════════════════════════════════════════
# AgentCore has NO update API for either online-eval configs OR custom evaluators,
# so any change is delete-then-recreate. Two service behaviours shape the design:
#
#   (A) NAME-RELEASE RACE (both kinds): after a delete call returns, the name
#       stays reserved for an unbounded interval (observed 10s..minutes) before a
#       create under that name stops failing with ConflictException.
#
#   (B) EVALUATOR LOCK (evaluators ONLY): an evaluator cannot be deleted while any
#       active online-eval config still references it —
#         ValidationException: "Cannot delete a locked evaluator. Please remove
#         evaluator from all active online evaluation configurations before
#         deleting". (Configs have no such lock.)
#
# A single delete-then-recreate strategy cannot serve both: deleting an evaluator
# up front while a config still references it trips lock (B), the name never frees,
# and a rollback can cascade into deleting the referencing configs. So the two
# kinds use DIFFERENT strategies:
#
# ── EVALUATORS → CFN REPLACEMENT via content-hashed names (breaks the lock) ──────
# The stack derives each evaluator's name as `<base>_<hash>` where the hash is
# computed over the evaluator's mutable content (model, maxTokens, instructions).
# A content change therefore changes the NAME, which makes CFN treat it as a
# REPLACEMENT of a differently-named resource:
#     1. CREATE the new-named evaluator (no conflict — different name);
#     2. the config CR (which references the evaluator id via Fn::GetAtt) UPDATEs
#        to point at the NEW id;
#     3. only AFTER nothing references it does CFN issue DELETE for the OLD
#        evaluator — which is now unlocked, so the delete succeeds.
# There is thus never a same-name create (no race (A) for evaluators) and never a
# delete-while-referenced (no lock (B)). The evaluator hooks below are create-only
# on Create/Update and delete-by-id on Delete — no waiting, no same-name juggling.
# Reuse-by-name on ConflictException is SAFE here precisely because same name ⟹
# same content (an orphan from a prior deploy is byte-identical), so it cannot mask
# a stale definition.
#
# ── CONFIGS → async waiter (handles race (A); no lock to worry about) ────────────
# Configs keep FIXED names (nothing references a config's id, so there is no
# replacement lever, and stable names keep the CfnOutputs/notebooks valid). An
# Update is still delete-then-recreate under the same name, so it uses the CDK
# provider's two hooks to ride out race (A):
#   * on_event    — issues the config delete (Update/Delete) ONCE, returns.
#   * is_complete — re-invoked by the provider's Step Functions poll loop
#                   (queryInterval apart, up to totalTimeout, far beyond a single
#                    Lambda's ceiling). Each poll attempts the create; a
#                    ConflictException just means "name not free yet" → returns
#                    IsComplete=False so the loop waits. No fixed cap, so the
#                    recreate cannot time out prematurely and net-delete the config.
#
# CONFIG INVARIANT (why an Update never falls back to a name lookup): post-delete
# the config name resolves to the corpse of the just-deleted config (or nothing),
# so resolving it would leave the config net-deleted. Only the Create path reuses
# an existing same-name config (idempotent re-deploy of an orphan).


# ══════════════════════════════════════════════════════════════════════════════
# Online evaluation config (Kind: 'config' — the default path)
# ══════════════════════════════════════════════════════════════════════════════
def _evaluator_list(props: dict) -> list:
    """Build the evaluators[] list: the built-ins plus any extra custom IDs.

    Parameters:
        props: the CloudFormation ResourceProperties.
            ``ExtraEvaluatorIds`` is an optional JSON-encoded list of custom
            evaluator IDs (CDK passes tokens as strings, so the list is
            serialized) to append to the built-ins.
            ``BuiltinEvaluatorIds`` is an optional JSON-encoded list of built-in
            evaluator IDs that REPLACES the default ``_BUILTIN_EVALUATORS`` for
            this config. The two query-agent configs pass ``["Builtin.Correctness"]``
            here because their custom ``GoalSuccess`` judge replaces the
            un-editable ``Builtin.GoalSuccessRate`` (which mis-grades the
            deterministic-graph agents by treating an intermediate
            intent-classification JSON span as the assistant's turn). When the
            prop is absent the default built-in set is used, so the three
            non-query configs (metadata, ontology, query_suggestions) are
            unaffected.

    Returns:
        A list of ``{'evaluatorId': <id>}`` dicts for create_online_evaluation_config.
    """
    raw_builtin = props.get('BuiltinEvaluatorIds')
    if raw_builtin:
        builtin_ids = json.loads(raw_builtin) if isinstance(raw_builtin, str) else raw_builtin
        evaluators = [{'evaluatorId': bid} for bid in builtin_ids if bid]
    else:
        evaluators = list(_BUILTIN_EVALUATORS)
    raw_extra = props.get('ExtraEvaluatorIds')
    if raw_extra:
        # CDK serializes the ID list to a JSON string (CFN props are strings/lists of strings).
        extra_ids = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
        evaluators.extend({'evaluatorId': eid} for eid in extra_ids if eid)
    return evaluators


def _create_config(client, props: dict) -> dict:
    """Create an online evaluation config from CFN resource properties.

    Makes a single create attempt with no in-process retry loop — transient
    ConflictException (name still held by an in-flight delete) and IAM-propagation
    delays are handled by the ``is_complete`` poll loop, which re-invokes this via
    the Step Functions waiter until it succeeds. The ConflictException therefore
    propagates OUT of this function so the caller can distinguish "retry" from a
    genuine failure.

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        props: the CloudFormation ResourceProperties for the config.

    Returns:
        The create_online_evaluation_config API response (includes the id).
    """
    return client.create_online_evaluation_config(
        onlineEvaluationConfigName=props['ConfigName'],
        description=props.get('Description', ''),
        rule={
            'samplingConfig': {'samplingPercentage': float(props['SamplingRate'])},
            'sessionConfig': {'sessionTimeoutMinutes': 15},
        },
        dataSourceConfig={
            'cloudWatchLogs': {
                'logGroupNames': [props['LogGroupName']],
                'serviceNames': [props['ServiceName']],
            }
        },
        evaluators=_evaluator_list(props),
        evaluationExecutionRoleArn=props['ExecutionRoleArn'],
        enableOnCreate=True,
    )


def _config_id_by_name(client, config_name: str):
    """Look up an existing online-eval config id by name, or None if absent.

    Used ONLY on the Create path to reuse an orphaned same-name config
    idempotently. Never used on Update (see the module INVARIANT note).

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        config_name: the online-eval config name to resolve.

    Returns:
        The matching onlineEvaluationConfigId, or None when no config has that name.
    """
    configs = client.list_online_evaluation_configs()
    return next(
        (c['onlineEvaluationConfigId'] for c in configs.get('onlineEvaluationConfigs', [])
         if c['onlineEvaluationConfigName'] == config_name),
        None,
    )


def _delete_config(client, config_id: str) -> None:
    """Best-effort delete of an online-eval config by id; swallows all errors.

    A failed delete must not break the custom-resource request path — a missing
    config is the desired end state, and a transient error is retried by the
    waiter (Create/Update) or tolerated (Delete).

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        config_id: the onlineEvaluationConfigId to delete.

    Returns:
        None.
    """
    try:
        client.delete_online_evaluation_config(onlineEvaluationConfigId=config_id)
    except Exception:  # nosec B110 — best-effort cleanup; failure must not break the request path
        pass


def _on_config_event(event: dict, client) -> dict:
    """on_event hook for an online-eval config: issue the delete, defer create.

    Create → no-op (the create happens in is_complete). Update/Delete → delete the
    resource identified by the current PhysicalResourceId (always the service id).
    Returns an empty dict so the framework keeps the existing PhysicalResourceId
    (Delete requires it unchanged; Update's new id is set later by is_complete).

    Parameters:
        event: the CloudFormation custom-resource event.
        client: a bedrock-agentcore-control boto3 client.

    Returns:
        An (empty) on_event result dict.
    """
    request_type = event['RequestType']
    if request_type in ('Update', 'Delete'):
        _delete_config(client, event['PhysicalResourceId'])
    return {}


def _is_complete_config_event(event: dict, client) -> dict:
    """is_complete hook for an online-eval config (polled by the waiter).

    Delete → complete immediately (on_event already issued the delete). Create/
    Update → attempt the create; ConflictException (name still held by the
    in-flight delete) or an IAM-propagation delay returns IsComplete=False so the
    waiter retries. On Create only, a ConflictException whose name resolves to an
    existing config reuses it idempotently (orphan re-deploy). On Update we NEVER
    reuse by name (module INVARIANT) — we keep polling until the name frees and a
    genuinely new config is created.

    Parameters:
        event: the resource event (on_event's return merged over the CFN request).
        client: a bedrock-agentcore-control boto3 client.

    Returns:
        A dict with ``IsComplete``; when complete, also ``PhysicalResourceId`` and
        ``Data.ConfigId`` carrying the live config id.
    """
    request_type = event['RequestType']
    if request_type == 'Delete':
        return {'IsComplete': True}

    props = event['ResourceProperties']
    try:
        config_id = _create_config(client, props)['onlineEvaluationConfigId']
        return {'IsComplete': True, 'PhysicalResourceId': config_id, 'Data': {'ConfigId': config_id}}
    except client.exceptions.ConflictException:
        if request_type == 'Create':
            # Orphaned same-name config from a prior deploy — reuse its id idempotently.
            existing = _config_id_by_name(client, props['ConfigName'])
            if existing:
                return {'IsComplete': True, 'PhysicalResourceId': existing, 'Data': {'ConfigId': existing}}
        # Update (or Create with the name still mid-delete): wait for the name to free.
        return {'IsComplete': False}
    except Exception as e:
        if 'permissions' in str(e).lower():
            # IAM propagation delay right after role creation — let the waiter retry.
            return {'IsComplete': False}
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Custom LLM-as-Judge evaluator (Kind: 'evaluator')
# ══════════════════════════════════════════════════════════════════════════════
def _create_evaluator(client, props: dict) -> dict:
    """Create a binary LLM-as-Judge evaluator from CFN resource properties.

    Single attempt, no in-process retry — the ``is_complete`` waiter re-invokes
    this until the name frees (see the module ASYNC note). ConflictException
    propagates out so the caller can treat it as "retry".

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        props: ResourceProperties carrying EvaluatorName, Level, Instructions, and
            JudgeModelId. The rating scale is a fixed binary pass/fail scale.

    Returns:
        The create_evaluator API response (includes ``evaluatorId``).
    """
    return client.create_evaluator(
        evaluatorName=props['EvaluatorName'],
        description=props.get('Description', ''),
        level=props['Level'],
        evaluatorConfig={
            'llmAsAJudge': {
                'instructions': props['Instructions'],
                'ratingScale': {
                    'numerical': [
                        {'value': 0.0, 'label': 'fail',
                         'definition': 'Does not satisfy the criterion.'},
                        {'value': 1.0, 'label': 'pass',
                         'definition': 'Fully satisfies the criterion.'},
                    ]
                },
                'modelConfig': {
                    'bedrockEvaluatorModelConfig': {
                        'modelId': props['JudgeModelId'],
                        'inferenceConfig': {'maxTokens': int(props.get('MaxTokens', 1024))},
                    }
                },
            }
        },
    )


def _evaluator_id_by_name(client, evaluator_name: str):
    """Find an existing evaluator id by name (paginated), or None if absent.

    Used ONLY on the Create path to reuse an orphaned same-name evaluator. Never
    used on Update (see the module INVARIANT note).

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        evaluator_name: the evaluator name to resolve.

    Returns:
        The matching evaluatorId, or None when no evaluator has that name.
    """
    next_token = None
    while True:
        kwargs = {'maxResults': 100}
        if next_token:
            kwargs['nextToken'] = next_token
        resp = client.list_evaluators(**kwargs)
        for ev in resp.get('evaluators', []):
            if ev.get('evaluatorName') == evaluator_name:
                return ev['evaluatorId']
        next_token = resp.get('nextToken')
        if not next_token:
            return None


def _delete_evaluator(client, evaluator_id: str) -> None:
    """Best-effort delete of a custom evaluator by id; swallows all errors.

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        evaluator_id: the evaluatorId to delete.

    Returns:
        None.
    """
    try:
        client.delete_evaluator(evaluatorId=evaluator_id)
    except Exception:  # nosec B110 — best-effort cleanup; failure must not break the request path
        pass


def _on_evaluator_event(event: dict, client) -> dict:
    """on_event hook for a custom evaluator (CFN-replacement strategy).

    CRITICAL — unlike the config hook, this NEVER deletes on Update. Evaluator
    names are content-hashed by the stack, so a content change yields a NEW name
    and CFN performs a REPLACEMENT: the new-named evaluator is created (in
    is_complete), the referencing config re-points to its id, and only then does
    CFN issue a separate Delete for the OLD evaluator — which is unlocked by then.
    Deleting the old evaluator here (while the config still references it) is
    exactly what triggered the "Cannot delete a locked evaluator" ValidationException
    and the 2026-07-01 config outage.

    Only a genuine Delete request deletes; Create/Update defer to is_complete.

    Parameters:
        event: the CloudFormation custom-resource event.
        client: a bedrock-agentcore-control boto3 client.

    Returns:
        An (empty) on_event result dict.
    """
    if event['RequestType'] == 'Delete':
        _delete_evaluator(client, event['PhysicalResourceId'])
    return {}


def _is_complete_evaluator_event(event: dict, client) -> dict:
    """is_complete hook for a custom evaluator (CFN-replacement strategy).

    Delete → complete immediately (on_event issued the delete). Create/Update →
    create the (content-hashed-name) evaluator and return its NEW id as the
    PhysicalResourceId; the changed id is what drives CFN's replacement (config
    re-points to the new id, then CFN deletes the old evaluator last). No waiting:
    the name is unique per content, so there is no same-name release race here.

    Reuse-by-name on ConflictException is SAFE for BOTH Create and Update here
    (unlike configs): an existing evaluator with this exact name is byte-identical
    (name ⟹ content), so reusing it cannot mask a stale definition. This tolerates
    idempotent re-deploys, CFN is_complete retries, and rollback restores (which
    re-create a specific prior-content name that may still exist).

    An IAM-propagation error ('permissions' in the message) returns IsComplete=False
    so the waiter retries rather than failing the deploy.

    Parameters:
        event: the resource event (on_event's return merged over the CFN request).
        client: a bedrock-agentcore-control boto3 client.

    Returns:
        A dict with ``IsComplete``; when complete, also ``PhysicalResourceId`` and
        ``Data.EvaluatorId`` carrying the live evaluator id.
    """
    request_type = event['RequestType']
    if request_type == 'Delete':
        return {'IsComplete': True}

    props = event['ResourceProperties']
    try:
        evaluator_id = _create_evaluator(client, props)['evaluatorId']
        return {'IsComplete': True, 'PhysicalResourceId': evaluator_id,
                'Data': {'EvaluatorId': evaluator_id}}
    except client.exceptions.ConflictException:
        # Same name ⟹ same content, so reusing the existing id is always correct.
        existing = _evaluator_id_by_name(client, props['EvaluatorName'])
        if existing:
            return {'IsComplete': True, 'PhysicalResourceId': existing,
                    'Data': {'EvaluatorId': existing}}
        # Name held by an in-flight delete of an identical-content orphan — wait.
        return {'IsComplete': False}
    except Exception as e:
        if 'permissions' in str(e).lower():
            return {'IsComplete': False}
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Framework entrypoints — dispatched on the ``Kind`` resource property:
#   'evaluator' → custom LLM-as-Judge evaluator; anything else → online-eval config.
# ══════════════════════════════════════════════════════════════════════════════
def on_event(event: dict, context) -> dict:
    """CDK provider-framework on_event entrypoint (issues deletes, defers creates).

    Parameters:
        event: the CloudFormation custom-resource event.
        context: the Lambda context (unused).

    Returns:
        The on_event result dict (empty — creates happen in is_complete).
    """
    client = boto3.client('bedrock-agentcore-control')
    kind = event.get('ResourceProperties', {}).get('Kind', 'config')
    if kind == 'evaluator':
        return _on_evaluator_event(event, client)
    return _on_config_event(event, client)


def is_complete(event: dict, context) -> dict:
    """CDK provider-framework is_complete entrypoint (polled until the create lands).

    Parameters:
        event: the resource event (on_event's return merged over the CFN request).
        context: the Lambda context (unused).

    Returns:
        A dict with ``IsComplete`` and, when complete, ``PhysicalResourceId``/``Data``.
    """
    client = boto3.client('bedrock-agentcore-control')
    kind = event.get('ResourceProperties', {}).get('Kind', 'config')
    if kind == 'evaluator':
        return _is_complete_evaluator_event(event, client)
    return _is_complete_config_event(event, client)
