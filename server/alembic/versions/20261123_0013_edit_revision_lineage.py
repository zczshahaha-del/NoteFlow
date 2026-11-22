"""Track the exact base revision for edit preview adjustments."""
import sqlalchemy as sa
from alembic import op

revision = "20261123_0013"
down_revision = "20261031_0012"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # A fresh installation's baseline uses the current model definitions.
    for table, column in (("note_edit_previews", "current_revision_id"),
                          ("note_edit_preview_revisions", "previous_revision_id")):
        if not any(item["name"] == column for item in inspector.get_columns(table)):
            op.add_column(table, sa.Column(column, sa.String(64), nullable=True))


def downgrade():
    op.drop_column("note_edit_preview_revisions", "previous_revision_id")
    op.drop_column("note_edit_previews", "current_revision_id")
