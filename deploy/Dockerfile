# python:3.12-slim, resolved 2026-04-29.
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    iptables \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock LICENSE README.md ./
RUN pip install --no-cache-dir --root-user-action=ignore "uv==0.9.2" \
    && uv export --quiet --frozen --format requirements-txt --no-dev --no-hashes \
        --no-emit-project --output-file /tmp/requirements.txt \
    && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
    && pip uninstall --yes --root-user-action=ignore uv \
    && rm -f /tmp/requirements.txt

COPY hivemind/ ./hivemind/
COPY agents/ ./agents/
COPY deploy/boot.sh /app/deploy/boot.sh
RUN pip install --no-cache-dir --root-user-action=ignore --no-deps . \
    && chmod +x /app/deploy/boot.sh

EXPOSE 8100

ENTRYPOINT ["/app/deploy/boot.sh"]
