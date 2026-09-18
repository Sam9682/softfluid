"""Serverless Docker Execution Service - Configuration"""

SERVERLESS_CONFIG = {
    'registry_whitelist': [
        'docker.io',
        'ghcr.io',
        'registry.example.com'
    ],
    'default_timeout': 300,
    'max_timeout': 3600,
    'default_memory_limit': '512m',
    'default_cpu_limit': '1',
    'max_concurrent_jobs': 100,
    'log_retention_days': 30,
    'poll_interval': 0.5,
    'container_stop_timeout': 10,
    'warm_pool_enabled': False,
    'warm_pool_size': 0,
}


def validate_image_registry(image: str, whitelist: list) -> bool:
    """Check if a Docker image comes from an approved registry.

    Parses the image reference to extract the registry hostname and validates
    it against the provided whitelist. Images without an explicit registry
    prefix (e.g. 'python:3.11' or 'library/python:3.11') are assumed to
    come from 'docker.io'.

    Args:
        image: Full or short image reference (e.g. 'ghcr.io/org/app:latest',
               'python:3.11', 'myregistry.com/image').
        whitelist: List of approved registry hostnames.

    Returns:
        True if the image's registry is in the whitelist, False otherwise.
    """
    # Strip tag or digest from the image reference
    # e.g. 'registry.example.com/myapp:latest' -> 'registry.example.com/myapp'
    image_ref = image.split("@")[0].split(":")[0]

    # Determine the registry from the first path component
    parts = image_ref.split("/")

    if len(parts) == 1:
        # Simple image name like 'python' — defaults to docker.io
        registry = "docker.io"
    elif len(parts) == 2 and "." not in parts[0] and ":" not in parts[0]:
        # User/image like 'library/python' — defaults to docker.io
        registry = "docker.io"
    else:
        # Explicit registry like 'ghcr.io/org/app' or 'registry.example.com/myapp'
        registry = parts[0]

    return registry in whitelist
