"""API routes for App Orchestrator"""
from flask import Blueprint, request, jsonify, session
from ..database_postgres import db_manager
import json
import subprocess
import os
import logging
from .. import config_postgres

# Configure logging for API activities
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# File handler
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log_file = os.path.join(PROJECT_ROOT, 'logs', 'orchestrator_routes.log')
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

orchestrator_bp = Blueprint('orchestrator', __name__, url_prefix='/api/orchestrator')

def require_auth():
    """Check if user is authenticated"""
    if 'user_id' not in session:
        return False
    return True

def require_admin():
    """Check if user is admin"""
    if not require_auth():
        return False
    
    user = db_manager.execute_query(
        'SELECT username FROM users WHERE id = ?', 
        (session['user_id'],), fetch_one=True
    )
    
    return user and user[0] == 'admin'

@orchestrator_bp.route('/user-applications', methods=['GET'])
def get_user_applications():
    """Get applications assigned to current user"""
    if not require_auth():
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        user_id = session['user_id']
        with db_manager.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT a.name
                FROM applications a
                JOIN user_applications ua ON a.id = ua.application_id
                WHERE ua.user_id = %s
                ORDER BY a.name
            ''', (user_id,))
            apps = [{'name': row[0]} for row in cursor.fetchall()]
        return jsonify(apps)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/git-urls', methods=['GET'])
def get_git_urls():
    """Get all git URLs from deployments table"""
    if not require_auth():
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        user_id = session['user_id']
        urls = set()
        
        with db_manager.get_db_connection() as conn:
            cursor = conn.cursor()
            # Get gitea_branch_url from deployments
            cursor.execute('''
                SELECT DISTINCT gitea_branch_url
                FROM deployments
                WHERE user_id = %s AND gitea_branch_url IS NOT NULL
            ''', (user_id,))
            for row in cursor.fetchall():
                if row[0]:
                    urls.add(row[0])
            
            # Get modification_history from deployments
            cursor.execute('''
                SELECT modification_history
                FROM deployments
                WHERE user_id = %s AND modification_history IS NOT NULL
            ''', (user_id,))
            for row in cursor.fetchall():
                if row[0]:
                    try:
                        history = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                        if isinstance(history, list):
                            for entry in history:
                                if isinstance(entry, dict) and 'gitea_url' in entry:
                                    urls.add(entry['gitea_url'])
                    except:
                        pass
        
        return jsonify(sorted(list(urls)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services', methods=['GET'])
def list_services():
    """List all services and their status"""
    if not require_auth():
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        from ..orchestrator import orchestrator
        user_id = session.get('user_id')
        services = orchestrator.get_service_status(user_id=user_id)
        return jsonify(services)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services/<service_name>', methods=['GET'])
def get_service(service_name):
    """Get specific service status"""
    if not require_auth():
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        from ..orchestrator import orchestrator
        user_id = session.get('user_id')
        services = orchestrator.get_service_status(service_name, user_id)
        if not services:
            return jsonify({'error': 'Service not found'}), 404
        return jsonify(services[0])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services', methods=['POST'])
def create_service():
    """Create a new service"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        data = request.get_json()
        logger.info(f"Received service creation request: {data}")
        
        # Validate required fields
        required_fields = ['name', 'image']
        for field in required_fields:
            if field not in data:
                logger.error(f"Missing required field: {field}")
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Extract parameters
        name = data['name']
        image = data['image']  # This is now git_url
        user_id = session['user_id']
        desired_replicas = data.get('desired_replicas', 1)
        ports = data.get('ports', {})
        environment = data.get('environment', {})
        volumes = data.get('volumes', [])
        health_check_path = data.get('health_check_path', '/health')
        
        logger.info(f"Creating service '{name}' for user_id={user_id}, replicas={desired_replicas}")
        
        # Create service in database
        orchestrator.create_service(
            name=name,
            image=image,
            user_id=user_id,
            desired_replicas=desired_replicas,
            ports=ports,
            environment=environment,
            volumes=volumes,
            health_check_path=health_check_path
        )
        
        logger.info(f"Service '{name}' created in database")
        
        # Get user info
        with db_manager.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT username FROM users WHERE id = %s', (user_id,))
            username = cursor.fetchone()[0]
            logger.info(f"Username: {username}")
            
            # Get server assignment
            cursor.execute('''
                SELECT s.server_ip, s.id
                FROM servers s
                LEFT JOIN instances i ON s.id = i.server_id AND i.status = 'running'
                WHERE s.server_status IN ('STAND_BY', 'ACTIVE')
                  AND s.server_capacity_appli_max IS NOT NULL
                GROUP BY s.id, s.server_capacity_appli_max
                HAVING COUNT(i.id) < s.server_capacity_appli_max
                ORDER BY COUNT(i.id) ASC
                LIMIT 1
            ''')
            server_result = cursor.fetchone()
            
            if not server_result:
                logger.error("No available server found")
                return jsonify({'error': 'No available server'}), 500
            
            server_ip, server_id = server_result
            logger.info(f"Selected server: {server_ip} (id={server_id})")
        
        # Deploy application using deployApp.sh
        try:
            app_dir = f'/home/ubuntu/deployments/{username}/{name.lower().replace(" ", "-")}'
            deploy_script = f'{app_dir}/deployApp.sh'
            
            logger.info(f"App directory: {app_dir}")
            logger.info(f"Deploy script: {deploy_script}")
            
            # SSH command with StrictHostKeyChecking disabled
            ssh_cmd = [
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                f'ubuntu@{server_ip}',
                f'cd {app_dir} && {deploy_script} start {user_id} {username}'
            ]
            
            logger.info(f"Executing SSH command: {' '.join(ssh_cmd)}")
            
            # Execute deployment
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            logger.info(f"SSH command return code: {result.returncode}")
            logger.info(f"SSH stdout: {result.stdout}")
            if result.stderr:
                logger.warning(f"SSH stderr: {result.stderr}")
            
            if result.returncode != 0:
                logger.error(f"Deployment failed for service '{name}'")
                return jsonify({
                    'message': f'Service {name} created but deployment failed',
                    'error': result.stderr
                }), 500
            
            logger.info(f"Service '{name}' deployed successfully")
            
            # Create instance record after successful deployment
            try:
                with db_manager.get_db_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Find available port for the instance
                    cursor.execute('''
                        SELECT port FROM instances 
                        WHERE server_id = %s AND status = 'running' AND port IS NOT NULL
                        ORDER BY port
                    ''', (server_id,))
                    used_ports = {row[0] for row in cursor.fetchall() if row[0]}
                    port = 8000
                    while port in used_ports:
                        port += 1
                    
                    # Create instance record for each desired replica
                    for i in range(desired_replicas):
                        instance_id = f"{name}-replica-{i+1}"
                        cursor.execute('''
                            INSERT INTO instances 
                            (service_name, instance_id, server_id, status, port, health_status)
                            VALUES (%s, %s, %s, 'running', %s, 'healthy')
                        ''', (name, instance_id, server_id, port + i))
                    
                    conn.commit()
                    logger.info(f"Created {desired_replicas} instance record(s) for service '{name}'")
                    
                    # Record billing activity for START action
                    from .billing_routes import record_billing_activity
                    record_billing_activity(user_id, name, 'START')
                    logger.info(f"Recorded billing activity for service '{name}' START")
                    
            except Exception as e:
                logger.error(f"Failed to create instance records: {str(e)}")
            
        except Exception as e:
            logger.error(f"Exception during deployment: {str(e)}")
            return jsonify({
                'message': f'Service {name} created but deployment failed',
                'error': str(e)
            }), 500
        
        return jsonify({'message': f'Service {name} created and deployed successfully'}), 201
        
    except Exception as e:
        logger.error(f"Exception in create_service: {str(e)}")
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services/<service_name>', methods=['PUT'])
def update_service(service_name):
    """Update service configuration"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        data = request.get_json()
        
        # For now, only support scaling
        if 'desired_replicas' in data:
            replicas = data['desired_replicas']
            if not isinstance(replicas, int) or replicas < 0:
                return jsonify({'error': 'desired_replicas must be a non-negative integer'}), 400
            
            orchestrator.scale_service(service_name, replicas)
            return jsonify({'message': f'Service {service_name} scaled to {replicas} replicas'})
        
        return jsonify({'error': 'No valid update parameters provided'}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services/<service_name>/scale', methods=['POST'])
def scale_service(service_name):
    """Scale a service"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        data = request.get_json()
        replicas = data.get('replicas')
        user_id = session['user_id']
        
        if replicas is None:
            return jsonify({'error': 'Missing replicas parameter'}), 400
        
        if not isinstance(replicas, int) or replicas < 0:
            return jsonify({'error': 'replicas must be a non-negative integer'}), 400
        
        orchestrator.scale_service(service_name, user_id, replicas)
        return jsonify({'message': f'Service {service_name} scaled to {replicas} replicas'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/services/<service_name>', methods=['DELETE'])
def delete_service(service_name):
    """Delete a service and all its instances"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        user_id = session['user_id']
        orchestrator.delete_service(service_name, user_id)
        return jsonify({'message': f'Service {service_name} deleted successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/health-check', methods=['POST'])
def trigger_health_check():
    """Manually trigger health check for all instances"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        orchestrator.health_check_instances()
        return jsonify({'message': 'Health check completed'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/nginx/config', methods=['GET'])
def get_nginx_config():
    """Get generated Nginx upstream configuration"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        config = orchestrator.generate_nginx_config()
        return jsonify({'config': config})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/nginx/reload', methods=['POST'])
def reload_nginx():
    """Reload Nginx configuration"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        success = orchestrator.reload_nginx()
        if success:
            return jsonify({'message': 'Nginx reloaded successfully'})
        else:
            return jsonify({'error': 'Failed to reload Nginx'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/reconcile', methods=['POST'])
def trigger_reconciliation():
    """Manually trigger reconciliation for all services"""
    if not require_admin():
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        from ..orchestrator import orchestrator
        
        with db_manager.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT name, user_id FROM services')
            services = cursor.fetchall()
            
            for service in services:
                orchestrator._reconcile_service(service[0], service[1])
        
        return jsonify({'message': 'Reconciliation completed'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@orchestrator_bp.route('/status', methods=['GET'])
def orchestrator_status():
    """Get orchestrator status and statistics"""
    if not require_auth():
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        with db_manager.get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Count services
            cursor.execute('SELECT COUNT(*) FROM services')
            total_services = cursor.fetchone()[0]
            
            # Count instances by status
            cursor.execute('''
                SELECT status, COUNT(*) 
                FROM instances 
                GROUP BY status
            ''')
            instance_stats = dict(cursor.fetchall())
            
            # Count healthy vs unhealthy instances
            cursor.execute('''
                SELECT health_status, COUNT(*) 
                FROM instances 
                WHERE status = 'running'
                GROUP BY health_status
            ''')
            health_stats = dict(cursor.fetchall())
            
            # Server utilization
            cursor.execute('''
                SELECT s.server_name, s.server_capacity_appli_max, COUNT(i.id) as current_instances
                FROM servers s
                LEFT JOIN instances i ON s.id = i.server_id AND i.status = 'running'
                GROUP BY s.id, s.server_name, s.server_capacity_appli_max
            ''')
            server_stats = []
            for row in cursor.fetchall():
                server_stats.append({
                    'name': row[0],
                    'capacity': row[1],
                    'current_instances': row[2],
                    'utilization': round((row[2] / row[1]) * 100, 2) if row[1] > 0 else 0
                })
        
        from ..orchestrator import orchestrator
        return jsonify({
            'total_services': total_services,
            'instance_stats': instance_stats,
            'health_stats': health_stats,
            'server_stats': server_stats,
            'reconciliation_running': orchestrator._running
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500