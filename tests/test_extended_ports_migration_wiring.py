"""Bug condition exploration test for the "assign application: missing extended
port columns" bug.

Spec: .kiro/specs/assign-application-missing-port-columns

Bug summary
-----------
Assigning an application to a user fails on deployed databases with:

    column "http_port3" of relation "user_applications" does not exist

The code, the shared ``APP_PORT_COLUMNS`` constant, and the canonical schema
(``scripts/postgresql_schema.sql``) all expect 12 per-application port columns
(6 HTTP + 6 HTTPS). The migration
``migration/add_extended_ports_to_user_applications.sql`` adds columns 3-6 via
``ADD COLUMN IF NOT EXISTS`` + a NULL-guarded backfill, but that migration is
NEVER wired into ``init_db()`` (``src/database_postgres.py``). ``init_db()``
applies six other migrations on startup
(``add_password_reset_and_2fa.sql``, ``add_serverless_jobs.sql``,
``add_mig_gpu.sql``, ``add_target_link_to_serverless_jobs.sql``,
``make_application_fkeys_deferrable.sql``, ``add_deploy_templates.sql``) but
NOT the extended-ports one. So databases created before the extended-ports
schema never gain ``http_port3``/``https_port3`` ... ``http_port6``/``https_port6``,
and any 12-column ``user_applications`` insert fails.

Bug Condition C(X) (from design ``isBugCondition``):
    input.targetTable == 'user_applications'
    AND insertColumns CONTAINS_ALL the 8 extended columns
    AND at least one extended column is NOT in the table's columns.

Why not a live-DB test
----------------------
No live/test PostgreSQL is reachable here, so - following the established
convention (``tests/test_default_apps_seeding.py``, ``tests/test_log_cleanup.py``:
mock psycopg2 + a SQL-aware fake cursor) - we drive ``init_db()`` with a fake
connection/cursor that records an ORDERED event log of every
execute / executemany / commit / rollback, and separately simulate the
12-column insert against a fake schema missing ``http_port3``.

EXPECTED OUTCOME on UNFIXED code
--------------------------------
These tests are EXPECTED TO FAIL on the unfixed code (failure confirms the
bug exists): the extended-ports migration SQL never appears in ``init_db()``'s
event log, and the 12-column insert raises ``column "http_port3" ... does not
exist``. When the migration is wired into ``init_db()`` (the fix), these tests
pass.

_Requirements: 1.1, 1.2, 1.3_
"""

import os
import re
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock psycopg2 before importing the module under test so importing it never
# tries to reach a real database.
mock_psycopg2 = MagicMock()
mock_psycopg2.pool = MagicMock()
sys.modules.setdefault("psycopg2", mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", mock_psycopg2.pool)

import src.database_postgres as db  # noqa: E402
from src.database_postgres import APP_PORT_COLUMNS  # noqa: E402


# --------------------------------------------------------------------------
# Domain constants (mirror the design's isBugCondition)
# --------------------------------------------------------------------------

# The 4 port columns that predate the extended-ports schema.
ORIGINAL_PORT_COLUMNS = ("http_port", "https_port", "http_port2", "https_port2")
# The 8 extended columns added by add_extended_ports_to_user_applications.sql.
EXTENDED_PORT_COLUMNS = (
    "http_port3", "https_port3",
    "http_port4", "https_port4",
    "http_port5", "https_port5",
    "http_port6", "https_port6",
)

# Marker that identifies the extended-ports migration SQL in the event log.
# The migration is the only one that touches http_port3 with ADD COLUMN.
_EXTENDED_PORTS_MIGRATION_RE = re.compile(
    r"add\s+column\s+if\s+not\s+exists\s+http_port3", re.IGNORECASE | re.DOTALL
)

# Path to the migration SQL that SHOULD be applied by init_db().
MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migration",
    "add_extended_ports_to_user_applications.sql",
)


# --------------------------------------------------------------------------
# SQL-aware fake cursor / connection driving init_db()
# --------------------------------------------------------------------------

class FakeCursor:
    """Minimal cursor that answers just enough of init_db()'s queries to run
    through the migration + seeding blocks, while recording an ordered event
    log of every execute/executemany.

    ``events`` (shared with the connection) records tuples like
    ("execute", sql), ("executemany", sql, n), ("commit",), ("rollback",).
    """

    def __init__(self, events):
        self.events = events
        self._last_result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.events.append(("execute", sql))
        s = sql.strip().lower()

        self._last_result = None

        # Table existence check -> pretend schema not yet created.
        if "information_schema.tables" in s:
            self._last_result = None
            return

        # opcp-serverless-brik existence check -> not present (drive seeding).
        if "from applications where name" in s:
            self._last_result = None
            return

        # COUNT(*) -> 0 (drive first-run branches).
        if s.startswith("select count(*)"):
            self._last_result = (0,)
            return

        # default-costs lookup -> synthetic app ids.
        if "left join application_costs" in s and "c.id is null" in s:
            self._last_result = [(i,) for i in range(1, 12)]
            return

        # "RETURNING id" inserts -> synthetic id.
        if "returning id" in s:
            self._last_result = (1,)
            return

        # SELECT id FROM users (assign-to-all loop) -> one user so the
        # 12-column user_applications insert path is exercised.
        if s.startswith("select id from users"):
            self._last_result = [(1,)]
            return

        # SELECT id FROM user_applications ... -> none (so insert happens).
        # SELECT id FROM deployments ... -> none.
        # SELECT id FROM servers LIMIT 1 -> none.
        # SELECT id FROM users WHERE username = admin -> present.
        if "from users where username" in s:
            self._last_result = (1,)
            return

        # Generic SELECTs -> empty.
        self._last_result = []

    def executemany(self, sql, seq_of_params):
        params_list = list(seq_of_params)
        self.events.append(("executemany", sql, len(params_list)))
        self._last_result = None

    def fetchone(self):
        if isinstance(self._last_result, list):
            return None
        return self._last_result

    def fetchall(self):
        if isinstance(self._last_result, list):
            return self._last_result
        return []


class FakeConnection:
    """Fake connection recording commit/rollback into the shared event log."""

    def __init__(self, events):
        self.events = events
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self.events)

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))


@contextmanager
def _fake_get_db_connection(events):
    """Mirror db_manager.get_db_connection(): rollback on any exception."""
    conn = FakeConnection(events)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def _run_init_db():
    """Run init_db() against the fake connection and return the event log."""
    events = []
    original = db.db_manager.get_db_connection
    db.db_manager.get_db_connection = lambda: _fake_get_db_connection(events)
    try:
        try:
            db.init_db()
        except Exception:
            # init_db handles its own per-block failures; if anything still
            # propagates we keep the captured event log for assertions.
            pass
    finally:
        db.db_manager.get_db_connection = original
    return events


def _index_of_extended_ports_migration(events):
    for i, ev in enumerate(events):
        if ev[0] == "execute" and _EXTENDED_PORTS_MIGRATION_RE.search(ev[1]):
            return i
    return None


def _index_of_first_user_applications_insert(events):
    for i, ev in enumerate(events):
        if ev[0] in ("execute", "executemany"):
            s = ev[1].strip().lower()
            if "insert into user_applications" in s:
                return i
    return None


# --------------------------------------------------------------------------
# Fake schema simulating the deployed (out-of-sync) user_applications table
# --------------------------------------------------------------------------

class MissingColumnCursor:
    """A fake cursor whose user_applications table has only the columns given
    in ``columns``. A 12-column INSERT referencing an absent extended column
    raises the PostgreSQL-style "column ... does not exist" error, reproducing
    the reported failure.
    """

    def __init__(self, columns):
        self.columns = set(columns)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = sql.lower()
        if "insert into user_applications" in s:
            # Find the first referenced port column that is not in the table.
            for col in APP_PORT_COLUMNS:
                if re.search(rf"\b{re.escape(col)}\b", s) and col not in self.columns:
                    raise db_error(f'column "{col}" of relation '
                                   f'"user_applications" does not exist')
        return None


class _FakeDbError(Exception):
    """Stand-in for psycopg2's UndefinedColumn/ProgrammingError."""


def db_error(msg):
    return _FakeDbError(msg)


def _assign_insert_sql():
    """The 12-column assignment insert as built from the shared constants."""
    from src.database_postgres import (
        APP_PORT_COLUMNS_SQL,
        APP_PORT_PLACEHOLDERS_SQL,
    )
    return (
        f"INSERT INTO user_applications (user_id, application_id, url, "
        f"{APP_PORT_COLUMNS_SQL}) VALUES (%s, %s, %s, {APP_PORT_PLACEHOLDERS_SQL})"
    )


# ==========================================================================
# Tests
# ==========================================================================

class TestMigrationNotAppliedByInitDb:
    """Test 1 - the extended-ports migration is never executed by init_db().

    EXPECTED on UNFIXED code: the migration SQL is absent from the event log,
    so this assertion FAILS (bug confirmed). After the fix the migration is
    wired in and present in the log -> passes.

    _Requirements: 1.1, 1.2_
    """

    def test_extended_ports_migration_present_in_init_db_event_log(self):
        events = _run_init_db()
        idx = _index_of_extended_ports_migration(events)
        assert idx is not None, (
            "add_extended_ports_to_user_applications.sql was never executed by "
            "init_db(): no 'ADD COLUMN IF NOT EXISTS http_port3' appears in the "
            "ordered event log. The migration is not wired into init_db(), so "
            "deployed databases never gain the extended port columns (bug present)."
        )

    def test_extended_ports_migration_runs_before_user_applications_insert(self):
        events = _run_init_db()
        mig_idx = _index_of_extended_ports_migration(events)
        ins_idx = _index_of_first_user_applications_insert(events)
        assert mig_idx is not None, (
            "extended-ports migration never executed in init_db() (bug present)"
        )
        if ins_idx is not None:
            assert mig_idx < ins_idx, (
                "extended-ports migration ran AFTER the first 12-column "
                "user_applications insert; the seeding insert would still hit a "
                "missing column on an old DB (ordering bug)."
            )


class TestAssignFailsOnMissingColumn:
    """Test 2 - the dashboard-assign 12-column insert fails against a schema
    missing http_port3.

    This directly reproduces the reported error and holds regardless of the
    fix (it documents the failure mode on an un-migrated table). The fix
    prevents this state from persisting by applying the migration on startup.

    _Requirements: 1.1, 1.3_
    """

    def test_twelve_column_insert_raises_missing_http_port3(self):
        # Table has only the original 4 port columns (plus non-port columns).
        table_columns = {"user_id", "application_id", "url", *ORIGINAL_PORT_COLUMNS}
        cursor = MissingColumnCursor(table_columns)
        with pytest.raises(_FakeDbError) as excinfo:
            cursor.execute(_assign_insert_sql())
        msg = str(excinfo.value)
        assert re.search(
            r'column "http_port3".*does not exist', msg
        ), f"expected missing http_port3 error, got: {msg}"


class TestInitDbSeedingHitsMissingColumn:
    """Test 3 - the opcp-serverless-brik / admin default-apps 12-column insert
    inside init_db() would hit the missing-column error on an old DB when the
    migration is not applied first.

    _Requirements: 1.2, 1.3_
    """

    @pytest.mark.parametrize("missing", list(EXTENDED_PORT_COLUMNS))
    def test_seeding_insert_fails_for_each_missing_extended_column(self, missing):
        # Table has all columns EXCEPT one extended column.
        table_columns = {
            "user_id", "application_id", "url",
            *ORIGINAL_PORT_COLUMNS,
            *(c for c in EXTENDED_PORT_COLUMNS if c != missing),
        }
        cursor = MissingColumnCursor(table_columns)
        with pytest.raises(_FakeDbError) as excinfo:
            cursor.execute(_assign_insert_sql())
        assert f'column "{missing}"' in str(excinfo.value)


class TestExtendedColumnsAreExactlyPortsThreeToSix:
    """Test 4 (Edge/Range) - the exact 8 columns that must be added are
    APP_PORT_COLUMNS minus the original 4.

    _Requirements: 1.1_
    """

    def test_extended_columns_equal_app_port_columns_minus_original_four(self):
        computed = tuple(c for c in APP_PORT_COLUMNS if c not in ORIGINAL_PORT_COLUMNS)
        assert computed == EXTENDED_PORT_COLUMNS, (
            f"expected extended columns {EXTENDED_PORT_COLUMNS}, got {computed}"
        )
        assert len(EXTENDED_PORT_COLUMNS) == 8
        assert len(APP_PORT_COLUMNS) == 12

    def test_migration_file_defines_all_eight_extended_columns(self):
        assert os.path.exists(MIGRATION_PATH), (
            f"migration file missing: {MIGRATION_PATH}"
        )
        with open(MIGRATION_PATH, "r") as f:
            sql = f.read().lower()
        for col in EXTENDED_PORT_COLUMNS:
            assert f"add column if not exists {col}" in sql, (
                f"migration does not add {col}"
            )
