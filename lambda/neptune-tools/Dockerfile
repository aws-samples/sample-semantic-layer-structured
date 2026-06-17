# Neptune Tools Lambda - ARM64 container image
FROM public.ecr.aws/lambda/python:3.12-arm64
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

# Copy requirements and install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy Lambda function code
COPY index.py ${LAMBDA_TASK_ROOT}/

# Set the handler
CMD ["index.lambda_handler"]
