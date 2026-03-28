FROM postgres:16

# Install wal-g and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    jq \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install wal-g binary
ARG WALG_VERSION=v3.0.8
RUN curl -fsSL "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64.tar.gz" \
    | tar xz -C /usr/local/bin/ \
    && mv /usr/local/bin/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g \
    && chmod +x /usr/local/bin/wal-g

# WAL-G environment (overridden at runtime by boot script)
ENV WALG_S3_PREFIX=""
ENV WALG_LIBSODIUM_KEY=""
ENV AWS_ENDPOINT=""
ENV AWS_ACCESS_KEY_ID=""
ENV AWS_SECRET_ACCESS_KEY=""
ENV AWS_S3_FORCE_PATH_STYLE="true"
ENV AWS_REGION="auto"

# Cron job for daily base backups
RUN echo "0 3 * * * root /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1" > /etc/cron.d/walg-backup \
    && chmod 0644 /etc/cron.d/walg-backup

# Build context is deploy/ (set in docker-compose.yaml)
COPY postgres/backup-push.sh /usr/local/bin/backup-push.sh
COPY postgres/init-archive.sh /docker-entrypoint-initdb.d/init-archive.sh
COPY postgres/entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh

RUN chmod +x /usr/local/bin/backup-push.sh \
    /docker-entrypoint-initdb.d/init-archive.sh \
    /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
