FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY cloud_api ./cloud_api

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[web,mcp,cloud]" \
    && python -m playwright install --with-deps chromium

EXPOSE 8000

CMD ["uvicorn", "cloud_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
