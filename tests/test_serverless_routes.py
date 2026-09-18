"""Tests for the serverless routes API - POST /api/jobs endpoint.

Verifies authentication, payload validation, registry whitelist enforcement,
timeout range validation, and successful job insertion.
"""

import json
from unittest.mock import patch, MagicMock
import uuid

import pytest
from flask import Flask

from src.routes.serverless_routes import serverless_bp


@pytest.fixture
def app():
    """Create a Flask app with the serverless blueprint for testing."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.register_blueprint(serverless_bp)
    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


class TestSubmitJobAuth:
    """Test authentication for POST /api/jobs."""

    def test_returns_401_when_not_authenticated(self, client):
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "python:3.11", "command": ["echo", "hi"]}),
            content_type='application/json',
        )
        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_201_when_authenticated(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo", "hi"]}),
            content_type='application/json',
        )
        assert response.status_code == 201


class TestSubmitJobPayloadValidation:
    """Test payload validation for POST /api/jobs."""

    def test_returns_400_when_body_is_invalid_json(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data='not valid json',
            content_type='application/json',
        )
        assert response.status_code == 400

    def test_returns_400_when_image_missing(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"command": ["echo", "hi"]}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "image" in response.get_json()["error"]

    def test_returns_400_when_image_not_string(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": 123, "command": ["echo"]}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "image" in response.get_json()["error"]

    def test_returns_400_when_command_missing(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11"}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "command" in response.get_json()["error"]

    def test_returns_400_when_command_not_list(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": "echo hi"}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "command" in response.get_json()["error"]

    def test_returns_400_when_env_not_dict(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "env": "bad"}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "env" in response.get_json()["error"]

    def test_returns_400_when_timeout_not_integer(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": "fast"}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "timeout" in response.get_json()["error"]


class TestSubmitJobTimeoutValidation:
    """Test timeout range validation (1-3600)."""

    def test_returns_400_when_timeout_zero(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": 0}),
            content_type='application/json',
        )
        assert response.status_code == 400
        assert "timeout" in response.get_json()["error"]

    def test_returns_400_when_timeout_negative(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": -1}),
            content_type='application/json',
        )
        assert response.status_code == 400

    def test_returns_400_when_timeout_exceeds_3600(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": 3601}),
            content_type='application/json',
        )
        assert response.status_code == 400

    @patch('src.routes.serverless_routes.db_manager')
    def test_accepts_timeout_at_lower_bound(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": 1}),
            content_type='application/json',
        )
        assert response.status_code == 201

    @patch('src.routes.serverless_routes.db_manager')
    def test_accepts_timeout_at_upper_bound(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"], "timeout": 3600}),
            content_type='application/json',
        )
        assert response.status_code == 201


class TestSubmitJobRegistryWhitelist:
    """Test image registry whitelist validation."""

    def test_returns_403_when_registry_not_approved(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "evil-registry.com/malware:latest", "command": ["echo"]}),
            content_type='application/json',
        )
        assert response.status_code == 403
        assert "not approved" in response.get_json()["error"]

    @patch('src.routes.serverless_routes.db_manager')
    def test_allows_image_from_docker_io(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "python:3.11", "command": ["echo"]}),
            content_type='application/json',
        )
        assert response.status_code == 201

    @patch('src.routes.serverless_routes.db_manager')
    def test_allows_image_from_ghcr_io(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "ghcr.io/org/myapp:latest", "command": ["run"]}),
            content_type='application/json',
        )
        assert response.status_code == 201


class TestSubmitJobSuccess:
    """Test successful job submission."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_201_with_job_id(self, mock_db, client, app):
        test_uuid = "550e8400-e29b-41d4-a716-446655440000"
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post(
            '/api/jobs',
            data=json.dumps({
                "image": "docker.io/python:3.11",
                "command": ["python", "script.py"],
                "env": {"KEY": "value"},
                "timeout": 600,
            }),
            content_type='application/json',
        )
        assert response.status_code == 201
        data = response.get_json()
        assert data["job_id"] == test_uuid

    @patch('src.routes.serverless_routes.db_manager')
    def test_inserts_with_correct_params(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 7
        client.post(
            '/api/jobs',
            data=json.dumps({
                "image": "registry.example.com/myapp:v1",
                "command": ["python", "main.py"],
                "env": {"DB_HOST": "localhost"},
                "timeout": 120,
            }),
            content_type='application/json',
        )
        # Verify the db call
        mock_db.execute_query.assert_called_once()
        call_args = mock_db.execute_query.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "INSERT INTO serverless_jobs" in query
        assert "RETURNING id" in query
        assert params[0] == 7  # user_id
        assert params[1] == "registry.example.com/myapp:v1"  # image
        assert json.loads(params[2]) == ["python", "main.py"]  # command as JSON
        assert json.loads(params[3]) == {"DB_HOST": "localhost"}  # env as JSON
        assert params[4] == 120  # timeout
        assert params[5] == "pending"  # status

    @patch('src.routes.serverless_routes.db_manager')
    def test_uses_default_env_when_not_provided(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/alpine:latest", "command": ["echo", "hi"]}),
            content_type='application/json',
        )
        call_args = mock_db.execute_query.call_args
        params = call_args[0][1]
        assert json.loads(params[3]) == {}  # default env

    @patch('src.routes.serverless_routes.db_manager')
    def test_uses_default_timeout_when_not_provided(self, mock_db, client, app):
        test_uuid = str(uuid.uuid4())
        mock_db.execute_query.return_value = (test_uuid,)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/alpine:latest", "command": ["echo"]}),
            content_type='application/json',
        )
        call_args = mock_db.execute_query.call_args
        params = call_args[0][1]
        assert params[4] == 300  # default timeout

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_500_on_db_failure(self, mock_db, client, app):
        mock_db.execute_query.side_effect = Exception("Connection refused")
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post(
            '/api/jobs',
            data=json.dumps({"image": "docker.io/python:3.11", "command": ["echo"]}),
            content_type='application/json',
        )
        assert response.status_code == 500


class TestGetJobStatusAuth:
    """Test authentication for GET /api/jobs/<job_id>."""

    def test_returns_401_when_not_authenticated(self, client):
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data


class TestGetJobStatusNotFound:
    """Test 404 handling for GET /api/jobs/<job_id>."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_404_when_job_does_not_exist(self, mock_db, client, app):
        mock_db.execute_query.return_value = None
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "Job not found"


class TestGetJobStatusOwnership:
    """Test ownership and admin access for GET /api/jobs/<job_id>."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_403_when_not_owner_and_not_admin(self, mock_db, client, app):
        from datetime import datetime
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',  # id
            99,  # user_id (different from session user)
            'docker.io/python:3.11',  # image
            'running',  # status
            datetime(2024, 1, 15, 10, 30, 0),  # created_at
            datetime(2024, 1, 15, 10, 30, 1),  # started_at
            None,  # completed_at
            None,  # exit_code
            'worker-001',  # worker_id
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = False
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 403
        data = response.get_json()
        assert data["error"] == "Access denied"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_200_when_user_is_owner(self, mock_db, client, app):
        from datetime import datetime
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,  # user_id matches session
            'docker.io/python:3.11',
            'running',
            datetime(2024, 1, 15, 10, 30, 0),
            datetime(2024, 1, 15, 10, 30, 1),
            None,
            None,
            'worker-001',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 200

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_200_when_user_is_admin(self, mock_db, client, app):
        from datetime import datetime
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            99,  # user_id does NOT match session
            'docker.io/python:3.11',
            'completed',
            datetime(2024, 1, 15, 10, 30, 0),
            datetime(2024, 1, 15, 10, 30, 1),
            datetime(2024, 1, 15, 10, 31, 0),
            0,
            'worker-001',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = True
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 200


class TestGetJobStatusResponse:
    """Test response format for GET /api/jobs/<job_id>."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_full_job_metadata(self, mock_db, client, app):
        from datetime import datetime
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'registry.example.com/myapp:latest',
            'running',
            datetime(2024, 1, 15, 10, 30, 0),
            datetime(2024, 1, 15, 10, 30, 1),
            None,
            None,
            'worker-001',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 200
        data = response.get_json()
        assert data["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert data["status"] == "running"
        assert data["image"] == "registry.example.com/myapp:latest"
        assert data["created_at"] == "2024-01-15T10:30:00Z"
        assert data["started_at"] == "2024-01-15T10:30:01Z"
        assert data["completed_at"] is None
        assert data["exit_code"] is None
        assert data["worker_id"] == "worker-001"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_completed_job_with_exit_code(self, mock_db, client, app):
        from datetime import datetime
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'docker.io/python:3.11',
            'completed',
            datetime(2024, 1, 15, 10, 30, 0),
            datetime(2024, 1, 15, 10, 30, 1),
            datetime(2024, 1, 15, 10, 31, 0),
            0,
            'worker-001',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "completed"
        assert data["completed_at"] == "2024-01-15T10:31:00Z"
        assert data["exit_code"] == 0

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_500_on_db_failure(self, mock_db, client, app):
        mock_db.execute_query.side_effect = Exception("Connection refused")
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000')
        assert response.status_code == 500


class TestGetJobResultAuth:
    """Test authentication for GET /api/jobs/<job_id>/result."""

    def test_returns_401_when_not_authenticated(self, client):
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data


class TestGetJobResultNotFound:
    """Test 404 handling for GET /api/jobs/<job_id>/result."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_404_when_job_does_not_exist(self, mock_db, client, app):
        mock_db.execute_query.return_value = None
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "Job not found"


class TestGetJobResultOwnership:
    """Test ownership and admin access for GET /api/jobs/<job_id>/result."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_403_when_not_owner_and_not_admin(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',  # id
            99,  # user_id (different from session user)
            'completed',  # status
            0,  # exit_code
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = False
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 403
        data = response.get_json()
        assert data["error"] == "Access denied"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_200_when_user_is_admin(self, mock_db, client, app):
        # First call: job query, second call: logs, third call: result
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',  # id
                99,  # user_id (different from session user)
                'completed',  # status
                0,  # exit_code
            ),
            [],  # logs
            None,  # result
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = True
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 200


class TestGetJobResultTerminalState:
    """Test 409 when job is not in terminal state."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_pending(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'pending',  # not terminal
            None,
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job is still in progress"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_running(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'running',  # not terminal
            None,
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job is still in progress"


class TestGetJobResultResponse:
    """Test response format for GET /api/jobs/<job_id>/result."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_full_result_with_logs(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            # First call: job query
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'completed',
                0,
            ),
            # Second call: logs query
            [
                ('stdout', 'Hello '),
                ('stdout', 'World\n'),
                ('stderr', 'warning: something\n'),
            ],
            # Third call: result query
            ({"key": "structured_output"},),
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 200
        data = response.get_json()
        assert data["exit_code"] == 0
        assert data["stdout"] == "Hello World\n"
        assert data["stderr"] == "warning: something\n"
        assert data["result"] == {"key": "structured_output"}

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_result_with_no_logs(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            # First call: job query
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'failed',
                1,
            ),
            # Second call: logs query (empty)
            [],
            # Third call: result query (none)
            None,
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 200
        data = response.get_json()
        assert data["exit_code"] == 1
        assert data["stdout"] == ""
        assert data["stderr"] == ""
        assert data["result"] is None

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_result_for_timeout_status(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'timeout',
                137,
            ),
            [('stderr', 'Process killed due to timeout\n')],
            None,
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 200
        data = response.get_json()
        assert data["exit_code"] == 137
        assert data["stdout"] == ""
        assert data["stderr"] == "Process killed due to timeout\n"
        assert data["result"] is None

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_result_for_cancelled_status(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'cancelled',
                None,
            ),
            [],
            None,
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 200
        data = response.get_json()
        assert data["exit_code"] is None
        assert data["stdout"] == ""
        assert data["stderr"] == ""
        assert data["result"] is None

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_500_on_db_failure(self, mock_db, client, app):
        mock_db.execute_query.side_effect = Exception("Connection refused")
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.get('/api/jobs/550e8400-e29b-41d4-a716-446655440000/result')
        assert response.status_code == 500


class TestCancelJobAuth:
    """Test authentication for POST /api/jobs/<job_id>/cancel."""

    def test_returns_401_when_not_authenticated(self, client):
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data


class TestCancelJobNotFound:
    """Test 404 handling for POST /api/jobs/<job_id>/cancel."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_404_when_job_does_not_exist(self, mock_db, client, app):
        mock_db.execute_query.return_value = None
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "Job not found"


class TestCancelJobOwnership:
    """Test ownership and admin access for POST /api/jobs/<job_id>/cancel."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_403_when_not_owner_and_not_admin(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',  # id
            99,  # user_id (different from session user)
            'pending',  # status
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = False
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 403
        data = response.get_json()
        assert data["error"] == "Access denied"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_200_when_user_is_admin(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',  # id
                99,  # user_id (different from session user)
                'pending',  # status
            ),
            None,  # update query result
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['is_admin'] = True
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 200


class TestCancelJobState:
    """Test cancellable state validation for POST /api/jobs/<job_id>/cancel."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_completed(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'completed',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job cannot be cancelled"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_failed(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'failed',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job cannot be cancelled"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_timeout(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'timeout',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job cannot be cancelled"

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_409_when_job_is_already_cancelled(self, mock_db, client, app):
        mock_db.execute_query.return_value = (
            '550e8400-e29b-41d4-a716-446655440000',
            42,
            'cancelled',
        )
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 409
        data = response.get_json()
        assert data["error"] == "Job cannot be cancelled"


class TestCancelJobSuccess:
    """Test successful job cancellation."""

    @patch('src.routes.serverless_routes.db_manager')
    def test_cancels_pending_job(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'pending',
            ),
            None,  # update query result
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 200
        data = response.get_json()
        assert data["message"] == "Job cancelled"
        assert data["job_id"] == "550e8400-e29b-41d4-a716-446655440000"

    @patch('src.routes.serverless_routes.db_manager')
    def test_cancels_running_job(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'running',
            ),
            None,  # update query result
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 200
        data = response.get_json()
        assert data["message"] == "Job cancelled"
        assert data["job_id"] == "550e8400-e29b-41d4-a716-446655440000"

    @patch('src.routes.serverless_routes.db_manager')
    def test_updates_status_to_cancelled_in_db(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'pending',
            ),
            None,  # update query result
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        # Verify the update call
        update_call = mock_db.execute_query.call_args_list[1]
        query = update_call[0][0]
        params = update_call[0][1]
        assert "UPDATE serverless_jobs SET status" in query
        assert params[0] == 'cancelled'
        assert params[1] == '550e8400-e29b-41d4-a716-446655440000'

    @patch('src.routes.serverless_routes.db_manager')
    def test_returns_500_on_db_failure_during_update(self, mock_db, client, app):
        mock_db.execute_query.side_effect = [
            (
                '550e8400-e29b-41d4-a716-446655440000',
                42,
                'pending',
            ),
            Exception("Connection refused"),  # update fails
        ]
        with client.session_transaction() as sess:
            sess['user_id'] = 42
        response = client.post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel')
        assert response.status_code == 500
