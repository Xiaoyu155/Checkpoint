FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app/xiao

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY xiao/pyproject.toml xiao/README.md xiao/LICENSE xiao/MANIFEST.in ./
COPY xiao/src ./src
COPY xiao/cloud_api ./cloud_api
COPY xiao/templates ./templates
COPY xiao/examples ./examples
COPY xiao/workflows ./workflows

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[web,cloud]" \
    && python -m playwright install --with-deps chromium

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --retries=3 \
    CMD curl --fail http://127.0.0.1:8000/v1/health || exit 1

CMD ["uvicorn", "cloud_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
