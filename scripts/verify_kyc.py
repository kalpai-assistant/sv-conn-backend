"""
Approve a provider's SUBMITTED KYC by phone number.

Wraps the same ``ProviderService.verify_kyc`` used by the admin API so the
CLI and the endpoint always resolve the same way (kyc_status=VERIFIED,
kyc_reviewed_at=now, kyc_rejection_reason cleared, refuses anything other
than SUBMITTED).

Reads DATABASE_URL from env (usually via ``.env`` for local dev). For prod
the incantation is:

    export AWS_PROFILE=eazyserv AWS_DEFAULT_REGION=ap-south-1
    export DATABASE_URL=$(aws ssm get-parameter \\
        --name /eazyserv/beta/DATABASE_URL --with-decryption \\
        --query 'Parameter.Value' --output text)
    uv run python scripts/verify_kyc.py +919876500002

Add --yes to skip the confirmation prompt (useful for scripting).
"""

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.exceptions import UserNotFound
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.services.provider_service import ProviderService

_PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")


async def verify_kyc(phone: str, assume_yes: bool) -> int:
    if not _PHONE_RE.match(phone):
        print(f"error: {phone!r} is not a valid +91 mobile number", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
        if user is None:
            print(f"error: no user with phone {phone}", file=sys.stderr)
            return 3

        profile = (
            await db.execute(select(ProviderProfile).where(ProviderProfile.user_id == user.id))
        ).scalar_one_or_none()

        print(f"user_id:          {user.id}")
        print(f"phone:            {user.phone}")
        print(f"name:             {user.name!r}")
        print(f"role:             {user.role.value}")
        if profile is None:
            print("kyc_status:       <no provider_profile>")
            print(
                f"error: {phone} has no provider_profile row — did they complete "
                "the provider onboarding + KYC upload?",
                file=sys.stderr,
            )
            return 4

        print(f"profile_id:       {profile.id}")
        print(f"kyc_status:       {profile.kyc_status.value}")
        print(f"kyc_submitted_at: {profile.kyc_submitted_at}")
        print(f"kyc_reviewed_at:  {profile.kyc_reviewed_at}")

        if profile.kyc_status is KYCStatus.VERIFIED:
            print("nothing to do — already VERIFIED")
            return 0
        if profile.kyc_status is not KYCStatus.SUBMITTED:
            print(
                f"error: cannot verify — status is {profile.kyc_status.value}. "
                "Only SUBMITTED profiles can be approved here.",
                file=sys.stderr,
            )
            return 5

        if not assume_yes:
            resp = input("\nApprove this KYC? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("aborted")
                return 1

        try:
            await ProviderService(db).verify_kyc(user.id)
        except UserNotFound:
            print(f"error: profile disappeared mid-run for {phone}", file=sys.stderr)
            return 6

        await db.commit()
        await db.refresh(profile)
        print(
            f"\napproved — kyc_status: {profile.kyc_status.value}, "
            f"kyc_reviewed_at: {profile.kyc_reviewed_at}"
        )
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Approve a provider's SUBMITTED KYC by phone number."
    )
    parser.add_argument("phone", help="Phone in +91XXXXXXXXXX format")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()
    sys.exit(asyncio.run(verify_kyc(args.phone, args.yes)))


if __name__ == "__main__":
    main()
