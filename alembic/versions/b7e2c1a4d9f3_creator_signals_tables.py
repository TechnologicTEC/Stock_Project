"""creator signals tables (creators, creator_videos, video_mentions)

Revision ID: b7e2c1a4d9f3
Revises: 97c19574a211
Create Date: 2026-07-09 00:00:00.000000

Idempotent: db.session.init_db() creates these tables via create_all on every
startup, so guard each create so `alembic upgrade head` is safe whether the
table already exists (built by create_all) or not (fresh alembic-only build).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e2c1a4d9f3"
down_revision: Union[str, Sequence[str], None] = "97c19574a211"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("creators"):
        op.create_table(
            "creators",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.String(length=32), nullable=False),
            sa.Column("handle", sa.String(length=120), nullable=True),
            sa.Column("display_name", sa.String(length=120), nullable=True),
            sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("added_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("channel_id", name="uq_creators_channel_id"),
        )

    if not _has_table("creator_videos"):
        op.create_table(
            "creator_videos",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("creator_id", sa.Integer(), nullable=False),
            sa.Column("video_id", sa.String(length=20), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("transcript_status", sa.String(length=20), nullable=False),
            sa.Column("transcript", sa.Text(), nullable=True),
            sa.Column("processed_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("video_id", name="uq_creator_videos_video_id"),
        )
        op.create_index("ix_creator_videos_creator_id", "creator_videos", ["creator_id"])

    if not _has_table("video_mentions"):
        op.create_table(
            "video_mentions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("video_id", sa.String(length=20), nullable=False),
            sa.Column("ticker", sa.String(length=10), nullable=False),
            sa.Column("company_name", sa.String(length=255), nullable=True),
            sa.Column("stance", sa.String(length=12), server_default="unknown", nullable=False),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("screener_score", sa.Float(), nullable=True),
            sa.Column("recommendation", sa.String(length=20), nullable=True),
            sa.Column("screened_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_video_mentions_video_id", "video_mentions", ["video_id"])
        op.create_index("ix_video_mentions_ticker", "video_mentions", ["ticker"])


def downgrade() -> None:
    op.drop_table("video_mentions")
    op.drop_table("creator_videos")
    op.drop_table("creators")
