FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY hivemind/ ./hivemind/
COPY deploy/boot.sh /app/deploy/boot.sh
RUN chmod +x /app/deploy/boot.sh

EXPOSE 8100

ENTRYPOINT ["/app/deploy/boot.sh"]
