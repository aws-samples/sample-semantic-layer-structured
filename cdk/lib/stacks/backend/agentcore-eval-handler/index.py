import json
import time
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
# Online evaluation config (Kind: 'config' — the default/legacy path)
# ══════════════════════════════════════════════════════════════════════════════
def _evaluator_list(props):
    """Build the evaluators[] list: the built-ins plus any extra custom IDs.

    Parameters:
        props: the CloudFormation ResourceProperties. ``ExtraEvaluatorIds`` is an
            optional JSON-encoded list of custom evaluator IDs (CDK passes tokens as
            strings, so the list is serialized) to append to the built-ins.

    Returns:
        A list of ``{'evaluatorId': <id>}`` dicts for create_online_evaluation_config.
    """
    evaluators = list(_BUILTIN_EVALUATORS)
    raw_extra = props.get('ExtraEvaluatorIds')
    if raw_extra:
        # CDK serializes the ID list to a JSON string (CFN props are strings/lists of strings).
        extra_ids = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
        evaluators.extend({'evaluatorId': eid} for eid in extra_ids if eid)
    return evaluators


def _create_config(client, props):
    """Create an online evaluation config, retrying through transient errors.

    Two distinct transient conditions are retried here, both with backoff:
      * IAM propagation delays right after the execution role is created
        (surfaced as a 'permissions' AccessDenied-style message); and
      * ``ConflictException`` because the config name is still reserved by an
        in-flight asynchronous delete (the Update path deletes the old config
        by id immediately before recreating under the SAME name). We MUST wait
        for the name to free and then actually create — never fall back to
        looking the name up, because on Update the name resolves to the corpse
        of the just-deleted config (or to nothing), leaving the config deleted
        and never recreated. That silent net-delete is exactly the failure this
        retry loop exists to prevent.

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        props: the CloudFormation ResourceProperties for the config.

    Returns:
        The create_online_evaluation_config API response (includes the id).
    """
    last_err = None
    # More attempts than the IAM-only loop: a delete can take ~10-20s to free
    # the name, so we back off up to 2**6 = 64s on the final ConflictException retry.
    for attempt in range(7):
        try:
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
        except client.exceptions.ConflictException as e:
            # Name still held by an in-flight delete — wait for it to free, then retry.
            if attempt < 6:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
        except Exception as e:
            if 'permissions' in str(e).lower() and attempt < 6:
                # IAM propagation delay — wait and retry
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
    raise last_err


def _config_id_by_name(client, config_name):
    """Look up an existing online-eval config id by name (for ConflictException reuse)."""
    configs = client.list_online_evaluation_configs()
    return next(
        (c['onlineEvaluationConfigId'] for c in configs.get('onlineEvaluationConfigs', [])
         if c['onlineEvaluationConfigName'] == config_name),
        config_name,
    )


def _on_config_event(event, client):
    """Handle Create/Update/Delete for an online evaluation config custom resource."""
    request_type = event['RequestType']
    props = event['ResourceProperties']

    if request_type == 'Create':
        # On a genuine Create the name may already exist from a prior orphaned
        # deploy — reuse that id idempotently. (Distinct from Update, where the
        # name MUST be recreated, not reused; see _create_config.)
        try:
            response = _create_config(client, props)
            config_id = response['onlineEvaluationConfigId']
        except client.exceptions.ConflictException:
            config_id = _config_id_by_name(client, props['ConfigName'])
        return {'PhysicalResourceId': config_id, 'Data': {'ConfigId': config_id}}

    if request_type == 'Update':
        # No update API exists — delete the old config and recreate with new properties.
        # _create_config retries through the ConflictException raised while the name is
        # still held by the in-flight delete, so it returns a genuinely NEW id. Returning
        # that new PhysicalResourceId makes CFN issue a DELETE for the old id, handled
        # silently below. We deliberately do NOT catch ConflictException here: falling
        # back to a name lookup would resolve to the just-deleted config's id and leave
        # the config net-deleted (the bug that took all five configs offline).
        try:
            client.delete_online_evaluation_config(
                onlineEvaluationConfigId=event['PhysicalResourceId'],
            )
        except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
            pass
        response = _create_config(client, props)
        config_id = response['onlineEvaluationConfigId']
        return {'PhysicalResourceId': config_id, 'Data': {'ConfigId': config_id}}

    # Delete
    try:
        client.delete_online_evaluation_config(
            onlineEvaluationConfigId=event['PhysicalResourceId'],
        )
    except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
        pass
    return {'PhysicalResourceId': event['PhysicalResourceId']}


# ══════════════════════════════════════════════════════════════════════════════
# Custom LLM-as-Judge evaluator (Kind: 'evaluator')
# ══════════════════════════════════════════════════════════════════════════════
def _create_evaluator(client, props):
    """Create a binary LLM-as-Judge evaluator from CFN resource properties.

    Parameters:
        client: a bedrock-agentcore-control boto3 client.
        props: ResourceProperties carrying EvaluatorName, Level, Instructions, and
            JudgeModelId. The rating scale is a fixed binary pass/fail scale.

    Returns:
        The create_evaluator API response (includes ``evaluatorId``).
    """
    # Retry through ConflictException: on the Update path the same-name evaluator
    # is deleted immediately before this create, and the name stays reserved while
    # that delete is in flight. We wait for it to free and create for real — never
    # fall back to a name lookup, which would resolve to the just-deleted id and
    # leave the evaluator net-deleted (same failure mode as the config path).
    last_err = None
    for attempt in range(7):
        try:
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
        except client.exceptions.ConflictException as e:
            if attempt < 6:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
    raise last_err


def _evaluator_id_by_name(client, evaluator_name):
    """Find an existing evaluator id by name (paginated), or None if absent."""
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


def _on_evaluator_event(event, client):
    """Handle Create/Update/Delete for a custom-evaluator custom resource."""
    request_type = event['RequestType']
    props = event['ResourceProperties']
    name = props['EvaluatorName']

    if request_type == 'Create':
        try:
            evaluator_id = _create_evaluator(client, props)['evaluatorId']
        except client.exceptions.ConflictException:
            # Same-name evaluator already exists — reuse it (idempotent re-deploy).
            evaluator_id = _evaluator_id_by_name(client, name) or name
        return {'PhysicalResourceId': evaluator_id, 'Data': {'EvaluatorId': evaluator_id}}

    if request_type == 'Update':
        # No UpdateEvaluator API — delete the old one and recreate. _create_evaluator
        # retries through the ConflictException raised while the name is still held by
        # the in-flight delete, so it returns a genuinely NEW id; the new PhysicalResourceId
        # makes CFN issue a Delete for the old id (handled silently below). We deliberately
        # do NOT catch ConflictException here — a name-lookup fallback would resolve to the
        # just-deleted evaluator and leave it net-deleted.
        try:
            client.delete_evaluator(evaluatorId=event['PhysicalResourceId'])
        except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
            pass
        evaluator_id = _create_evaluator(client, props)['evaluatorId']
        return {'PhysicalResourceId': evaluator_id, 'Data': {'EvaluatorId': evaluator_id}}

    # Delete
    try:
        client.delete_evaluator(evaluatorId=event['PhysicalResourceId'])
    except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
        pass
    return {'PhysicalResourceId': event['PhysicalResourceId']}


def on_event(event, context):
    """Custom-resource entrypoint. Dispatches on the ``Kind`` resource property:
    'evaluator' → manage a custom LLM-as-Judge evaluator; anything else → online-eval config.
    """
    client = boto3.client('bedrock-agentcore-control')
    kind = event.get('ResourceProperties', {}).get('Kind', 'config')
    if kind == 'evaluator':
        return _on_evaluator_event(event, client)
    return _on_config_event(event, client)
