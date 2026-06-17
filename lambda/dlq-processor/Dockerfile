# Build DLQ processor Lambda for AWS
FROM public.ecr.aws/lambda/python:3.12 as builder
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

# Copy requirements and source
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
COPY index.py ${LAMBDA_TASK_ROOT}/

# Install dependencies
RUN pip install -r ${LAMBDA_TASK_ROOT}/requirements.txt -t "${LAMBDA_TASK_ROOT}" --no-cache-dir

# Final stage
FROM public.ecr.aws/lambda/python:3.12
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

# Copy from builder
COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}

# Set handler
CMD ["index.handler"]
