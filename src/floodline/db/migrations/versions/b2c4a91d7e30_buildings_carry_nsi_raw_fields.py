"""buildings carry NSI's raw fields

Revision ID: b2c4a91d7e30
Revises: e17b0c5fff06
Create Date: 2026-09-07 17:42:11.008314

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c4a91d7e30"
down_revision: str | Sequence[str] | None = "e17b0c5fff06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("buildings", sa.Column("st_damcat", sa.String(length=32), nullable=True))
    op.add_column("buildings", sa.Column("val_vehic", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("ftprntsqft", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("found_type", sa.String(length=32), nullable=True))
    op.add_column("buildings", sa.Column("ground_elv", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("med_yr_blt", sa.String(length=16), nullable=True))
    op.add_column("buildings", sa.Column("pop2amu65", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("pop2amo65", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("pop2pmu65", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("pop2pmo65", sa.Float(), nullable=True))
    # The night and day totals were the sum of the four columns added above. Keeping
    # both would put one quantity in two places, and the derivation that produces them
    # from the API's response is the same one that now produces them from a query.
    op.drop_column("buildings", "pop_night")
    op.drop_column("buildings", "pop_day")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("buildings", sa.Column("pop_day", sa.Float(), nullable=True))
    op.add_column("buildings", sa.Column("pop_night", sa.Float(), nullable=True))
    op.execute("UPDATE buildings SET pop_night = pop2amu65 + pop2amo65")
    op.execute("UPDATE buildings SET pop_day = pop2pmu65 + pop2pmo65")
    for name in (
        "pop2pmo65",
        "pop2pmu65",
        "pop2amo65",
        "pop2amu65",
        "med_yr_blt",
        "ground_elv",
        "found_type",
        "ftprntsqft",
        "val_vehic",
        "st_damcat",
    ):
        op.drop_column("buildings", name)
