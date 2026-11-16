"""Explicit platform identities and exact scoped grants; no users or grants seeded."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_user_status"),
    )
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("provider", "namespace", "external_id", name="uq_external_identity"),
    )
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.UniqueConstraint("name", name="uq_access_role_name"),
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.UniqueConstraint("name", name="uq_access_permission_name"),
        sa.CheckConstraint("name <> '*'", name="ck_no_wildcard_permission"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id"), primary_key=True),
        sa.Column(
            "permission_id", sa.String(36), sa.ForeignKey("permissions.id"), primary_key=True
        ),
    )
    op.create_table(
        "role_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", name="uq_role_binding"),
        sa.CheckConstraint("scope_type IN ('global','node','project')", name="ck_role_scope_type"),
        sa.CheckConstraint(
            "(scope_type='global' AND scope_id='*') OR "
            "(scope_type<>'global' AND scope_id<>'' AND scope_id<>'*')",
            name="ck_role_scope_id",
        ),
    )
    op.create_index("ix_role_bindings_user_id", "role_bindings", ["user_id"])
    op.create_table(
        "node_user_mappings",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("os_username", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.CheckConstraint("confidence IN ('verified','unverified')", name="ck_mapping_confidence"),
    )
    op.create_index("ix_node_user_mappings_user_id", "node_user_mappings", ["user_id"])


def downgrade() -> None:
    op.drop_table("node_user_mappings")
    op.drop_table("role_bindings")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("external_identities")
    op.drop_table("users")
