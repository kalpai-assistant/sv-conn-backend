"""
Upload service banner videos to S3 + wire ``services.banner_video_url``.

Reads a JSON mapping file where each entry describes one video and the
services it covers. Same video can back multiple services when they share
a visual (Vipul's request: reuse videos across related SKUs).

## Mapping shape

```json
[
  {
    "video": "salon/facial.mp4",
    "sub_category": "salon-women",
    "services": ["Facial"]
  },
  {
    "video": "Nail Art/premium-nail-art.mp4",
    "sub_category": "nail-art",
    "services": ["Premium nail art", "Bridal basic nail set", "Luxury nail set"]
  }
]
```

## Behaviour

1. **Validate everything first, upload nothing** — resolves every
   sub-category slug and service name against the DB before touching S3
   or the URL column. Missing entries fail loudly with a clear message.
2. **One S3 object per source (video, service_id)** — the same video
   content is uploaded per-service so each service's ``banner_video_url``
   points at a URL scoped to its own service_id. Cache-busting is trivial
   (URL includes UUID); browsers cache aggressively; re-uploads overwrite.
3. **Idempotent** — re-runs safely overwrite the S3 object AND re-set the
   DB column. No no-op detection; the wire-cost is trivial.
4. **DB commit is atomic per run** — if any single upload fails, prior
   S3 uploads stay (S3 is authoritative), but the DB URLs are only set
   after the loop finishes. Partial state on failure is loud rather than
   silently inconsistent.

## Usage

```bash
cd backend
export AWS_PROFILE=eazyserv AWS_DEFAULT_REGION=ap-south-1
export DATABASE_URL=$(aws ssm get-parameter \\
    --name /eazyserv/beta/DATABASE_URL --with-decryption \\
    --query 'Parameter.Value' --output text)

# Dry run first to confirm the resolution
uv run python scripts/upload_service_videos.py \\
    --video-dir "/Users/vips/Downloads/S3 uploads/womens" \\
    --mapping scripts/data/womens_video_mapping.json \\
    --dry-run

# Then for real
uv run python scripts/upload_service_videos.py \\
    --video-dir "/Users/vips/Downloads/S3 uploads/womens" \\
    --mapping scripts/data/womens_video_mapping.json
```

## Depends on

- SVC-293 backend migration (adds ``services.banner_video_url`` column).
  If the column doesn't exist yet, run the migration first or this
  script errors with an AttributeError on Service.banner_video_url.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.database import AsyncSessionLocal
from app.models.category import Category
from app.models.service import Service

S3_BUCKET = "eazyserv-downloads"
S3_PREFIX = "services-media"
S3_REGION = "ap-south-1"


@dataclass
class ResolvedUpload:
    """One (video file, target service) pair, ready to upload."""

    video_path: Path
    service_id: str
    service_name: str
    sub_category_slug: str

    @property
    def s3_key(self) -> str:
        return f"{S3_PREFIX}/{self.service_id}/banner.mp4"

    @property
    def public_url(self) -> str:
        return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{self.s3_key}"


async def _resolve_mapping(mapping: list[dict[str, Any]], video_dir: Path) -> list[ResolvedUpload]:
    """Fully resolve every mapping entry before uploading. Fails loud on any
    missing sub-cat, missing service name, or missing video file — so we
    never upload half a batch."""
    resolved: list[ResolvedUpload] = []

    async with AsyncSessionLocal() as db:
        # Cache sub-cats + their services once per slug to avoid N queries.
        sub_cat_cache: dict[str, Category] = {}
        service_cache: dict[tuple[str, str], Service] = {}

        for entry in mapping:
            video_rel = entry["video"]
            sub_slug = entry["sub_category"]
            service_names = entry["services"]

            video_path = (video_dir / video_rel).resolve()
            if not video_path.is_file():
                raise SystemExit(f"error: video not found: {video_path}")

            if sub_slug not in sub_cat_cache:
                sub_cat = (
                    await db.execute(select(Category).where(Category.slug == sub_slug))
                ).scalar_one_or_none()
                if sub_cat is None:
                    raise SystemExit(f"error: sub-category slug '{sub_slug}' not found in DB")
                sub_cat_cache[sub_slug] = sub_cat

            sub_cat = sub_cat_cache[sub_slug]

            for name in service_names:
                key = (sub_slug, name)
                if key not in service_cache:
                    svc = (
                        await db.execute(
                            select(Service).where(
                                Service.category_id == sub_cat.id,
                                Service.name == name,
                            )
                        )
                    ).scalar_one_or_none()
                    if svc is None:
                        raise SystemExit(
                            f"error: service '{name}' not found under sub-category '{sub_slug}'"
                        )
                    service_cache[key] = svc
                svc = service_cache[key]

                resolved.append(
                    ResolvedUpload(
                        video_path=video_path,
                        service_id=str(svc.id),
                        service_name=name,
                        sub_category_slug=sub_slug,
                    )
                )

    return resolved


def _upload_one(s3_client, item: ResolvedUpload) -> None:
    """Upload the video to S3. Public-read + long cache since URL is
    UUID-scoped so any change is a new URL anyway."""
    s3_client.upload_file(
        Filename=str(item.video_path),
        Bucket=S3_BUCKET,
        Key=item.s3_key,
        ExtraArgs={
            "ContentType": "video/mp4",
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )


async def _wire_urls(items: list[ResolvedUpload]) -> None:
    """Set services.banner_video_url in one commit at the end."""
    async with AsyncSessionLocal() as db:
        for item in items:
            svc = await db.get(Service, item.service_id)
            if svc is None:
                # Shouldn't happen — we resolved just now — but be loud.
                raise SystemExit(f"error: service {item.service_id} vanished mid-run")
            svc.banner_video_url = item.public_url
        await db.commit()


async def run(mapping_path: Path, video_dir: Path, dry_run: bool) -> int:
    with open(mapping_path) as f:
        mapping = json.load(f)

    print(f"loaded mapping: {len(mapping)} video entries")

    resolved = await _resolve_mapping(mapping, video_dir)
    unique_videos = {item.video_path for item in resolved}
    unique_services = {item.service_id for item in resolved}

    print(
        f"resolved: {len(resolved)} (video, service) pairs "
        f"from {len(unique_videos)} unique videos to {len(unique_services)} services"
    )

    if dry_run:
        print("\n--- DRY RUN — no S3 upload, no DB write ---\n")
        for item in resolved:
            print(f"  {item.sub_category_slug}/{item.service_name:38s} <- {item.video_path.name}")
        return 0

    s3 = boto3.client("s3", region_name=S3_REGION)

    # Upload sequentially. Parallel would be faster but complicates error
    # reporting; a ~50-item batch takes a couple minutes on a decent link,
    # which is fine for a one-off / occasional run.
    for i, item in enumerate(resolved, 1):
        print(
            f"[{i:>3}/{len(resolved)}] uploading {item.video_path.name} "
            f"-> {item.sub_category_slug}/{item.service_name}"
        )
        try:
            _upload_one(s3, item)
        except (BotoCoreError, ClientError) as exc:
            raise SystemExit(f"error: S3 upload failed for {item.video_path.name}: {exc}") from exc

    print("\nAll uploads succeeded — updating DB URLs...")
    await _wire_urls(resolved)
    print(f"done — {len(resolved)} services now have banner_video_url set")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload service banner videos to S3 + wire the DB URL column"
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        required=True,
        help="Root directory containing videos (paths in --mapping are relative to this)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        required=True,
        help="JSON mapping file (see script docstring for shape)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve + report the plan without uploading or writing to the DB",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.mapping, args.video_dir, args.dry_run)))


if __name__ == "__main__":
    main()
