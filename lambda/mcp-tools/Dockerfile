# MCP tools Lambda — invoked by AgentCore Gateway (item #6).
FROM public.ecr.aws/lambda/python:3.12-arm64
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY index.py ${LAMBDA_TASK_ROOT}/

CMD ["index.lambda_handler"]
