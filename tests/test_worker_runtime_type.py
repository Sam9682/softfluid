"""Tests that the serverless worker resolves and propagates runtime_type.

Covers:
- resolve_runtime_type returning 'runc' on any DB error (defensive default).
- ServerlessWorker passing its resolved runtime_type to runtime.run_container.
- ServerlessWorker accepting the runtime_type kwarg (and defaulting to 'runc').
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock psycopg2 before importing the worker module. This mirrors the pattern in
# tests/test_worker.py: when this module is imported first, it registers a
# MagicMock psycopg2 that exposes `.extras`, so that test_worker.py's own
# setdefault-based mock stays valid regardless of test execution order.
mock_psycopg2 = MagicMock()
mock_psycopg2.extras = MagicMock()
sys.modules.setdefault("psycopg2", mock_psycopg2)
sys.modules.setdefault("psycopg2.extras", mock_psycopg2.extras)

from src.serverless.worker import ServerlessWorker, resolve_runtime_type  # noqa: E402

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "test",
    "user": "user",
    "password": "pass",
}


class TestResolveRuntimeType:
    def test_returns_runc_on_db_error(self):
        # psycopg2.connect raising should be swallowed and default to 'runc'.
        with patch(
            "src.serverless.worker.psycopg2.connect",
            side_effect=Exception("connection refused"),
        ):
            assert resolve_runtime_type(DB_CONFIG) == "runc"


class TestWorkerConstruction:
    def test_defaults_to_runc(self):
        worker = ServerlessWorker(
            worker_id="w", runtime=MagicMock(), db_config=DB_CONFIG
        )
        assert worker.runtime_type == "runc"

    def test_accepts_runtime_type(self):
        worker = ServerlessWorker(
            worker_id="w",
            runtime=MagicMock(),
            db_config=DB_CONFIG,
            runtime_type="kata",
        )
        assert worker.runtime_type == "kata"


class TestProcessJobPropagatesRuntimeType:
    def _make_worker(self, runtime_type):
        return ServerlessWorker(
            worker_id="w",
            runtime=MagicMock(),
            db_config=DB_CONFIG,
            runtime_type=runtime_type,
        )

    def test_run_container_called_with_worker_runtime_type(self):
        worker = self._make_worker("kata")
        worker.runtime.run_container.return_value = "container-abc"
        worker.runtime.get_logs.return_value = ""

        job = {
            "id": "job-1",
            "image": "python:3.11",
            "command": ["python", "-c", "print(1)"],
            "environment": {},
            "timeout_seconds": 300,
            "user_id": 1,
        }

        with patch(
            "src.serverless.worker.validate_image_registry", return_value=True
        ), patch.object(
            worker, "_wait_with_cancellation_check", return_value=0
        ), patch.object(
            worker, "store_result"
        ), patch.object(
            worker, "mark_completed"
        ):
            worker.execute_job(job)

        kwargs = worker.runtime.run_container.call_args[1]
        assert kwargs["runtime_type"] == "kata"
