import asyncio
import mimetypes
import os
from typing import Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config

from utils.get_env import (
    get_object_storage_access_key_id_env,
    get_object_storage_bucket_name_env,
    get_object_storage_endpoint_env,
    get_object_storage_force_path_style_env,
    get_object_storage_prefix_env,
    get_object_storage_region_env,
    get_object_storage_request_checksum_calculation_env,
    get_object_storage_response_checksum_validation_env,
    get_object_storage_secret_access_key_env,
    get_object_storage_transfer_max_concurrency_env,
    get_object_storage_transfer_use_threads_env,
)

_s3_client = None


def _build_s3_client():
    endpoint = get_object_storage_endpoint_env()
    access_key = get_object_storage_access_key_id_env()
    secret_key = get_object_storage_secret_access_key_env()
    region = get_object_storage_region_env()

    if not endpoint or not access_key or not secret_key:
        # S3 is not configured – caller should handle this case.
        return None

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            s3={
                "addressing_style": (
                    "path" if get_object_storage_force_path_style_env() else "virtual"
                )
            },
            request_checksum_calculation=get_object_storage_request_checksum_calculation_env(),
            response_checksum_validation=get_object_storage_response_checksum_validation_env(),
        ),
    )


def _build_transfer_config() -> TransferConfig:
    use_threads = get_object_storage_transfer_use_threads_env()
    return TransferConfig(
        max_concurrency=get_object_storage_transfer_max_concurrency_env(),
        use_threads=use_threads,
        # CRT ignores use_threads; an explicitly serial profile must use the
        # classic transfer manager even if awscrt is installed in the image.
        preferred_transfer_client="auto" if use_threads else "classic",
    )


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = _build_s3_client()
    return _s3_client


def _upload_file_to_s3_sync(
    local_path: str,
    postfix: Optional[str] = None,
) -> Optional[str]:
    """
    Synchronous helper that uploads a file to S3 and returns the object key
    (prefix[/postfix]/filename) that can later be used to download it again.

    Returns:
        The object key of the uploaded file or None if uploading is disabled
        or any configuration is missing.
    """
    client = get_s3_client()
    bucket = get_object_storage_bucket_name_env()

    if client is None or not bucket:
        return None

    filename = os.path.basename(local_path)
    base_prefix = get_object_storage_prefix_env() or ""

    # Build key like: <base_prefix>/<postfix>/<filename>
    # If either base_prefix or postfix are missing, they are simply skipped.
    key_parts = []
    if base_prefix:
        key_parts.append(base_prefix.strip("/"))
    if postfix:
        key_parts.append(str(postfix).strip("/"))
    key_parts.append(filename)

    key = "/".join(key_parts)

    extra_args = {}
    content_type, _ = mimetypes.guess_type(local_path)
    if content_type:
        extra_args["ContentType"] = content_type

    client.upload_file(
        local_path,
        bucket,
        key,
        ExtraArgs=extra_args or None,
        Config=_build_transfer_config(),
    )

    # We store and return only the S3 object key, not a public URL.
    return key


async def upload_file_to_s3(
    local_path: str,
    postfix: Optional[str] = None,
) -> Optional[str]:
    """
    Asynchronously upload a local file to S3/MinIO and return its object key.

    If S3 is not configured or upload fails, returns None so callers can fall
    back to existing filesystem-based behavior.
    """
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _upload_file_to_s3_sync, local_path, postfix
        )
    except Exception as e:
        # Transport errors may contain signed URLs; log only the exception class.
        print(f"Error uploading file to S3 ({type(e).__name__})")
        return None


def _download_file_from_s3_sync(object_key: str, local_path: str) -> Optional[str]:
    """
    Synchronous helper that downloads an object from S3 using its key into
    the specified local_path.

    Returns:
        The local_path of the downloaded file, or None if downloading is
        disabled or fails.
    """
    client = get_s3_client()
    bucket = get_object_storage_bucket_name_env()

    if client is None or not bucket:
        return None

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    client.download_file(
        bucket, object_key, local_path, Config=_build_transfer_config()
    )
    return local_path


async def download_file_from_s3(object_key: str, local_path: str) -> Optional[str]:
    """
    Asynchronously download an object from S3/MinIO by its key into the given
    local_path.
    """
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _download_file_from_s3_sync, object_key, local_path
        )
    except Exception as e:
        print(f"Error downloading file from S3 ({type(e).__name__})")
        return None
