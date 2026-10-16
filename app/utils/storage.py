"""
S3 / MinIO file upload helper.
Boto3 client is created lazily; S3_ENDPOINT_URL enables MinIO for local dev.
"""

import boto3

from app.config import get_settings

settings = get_settings()

_s3_client = None


def _get_client():
    global _s3_client
    if _s3_client is None:
        kwargs: dict = {}
        # SVC-250: only inject static creds when both are set. Otherwise let
        # boto3's default credential chain resolve (App Runner injects
        # `AWS_CONTAINER_CREDENTIALS_FULL_URI` for the instance role; passing
        # empty-string keys here silently overrode that chain and caused every
        # S3 call to fail with `AuthorizationHeaderMalformed`).
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        if settings.AWS_REGION:
            kwargs["region_name"] = settings.AWS_REGION
        if settings.S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
        _s3_client = boto3.client("s3", **kwargs)
    return _s3_client


def upload_file(file_obj, key: str, content_type: str = "application/octet-stream") -> str:
    """
    Upload a file-like object to S3/MinIO.
    Returns the public URL (or S3 key if no public URL is configured).
    """
    client = _get_client()
    client.upload_fileobj(
        file_obj,
        settings.S3_BUCKET_NAME,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    if settings.S3_ENDPOINT_URL:
        return f"{settings.S3_ENDPOINT_URL}/{settings.S3_BUCKET_NAME}/{key}"
    return f"https://{settings.S3_BUCKET_NAME}.s3.amazonaws.com/{key}"


def download_bytes(key: str) -> bytes:
    """Read an S3 object into memory (SVC-307).

    Used by the KYC verifier to score exactly the docs we stored, without
    threading the raw bytes through two service methods. Small objects
    only — Aadhaar images cap at 5 MB per the ``/providers/me/kyc``
    validator — so an in-memory read is fine.
    """
    import io

    buf = io.BytesIO()
    _get_client().download_fileobj(settings.S3_BUCKET_NAME, key, buf)
    return buf.getvalue()


def delete_file(key: str) -> None:
    """Remove an object from the bucket."""
    _get_client().delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)


def key_from_url(url: str) -> str | None:
    """The S3 key behind a URL ``upload_file`` returned, or None for anything else.

    Photo columns store the URL, not the key (SVC-367 needs the key to delete
    the object). Mirrors the two shapes ``upload_file`` builds.
    """
    prefixes = [f"https://{settings.S3_BUCKET_NAME}.s3.amazonaws.com/"]
    if settings.S3_ENDPOINT_URL:
        prefixes.append(f"{settings.S3_ENDPOINT_URL}/{settings.S3_BUCKET_NAME}/")
    for prefix in prefixes:
        if url.startswith(prefix) and len(url) > len(prefix):
            return url[len(prefix) :]
    return None


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    """
    Generate a presigned GET URL for a private S3 object.

    Used to expose KYC documents to admin reviewers without making the bucket
    public. Default TTL is 1 hour — long enough for an admin to open the page,
    view docs, and decide, short enough that a leaked URL has limited blast
    radius.
    """
    return _get_client().generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )
