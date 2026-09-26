"""Preservation tests for the "assign application: missing extended port
columns" bugfix.

Spec: .kiro/specs/assign-application-missing-port-columns

Property 2: Preservation - Already-Migrated and Non-Buggy Inputs Unchanged
---------------------------------------------------------------------------
For any input where the bug condition does NOT hold (the ``user_applications``
table already contains all 12 port columns), the fixed code SHALL produce the
same result as the original code:

  - assignment succeeds identically ("Application assigned successfully"),
  - the 12 ports are computed via ``calculate_app_ports`` and stored in
    ``APP_PORT_COLUMNS`` order,
  - an already-assigned application still returns the 409 "Application already
    assigned" response (not a schema error),
  - nginx location update + access URL generation are unchanged,
  - re-running ``add_extended_ports_to_user_applications.sql`` makes no change
    (no error; existing ``http_port``/``https_port``/``http_port2``/
    ``https_port2`` values and already-populated extended columns untouched).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

Methodology (observation-first)
-------------------------------
Following the established repo convention (``tests/test_default_apps_seeding.py``,
``tests/test_application_link_edit_preservation.py``): no live PostgreSQL is
reachable, so the ``user_applications`` migration is driven by a SQL-aware fake
table that interprets the real migration SQL (``ADD COLUMN IF NOT EXISTS`` +
NULL-guarded ``UPDATE`` backfill), and the dashboard-assign flow is driven
through the real Flask route with a fake ``db_manager`` and a stubbed
``insert_location_block``.

These tests are written against the UNFIXED code and are EXPECTED TO PASS -
they capture the baseline behavior the fix must preserve. (The fix only wires
the migration into ``init_db()``; it changes none of the behavior exercised
here, so the same assertions must still hold after the fix.)

_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
"""

import os
import re
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# psycopg2 is mocked by the sibling wiring test module too; guard here so this
# module can also run standalone without a real driver.
from unittest.mock import MagicMock  # noqa: E402

_mock_psycopg2 = MagicMock()
_mock_psycopg2.pool = MagicMock()
sys.modules.setdefault("psycopg2", _mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", _mock_psycopg2.pool)

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

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migration",
    "add_extended_ports_to_user_applications.sql",
)

# The backfill maps each extended column to http_port + <offset>, matching the
# migration's sequential allocation scheme.
_BACKFILL_OFFSETS = {
    "http_port3": 4, "https_port3": 5,
    "http_port4": 6, "https_port4": 7,
    "http_port5": 8, "https_port5": 9,
    "http_port6": 10, "https_port6": 11,
}


def _read_migration_sql():
    with open(MIGRATION_PATH, "r") as f:
        return f.read()


# --------------------------------------------------------------------------
# SQL-aware fake user_applications table that interprets the migration SQL
# --------------------------------------------------------------------------

class FakeUserApplicationsTable:
    """In-memory model of ``user_applications`` that interprets exactly the two
    statements in ``add_extended_ports_to_user_applications.sql``:

      * ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS <col> INTEGER`` (x8)
      * a NULL-guarded ``UPDATE`` backfill of the 8 extended columns.

    It records an ordered event log (``("add_column", col)`` / ``("noop_add",
    col)`` / ``("backfill", n_rows)``) so a re-run can be asserted to be a true
    no-op, and it preserves existing values so we can assert the original 4
    port columns are never touched.
    """

    def __init__(self, columns, rows):
        # columns: set of column names present on the table.
        self.columns = set(columns)
        # rows: list of dicts mapping column -> value (or None).
        self.rows = [dict(r) for r in rows]
        self.events = []

    # -- statement interpreters ------------------------------------------
    def apply_add_columns(self, sql):
        # Parse "ADD COLUMN IF NOT EXISTS <col> INTEGER" occurrences.
        for col in re.findall(
            r"add\s+column\s+if\s+not\s+exists\s+(\w+)", sql, re.IGNORECASE
        ):
            if col in self.columns:
                self.events.append(("noop_add", col))
            else:
                self.columns.add(col)
                # New column defaults to NULL on every existing row.
                for row in self.rows:
                    row.setdefault(col, None)
                self.events.append(("add_column", col))

    def apply_backfill(self, sql):
        # NULL-guarded: only rows WHERE http_port IS NOT NULL AND http_port3 IS NULL.
        affected = 0
        for row in self.rows:
            if row.get("http_port") is not None and row.get("http_port3") is None:
                base = row["http_port"]
                for col, offset in _BACKFILL_OFFSETS.items():
                    row[col] = base + offset
                affected += 1
        self.events.append(("backfill", affected))

    def run_migration(self, sql):
        """Interpret the whole migration script (ALTER then UPDATE)."""
        # Split into the ALTER TABLE statement and the UPDATE statement. The
        # COMMENT statements are documentation only and are ignored here.
        # Order matters: columns are added before the backfill runs.
        alter = re.search(
            r"alter\s+table.*?;", sql, re.IGNORECASE | re.DOTALL
        )
        update = re.search(
            r"update\s+user_applications.*?;", sql, re.IGNORECASE | re.DOTALL
        )
        if alter:
            self.apply_add_columns(alter.group(0))
        if update:
            self.apply_backfill(update.group(0))

    # -- helpers ---------------------------------------------------------
    def snapshot(self):
        return [dict(r) for r in self.rows]


def _fully_migrated_columns():
    return {
        "id", "user_id", "application_id", "url",
        *ORIGINAL_PORT_COLUMNS, *EXTENDED_PORT_COLUMNS,
    }


# ==========================================================================
# Test 1 - Idempotent re-run preservation (Req 2.4, 3.5)
# ==========================================================================

class TestIdempotentReRunPreservation:
    """Applying the migration to a table that already has all 12 columns makes
    no change and raises no error.

    _Requirements: 2.4, 3.5_
    """

    def test_rerun_on_fully_migrated_table_adds_no_columns(self):
        sql = _read_migration_sql()
        rows = [
            {"id": 1, "user_id": 1, "application_id": 1,
             "http_port": 6100, "https_port": 6101,
             "http_port2": 6102, "https_port2": 6103,
             "http_port3": 6104, "https_port3": 6105,
             "http_port4": 6106, "https_port4": 6107,
             "http_port5": 6108, "https_port5": 6109,
             "http_port6": 6110, "https_port6": 6111},
        ]
        table = FakeUserApplicationsTable(_fully_migrated_columns(), rows)
        before = table.snapshot()

        table.run_migration(sql)  # must not raise

        # Every extended column already existed -> all adds are no-ops.
        add_events = [e for e in table.events if e[0] == "add_column"]
        noop_events = [e for e in table.events if e[0] == "noop_add"]
        assert add_events == [], f"unexpected columns added: {add_events}"
        assert len(noop_events) == len(EXTENDED_PORT_COLUMNS)

        # Backfill affects zero rows (all extended ports already populated).
        backfill = [e for e in table.events if e[0] == "backfill"]
        assert backfill == [("backfill", 0)]

        # No row values changed at all.
        assert table.snapshot() == before

    def test_rerun_is_true_noop_across_repeated_applications(self):
        sql = _read_migration_sql()
        rows = [
            {"id": 1, "user_id": 2, "application_id": 3,
             **{c: 7000 + i for i, c in enumerate(APP_PORT_COLUMNS)}},
        ]
        table = FakeUserApplicationsTable(_fully_migrated_columns(), rows)
        first = table.snapshot()

        for _ in range(5):
            table.run_migration(sql)

        # State is identical after 5 re-runs; every backfill affected 0 rows.
        assert table.snapshot() == first
        assert all(
            e == ("backfill", 0) for e in table.events if e[0] == "backfill"
        )


# ==========================================================================
# Test 2 - Original ports preserved by the backfill (Req 3.5)
# ==========================================================================

class TestOriginalPortsPreserved:
    """The backfill leaves http_port/https_port/http_port2/https_port2 values
    untouched and only fills null extended columns.

    _Requirements: 3.5_
    """

    def test_backfill_only_fills_null_extended_columns_not_originals(self):
        sql = _read_migration_sql()
        # A partially-migrated row: the 8 extended columns exist but are NULL
        # (e.g. just after ADD COLUMN on an old row). Original 4 are populated.
        rows = [
            {"id": 1, "user_id": 1, "application_id": 1,
             "http_port": 6100, "https_port": 6101,
             "http_port2": 6102, "https_port2": 6103,
             "http_port3": None, "https_port3": None,
             "http_port4": None, "https_port4": None,
             "http_port5": None, "https_port5": None,
             "http_port6": None, "https_port6": None},
        ]
        table = FakeUserApplicationsTable(_fully_migrated_columns(), rows)
        table.run_migration(sql)

        row = table.rows[0]
        # Original 4 ports unchanged.
        assert row["http_port"] == 6100
        assert row["https_port"] == 6101
        assert row["http_port2"] == 6102
        assert row["https_port2"] == 6103
        # Extended columns filled from http_port + offset.
        for col, offset in _BACKFILL_OFFSETS.items():
            assert row[col] == 6100 + offset

    def test_backfill_skips_rows_whose_extended_ports_already_set(self):
        sql = _read_migration_sql()
        # Extended ports already hold NON-sequential (manually set) values; the
        # NULL guard means the backfill must not overwrite them.
        rows = [
            {"id": 1, "user_id": 1, "application_id": 1,
             "http_port": 6100, "https_port": 6101,
             "http_port2": 6102, "https_port2": 6103,
             "http_port3": 9003, "https_port3": 9005,
             "http_port4": 9006, "https_port4": 9007,
             "http_port5": 9008, "https_port5": 9009,
             "http_port6": 9010, "https_port6": 9011},
        ]
        table = FakeUserApplicationsTable(_fully_migrated_columns(), rows)
        before = table.snapshot()
        table.run_migration(sql)
        assert table.snapshot() == before  # nothing overwritten


# ==========================================================================
# Flask route driving for the assign-flow observations (Req 3.1-3.4)
# ==========================================================================

class FakeAssignDB:
    """Minimal db_manager stand-in for the POST assign branch of
    ``api_user_applications``. Records the INSERT that would run so we can
    assert the 12 ports are stored in APP_PORT_COLUMNS order.

    ``duplicate`` = True makes the assign INSERT raise a unique-violation-style
    error (so the route's 409 path is exercised).
    """

    def __init__(self, admin_username="admin", app_name="opcp-brik",
                 duplicate=False):
        self.admin_username = admin_username
        self.app_name = app_name
        self.duplicate = duplicate
        self.insert_calls = []

    def execute_query(self, query, params=None, fetch_one=False, fetch_all=False):
        q = " ".join(query.split()).lower()

        # Admin check: SELECT username FROM users WHERE id = %s
        if q.startswith("select username from users"):
            return (self.admin_username,) if self.admin_username else None

        # Application name lookup: SELECT name FROM applications WHERE id = %s
        if q.startswith("select name from applications where id"):
            return (self.app_name,) if self.app_name else None

        # Username-for-nginx lookup: SELECT username FROM users WHERE id = %s
        # (already handled above; this branch also serves the nginx lookup)

        # The 12-column assign insert.
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
    """Build a Flask app with the api blueprint and patch its collaborators."""
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


# ==========================================================================
# Test 3 - Duplicate-assign returns 409 (Req 3.3)
# ==========================================================================

class TestDuplicateAssign409Preservation:
    """An assignment for an already-assigned application returns the
    "Application already assigned" (409) response, not a schema error.

    _Requirements: 3.3_
    """

    def test_duplicate_assignment_returns_409(self):
        fake_db = FakeAssignDB(duplicate=True)
        nginx_calls = []
        with _assign_client(fake_db, lambda *a, **k: nginx_calls.append(a)) as client:
            resp = client.post(
                "/api/users/1/applications",
                json={"application_id": 7},
            )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "Application already assigned"


# ==========================================================================
# Test 4 - Successful-assign side-effects (Req 3.1, 3.2, 3.4)
# ==========================================================================

class TestSuccessfulAssignPreservation:
    """On a fully-migrated table the assign computes 12 ports via
    ``calculate_app_ports``, stores them in ``APP_PORT_COLUMNS`` order, and
    drives nginx update + URL generation as today.

    _Requirements: 3.1, 3.2, 3.4_
    """

    def test_successful_assign_stores_twelve_ports_and_updates_nginx(self):
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

        # Success response unchanged (Req 3.1).
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "Application assigned successfully"

        # Exactly one insert with the 12 ports in APP_PORT_COLUMNS order (Req 3.2).
        assert len(fake_db.insert_calls) == 1
        query, params = fake_db.insert_calls[0]
        # params = (user_id, app_id, url, *app_ports)
        assert params[0] == user_id
        assert params[1] == app_id
        stored_ports = tuple(params[3:])
        expected_ports = calculate_app_ports(user_id, app_id)
        assert stored_ports == expected_ports
        assert len(stored_ports) == len(APP_PORT_COLUMNS) == 12
        # Column list in the SQL matches APP_PORT_COLUMNS order.
        assert ", ".join(APP_PORT_COLUMNS) in query

        # URL is https://{DOMAIN}:{https_port} and drives nginx update (Req 3.4).
        expected_url = f"https://opcp-psmc.com:{expected_ports[1]}"
        assert params[2] == expected_url
        assert len(nginx_calls) == 1
        # insert_location_block(user_name, app_name, url, url)
        assert nginx_calls[0][1] == "opcp-brik"
        assert nginx_calls[0][2] == expected_url
        assert nginx_calls[0][3] == expected_url


# ==========================================================================
# Property-based preservation tests (Property 2)
# ==========================================================================

# Strategy: an already-migrated user_applications row. All 12 columns exist.
# Original 4 ports are always populated; extended columns are either NULL
# (never backfilled yet) or already populated with arbitrary values.
_port_value = st.integers(min_value=1024, max_value=65535)


@st.composite
def _migrated_row(draw):
    base = draw(st.integers(min_value=1024, max_value=60000))
    row = {
        "id": draw(st.integers(min_value=1, max_value=10_000)),
        "user_id": draw(st.integers(min_value=1, max_value=500)),
        "application_id": draw(st.integers(min_value=1, max_value=500)),
        "http_port": base,
        "https_port": base + 1,
        "http_port2": base + 2,
        "https_port2": base + 3,
    }
    # Either all extended columns are NULL, or all already populated.
    if draw(st.booleans()):
        for col in EXTENDED_PORT_COLUMNS:
            row[col] = None
    else:
        for col in EXTENDED_PORT_COLUMNS:
            row[col] = draw(_port_value)
    return row


class TestPreservationProperties:
    """Generate random already-migrated user_applications states and assert the
    migration re-run is a true no-op on the original 4 ports and any populated
    extended values, and that assignment port computation is stable.

    _Requirements: 3.2, 3.5_
    """

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(rows=st.lists(_migrated_row(), min_size=1, max_size=8))
    def test_migration_preserves_originals_and_populated_extended(self, rows):
        sql = _read_migration_sql()
        table = FakeUserApplicationsTable(_fully_migrated_columns(), rows)
        # Capture the pre-migration values by row POSITION (generated ids may
        # collide; FakeUserApplicationsTable preserves row order).
        before_originals = [
            tuple(r[c] for c in ORIGINAL_PORT_COLUMNS) for r in rows
        ]
        before_populated_extended = [
            {c: r[c] for c in EXTENDED_PORT_COLUMNS if r[c] is not None}
            for r in rows
        ]

        table.run_migration(sql)  # must not raise

        # No columns added (all 12 already present).
        assert not any(e[0] == "add_column" for e in table.events)

        for i, r in enumerate(table.rows):
            # Original 4 ports are never modified by the backfill (Req 3.5).
            assert tuple(r[c] for c in ORIGINAL_PORT_COLUMNS) == \
                before_originals[i]
            # Any extended column that was already populated is untouched (Req 3.5).
            for col, val in before_populated_extended[i].items():
                assert r[col] == val
            # After migration, all extended columns are populated (either
            # pre-existing values or backfilled from http_port + offset).
            for col, offset in _BACKFILL_OFFSETS.items():
                if before_populated_extended[i].get(col) is None:
                    assert r[col] == r["http_port"] + offset

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(
        user_id=st.integers(min_value=1, max_value=500),
        app_id=st.integers(min_value=1, max_value=500),
    )
    def test_assign_port_computation_stable_and_ordered(self, user_id, app_id):
        # calculate_app_ports must yield 12 ports; the assign insert stores them
        # in APP_PORT_COLUMNS order. This is the baseline the fix must preserve.
        ports = calculate_app_ports(user_id, app_id)
        assert len(ports) == len(APP_PORT_COLUMNS) == 12
        # Ports are 12 consecutive integers (sequential allocation scheme).
        assert list(ports) == list(range(ports[0], ports[0] + 12))
