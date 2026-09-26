"""Ordering, idempotency, integration and regression tests for the
"assign application: missing extended port columns" bugfix.

Spec: .kiro/specs/assign-application-missing-port-columns  (Task 4)

The fix (Task 3) wired ``migration/add_extended_ports_to_user_applications.sql``
into ``init_db()`` (``src/database_postgres.py``), placed immediately after the
``add_deploy_templates.sql`` block and BEFORE the ``opcp-serverless-brik``
seeding / admin default-apps assignment blocks (both of which insert into
``user_applications`` with the full 12-column port list). The migration block
mirrors the other six: ``if os.path.exists(...)`` guard, read SQL,
``cursor.execute`` + ``conn.commit()``, wrapped in ``try/except`` that calls
``conn.rollback()`` and logs ``[INFO] Extended ports migration check: {e}``.

This module verifies, on the FIXED code:

  * Ordering unit test  - the extended-ports migration SQL executes in the
    ``init_db()`` event log BEFORE the first 12-column ``user_applications``
    insert (serverless-brik / admin seeding).
  * Try/except unit test - the migration block is fault-isolated: a failing
    ``execute`` on the migration SQL triggers ``rollback()`` and does NOT
    propagate; ``init_db()`` continues to the later seeding blocks.
  * Property-based test  - for random ``user_applications`` schemas (varying
    which of the 8 extended columns are missing) after applying the migration
    all 12 columns are present and a 12-column insert succeeds.
  * Integration/ordering  - against a fake OLD DB missing columns 3-6, the
    migration runs first so the serverless-brik / admin seeding 12-column
    inserts complete without missing-column errors.
  * Regression  - against an already-migrated fake DB, assign,
    duplicate-assign 409, nginx update, and URL generation are unchanged.

No live PostgreSQL is reachable here, so - following the established
convention (``tests/test_extended_ports_migration_wiring.py``,
``tests/test_extended_ports_preservation.py``, ``tests/test_default_apps_seeding.py``) -
``init_db()`` is driven with a SQL-aware fake connection/cursor recording an
ordered event log, and the assign flow is driven through the real Flask route.

_Requirements: 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5_
"""

import os
import re
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock psycopg2 before importing the module under test so importing it never
# tries to reach a real database.
_mock_psycopg2 = MagicMock()
_mock_psycopg2.pool = MagicMock()
sys.modules.setdefault("psycopg2", _mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", _mock_psycopg2.pool)

import src.database_postgres as db  # noqa: E402
from src.database_postgres import (  # noqa: E402
    APP_PORT_COLUMNS,
    calculate_app_ports,
)


# --------------------------------------------------------------------------
# Domain constants (mirror the design's isBugCondition)
# --------------------------------------------------------------------------

ORIGINAL_PORT_COLUMNS = ("http_port", "https_port", "http_port2", "https_port2")
EXTENDED_PORT_COLUMNS = (
    "http_port3", "https_port3",
    "http_port4", "https_port4",
    "http_port5", "https_port5",
    "http_port6", "https_port6",
)

# Marker that identifies the extended-ports migration SQL in the event log:
# the migration is the only statement that ADDs http_port3.
_EXTENDED_PORTS_MIGRATION_RE = re.compile(
    r"add\s+column\s+if\s+not\s+exists\s+http_port3", re.IGNORECASE | re.DOTALL
)

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migration",
    "add_extended_ports_to_user_applications.sql",
)


def _read_migration_sql():
    with open(MIGRATION_PATH, "r") as f:
        return f.read()


# --------------------------------------------------------------------------
# SQL-aware fake cursor / connection driving init_db()
# --------------------------------------------------------------------------

class FakeCursor:
    """Minimal cursor that answers just enough of init_db()'s queries to run
    through the migration + seeding blocks while recording an ordered event
    log of every execute / executemany.

    ``fail_migration_sql`` (optional): when the extended-ports migration SQL is
    executed, raise the supplied exception. Used to prove the migration block's
    try/except fault isolation.
    """

    def __init__(self, events, fail_migration_sql=None):
        self.events = events
        self.fail_migration_sql = fail_migration_sql
        self._last_result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.events.append(("execute", sql))
        s = sql.strip().lower()

        # Inject a failure specifically on the extended-ports migration SQL.
        if self.fail_migration_sql and _EXTENDED_PORTS_MIGRATION_RE.search(sql):
            raise self.fail_migration_sql

        self._last_result = None

        if "information_schema.tables" in s:
            self._last_result = None
            return
        if "from applications where name" in s:
            self._last_result = None
            return
        if s.startswith("select count(*)"):
            self._last_result = (0,)
            return
        if "left join application_costs" in s and "c.id is null" in s:
            self._last_result = [(i,) for i in range(1, 12)]
            return
        if "returning id" in s:
            self._last_result = (1,)
            return
        if s.startswith("select id from users"):
            self._last_result = [(1,)]
            return
        if "from users where username" in s:
            self._last_result = (1,)
            return
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

    def __init__(self, events, fail_migration_sql=None):
        self.events = events
        self.fail_migration_sql = fail_migration_sql
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self.events, self.fail_migration_sql)

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))


@contextmanager
def _fake_get_db_connection(events, fail_migration_sql=None):
    conn = FakeConnection(events, fail_migration_sql)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def _run_init_db(fail_migration_sql=None):
    """Run init_db() against the fake connection and return the event log."""
    events = []
    original = db.db_manager.get_db_connection
    db.db_manager.get_db_connection = (
        lambda: _fake_get_db_connection(events, fail_migration_sql)
    )
    try:
        try:
            db.init_db()
        except Exception:
            # init_db handles its own per-block failures; keep the log.
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


# ==========================================================================
# Ordering unit test
# ==========================================================================

class TestMigrationOrdering:
    """The extended-ports migration executes BEFORE the first 12-column
    user_applications insert (serverless-brik / admin seeding).

    _Requirements: 2.2, 2.3_
    """

    def test_migration_executed_by_init_db(self):
        events = _run_init_db()
        assert _index_of_extended_ports_migration(events) is not None, (
            "extended-ports migration SQL never executed by init_db() - the "
            "fix should wire it in via cursor.execute(migration_sql)."
        )

    def test_migration_runs_before_first_user_applications_insert(self):
        events = _run_init_db()
        mig_idx = _index_of_extended_ports_migration(events)
        ins_idx = _index_of_first_user_applications_insert(events)

        assert mig_idx is not None, "extended-ports migration never executed"
        assert ins_idx is not None, (
            "no user_applications 12-column insert was reached in init_db(); "
            "the ordering test needs the seeding insert to be exercised."
        )
        assert mig_idx < ins_idx, (
            f"extended-ports migration (event #{mig_idx}) must run BEFORE the "
            f"first user_applications insert (event #{ins_idx}); otherwise the "
            f"seeding insert hits a missing extended column on an old DB."
        )

    def test_migration_commit_precedes_seeding_insert(self):
        # The migration block commits before the seeding block inserts, so the
        # column additions are durable before any 12-column insert runs.
        events = _run_init_db()
        mig_idx = _index_of_extended_ports_migration(events)
        ins_idx = _index_of_first_user_applications_insert(events)
        assert mig_idx is not None and ins_idx is not None
        # There is a commit between the migration execute and the first insert.
        commit_between = any(
            ev[0] == "commit" for ev in events[mig_idx + 1:ins_idx]
        )
        assert commit_between, (
            "expected a commit between the extended-ports migration and the "
            "first user_applications insert (migration block commits its work)."
        )


# ==========================================================================
# Try/except fault-isolation unit test
# ==========================================================================

class TestMigrationFaultIsolation:
    """The migration block is wrapped in try/except with conn.rollback() and
    does not propagate errors - init_db() continues to the seeding blocks even
    when the migration execute fails.

    _Requirements: 2.3_
    """

    def test_failing_migration_does_not_propagate_and_init_db_continues(self):
        boom = RuntimeError("simulated migration failure")
        events = _run_init_db(fail_migration_sql=boom)

        # The migration SQL was attempted (execute recorded before the raise).
        assert _index_of_extended_ports_migration(events) is not None, (
            "migration execute was never attempted"
        )

        # init_db() did not crash: seeding continued past the failed migration,
        # so a later user_applications insert is still reached.
        ins_idx = _index_of_first_user_applications_insert(events)
        assert ins_idx is not None, (
            "init_db() did not continue after the migration failure; the "
            "seeding insert was never reached (error propagated)."
        )

    def test_failing_migration_triggers_rollback_after_the_migration_execute(self):
        boom = RuntimeError("simulated migration failure")
        events = _run_init_db(fail_migration_sql=boom)

        mig_idx = _index_of_extended_ports_migration(events)
        assert mig_idx is not None
        # A rollback must occur after the failed migration execute (the block's
        # except: conn.rollback()). It must appear before the seeding insert.
        ins_idx = _index_of_first_user_applications_insert(events)
        assert ins_idx is not None
        rollback_after_mig = any(
            ev[0] == "rollback" for ev in events[mig_idx + 1:ins_idx + 1]
        )
        assert rollback_after_mig, (
            "expected conn.rollback() after the failing extended-ports "
            "migration execute (fault isolation, consistent with the other "
            "six migration blocks)."
        )


# ==========================================================================
# SQL-aware fake user_applications table (interprets the migration SQL)
# ==========================================================================

_BACKFILL_OFFSETS = {
    "http_port3": 4, "https_port3": 5,
    "http_port4": 6, "https_port4": 7,
    "http_port5": 8, "https_port5": 9,
    "http_port6": 10, "https_port6": 11,
}


class FakeSchemaTable:
    """In-memory model of ``user_applications`` that interprets the migration
    (``ADD COLUMN IF NOT EXISTS`` x8 + NULL-guarded backfill) and rejects a
    12-column INSERT that references a column the table does not have, mimicking
    PostgreSQL's ``column "..." does not exist`` error.
    """

    class ColumnError(Exception):
        pass

    def __init__(self, columns, rows=None):
        self.columns = set(columns)
        self.rows = [dict(r) for r in (rows or [])]

    def run_migration(self, sql):
        alter = re.search(r"alter\s+table.*?;", sql, re.IGNORECASE | re.DOTALL)
        update = re.search(
            r"update\s+user_applications.*?;", sql, re.IGNORECASE | re.DOTALL
        )
        if alter:
            for col in re.findall(
                r"add\s+column\s+if\s+not\s+exists\s+(\w+)",
                alter.group(0), re.IGNORECASE,
            ):
                if col not in self.columns:
                    self.columns.add(col)
                    for row in self.rows:
                        row.setdefault(col, None)
        if update:
            for row in self.rows:
                if row.get("http_port") is not None and row.get("http_port3") is None:
                    base = row["http_port"]
                    for col, offset in _BACKFILL_OFFSETS.items():
                        row[col] = base + offset

    def insert_twelve_columns(self, values):
        """Simulate the assign 12-column insert; raise if any referenced port
        column is absent (the reported bug)."""
        for col in APP_PORT_COLUMNS:
            if col not in self.columns:
                raise self.ColumnError(
                    f'column "{col}" of relation "user_applications" '
                    f"does not exist"
                )
        row = {
            "user_id": values[0],
            "application_id": values[1],
            "url": values[2],
        }
        for col, val in zip(APP_PORT_COLUMNS, values[3:]):
            row[col] = val
        self.rows.append(row)
        return row


# ==========================================================================
# Property-based test: random missing-column schemas end up complete
# ==========================================================================

# Draw a non-empty subset of the 8 extended columns to be MISSING (bug cond.).
_missing_subset = st.lists(
    st.sampled_from(EXTENDED_PORT_COLUMNS),
    min_size=1, max_size=len(EXTENDED_PORT_COLUMNS), unique=True,
).map(tuple)


class TestMigrationCompletesSchemaProperty:
    """For any user_applications schema missing one or more of the 8 extended
    columns, applying the migration yields all 12 columns and a subsequent
    12-column insert succeeds.

    Validates: Requirements 2.2, 2.4

    _Requirements: 2.2, 2.4_
    """

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(missing=_missing_subset)
    def test_after_migration_all_twelve_columns_present_and_insert_succeeds(
        self, missing
    ):
        sql = _read_migration_sql()
        # Start from a table that has the base + original 4 ports + the extended
        # columns EXCEPT the randomly chosen missing ones (bug condition holds).
        present = {
            "id", "user_id", "application_id", "url",
            *ORIGINAL_PORT_COLUMNS,
            *(c for c in EXTENDED_PORT_COLUMNS if c not in missing),
        }
        table = FakeSchemaTable(present)

        # Pre-condition: at least one extended column is missing (bug present),
        # and the 12-column insert would fail before the migration.
        assert any(c not in table.columns for c in EXTENDED_PORT_COLUMNS)
        ports = calculate_app_ports(1, 1)
        with pytest.raises(FakeSchemaTable.ColumnError):
            table.insert_twelve_columns((1, 1, "https://x", *ports))

        # Apply the migration (as init_db() does on startup).
        table.run_migration(sql)

        # All 12 port columns are now present ...
        for col in APP_PORT_COLUMNS:
            assert col in table.columns, f"{col} missing after migration"
        # ... and the 12-column insert now succeeds.
        row = table.insert_twelve_columns((1, 1, "https://x", *ports))
        for col, val in zip(APP_PORT_COLUMNS, ports):
            assert row[col] == val

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(missing=_missing_subset)
    def test_migration_only_adds_the_missing_columns(self, missing):
        sql = _read_migration_sql()
        present = {
            "id", "user_id", "application_id", "url",
            *ORIGINAL_PORT_COLUMNS,
            *(c for c in EXTENDED_PORT_COLUMNS if c not in missing),
        }
        before = set(present)
        table = FakeSchemaTable(present)
        table.run_migration(sql)
        added = table.columns - before
        # Exactly the missing extended columns were added; nothing else.
        assert added == set(missing)


# ==========================================================================
# Integration / ordering test: old DB missing columns 3-6 seeds cleanly
# ==========================================================================

class TestOldDbSeedingCompletesAfterMigration:
    """An old DB missing columns 3-6: init_db() applies the migration first, so
    the serverless-brik / admin seeding 12-column inserts complete without
    missing-column errors.

    This models the ordering end-to-end: the migration mutates the shared fake
    schema, then the seeding insert is validated against that (now complete)
    schema.

    _Requirements: 2.2, 2.3_
    """

    def test_seeding_insert_succeeds_after_migration_on_old_db(self):
        sql = _read_migration_sql()
        # Old DB: only the original 4 port columns exist (ports 3-6 missing).
        old_db = FakeSchemaTable(
            {"id", "user_id", "application_id", "url", *ORIGINAL_PORT_COLUMNS}
        )

        # Before migration the seeding insert would fail.
        ports = calculate_app_ports(1, 2)
        with pytest.raises(FakeSchemaTable.ColumnError):
            old_db.insert_twelve_columns((1, 2, "https://x", *ports))

        # init_db() ordering: migration block runs BEFORE the seeding insert.
        events = _run_init_db()
        mig_idx = _index_of_extended_ports_migration(events)
        ins_idx = _index_of_first_user_applications_insert(events)
        assert mig_idx is not None and ins_idx is not None
        assert mig_idx < ins_idx

        # Apply the migration to the old DB (as the ordered init_db() does),
        # then the serverless-brik / admin seeding insert completes cleanly.
        old_db.run_migration(sql)
        row = old_db.insert_twelve_columns((1, 2, "https://x", *ports))
        assert all(col in old_db.columns for col in APP_PORT_COLUMNS)
        assert row["user_id"] == 1 and row["application_id"] == 2

    def test_no_seeding_insert_precedes_the_migration(self):
        # Stronger ordering guarantee: there is no user_applications insert at
        # all before the migration executes in the init_db() event log.
        events = _run_init_db()
        mig_idx = _index_of_extended_ports_migration(events)
        assert mig_idx is not None
        for ev in events[:mig_idx]:
            if ev[0] in ("execute", "executemany"):
                assert "insert into user_applications" not in ev[1].lower(), (
                    "a user_applications insert ran BEFORE the extended-ports "
                    "migration; ordering fix violated."
                )


# ==========================================================================
# Regression test: already-migrated DB - assign / 409 / nginx / URL unchanged
# ==========================================================================

class FakeAssignDB:
    """Minimal db_manager stand-in for the POST assign branch of
    ``api_user_applications`` (mirrors the preservation test's helper).
    """

    def __init__(self, admin_username="admin", app_name="opcp-brik",
                 duplicate=False):
        self.admin_username = admin_username
        self.app_name = app_name
        self.duplicate = duplicate
        self.insert_calls = []

    def execute_query(self, query, params=None, fetch_one=False, fetch_all=False):
        q = " ".join(query.split()).lower()
        if q.startswith("select username from users"):
            return (self.admin_username,) if self.admin_username else None
        if q.startswith("select name from applications where id"):
            return (self.app_name,) if self.app_name else None
        if "insert into user_applications" in q:
            self.insert_calls.append((query, params))
            if self.duplicate:
                raise Exception(
                    "duplicate key value violates unique constraint "
                    '"user_applications_user_id_application_id_key"'
                )
            return None
        return None


@contextmanager
def _assign_client(fake_db, capture_nginx):
    from flask import Flask
    from src.routes import api_routes

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(api_routes.api_bp, url_prefix="/api")

    with patch.object(api_routes, "db_manager", fake_db), \
         patch.object(api_routes, "insert_location_block", capture_nginx), \
         patch.object(api_routes, "DOMAIN", "opcp-psmc.com"):
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1  # admin id
            yield client


class TestAlreadyMigratedRegression:
    """Against an already-migrated DB the assign flow is unchanged: successful
    assign stores 12 ports + updates nginx + generates the URL, and a duplicate
    assign returns 409.

    _Requirements: 3.1, 3.2, 3.3, 3.4_
    """

    def test_successful_assign_unchanged(self):
        user_id, app_id = 1, 7
        fake_db = FakeAssignDB(app_name="opcp-brik", duplicate=False)
        nginx_calls = []
        with _assign_client(
            fake_db, lambda *a, **k: nginx_calls.append(a)
        ) as client:
            resp = client.post(
                f"/api/users/{user_id}/applications",
                json={"application_id": app_id},
            )

        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Application assigned successfully"

        assert len(fake_db.insert_calls) == 1
        query, params = fake_db.insert_calls[0]
        assert params[0] == user_id
        assert params[1] == app_id
        stored_ports = tuple(params[3:])
        expected_ports = calculate_app_ports(user_id, app_id)
        assert stored_ports == expected_ports
        assert len(stored_ports) == len(APP_PORT_COLUMNS) == 12
        assert ", ".join(APP_PORT_COLUMNS) in query

        # nginx update + URL generation unchanged.
        expected_url = f"https://opcp-psmc.com:{expected_ports[1]}"
        assert params[2] == expected_url
        assert len(nginx_calls) == 1
        assert nginx_calls[0][1] == "opcp-brik"
        assert nginx_calls[0][2] == expected_url
        assert nginx_calls[0][3] == expected_url

    def test_duplicate_assign_returns_409(self):
        fake_db = FakeAssignDB(duplicate=True)
        nginx_calls = []
        with _assign_client(
            fake_db, lambda *a, **k: nginx_calls.append(a)
        ) as client:
            resp = client.post(
                "/api/users/1/applications",
                json={"application_id": 7},
            )
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "Application already assigned"
        # No nginx update on the duplicate (failed) path.
        assert nginx_calls == []

    def test_migration_rerun_on_migrated_db_is_noop_and_insert_still_works(self):
        # An already-migrated DB: re-running the migration adds no columns and
        # a 12-column insert still succeeds (idempotency + regression).
        sql = _read_migration_sql()
        migrated = FakeSchemaTable({
            "id", "user_id", "application_id", "url",
            *ORIGINAL_PORT_COLUMNS, *EXTENDED_PORT_COLUMNS,
        })
        before = set(migrated.columns)
        migrated.run_migration(sql)
        assert migrated.columns == before  # no columns added

        ports = calculate_app_ports(3, 9)
        row = migrated.insert_twelve_columns((3, 9, "https://x", *ports))
        for col, val in zip(APP_PORT_COLUMNS, ports):
            assert row[col] == val
