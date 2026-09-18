"""Serverless Docker Execution API routes for job submission, status, results, and management."""
from flask import Blueprint, request, jsonify, session
from ..database_postgres import db_manager
from ..serverless.config import validate_image_registry, SERVERLESS_CONFIG
import json
import os
import logging

# Configure logging for serverless API activities
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Remove existing handlers to avoid duplicates
if logger.handlers:
    logger.handlers.clear()

# File handler
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log_file = os.path.join(PROJECT_ROOT, 'logs', 'serverless_routes.log')
os.makedirs(os.path.dirname(log_file), exist_ok=True)
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(file_formatter)
logger.addHandler(console_handler)

# Prevent propagation to avoid duplicate logs
logger.propagate = False

serverless_bp = Blueprint('serverless', __name__, url_prefix='/api')


@serverless_bp.route('/jobs', methods=['POST'])
def submit_job():
    """Submit a new serverless Docker execution job."""
    # Auth check
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    # Parse request payload
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    # Validate required fields
    image = data.get('image')
    command = data.get('command')

    if not image or not isinstance(image, str):
        return jsonify({"error": "Field 'image' is required and must be a string"}), 400

    if not command or not isinstance(command, list):
        return jsonify({"error": "Field 'command' is required and must be a list"}), 400

    # Validate optional fields
    env = data.get('env', {})
    if not isinstance(env, dict):
        return jsonify({"error": "Field 'env' must be a dictionary"}), 400

    timeout = data.get('timeout', SERVERLESS_CONFIG['default_timeout'])
    if not isinstance(timeout, int):
        return jsonify({"error": "Field 'timeout' must be an integer"}), 400

    # Validate timeout range (1-3600)
    if timeout < 1 or timeout > 3600:
        return jsonify({"error": "Field 'timeout' must be between 1 and 3600 seconds"}), 400

    # Validate image against registry whitelist
    whitelist = SERVERLESS_CONFIG['registry_whitelist']
    if not validate_image_registry(image, whitelist):
        return jsonify({"error": "Image registry is not approved"}), 403

    # Insert job record into database
    try:
        result = db_manager.execute_query(
            '''INSERT INTO serverless_jobs (user_id, image, command, environment, timeout_seconds, status)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id''',
            (user_id, image, json.dumps(command), json.dumps(env), timeout, 'pending'),
            fetch_one=True
        )
        job_id = str(result[0])
    except Exception as e:
        logger.error(f"Failed to insert job record: {e}")
        return jsonify({"error": "Failed to create job"}), 500

    logger.info(f"Job submitted: job_id={job_id}, user_id={user_id}, image={image}")

    return jsonify({"job_id": job_id}), 201


@serverless_bp.route('/jobs/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get the status and metadata for a specific job."""
    # Auth check
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    # Query job by ID
    try:
        job = db_manager.execute_query(
            '''SELECT id, user_id, image, status, created_at, started_at, completed_at, exit_code, worker_id
               FROM serverless_jobs WHERE id = %s''',
            (job_id,),
            fetch_one=True
        )
    except Exception as e:
        logger.error(f"Failed to query job {job_id}: {e}")
        return jsonify({"error": "Failed to retrieve job"}), 500

    # 404 if job not found
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Ownership check: must be job owner or admin
    job_user_id = job[1]
    is_admin = session.get('is_admin', False)
    if job_user_id != user_id and not is_admin:
        return jsonify({"error": "Access denied"}), 403

    # Build response
    response_data = {
        "job_id": str(job[0]),
        "status": job[3],
        "image": job[2],
        "created_at": job[4].isoformat() + 'Z' if job[4] else None,
        "started_at": job[5].isoformat() + 'Z' if job[5] else None,
        "completed_at": job[6].isoformat() + 'Z' if job[6] else None,
        "exit_code": job[7],
        "worker_id": job[8],
    }

    logger.info(f"Job status retrieved: job_id={job_id}, user_id={user_id}")

    return jsonify(response_data), 200


@serverless_bp.route('/jobs/<job_id>/result', methods=['GET'])
def get_job_result(job_id):
    """Get the result of a completed job including stdout, stderr, and structured result."""
    # Auth check
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    # Query job by ID
    try:
        job = db_manager.execute_query(
            '''SELECT id, user_id, status, exit_code
               FROM serverless_jobs WHERE id = %s''',
            (job_id,),
            fetch_one=True
        )
    except Exception as e:
        logger.error(f"Failed to query job {job_id}: {e}")
        return jsonify({"error": "Failed to retrieve job"}), 500

    # 404 if job not found
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Ownership check: must be job owner or admin
    job_user_id = job[1]
    is_admin = session.get('is_admin', False)
    if job_user_id != user_id and not is_admin:
        return jsonify({"error": "Access denied"}), 403

    # Verify job is in terminal state
    terminal_states = ('completed', 'failed', 'timeout', 'cancelled')
    job_status = job[2]
    if job_status not in terminal_states:
        return jsonify({"error": "Job is still in progress"}), 409

    # Query job logs (stdout and stderr) ordered by created_at
    try:
        logs = db_manager.execute_query(
            '''SELECT stream, content FROM serverless_job_logs
               WHERE job_id = %s ORDER BY created_at ASC''',
            (job_id,),
            fetch_all=True
        )
    except Exception as e:
        logger.error(f"Failed to query job logs for {job_id}: {e}")
        logs = []

    # Concatenate stdout and stderr separately
    stdout_parts = []
    stderr_parts = []
    if logs:
        for log_entry in logs:
            stream = log_entry[0]
            content = log_entry[1]
            if stream == 'stdout':
                stdout_parts.append(content)
            elif stream == 'stderr':
                stderr_parts.append(content)

    # Query job result (JSONB)
    try:
        result_row = db_manager.execute_query(
            '''SELECT result FROM serverless_job_results WHERE job_id = %s''',
            (job_id,),
            fetch_one=True
        )
    except Exception as e:
        logger.error(f"Failed to query job result for {job_id}: {e}")
        result_row = None

    # Build response
    response_data = {
        "exit_code": job[3],
        "stdout": ''.join(stdout_parts),
        "stderr": ''.join(stderr_parts),
        "result": result_row[0] if result_row else None,
    }

    logger.info(f"Job result retrieved: job_id={job_id}, user_id={user_id}")

    return jsonify(response_data), 200


@serverless_bp.route('/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    """Cancel a pending or running job."""
    # Auth check
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    # Query job by ID
    try:
        job = db_manager.execute_query(
            '''SELECT id, user_id, status
               FROM serverless_jobs WHERE id = %s''',
            (job_id,),
            fetch_one=True
        )
    except Exception as e:
        logger.error(f"Failed to query job {job_id}: {e}")
        return jsonify({"error": "Failed to retrieve job"}), 500

    # 404 if job not found
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Ownership check: must be job owner or admin
    job_user_id = job[1]
    is_admin = session.get('is_admin', False)
    if job_user_id != user_id and not is_admin:
        return jsonify({"error": "Access denied"}), 403

    # Check if job is in a cancellable state
    job_status = job[2]
    cancellable_states = ('pending', 'running')
    if job_status not in cancellable_states:
        return jsonify({"error": "Job cannot be cancelled"}), 409

    # Update job status to cancelled
    try:
        db_manager.execute_query(
            '''UPDATE serverless_jobs SET status = %s, completed_at = CURRENT_TIMESTAMP
               WHERE id = %s''',
            ('cancelled', job_id)
        )
    except Exception as e:
        logger.error(f"Failed to cancel job {job_id}: {e}")
        return jsonify({"error": "Failed to cancel job"}), 500

    logger.info(f"Job cancelled: job_id={job_id}, user_id={user_id}")

    return jsonify({"message": "Job cancelled", "job_id": str(job[0])}), 200
