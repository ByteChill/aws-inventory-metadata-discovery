# One image for every component. Lambda functions override CMD with their handler;
# ECS tasks override the entrypoint (API: uvicorn --factory inventory.api.app:create_app,
# worker: python -m inventory.handlers.worker).
FROM public.ecr.aws/lambda/python:3.12

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR ${LAMBDA_TASK_ROOT}
# pip --target puts console scripts (uvicorn, alembic) under ./bin; make them and the
# package importable for the ECS entrypoints as well as for Lambda.
ENV PATH="${LAMBDA_TASK_ROOT}/bin:${PATH}" \
    PYTHONPATH="${LAMBDA_TASK_ROOT}"

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o requirements.txt \
    && pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}" \
    && rm requirements.txt

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY inventory/ ./inventory/

CMD ["inventory.handlers.broker_lambda.handler"]
