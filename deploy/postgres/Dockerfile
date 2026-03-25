FROM postgres:16-alpine

# Install wal-g and dependencies
RUN apk add --no-cache \
    curl \
    jq \
    bash \
    python3 \
    py3-pip \
    supercronic \
    && pip install --no-cache-dir --break-system-packages dstack-sdk \
    && rm -rf /var/cache/apk/*

# Install wal-g binary
ARG WALG_VERSION=v3.0.3
RUN curl -fsSL "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-linux-amd64.tar.gz" \
    | tar xz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/wal-g

# WAL-G environment (overridden at runtime by boot script)
ENV WALG_S3_PREFIX=""
ENV WALG_LIBSODIUM_KEY=""
ENV AWS_ENDPOINT=""
ENV AWS_ACCESS_KEY_ID=""
ENV AWS_SECRET_ACCESS_KEY=""
ENV AWS_S3_FORCE_PATH_STYLE="true"
ENV AWS_REGION="auto"

# Cron job for daily base backups (runs as postgres user)
RUN echo "0 3 * * * /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1" > /etc/crontab

# Build context is deploy/ (set in docker-compose.yaml)
COPY postgres/backup-push.sh /usr/local/bin/backup-push.sh
COPY postgres/init-archive.sh /docker-entrypoint-initdb.d/init-archive.sh
COPY postgres/entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh
COPY kms.py /usr/local/bin/kms.py

RUN chmod +x /usr/local/bin/backup-push.sh \
    /docker-entrypoint-initdb.d/init-archive.sh \
    /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
