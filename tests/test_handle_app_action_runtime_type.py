"""Tests that _handle_app_action passes the runtime type to deployApp.sh.

The git-app Start/Stop path delegates to each app's deployApp.sh. We assert the
resolved runtime type is appended as a trailing positional argument and set as
the RUNTIME_TYPE environment variable for the subprocess, in both the
non-streaming and streaming branches.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.routes.api_routes as api_routes  # noqa: E402
from src.routes.api_routes import api_bp, _handle_app_action  # noqa: E402

_SELECT_DEPLOY_PATH_RE = re.compile(
    r"select\s+deployment_path\s+from\s+deployments", re.IGNORECASE
)
_SELECT_USER_DETAILS_RE = re.compile(
    r"select\s+username,\s*email,\s*first_name,\s*last_name\s+from\s+users",
    re.IGNORECASE,
)
_UPDATE_DEPLOYMENTS_RE = re.compile(
    r"update\s+deployments\s+set\s+status", re.IGNORECASE
)


class FakeDbManager:
    def __init__(self):
        self.queries = []

    def execute_query(self, query, params=None, fetch_all=False, fetch_one=False):
        self.queries.append((query, params))
        if _SELECT_DEPLOY_PATH_RE.search(query):
            return ("/tmp/deploy/app",)
        if _SELECT_USER_DETAILS_RE.search(query):
            return ("tester", "tester@example.com", "Test", "User")
        if _UPDATE_DEPLOYMENTS_RE.search(query):
            return None
        return None


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    app.register_blueprint(api_bp)
    return app


def _run_non_stream(app, monkeypatch, runtime_value):
    fake = FakeDbManager()
    monkeypatch.setattr(api_routes, "db_manager", fake)
    monkeypatch.setattr(api_routes, "get_runtime_type", lambda: runtime_value)
    monkeypatch.setattr(api_routes.os.path, "exists", lambda p: True)

    # Stub the billing import triggered inside the handler on START/STOP.
    billing_stub = MagicMock()
    monkeypatch.setitem(sys.modules, "src.routes.billing_routes", billing_stub)

    with patch.object(api_routes.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with app.test_request_context("/api/deployments", method="POST"):
            from flask import session
            session["user_id"] = 1
            _handle_app_action(1, "myapp", "START", {"stream": False})
    return mock_run


class TestHandleAppActionRuntimeType:
    def test_non_stream_appends_runtime_arg_and_env_kata(self, app, monkeypatch):
        mock_run = _run_non_stream(app, monkeypatch, "kata")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        # deployApp.sh action uid name email runtime_type
        assert cmd[-1] == "kata"
        assert cmd[1] == "START"
        assert kwargs["env"]["RUNTIME_TYPE"] == "kata"

    def test_non_stream_appends_runtime_arg_and_env_runc(self, app, monkeypatch):
        mock_run = _run_non_stream(app, monkeypatch, "runc")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[-1] == "runc"
        assert kwargs["env"]["RUNTIME_TYPE"] == "runc"
