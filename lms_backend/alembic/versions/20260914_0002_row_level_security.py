"""Row-Level Security and audit-log hardening

The application connects as an unprivileged role and sets
`app.current_tenant` at the start of every transaction (see
`app.db.session.set_tenant_context`). These policies make that setting
load-bearing: a query without it returns zero rows, and a query with it can
only ever see one tenant's data. A forgotten `WHERE tenant_id = ...` becomes
an empty result instead of a cross-customer data leak.

`FORCE ROW LEVEL SECURITY` is applied so the policies bind even for the
table owner, which is otherwise exempt.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14

"""
from typing import Sequence, Union

from alembic import op

from app.db.base_class import DB_SCHEMA
from app.models import TENANT_SCOPED_TABLES

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_NAME = "tenant_isolation"


def _qualified(table: str) -> str:
    return f'"{DB_SCHEMA}"."{table}"' if DB_SCHEMA else f'"{table}"'


def upgrade() -> None:
    for table in sorted(TENANT_SCOPED_TABLES):
        target = _qualified(table)
        op.execute(f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY")
        # NULLIF + the `true` ("missing_ok") argument to current_setting mean
        # an unset GUC yields NULL, and `tenant_id = NULL` is never true —
        # so no context means no rows, which is the fail-safe direction.
        op.execute(
            f"""
            CREATE POLICY {POLICY_NAME} ON {target}
            USING (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            """
        )

    # The audit ledger must be append-only: a compromised application role
    # should be unable to rewrite history, only add to it.
    audit_table = _qualified("audit_logs")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {DB_SCHEMA or 'public'}.audit_logs_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only (attempted %)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER audit_logs_no_mutation
        BEFORE UPDATE OR DELETE ON {audit_table}
        FOR EACH ROW EXECUTE FUNCTION {DB_SCHEMA or 'public'}.audit_logs_immutable()
        """
    )


def downgrade() -> None:
    audit_table = _qualified("audit_logs")
    op.execute(f"DROP TRIGGER IF EXISTS audit_logs_no_mutation ON {audit_table}")
    op.execute(
        f"DROP FUNCTION IF EXISTS {DB_SCHEMA or 'public'}.audit_logs_immutable()"
    )

    for table in sorted(TENANT_SCOPED_TABLES):
        target = _qualified(table)
        op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {target}")
        op.execute(f"ALTER TABLE {target} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {target} DISABLE ROW LEVEL SECURITY")
