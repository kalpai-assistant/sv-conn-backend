"""merge razorpay payments line with main (SVC-39)

Revision ID: 28c483927f67
Revises: c72f8d3e91a5, a3c5e7f9b1d2, b4d6f8a0c2e3
Create Date: 2026-08-11 13:23:08.623833

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = '28c483927f67'
down_revision: Union[str, None] = ('c72f8d3e91a5', 'a3c5e7f9b1d2', 'b4d6f8a0c2e3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
