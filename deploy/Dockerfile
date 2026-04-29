FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir --root-user-action=ignore "uv==0.9.2" \
    && uv export --quiet --frozen --format requirements-txt --no-dev --no-hashes \
        --no-emit-project --output-file /tmp/requirements.txt \
    && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
    && pip uninstall --yes --root-user-action=ignore uv \
    && rm -f /tmp/requirements.txt

COPY hivemind/ ./hivemind/
COPY deploy/boot.sh /app/deploy/boot.sh
RUN pip install --no-cache-dir --root-user-action=ignore --no-deps . \
    && chmod +x /app/deploy/boot.sh

EXPOSE 8100

ENTRYPOINT ["/app/deploy/boot.sh"]
