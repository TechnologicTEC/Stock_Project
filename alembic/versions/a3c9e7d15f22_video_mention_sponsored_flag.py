"""video_mentions: flag mentions from a paid (sponsored) segment

Revision ID: a3c9e7d15f22
Revises: e2a7c5f19b03
Create Date: 2026-10-16 00:00:00.000000

ZipTrader closes most videos with "our sponsored segment on <company>, ticker
symbol <X>", and extraction recorded those stocks as ordinary — usually
bullish — mentions: 40 of 54 videos had one by Oct 2026, VNRX sponsored three
times. Tane, 16 Oct: sponsored segments don't count toward the creator queue.

NOT NULL with a false default, so every existing row reads "not sponsored"
until the backfill marks the ones that are. No RLS change (the table's shared
policy covers new columns).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3c9e7d15f22"
down_revision: Union[str, Sequence[str], None] = "e2a7c5f19b03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("video_mentions", sa.Column("sponsored", sa.Boolean(), nullable=False,
                                              server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("video_mentions", "sponsored")
