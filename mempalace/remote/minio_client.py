"""Shared MinIO client factory for push/pull operations."""

import logging

logger = logging.getLogger(__name__)


def get_minio_client(config):
    """Build a Minio client from MempalaceConfig.

    Raises ImportError if the minio package is not installed,
    or ValueError if required config is missing.
    """
    try:
        from minio import Minio
    except ImportError:
        raise ImportError(
            "The 'minio' package is required for remote sync. "
            "Install it with: pip install mempalace[minio]"
        )

    endpoint = config.minio_endpoint
    if not endpoint:
        raise ValueError(
            "MinIO endpoint not configured. "
            "Set 'minio.endpoint' in ~/.mempalace/config.json "
            "or MEMPALACE_MINIO_ENDPOINT env var."
        )

    access_key = config.minio_access_key
    secret_key = config.minio_secret_key
    if not access_key or not secret_key:
        raise ValueError(
            "MinIO credentials not configured. "
            "Set 'minio.access_key' and 'minio.secret_key' in ~/.mempalace/config.json "
            "or MEMPALACE_MINIO_ACCESS_KEY / MEMPALACE_MINIO_SECRET_KEY env vars."
        )

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=config.minio_secure,
    )


def ensure_bucket(client, bucket_name):
    """Create the bucket if it doesn't exist."""
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        logger.info("Created MinIO bucket: %s", bucket_name)
