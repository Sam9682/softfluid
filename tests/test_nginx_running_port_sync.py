"""Tests for the nginx running-port sync bugfix.

Root cause of the bug: ``user_applications.url`` / ``http_port`` / ``https_port``
can go stale relative to the actually running docker containers when an app is
redeployed on a different port. The sync faithfully rendered the stale DB value,
so nginx kept proxying to the old (wrong) port — e.g. it wrote
``proxy_pass http://host:6112`` while the app was really serving on ``6137``.

The fix makes ``src.nginx_manager`` discover the real ports from Docker and
prefer them, falling back to the stored URL only when discovery yields nothing.
It also hardens the dynamic-block clearing so it can never emit a malformed
block (a ``location`` with no ``proxy_pass``).

These tests exercise the pure helpers and the block generation/stripping logic
without touching Docker or nginx.
"""

import json
from unittest.mock import patch

from src import nginx_manager as nm


# ---------------------------------------------------------------------------
# Port-string parsing helpers
# ---------------------------------------------------------------------------

def test_published_port_for_target_prefers_ipv4():
    ports = "80/tcp, 0.0.0.0:6136->443/tcp, [::]:6136->443/tcp"
    assert nm._published_port_for_target(ports, 443) == 6136


def test_published_port_for_target_missing_returns_none():
    ports = "0.0.0.0:6137->5000/tcp"
    assert nm._published_port_for_target(ports, 443) is None


def test_first_published_port_app_agnostic_internal_port():
    # app containers use different internal ports (5000, 8000, ...)
    assert nm._first_published_port("0.0.0.0:6125->8000/tcp, [::]:6125->8000/tcp") == 6125
    assert nm._first_published_port("0.0.0.0:6137->5000/tcp") == 6137


def test_component_from_name():
    assert nm._component_from_name("opcp-openstack-first-steps-nginx-1-6136") == nm.FRONTEND_COMPONENT
    assert nm._component_from_name("opcp-openstack-first-steps-app-1-6137") == nm.BACKEND_COMPONENT
    assert nm._component_from_name("ai-shai-web-interface-postgres-1-5432") == "postgres"
    assert nm._component_from_name("weird-name") is None


def test_user_app_from_working_dir():
    wd = "/home/ubuntu/deployments/admin/opcp-openstack-first-steps"
    assert nm._user_app_from_working_dir(wd) == ("admin", "opcp-openstack-first-steps")
    assert nm._user_app_from_working_dir("/home/ubuntu/somewhere/else") is None
    assert nm._user_app_from_working_dir(None) is None


def test_label_value_extraction():
    labels = (
        "com.docker.compose.project=x,"
        "com.docker.compose.project.working_dir=/home/ubuntu/deployments/admin/app,"
        "other=1"
    )
    assert nm._label_value(labels, "com.docker.compose.project.working_dir") == \
        "/home/ubuntu/deployments/admin/app"
    assert nm._label_value(labels, "missing") is None


def test_host_and_port_from_url():
    assert nm._host_and_port_from_url("https://opcp-psmc.com:6113") == ("opcp-psmc.com", 6113)
    assert nm._host_and_port_from_url("") == (None, None)


# ---------------------------------------------------------------------------
# Upstream selection: real running port wins over stale URL
# ---------------------------------------------------------------------------

def test_build_http_upstream_prefers_real_port():
    # Stale URL says 6113 (https) -> would derive 6112, but the container
    # actually serves HTTP on 6137. The real port must win.
    assert nm.build_http_upstream("https://opcp-psmc.com:6113", 6137) == \
        "http://opcp-psmc.com:6137"


def test_build_http_upstream_falls_back_to_url_when_no_real_port():
    assert nm.build_http_upstream("https://opcp-psmc.com:6113", None) == \
        "http://opcp-psmc.com:6112"


def test_generate_location_block_uses_real_port():
    block = nm.generate_location_block(
        "admin", "opcp-openstack-first-steps", "x",
        "https://opcp-psmc.com:6113", real_http_port=6137,
    )
    assert "proxy_pass http://opcp-psmc.com:6137/;" in block
    # The stale port must NOT appear anywhere in the block.
    assert "6112" not in block
    assert "6113" not in block
    # Marker and closing brace present.
    assert "# Dynamic location for user admin - opcp-openstack-first-steps" in block
    assert block.count("{") == block.count("}")


# ---------------------------------------------------------------------------
# discover_running_ports end-to-end (docker ps mocked)
# ---------------------------------------------------------------------------

def _fake_docker_ps(*containers):
    """Build a fake subprocess.run result for `docker ps --format {{json .}}`."""
    class R:
        returncode = 0
        stderr = ""
        stdout = "\n".join(json.dumps(c) for c in containers)
    return R()


def test_discover_running_ports_maps_app_and_sidecar():
    wd = "/home/ubuntu/deployments/admin/opcp-openstack-first-steps"
    label = f"com.docker.compose.project.working_dir={wd}"
    containers = [
        {"Names": "opcp-openstack-first-steps-nginx-1-6136",
         "Ports": "80/tcp, 0.0.0.0:6136->443/tcp, [::]:6136->443/tcp",
         "Labels": label},
        {"Names": "opcp-openstack-first-steps-app-1-6137",
         "Ports": "0.0.0.0:6137->5000/tcp, [::]:6137->5000/tcp",
         "Labels": label},
    ]
    with patch.object(nm.subprocess, "run", return_value=_fake_docker_ps(*containers)):
        ports = nm.discover_running_ports()
    assert ports[("admin", "opcp-openstack-first-steps")] == {"https": 6136, "http": 6137}


def test_discover_running_ports_handles_varied_internal_port():
    wd = "/home/ubuntu/deployments/mdc/ai-shai-web-interface"
    label = f"com.docker.compose.project.working_dir={wd}"
    containers = [
        {"Names": "ai-shai-web-interface-nginx-25-8524",
         "Ports": "80/tcp, 0.0.0.0:8524->443/tcp", "Labels": label},
        {"Names": "ai-shai-web-interface-app-25-8525",
         "Ports": "0.0.0.0:8525->8000/tcp", "Labels": label},
        {"Names": "ai-shai-web-interface-postgres-25-5432",
         "Ports": "0.0.0.0:8528->5432/tcp", "Labels": label},
    ]
    with patch.object(nm.subprocess, "run", return_value=_fake_docker_ps(*containers)):
        ports = nm.discover_running_ports()
    # http comes from the -app- container (8525), not postgres and not derived.
    assert ports[("mdc", "ai-shai-web-interface")] == {"https": 8524, "http": 8525}


def test_discover_running_ports_docker_unavailable_returns_empty():
    with patch.object(nm.subprocess, "run", side_effect=FileNotFoundError()):
        assert nm.discover_running_ports() == {}


# ---------------------------------------------------------------------------
# Robust dynamic-block stripping
# ---------------------------------------------------------------------------

BASE_CONFIG = """server {
    listen 443 ssl;

    # Dynamic location for user admin - opcp-openstack-first-steps
    location /admin/opcp-openstack-first-steps/ {
        proxy_pass http://opcp-psmc.com:6112/;
        proxy_set_header Host $host;
        proxy_set_header Connection "upgrade";
        sub_filter 'href="/' 'href="/admin/opcp-openstack-first-steps/';
    }

    # Dynamic location for user demo - ai-staticwebsite
    location /demo/ai-staticwebsite/ {
        proxy_pass http://opcp-psmc.com:6204/;
    }

    location / {
        proxy_pass http://localhost:5000;
    }
}
"""


def test_strip_dynamic_blocks_removes_all():
    stripped = nm._strip_dynamic_blocks(BASE_CONFIG)
    assert "# Dynamic location for user" not in stripped
    # The real root location must be preserved.
    assert "location / {" in stripped
    assert "proxy_pass http://localhost:5000;" in stripped
    # Braces stay balanced (no malformed leftovers).
    assert stripped.count("{") == stripped.count("}")


def test_strip_dynamic_blocks_removes_only_target():
    stripped = nm._strip_dynamic_blocks(
        BASE_CONFIG, only_user="admin", only_app="opcp-openstack-first-steps"
    )
    assert "opcp-openstack-first-steps" not in stripped
    # The other dynamic block is kept.
    assert "# Dynamic location for user demo - ai-staticwebsite" in stripped
    assert stripped.count("{") == stripped.count("}")


def test_strip_then_regenerate_is_wellformed():
    """Clear + regenerate must never yield a location without a proxy_pass."""
    stripped = nm._strip_dynamic_blocks(BASE_CONFIG)
    block = nm.generate_location_block(
        "admin", "opcp-openstack-first-steps", "x",
        "https://opcp-psmc.com:6113", real_http_port=6137,
    )
    idx = stripped.find("    location / {")
    rebuilt = stripped[:idx] + block + "\n" + stripped[idx:]
    # Every dynamic location block has a proxy_pass.
    for chunk in rebuilt.split("# Dynamic location for user")[1:]:
        assert "proxy_pass" in chunk
    assert rebuilt.count("{") == rebuilt.count("}")
    assert "proxy_pass http://opcp-psmc.com:6137/;" in rebuilt


# ---------------------------------------------------------------------------
# sync_all_locations wiring (DB + docker mocked)
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self, apps):
        self._apps = apps
        self.updates = []

    def execute_query(self, query, params=None, fetch_one=False, fetch_all=False):
        q = " ".join(query.lower().split())
        if q.startswith("select") and "from user_applications" in q:
            return list(self._apps)
        if q.startswith("update user_applications"):
            self.updates.append(params)
            return 1
        return None


def test_sync_all_locations_uses_real_port_and_reconciles():
    apps = [("admin", "opcp-openstack-first-steps", "https://opcp-psmc.com:6113")]
    fake_db = FakeDB(apps)

    running = {("admin", "opcp-openstack-first-steps"): {"https": 6136, "http": 6137}}

    written = {}

    def fake_write(config):
        written["config"] = config
        return True

    with patch.object(nm, "read_nginx_config", return_value=BASE_CONFIG), \
         patch.object(nm, "discover_running_ports", return_value=running), \
         patch.object(nm, "write_nginx_config", side_effect=fake_write), \
         patch.object(nm, "reload_nginx", return_value=True):
        assert nm.sync_all_locations(fake_db) is True

    cfg = written["config"]
    # nginx now points at the real running app port, not the stale 6112.
    assert "proxy_pass http://opcp-psmc.com:6137/;" in cfg
    assert "6112" not in cfg
    # The DB was reconciled with the real ports.
    assert fake_db.updates, "expected the stale DB row to be reconciled"
    params = fake_db.updates[0]
    assert "https://opcp-psmc.com:6136" in params  # new url
    assert 6137 in params and 6136 in params       # new http/https ports


def test_sync_all_locations_falls_back_to_url_without_discovery():
    apps = [("admin", "opcp-serverless-brik", "https://opcp-psmc.com:6133")]
    fake_db = FakeDB(apps)

    written = {}
    with patch.object(nm, "read_nginx_config", return_value=BASE_CONFIG), \
         patch.object(nm, "discover_running_ports", return_value={}), \
         patch.object(nm, "write_nginx_config",
                      side_effect=lambda c: written.setdefault("config", c) or True), \
         patch.object(nm, "reload_nginx", return_value=True):
        assert nm.sync_all_locations(fake_db) is True

    # No discovery -> derive from URL (6133 https -> 6132 http). No DB update.
    assert "proxy_pass http://opcp-psmc.com:6132/;" in written["config"]
    assert fake_db.updates == []