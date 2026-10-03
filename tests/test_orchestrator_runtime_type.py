"""Tests that LightOrchestrator._start_container honors the configured runtime type.

Verifies that 'kata' injects `--runtime kata` into the docker run command and
that 'runc' (the Docker default) leaves the command without a --runtime flag.
"""

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# Mock psycopg2 and related modules before importing the orchestrator so that
# importing it does not require a real PostgreSQL driver/connection.
mock_psycopg2 = MagicMock()
mock_psycopg2.pool = MagicMock()
mock_psycopg2.extras = MagicMock()
sys.modules.setdefault("psycopg2", mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", mock_psycopg2.pool)
sys.modules.setdefault("psycopg2.extras", mock_psycopg2.extras)

from src.orchestrator import LightOrchestrator  # noqa: E402


def _fake_db_connection(server_ip="10.0.0.1"):
    """Build a context-manager that yields a conn whose cursor returns server_ip."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (server_ip,)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def _cm():
        yield conn

    return _cm()


def _start(runtime_value):
    """Invoke _start_container with the given runtime_type and capture the cmd."""
    orch = LightOrchestrator()
    with patch("src.orchestrator.db_manager") as mock_db, patch(
        "src.orchestrator.get_runtime_type", return_value=runtime_value
    ), patch("src.orchestrator.subprocess.run") as mock_run:
        mock_db.get_db_connection.return_value = _fake_db_connection()
        mock_run.return_value = MagicMock(returncode=0, stdout="container123\n", stderr="")
        orch._start_container(
            server_id=1,
            instance_id="svc-replica-0",
            image="nginx:latest",
            port=8080,
            ports={"80": "8080"},
            environment={},
            volumes=[],
        )
        return mock_run.call_args[0][0]


class TestStartContainerRuntimeType:
    def test_kata_injects_runtime_flag(self):
        cmd = _start("kata")
        assert "--runtime" in cmd
        idx = cmd.index("--runtime")
        assert cmd[idx + 1] == "kata"

    def test_runc_omits_runtime_flag(self):
        cmd = _start("runc")
        assert "--runtime" not in cmd

    def test_image_and_name_still_present_with_kata(self):
        cmd = _start("kata")
        assert cmd[:4] == ["docker", "run", "-d", "--name"]
        assert cmd[-1] == "nginx:latest"
