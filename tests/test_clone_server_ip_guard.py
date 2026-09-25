"""Backend tests for the clone server-IP guard in ``_handle_clone_action``.

Spec: .kiro/specs/clone-hardcoded-server-id

The clone handler resolves a server IP from the DB via
``SELECT server_ip FROM servers WHERE id = %s`` and (per the design) must
reject a resolved-but-unusable IP (NULL / empty / whitespace) with HTTP 400
before constructing any local or remote (``ssh ubuntu@<ip>``) command.

This module provides the shared scaffolding (fixtures + helpers + a SQL-faithful
fake ``db_manager``) that the edge-case, preservation, and happy-path tests
(tasks 2.2-2.4) build on. It mirrors the fake-``db_manager`` pattern from
``tests/test_server_allocate_no_servers.py``: register ``api_bp`` on a Flask
test app, monkeypatch ``api_routes.db_manager`` with a fake whose
``execute_query`` reacts to the SQL text, authenticate the session, and POST to
``/api/deployments`` with ``action: 'clone'``.

_Requirements: 4.1, 4.2, 5.1, 5.2, 5.3_
"""

import os
import re
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module (not just the function) so we can monkeypatch its
# module-level ``db_manager`` reference that the deployments route and
# ``_handle_clone_action`` use.
import src.routes.api_routes as api_routes  # noqa: E402
from src.routes.api_routes import api_bp  # noqa: E402


# --------------------------------------------------------------------------
# SQL-faithful fake db_manager
# --------------------------------------------------------------------------

_SELECT_USERNAME_RE = re.compile(
    r"select\s+username\s+from\s+users\s+where\s+id\s*=\s*%s", re.IGNORECASE
)
_SELECT_SERVER_IP_RE = re.compile(
    r"select\s+server_ip\s+from\s+servers\s+where\s+id\s*=\s*%s", re.IGNORECASE
)


class FakeDbManager:
    """Emulates the subset of SQL the clone flow reaches.

    The clone request path issues two queries through ``execute_query``:

      * ``SELECT username FROM users WHERE id = %s``  (route: resolve the
        deploying user's username for the deployment_path)
      * ``SELECT server_ip FROM servers WHERE id = %s`` (clone handler:
        resolve the target server IP)

    Seed with:
      users:   dict mapping user_id -> username
      servers: dict mapping server_id -> server_ip value (str, None, ...)

    ``server_ip`` values may be a usable string, ``None``, or an empty/
    whitespace string so tests can drive the guard's branches. A server_id
    absent from ``servers`` makes the lookup return ``None`` (row not found).
    """

    def __init__(self, users=None, servers=None):
        # Default: user 1 exists so the route's username lookup succeeds.
        self.users = users if users is not None else {1: "tester"}
        self.servers = servers if servers is not None else {}
        self.queries = []  # (query, params) log for assertions if needed

    def execute_query(self, query, params=None, fetch_all=False, fetch_one=False):
        self.queries.append((query, params))

        if _SELECT_USERNAME_RE.search(query):
            uid = params[0]
            username = self.users.get(uid)
            return (username,) if username is not None else None

        if _SELECT_SERVER_IP_RE.search(query):
            sid = params[0]
            if sid not in self.servers:
                return None  # no matching row -> "Server {id} not found"
            return (self.servers[sid],)

        raise AssertionError(f"Unexpected query in fake db_manager: {query!r}")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def install_db(monkeypatch, users=None, servers=None):
    """Install a SQL-faithful fake ``db_manager`` on the api_routes module."""
    fake = FakeDbManager(users=users, servers=servers)
    monkeypatch.setattr(api_routes, "db_manager", fake)
    return fake


def authed(client, user_id=1):
    """Authenticate the test session as ``user_id``."""
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def clone(client, server_id=4, application_name="myapp",
          git_url="https://github.com/example/repo.git", **extra):
    """POST a clone request to ``/api/deployments``.

    ``extra`` allows adding fields such as ``target_user_id`` for admin tests.
    """
    body = {
        "action": "clone",
        "application_name": application_name,
        "git_url": git_url,
        "server_id": server_id,
        "stream": False,
    }
    body.update(extra)
    return client.post("/api/deployments", json=body)


# --------------------------------------------------------------------------
# Smoke structure (scaffolding sanity check)
# --------------------------------------------------------------------------

class TestScaffolding:
    """Minimal smoke coverage confirming the fixtures/fake are wired up.

    The concrete guard/preservation/happy-path assertions live in tasks
    2.2-2.4; these smoke tests only verify the scaffolding reaches the clone
    handler and the fake responds to the expected SQL.
    """

    def test_unauthenticated_clone_is_rejected(self, client, monkeypatch):
        # No authed(client): the route guards on session user_id.
        install_db(monkeypatch, servers={4: "57.130.47.210"})
        resp = clone(client)
        assert resp.status_code == 401, resp.get_json()
        assert resp.get_json()["error"] == "Authentication required"

    def test_missing_server_row_reaches_clone_handler(self, client, monkeypatch):
        # server_id 99 absent -> fake returns None -> handler's not-found 400.
        install_db(monkeypatch, servers={4: "57.130.47.210"})
        authed(client)
        resp = clone(client, server_id=99)
        assert resp.status_code == 400, resp.get_json()
        assert "not found" in resp.get_json()["error"].lower()
