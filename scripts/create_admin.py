"""
Give someone an admin console login (SVC-422).

    # The existing Super Admin, on its own users row:
    uv run python scripts/create_admin.py --email vipul@kalpai.in --phone +919999900001
    # Someone new, with no phone needed:
    uv run python scripts/create_admin.py --email someone@kalpai.in --role ops

The password comes from ADMIN_USER_PASSWORD or a prompt, never an argument, so it
stays out of shell history and process lists. TOTP is set up at first login.
"""

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.models  # noqa: F401, E402 - registers every mapper the FKs need
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.user import AdminRole  # noqa: E402
from app.services.admin_auth_service import provision_admin  # noqa: E402


def read_password() -> str:
    password = os.environ.get("ADMIN_USER_PASSWORD")
    if password:
        return password
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return password


async def main(email: str, phone: str | None, role: AdminRole, password: str) -> None:
    async with AsyncSessionLocal() as db:
        try:
            admin = await provision_admin(db, email, role, password, phone=phone)
        except ValueError as exc:
            sys.exit(str(exc))
        await db.commit()
        print(f"{admin.email} is now {role.value}. TOTP is set up at first login.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Give someone an admin console login.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--phone", help="An existing admin's number, or a new one to record")
    parser.add_argument(
        "--role", choices=[r.value for r in AdminRole], default=AdminRole.ADMIN.value
    )
    args = parser.parse_args()
    asyncio.run(main(args.email, args.phone, AdminRole(args.role), read_password()))
