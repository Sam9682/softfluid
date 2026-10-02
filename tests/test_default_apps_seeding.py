"""Regression test for the "default apps seeding creates only one app" bug.

Spec: .kiro/specs/default-apps-seeding-only-one-app

Bug summary
-----------
On the first run of ``init_db()`` (``src/database_postgres.py``) against an
empty PostgreSQL database, only ONE application (``opcp-serverless-brik``) ends
up in the ``applications`` table, even though ``conf/default_apps`` lists 11
applications that should ALL be created.

Root cause: transaction atomicity. ``db_manager.get_db_connection()`` sets
``autocommit = False`` and rolls back on any exception. The
``opcp-serverless-brik`` block commits its single row in its own try/except.
The default-apps ``executemany(... ON CONFLICT (name) DO NOTHING ...)`` that
follows is NOT committed on its own -- it shares one transaction with a long
downstream block (default costs, server insert, admin/demo user creation,
``user_applications`` inserts, payment modes, configuration). If ANY statement
in that later block raises, the whole transaction is rolled back, discarding the
10 uncommitted default apps while the already-committed serverless-brik row
survives -> exactly 1 application.

Why not a live-DB test
----------------------
The ideal end-to-end assertion is ``SELECT COUNT(*) FROM applications == 11``
after running ``init_db()`` against a fresh PostgreSQL. No live/test PostgreSQL
is reachable in this environment, so instead -- following the established
convention in this repo (``tests/test_log_cleanup.py``, mock psycopg2 + a
MagicMock-style cursor) -- we drive ``init_db()`` with a SQL-aware fake
connection/cursor that records an ORDERED event log of every execute /
executemany / commit / rollback.

The fake injects a failure into a downstream statement (the demo/admin
``INSERT INTO users``) to simulate the exact bug trigger. The test then asserts
that the default applications (and their default costs) were COMMITTED *before*
that downstream failure occurred -- i.e. they survive the later rollback.

On the UNFIXED code the default-apps insert has no dedicated commit, so no
commit exists between the apps ``executemany`` and the failing statement and the
apps would be discarded: the test FAILS. On the FIXED code the apps + costs are
committed in their own try block before the downstream block runs: the test
PASSES.
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
from src.database_postgres import load_default_apps  # noqa: E402


# Path to the config so the count expectation is derived from the real file.
CONF_DEFAULT_APPS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "conf", "default_apps"
)


def _distinct_default_app_names():
    """Distinct application names declared in conf/default_apps."""
    return {row[0] for row in load_default_apps()}


# --------------------------------------------------------------------------
# SQL-aware fake cursor / connection
# --------------------------------------------------------------------------

class FakeCursor:
    """Minimal cursor that answers just enough of init_db()'s queries to reach
    (and pass) the default-apps block, while recording an ordered event log.

    ``events`` (shared with the connection) records tuples like
    ("execute", sql), ("executemany", sql, n), ("commit",), ("rollback",).

    ``fail_on`` is a regex; the first execute whose SQL matches raises, to
    simulate a downstream failure inside the shared transaction.
    """

    def __init__(self, events, fail_on=None):
        self.events = events
        self._fail_on = re.compile(fail_on, re.IGNORECASE | re.DOTALL) if fail_on else None
        self._last_result = None
        self.description = None

    # context manager protocol (used as `with conn.cursor() as cursor:`)
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _record_and_maybe_fail(self, sql):
        if self._fail_on is not None and self._fail_on.search(sql):
            raise RuntimeError("simulated downstream failure inside shared transaction")

    def execute(self, sql, params=None):
        self.events.append(("execute", sql))
        self._record_and_maybe_fail(sql)
        s = sql.strip().lower()

        # Default every fetch to "empty / zero" so init_db takes the
        # first-run seeding paths.
        self._last_result = None

        # Table existence check -> pretend schema not yet created so the
        # schema file is loaded (harmless: fake just records it).
        if "information_schema.tables" in s:
            self._last_result = None  # fetchone() -> None (schema missing)
            return

        # opcp-serverless-brik existence check -> not present.
        if "from applications where name" in s:
            self._last_result = None
            return

        # COUNT(*) style queries -> 0 (drive first-run branches).
        if s.startswith("select count(*)"):
            self._last_result = (0,)
            return

        # The default-costs lookup: applications lacking an application_costs
        # row. Return synthetic app ids so the code's cost-insert loop runs
        # (one row per default app) inside the committed block.
        if "left join application_costs" in s and "c.id is null" in s:
            self._last_result = [(i,) for i in range(1, 12)]
            return

        # "RETURNING id" inserts -> return a synthetic id.
        if "returning id" in s:
            self._last_result = (1,)
            return

        # Generic SELECTs that fetchall() over -> empty list.
        self._last_result = []

    def executemany(self, sql, seq_of_params):
        params_list = list(seq_of_params)
        self.events.append(("executemany", sql, len(params_list)))
        self._record_and_maybe_fail(sql)
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

    def __init__(self, events, fail_on=None):
        self.events = events
        self._fail_on = fail_on
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self.events, fail_on=self._fail_on)

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))


@contextmanager
def _fake_get_db_connection(events, fail_on):
    """Mirror db_manager.get_db_connection(): rollback on any exception."""
    conn = FakeConnection(events, fail_on=fail_on)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------
# Helpers over the event log
# --------------------------------------------------------------------------

_DEFAULT_APPS_INSERT_RE = re.compile(
    r"insert\s+into\s+applications.*on\s+conflict\s*\(\s*name\s*\)\s*do\s+nothing",
    re.IGNORECASE | re.DOTALL,
)
_DEFAULT_COSTS_INSERT_RE = re.compile(
    r"insert\s+into\s+application_costs", re.IGNORECASE | re.DOTALL
)
_DOWNSTREAM_FAIL_RE = r"insert\s+into\s+users"


def _index_of_default_apps_insert(events):
    for i, ev in enumerate(events):
        if ev[0] == "executemany" and _DEFAULT_APPS_INSERT_RE.search(ev[1]):
            return i
    return None


def _index_of_first_downstream_failure(events):
    """The execute that matched the injected failure is the LAST recorded
    execute event before the rollback (it raised right after being recorded)."""
    for i, ev in enumerate(events):
        if ev[0] == "execute" and re.search(_DOWNSTREAM_FAIL_RE, ev[1], re.IGNORECASE):
            return i
    return None


def _index_of_commit_after(events, start_idx):
    for i in range(start_idx + 1, len(events)):
        if events[i][0] == "commit":
            return i
    return None


# ==========================================================================
# Tests
# ==========================================================================

class TestDefaultAppsConfig:
    """Sanity: the config really declares 11 distinct apps (parser is correct)."""

    def test_config_declares_eleven_distinct_apps(self):
        names = _distinct_default_app_names()
        assert len(names) == 11, f"expected 11 distinct default apps, got {sorted(names)}"
        assert "opcp-serverless-brik" in names


class TestDefaultAppsSurviveDownstreamFailure:
    """Bug condition / fix checking.

    Run init_db() with a downstream failure injected at the demo/admin
    ``INSERT INTO users`` step. The default applications and their costs must
    have been COMMITTED before that failure, so a later rollback cannot discard
    them.

    UNFIXED code: no commit between the default-apps executemany and the failing
    INSERT INTO users -> assertion fails (bug reproduced).
    FIXED code: apps + costs committed in their own try block first -> passes.

    _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2_
    """

    def _run_init_db_with_downstream_failure(self):
        events = []
        original = db.db_manager.get_db_connection
        db.db_manager.get_db_connection = lambda: _fake_get_db_connection(
            events, fail_on=_DOWNSTREAM_FAIL_RE
        )
        try:
            # init_db() must not propagate the simulated failure past its own
            # per-block handling for the assertion to be meaningful; if it does
            # propagate we still capture the event log to assert on ordering.
            try:
                db.init_db()
            except RuntimeError:
                pass
        finally:
            db.db_manager.get_db_connection = original
        return events

    def test_default_apps_executemany_receives_all_eleven(self):
        events = self._run_init_db_with_downstream_failure()
        apps_idx = _index_of_default_apps_insert(events)
        assert apps_idx is not None, "default-apps executemany was never issued"
        n = events[apps_idx][2]
        assert n == 11, f"expected all 11 default apps in executemany, got {n}"

    def test_default_apps_committed_before_downstream_failure(self):
        events = self._run_init_db_with_downstream_failure()

        apps_idx = _index_of_default_apps_insert(events)
        assert apps_idx is not None, "default-apps executemany was never issued"

        fail_idx = _index_of_first_downstream_failure(events)
        assert fail_idx is not None, (
            "expected a downstream INSERT INTO users to be attempted (and fail)"
        )

        commit_idx = _index_of_commit_after(events, apps_idx)
        assert commit_idx is not None, (
            "no commit was issued after the default-apps insert -- the apps share "
            "the downstream transaction and would be lost on rollback (bug present)"
        )
        assert commit_idx < fail_idx, (
            "default apps were not committed before the downstream failure; a "
            "rollback would discard them, leaving only the pre-committed "
            "opcp-serverless-brik row (bug present)"
        )

    def test_default_costs_committed_with_apps_before_failure(self):
        events = self._run_init_db_with_downstream_failure()

        apps_idx = _index_of_default_apps_insert(events)
        assert apps_idx is not None

        fail_idx = _index_of_first_downstream_failure(events)
        assert fail_idx is not None

        # A default application_costs insert must occur after the apps insert
        # and before the commit that protects the apps (i.e. before the failure).
        commit_idx = _index_of_commit_after(events, apps_idx)
        assert commit_idx is not None and commit_idx < fail_idx

        cost_idx = None
        for i in range(apps_idx + 1, commit_idx):
            ev = events[i]
            if ev[0] == "execute" and _DEFAULT_COSTS_INSERT_RE.search(ev[1]):
                cost_idx = i
                break
        assert cost_idx is not None, (
            "default application_costs were not inserted in the same committed "
            "block as the default apps"
        )
