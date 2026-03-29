"""S3 upload utility for query agent result storage."""

from __future__ import annotations

import logging

import boto3
from botocore.config import Config as BotoConfig

from .config import Settings

logger = logging.getLogger(__name__)


class S3Uploader:
    """Uploads files to S3 (or S3-compatible storage)."""

    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.prefix = settings.s3_prefix

        kwargs: dict = {}
        if settings.s3_region:
            kwargs["region_name"] = settings.s3_region
        if settings.s3_access_key_id:
            kwargs["aws_access_key_id"] = settings.s3_access_key_id
        if settings.s3_secret_access_key:
            kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url

        self._client = boto3.client(
            "s3",
            config=BotoConfig(
                retries={"max_attempts": 2},
                signature_version="s3v4",
            ),
            **kwargs,
        )

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes to S3. Returns the full S3 URI (s3://bucket/key)."""
        full_key = f"{self.prefix}/{key}" if self.prefix else key
        self._client.put_object(
            Bucket=self.bucket,
            Key=full_key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("Uploaded %d bytes to s3://%s/%s", len(data), self.bucket, full_key)
        return f"s3://{self.bucket}/{full_key}"

    def presign_url(self, s3_url: str, expires_in: int = 604800) -> str | None:
        """Generate a presigned GET URL from an s3:// URI. Default expiry: 7 days."""
        if not s3_url or not s3_url.startswith("s3://"):
            return None
        # s3://bucket/key → bucket, key
        without_scheme = s3_url[len("s3://"):]
        bucket, _, key = without_scheme.partition("/")
        if not key:
            return None
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            logger.warning("Failed to generate presigned URL for %s: %s", s3_url, e)
            return None
