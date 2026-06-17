FROM public.ecr.aws/lambda/python:3.13 as builder
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
COPY index.py ${LAMBDA_TASK_ROOT}/

RUN pip install -r ${LAMBDA_TASK_ROOT}/requirements.txt -t "${LAMBDA_TASK_ROOT}" --no-cache-dir

FROM public.ecr.aws/lambda/python:3.13
# checkov:skip=CKV_DOCKER_3:Lambda runtime requires root; non-root USER breaks the Lambda bootstrap entrypoint
# checkov:skip=CKV_DOCKER_2:Lambda health is managed by the AWS runtime; HEALTHCHECK instruction not applicable

COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}

CMD ["index.handler"]
