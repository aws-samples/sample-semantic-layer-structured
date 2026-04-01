import time
import boto3


def _create_config(client, props):
    """Create an online evaluation config, retrying on IAM propagation delays."""
    last_err = None
    for attempt in range(5):
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
                evaluators=[
                    {'evaluatorId': 'Builtin.GoalSuccessRate'},
                    {'evaluatorId': 'Builtin.Correctness'},
                    {'evaluatorId': 'Builtin.ToolParameterAccuracy'},
                    {'evaluatorId': 'Builtin.ToolSelectionAccuracy'},
                ],
                evaluationExecutionRoleArn=props['ExecutionRoleArn'],
                enableOnCreate=True,
            )
        except client.exceptions.ConflictException:
            raise
        except Exception as e:
            if 'permissions' in str(e).lower() and attempt < 4:
                # IAM propagation delay — wait and retry
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
    raise last_err


def on_event(event, context):
    request_type = event['RequestType']
    props = event['ResourceProperties']
    client = boto3.client('bedrock-agentcore-control')

    if request_type == 'Create':
        try:
            response = _create_config(client, props)
            config_id = response['onlineEvaluationConfigId']
        except client.exceptions.ConflictException:
            configs = client.list_online_evaluation_configs()
            config_id = next(
                (c['onlineEvaluationConfigId'] for c in configs.get('onlineEvaluationConfigs', [])
                 if c['onlineEvaluationConfigName'] == props['ConfigName']),
                props['ConfigName'],
            )
        return {'PhysicalResourceId': config_id, 'Data': {'ConfigId': config_id}}

    elif request_type == 'Update':
        # No update API exists — delete old config and recreate with new properties.
        # Returning a new PhysicalResourceId causes CFN to issue a DELETE for the old ID,
        # which our Delete handler handles silently.
        try:
            client.delete_online_evaluation_config(
                onlineEvaluationConfigId=event['PhysicalResourceId'],
            )
        except Exception:
            pass
        try:
            response = _create_config(client, props)
            config_id = response['onlineEvaluationConfigId']
        except client.exceptions.ConflictException:
            configs = client.list_online_evaluation_configs()
            config_id = next(
                (c['onlineEvaluationConfigId'] for c in configs.get('onlineEvaluationConfigs', [])
                 if c['onlineEvaluationConfigName'] == props['ConfigName']),
                props['ConfigName'],
            )
        return {'PhysicalResourceId': config_id, 'Data': {'ConfigId': config_id}}

    elif request_type == 'Delete':
        try:
            client.delete_online_evaluation_config(
                onlineEvaluationConfigId=event['PhysicalResourceId'],
            )
        except Exception:
            pass
        return {'PhysicalResourceId': event['PhysicalResourceId']}
