"""bot_decisions: record what the broker actually filled

Revision ID: e2a7c5f19b03
Revises: d8f3a91c4b27
Create Date: 2026-10-13 00:00:00.000000

Three nullable columns, written back by each live run from Alpaca's own record
of the order (engine/bot/fill_sync). Until now the journal held only what was
ASKED for — dollars for a buy — so a book rebuilt without broker keys had to
estimate share counts from the next open, and ran up to 0.9% off the account.

Additive and nullable: existing rows are untouched, and the first live run
after deploy fills them in from Alpaca. No RLS change (the table's policy
covers new columns).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e2a7c5f19b03"
down_revision: Union[str, Sequence[str], None] = "d8f3a91c4b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bot_decisions", sa.Column("filled_qty", sa.Float(), nullable=True))
    op.add_column("bot_decisions", sa.Column("filled_avg_price", sa.Float(), nullable=True))
    op.add_column("bot_decisions", sa.Column("filled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("bot_decisions", "filled_at")
    op.drop_column("bot_decisions", "filled_avg_price")
    op.drop_column("bot_decisions", "filled_qty")
