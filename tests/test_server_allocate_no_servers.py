"""Bug condition exploration test for POST /api/server/allocate.

Spec: .kiro/specs/server-allocate-no-servers

Property 1 (Bug Condition): api_server_allocate must count usage accurately
(excluding NULL-server_id deployments) and allocate a server safely when a
candidate has NULL capacity columns.

CRITICAL: These tests are EXPECTED TO FAIL on the UNFIXED code. The failure
CONFIRMS the bug exists and is the SUCCESS case for this exploration task.

Approach (scoped PBT for deterministic DB-state bugs)
-----------------------------------------------------
`api_server_allocate` talks to the DB exclusively through
`db_manager.execute_query(query, params=None, fetch_all=..., fetch_one=...)`.
We install a *SQL-faithful* fake db_manager that computes rows from seeded
in-memory ``servers`` and ``deployments`` tables, reproducing the exact SQL
semantics that trigger the bug:

  * The usage subqueries ``GROUP BY server_id`` WITHOUT ``WHERE server_id IS
    NOT NULL`` collapse orphaned (NULL server_id) deployments into a phantom
    NULL bucket that never joins a real server -> counts under-reported. When
    the fix adds ``WHERE server_id IS NOT NULL`` the fake honors it.
  * The capacity projection returns the raw (possibly NULL) capacity unless
    the query wraps it in ``COALESCE(..., 100)`` (the convention already used
    in ``orchestrator.py``). A raw NULL flows into the Python comparison
    ``user_count < user_max`` -> ``int < None`` -> TypeError -> 500.

Because the fake reacts to the SQL text, the SAME assertions here fail on the
unfixed code (bug present) and pass once the fix lands (task 3.4 re-runs this).
"""

import os
import re
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module (not just the function) so we can monkeypatch its
# module-level ``db_manager`` reference that api_server_allocate uses.
import src.routes.api_routes as api_routes  # noqa: E402
from src.routes.api_routes import api_bp  # noqa: E402


# --------------------------------------------------------------------------
# SQL-faithful fake db_manager
# --------------------------------------------------------------------------

_SELECT_SERVERS_RE = re.compile(r"from\s+servers\s+s", re.IGNORECASE)
_UPDATE_ACTIVE_RE = re.compile(
    r"update\s+servers\s+set\s+server_status\s*=\s*'active'", re.IGNORECASE
)


class FakeDbManager:
    """Emulates the subset of SQL used by api_server_allocate.

    Seed with:
      servers: list of dicts with keys
        id, server_status, server_capacity_user_max, server_capacity_appli_max
      deployments: list of dicts with keys
        server_id (int or None), user_id, application_name
    """

    def __init__(self, servers, deployments):
        self.servers = servers
        self.deployments = deployments
        self.promoted_ids = []  # server ids that received the STAND_BY->ACTIVE UPDATE

    # -- helpers -----------------------------------------------------------
    def _user_counts(self, exclude_null_server_id):
        counts = {}
        for d in self.deployments:
            sid = d["server_id"]
            if exclude_null_server_id and sid is None:
                continue
            counts.setdefault(sid, set()).add(d["user_id"])
        return {sid: len(users) for sid, users in counts.items()}

    def _app_counts(self, exclude_null_server_id):
        counts = {}
        for d in self.deployments:
            sid = d["server_id"]
            if exclude_null_server_id and sid is None:
                continue
            counts.setdefault(sid, set()).add(d["application_name"])
        return {sid: len(apps) for sid, apps in counts.items()}

    # -- the only method the endpoint calls -------------------------------
    def execute_query(self, query, params=None, fetch_all=False, fetch_one=False):
        # UPDATE servers SET server_status = 'ACTIVE' WHERE id = %s AND server_status = 'STAND_BY'
        if _UPDATE_ACTIVE_RE.search(query):
            target_id = params[0]
            for s in self.servers:
                if s["id"] == target_id and s["server_status"] == "STAND_BY":
                    s["server_status"] = "ACTIVE"
                    self.promoted_ids.append(target_id)
            return None

        # SELECT ... FROM servers s ...  (the candidate list with usage counts)
        if _SELECT_SERVERS_RE.search(query) and fetch_all:
            # The FIXED query filters NULL server_id in the usage subqueries.
            exclude_null = bool(re.search(r"server_id\s+is\s+not\s+null", query, re.IGNORECASE))
            # The FIXED query wraps capacity columns in COALESCE(..., 100).
            coalesce_user = bool(
                re.search(r"coalesce\s*\(\s*s\.server_capacity_user_max\s*,\s*100\s*\)", query, re.IGNORECASE)
            )
            coalesce_appli = bool(
                re.search(r"coalesce\s*\(\s*s\.server_capacity_appli_max\s*,\s*100\s*\)", query, re.IGNORECASE)
            )

            user_counts = self._user_counts(exclude_null)
            app_counts = self._app_counts(exclude_null)

            rows = []
            # WHERE server_status IN ('STAND_BY','ACTIVE') ORDER BY server_status ASC
            candidates = [
                s for s in self.servers if s["server_status"] in ("STAND_BY", "ACTIVE")
            ]
            candidates.sort(key=lambda s: s["server_status"])  # ACTIVE < STAND_BY

            for s in candidates:
                user_max = s["server_capacity_user_max"]
                appli_max = s["server_capacity_appli_max"]
                if coalesce_user and user_max is None:
                    user_max = 100
                if coalesce_appli and appli_max is None:
                    appli_max = 100
                cur_users = user_counts.get(s["id"], 0)
                cur_apps = app_counts.get(s["id"], 0)
                rows.append((s["id"], user_max, appli_max, cur_users, cur_apps))
            return rows

        raise AssertionError(f"Unexpected query in fake db_manager: {query!r}")


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    app.register_blueprint(api_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _install_db(monkeypatch, servers, deployments):
    fake = FakeDbManager(servers, deployments)
    monkeypatch.setattr(api_routes, "db_manager", fake)
    return fake


def _authed(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1


def _allocate(client):
    return client.post(
        "/api/server/allocate",
        json={"application_name": "myapp"},
    )


# --------------------------------------------------------------------------
# Bug (b): NULL capacity on a single STAND_BY server
# --------------------------------------------------------------------------

class TestNullCapacitySingleServer:
    """Expected: 200 with a valid server_id (NULL capacity treated as 100).

    Unfixed: TypeError (int < None) -> 500 'Server allocation failed'.
    """

    def test_null_user_max_still_allocates(self, client, monkeypatch):
        fake = _install_db(
            monkeypatch,
            servers=[
                {
                    "id": 10,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": None,
                    "server_capacity_appli_max": 100,
                }
            ],
            deployments=[
                {"server_id": 10, "user_id": 1, "application_name": "a"},
            ],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["server_id"] == 10
        assert 10 in fake.promoted_ids

    def test_null_appli_max_still_allocates(self, client, monkeypatch):
        _install_db(
            monkeypatch,
            servers=[
                {
                    "id": 11,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 100,
                    "server_capacity_appli_max": None,
                }
            ],
            deployments=[],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["server_id"] == 11


# --------------------------------------------------------------------------
# Bug (b) + loop abort: NULL capacity earlier, valid server later
# --------------------------------------------------------------------------

class TestLoopAbortReachesLaterServer:
    """Server A (NULL capacity, evaluated first) must not abort the loop before
    Server B (valid, has spare capacity) is reached.

    Unfixed: TypeError on A propagates to outer except -> 500, B never reached.
    """

    def test_valid_later_server_is_allocated(self, client, monkeypatch):
        # Both STAND_BY so ordering is stable by insertion; A first.
        fake = _install_db(
            monkeypatch,
            servers=[
                {
                    "id": 20,  # Server A - NULL capacity, evaluated first
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": None,
                    "server_capacity_appli_max": None,
                },
                {
                    "id": 21,  # Server B - valid, has spare capacity
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 50,
                    "server_capacity_appli_max": 50,
                },
            ],
            deployments=[],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        # A valid server must be allocated; B (21) is definitely allocatable.
        assert resp.get_json()["server_id"] in (20, 21)
        assert fake.promoted_ids, "a server should have been promoted"


# --------------------------------------------------------------------------
# Bug (a): NULL server_id deployments must not distort per-server counts
# --------------------------------------------------------------------------

class TestNullServerIdCounting:
    """Orphaned (NULL server_id) deployments belong to no server; a server that
    is genuinely under capacity must still be allocated.

    We seed a server whose real (assigned) usage is under capacity, plus many
    orphaned deployments. Correct counting ignores the orphans and allocates.
    """

    def test_orphaned_deployments_do_not_block_allocation(self, client, monkeypatch):
        fake = _install_db(
            monkeypatch,
            servers=[
                {
                    "id": 30,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 5,
                    "server_capacity_appli_max": 5,
                }
            ],
            deployments=(
                # 2 real users / 2 real apps assigned to server 30 (under cap 5)
                [
                    {"server_id": 30, "user_id": 1, "application_name": "a"},
                    {"server_id": 30, "user_id": 2, "application_name": "b"},
                ]
                # 10 orphaned deployments (server_id NULL) that must be ignored
                + [
                    {"server_id": None, "user_id": 100 + i, "application_name": f"orphan{i}"}
                    for i in range(10)
                ]
            ),
        )
        _authed(client)
        resp = _allocate(client)
        # Server 30 has real usage 2/2, well under 5/5, so it must be allocated.
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["server_id"] == 30
        assert 30 in fake.promoted_ids


# --------------------------------------------------------------------------
# Edge case: all candidate servers have NULL capacity and are under 100
# --------------------------------------------------------------------------

class TestAllNullCapacityUnder100:
    """All candidates NULL capacity, all under 100 -> one must be allocated.

    Unfixed: TypeError on the first candidate -> 500.
    """

    def test_allocates_when_all_null_capacity(self, client, monkeypatch):
        _install_db(
            monkeypatch,
            servers=[
                {
                    "id": 40,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": None,
                    "server_capacity_appli_max": None,
                },
                {
                    "id": 41,
                    "server_status": "ACTIVE",
                    "server_capacity_user_max": None,
                    "server_capacity_appli_max": None,
                },
            ],
            deployments=[
                {"server_id": 40, "user_id": 1, "application_name": "a"},
                {"server_id": 41, "user_id": 2, "application_name": "b"},
            ],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["server_id"] in (40, 41)


# ==========================================================================
# Task 2 - Property 2 (Preservation): Non-buggy inputs unchanged
# ==========================================================================
#
# Observation-first methodology
# -----------------------------
# These tests were derived by observing the CURRENT (unfixed) behavior of
# `api_server_allocate` for inputs where isBugCondition(input) is FALSE:
#   * every deployment has a non-NULL server_id, AND
#   * every candidate server has non-NULL server_capacity_user_max and
#     server_capacity_appli_max.
#
# For such inputs the SQL-faithful FakeDbManager above behaves identically
# whether or not the fix (WHERE server_id IS NOT NULL / COALESCE(...,100)) is
# present, because there are no NULL server_ids to filter and no NULL
# capacities to coalesce. So the observed outputs recorded here form the
# baseline that the fix (task 3) must preserve, and task 3.5 re-runs them.
#
# Observed outputs on the UNFIXED code (recorded, then asserted):
#   Normal allocation      -> 200 {"server_id": <first STAND_BY w/ capacity>},
#                             STAND_BY promoted to ACTIVE.
#   At-capacity (all full)  -> 503 {"error": "All servers at capacity"}.
#   No standby/active srv   -> 503 {"error": "No standby servers available"}.
#   Missing session user_id -> 401 {"error": "Authentication required"}.
#   Missing application_name-> 400 {"error": "Application name required"}.
#   Usage counts            -> per-server current_users/current_apps computed
#                             over the assigned (non-NULL server_id) rows.

from hypothesis import given, settings, strategies as st  # noqa: E402


def _non_buggy_servers(servers):
    """A candidate set is non-buggy iff no STAND_BY/ACTIVE server has NULL cap."""
    for s in servers:
        if s["server_status"] in ("STAND_BY", "ACTIVE"):
            assert s["server_capacity_user_max"] is not None
            assert s["server_capacity_appli_max"] is not None
    return servers


# --------------------------------------------------------------------------
# 3.1 / 3.3 Normal allocation: first STAND_BY with spare capacity, promoted.
# --------------------------------------------------------------------------

class TestPreservationNormalAllocation:
    """Non-buggy, fully-populated set: allocate first STAND_BY with capacity,
    promote STAND_BY -> ACTIVE, respond {"server_id": <id>}.
    _Requirements: 3.3, 3.6_
    """

    def test_first_standby_with_capacity_allocated_and_promoted(self, client, monkeypatch):
        fake = _install_db(
            monkeypatch,
            servers=_non_buggy_servers([
                {
                    "id": 50,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
                {
                    "id": 51,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
            ]),
            deployments=[
                {"server_id": 50, "user_id": 1, "application_name": "a"},
            ],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        # STAND_BY servers ordered stably; the first with spare capacity wins.
        assert resp.get_json()["server_id"] == 50
        assert fake.promoted_ids == [50], "selected STAND_BY promoted to ACTIVE"

    def test_standby_preferred_over_active(self, client, monkeypatch):
        """ORDER BY server_status ASC -> 'ACTIVE' sorts before 'STAND_BY'.

        Observed on unfixed code: with the fake's sort key (server_status
        string, ACTIVE < STAND_BY), the ACTIVE server is evaluated first and,
        when it has spare capacity, it is the one allocated. This test records
        that observed ordering behavior so the fix preserves it.
        """
        fake = _install_db(
            monkeypatch,
            servers=_non_buggy_servers([
                {
                    "id": 60,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
                {
                    "id": 61,
                    "server_status": "ACTIVE",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
            ]),
            deployments=[],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        # ACTIVE (61) is evaluated first under ORDER BY server_status ASC.
        assert resp.get_json()["server_id"] == 61
        # An ACTIVE server is NOT promoted (promotion only fires on STAND_BY).
        assert fake.promoted_ids == [], "ACTIVE server must not be promoted"

    def test_active_server_not_promoted(self, client, monkeypatch):
        """A selected ACTIVE server returns its id but is never promoted.
        _Requirements: 3.3_
        """
        fake = _install_db(
            monkeypatch,
            servers=_non_buggy_servers([
                {
                    "id": 70,
                    "server_status": "ACTIVE",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
            ]),
            deployments=[],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["server_id"] == 70
        assert fake.promoted_ids == []


# --------------------------------------------------------------------------
# 3.2 At-capacity terminal state -> 503 "All servers at capacity".
# --------------------------------------------------------------------------

class TestPreservationAtCapacity:
    """All candidate servers genuinely at/over capacity -> 503.
    _Requirements: 3.2_
    """

    def test_all_servers_at_capacity_returns_503(self, client, monkeypatch):
        _install_db(
            monkeypatch,
            servers=_non_buggy_servers([
                {
                    "id": 80,
                    "server_status": "STAND_BY",
                    "server_capacity_user_max": 1,
                    "server_capacity_appli_max": 1,
                },
                {
                    "id": 81,
                    "server_status": "ACTIVE",
                    "server_capacity_user_max": 1,
                    "server_capacity_appli_max": 1,
                },
            ]),
            deployments=[
                # server 80 full: 1 user / 1 app (user_count == user_max)
                {"server_id": 80, "user_id": 1, "application_name": "a"},
                # server 81 full: 1 user / 1 app
                {"server_id": 81, "user_id": 2, "application_name": "b"},
            ],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 503, resp.get_json()
        assert resp.get_json()["error"] == "All servers at capacity"


# --------------------------------------------------------------------------
# 3.1 No-servers terminal state -> 503 "No standby servers available".
# --------------------------------------------------------------------------

class TestPreservationNoServers:
    """Zero STAND_BY/ACTIVE servers -> 503.
    _Requirements: 3.1_
    """

    def test_no_candidate_servers_returns_503(self, client, monkeypatch):
        _install_db(
            monkeypatch,
            servers=[
                # Not a candidate: status not STAND_BY/ACTIVE.
                {
                    "id": 90,
                    "server_status": "MAINTENANCE",
                    "server_capacity_user_max": 10,
                    "server_capacity_appli_max": 10,
                },
            ],
            deployments=[],
        )
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 503, resp.get_json()
        assert resp.get_json()["error"] == "No standby servers available"

    def test_empty_server_table_returns_503(self, client, monkeypatch):
        _install_db(monkeypatch, servers=[], deployments=[])
        _authed(client)
        resp = _allocate(client)
        assert resp.status_code == 503, resp.get_json()
        assert resp.get_json()["error"] == "No standby servers available"


# --------------------------------------------------------------------------
# 3.4 / 3.5 Guard clauses: 401 (no user_id) and 400 (no application_name).
# --------------------------------------------------------------------------

class TestPreservationGuardClauses:
    """Authentication and validation guards unchanged.
    _Requirements: 3.4, 3.5_
    """

    def test_missing_user_id_returns_401(self, client, monkeypatch):
        _install_db(monkeypatch, servers=[], deployments=[])
        # No _authed(client): session has no user_id.
        resp = _allocate(client)
        assert resp.status_code == 401, resp.get_json()
        assert resp.get_json()["error"] == "Authentication required"

    def test_missing_application_name_returns_400(self, client, monkeypatch):
        _install_db(monkeypatch, servers=[], deployments=[])
        _authed(client)
        resp = client.post("/api/server/allocate", json={})
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["error"] == "Application name required"


# --------------------------------------------------------------------------
# 3.6 Usage-count parity + preservation property across the non-buggy domain.
# --------------------------------------------------------------------------

class TestPreservationProperty:
    """Property 2: for inputs where isBugCondition is FALSE (all deployments
    have non-NULL server_id, all candidate servers have non-NULL capacities),
    the endpoint's allocation decision matches the observed baseline.

    We generate varied server counts, statuses, usage levels and non-NULL
    capacities, compute the expected outcome directly from the same SQL
    semantics (usage counted over assigned rows only; first candidate in
    ORDER BY server_status ASC with strict spare capacity wins), and assert
    the endpoint agrees.

    **Validates: Requirements 3.1, 3.2, 3.6**
    """

    # Non-NULL capacities only (non-buggy domain).
    _capacity = st.integers(min_value=1, max_value=6)
    _status = st.sampled_from(["STAND_BY", "ACTIVE"])

    @settings(max_examples=150, deadline=None)
    @given(
        specs=st.lists(
            st.fixed_dictionaries(
                {
                    "server_status": _status,
                    "server_capacity_user_max": _capacity,
                    "server_capacity_appli_max": _capacity,
                    # how many distinct assigned users/apps to seed (usage)
                    "users": st.integers(min_value=0, max_value=6),
                    "apps": st.integers(min_value=0, max_value=6),
                }
            ),
            min_size=1,
            max_size=5,
        ),
    )
    def test_non_buggy_allocation_matches_baseline(self, specs):
        # Build a fresh Flask app per generated input (Hypothesis re-runs the
        # test body for each example; a function-scoped fixture would not reset).
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret-key"
        app.register_blueprint(api_bp)
        # Build servers with stable, unique ids.
        servers = []
        deployments = []
        for idx, spec in enumerate(specs):
            sid = 1000 + idx
            servers.append(
                {
                    "id": sid,
                    "server_status": spec["server_status"],
                    "server_capacity_user_max": spec["server_capacity_user_max"],
                    "server_capacity_appli_max": spec["server_capacity_appli_max"],
                }
            )
            # Seed non-NULL server_id deployments: distinct users and apps so
            # COUNT(DISTINCT ...) equals the requested usage level.
            for u in range(spec["users"]):
                deployments.append(
                    {"server_id": sid, "user_id": f"{sid}-u{u}", "application_name": f"{sid}-shared"}
                )
            for a in range(spec["apps"]):
                deployments.append(
                    {"server_id": sid, "user_id": f"{sid}-fixed", "application_name": f"{sid}-a{a}"}
                )

        # Expected outcome computed from the SAME SQL semantics the endpoint uses:
        # candidates ordered by server_status ASC (ACTIVE < STAND_BY), first one
        # with strict spare capacity (users < user_max AND apps < appli_max) wins.
        candidates = [s for s in servers if s["server_status"] in ("STAND_BY", "ACTIVE")]
        candidates.sort(key=lambda s: s["server_status"])

        def usage_users(sid):
            return len({d["user_id"] for d in deployments if d["server_id"] == sid})

        def usage_apps(sid):
            return len({d["application_name"] for d in deployments if d["server_id"] == sid})

        expected_id = None
        for s in candidates:
            if usage_users(s["id"]) < s["server_capacity_user_max"] and usage_apps(s["id"]) < s["server_capacity_appli_max"]:
                expected_id = s["id"]
                break

        fake = FakeDbManager(servers, deployments)
        original_db = api_routes.db_manager
        api_routes.db_manager = fake
        try:
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = 1
            resp = client.post("/api/server/allocate", json={"application_name": "myapp"})

            if not candidates:
                assert resp.status_code == 503
                assert resp.get_json()["error"] == "No standby servers available"
            elif expected_id is None:
                assert resp.status_code == 503
                assert resp.get_json()["error"] == "All servers at capacity"
            else:
                assert resp.status_code == 200, resp.get_json()
                assert resp.get_json()["server_id"] == expected_id
        finally:
            api_routes.db_manager = original_db
