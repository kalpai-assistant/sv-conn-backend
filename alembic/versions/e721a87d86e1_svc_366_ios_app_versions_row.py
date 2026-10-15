"""SVC-366: iOS gets its own app_versions row

Until now iPhones were checked against the ``pwa`` row, so bumping the PWA
(or its ``min_supported_version``) would nag or hard-block iPhone users who
have no build to update to. This adds ``ios`` to the ``appplatform`` enum and
seeds an ``ios`` row copied from the ``pwa`` row's ``version`` and
``min_supported_version``, so nothing changes for iPhone users on deploy.
``url`` and ``release_notes`` are NULL: there is no iOS release pipeline yet.

``ALTER TYPE ... ADD VALUE`` runs in an autocommit block because Postgres
won't let a value added in a transaction be used in that same transaction,
and the INSERT below uses it.

Downgrade deletes the ``ios`` row but leaves the ``ios`` enum value in place:
Postgres has no ``ALTER TYPE ... DROP VALUE``. A re-upgrade is safe either way
(``IF NOT EXISTS`` + ``ON CONFLICT DO NOTHING``).

Revision ID: e721a87d86e1
Revises: 1866e2059674
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e721a87d86e1"
down_revision: str | None = "1866e2059674"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE appplatform ADD VALUE IF NOT EXISTS 'ios'")

    op.execute(
        """
        INSERT INTO app_versions
            (id, platform, version, min_supported_version, url, release_notes, released_at)
        SELECT gen_random_uuid(), 'ios', version, min_supported_version, NULL, NULL, now()
        FROM app_versions
        WHERE platform = 'pwa'
        ON CONFLICT (platform) DO NOTHING
        """
    )


def downgrade() -> None:
    # The enum value stays: Postgres can't drop one.
    op.execute("DELETE FROM app_versions WHERE platform = 'ios'")
