"""Tests for the configurable systemd unit (scripts/controlplan_service.py).

The control-plan service unit must derive User= and the /home/<user>/<folder>
paths from LINUX_USER_INSTALLATION and PLTF_FOLDER in conf/deploy.ini, with
canonical fallbacks (psmc / agentic-ai-pltf) when the file/keys are missing. The
committed artifact must stay in sync with those config values.
"""

import importlib.util
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD_PATH = os.path.join(REPO_ROOT, 'scripts', 'controlplan_service.py')


def _load():
    spec = importlib.util.spec_from_file_location('controlplan_service', MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cps = _load()


def test_render_uses_configured_identity():
    unit = cps.render_controlplan_unit('acmeuser', 'acme-cloud')
    assert 'User=acmeuser' in unit
    assert 'WorkingDirectory=/home/acmeuser/acme-cloud' in unit
    assert 'ExecStart=/home/acmeuser/acme-cloud/scripts/start_swautomorph_controlplan.sh' in unit
    assert 'ExecStop=/home/acmeuser/acme-cloud/scripts/stop_swautomorph_controlplan.sh' in unit
    # No stray platform-identity literals from a different customer.
    assert 'agentic-ai-pltf' not in unit
    assert 'psmc' not in unit


def test_resolve_prefers_deploy_ini():
    with tempfile.TemporaryDirectory() as tmp:
        ini = os.path.join(tmp, 'deploy.ini')
        with open(ini, 'w') as f:
            f.write("LINUX_USER_INSTALLATION=acmeuser\nPLTF_FOLDER=acme-cloud\n")
        user, folder = cps.resolve_service_identity(config_path=ini)
        assert user == 'acmeuser'
        assert folder == 'acme-cloud'


def test_resolve_canonical_fallback_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, 'does-not-exist.ini')
        user, folder = cps.resolve_service_identity(config_path=missing)
        assert user == 'psmc'
        assert folder == 'agentic-ai-pltf'


def test_committed_artifact_matches_current_config():
    """The checked-in unit must equal what the generator produces from the
    repo's own conf/deploy.ini - i.e. it was regenerated, not hand-edited."""
    user, folder = cps.resolve_service_identity()
    expected = cps.render_controlplan_unit(user, folder)
    artifact = os.path.join(REPO_ROOT, 'scripts', cps.SERVICE_FILENAME)
    with open(artifact, 'r') as f:
        assert f.read() == expected
