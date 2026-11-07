# The demo API: FastAPI + MCP over a snapshot of the offers, query embeddings with int8 bge-m3 on the CPU.
# One image for both clouds: Azure Container Apps runs it as a plain container; on AWS Lambda the
# Lambda Web Adapter extension below forwards invocations to the same HTTP server.
#
#   uv run jm snapshot snapshot && docker build -t job-matcher-demo .

FROM python:3.13-slim AS model
ARG MODEL_REV=4de13258303883538bd53b696b452bf8099f0858
ADD --chmod=644 https://huggingface.co/Xenova/bge-m3/resolve/${MODEL_REV}/onnx/model_int8.onnx /model/model_int8.onnx
ADD --chmod=644 https://huggingface.co/Xenova/bge-m3/resolve/${MODEL_REV}/tokenizer.json /model/tokenizer.json

FROM python:3.13-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter
RUN useradd --create-home app
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --from=model /model /model
COPY snapshot /app/snapshot
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    JM_SNAPSHOT=/app/snapshot JM_ONNX_DIR=/model JM_QUERY_EMBEDDER=onnx \
    PORT=8080 AWS_LWA_READINESS_CHECK_PATH=/healthz
USER app
EXPOSE 8080
CMD ["sh", "-c", "uvicorn --factory job_matcher.api:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
