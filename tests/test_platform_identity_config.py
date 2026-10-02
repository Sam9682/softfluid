"""Tests for platform-identity resolution in src.config_postgres.

These assert the two-tier resolution contract for the configurable platform
identity feature:

  * When conf/deploy.ini is present and defines a key, the stored value wins.
  * When the key (or the file) is missing/unreadable, the canonical fallback
    is returned. The canonical fallback for the folder slug is ``opcp-explorer``
    - identical to gunicorn.conf.py, deployControlPlan.sh, start_app.sh and
    scripts/*.sh - so no entry point can silently diverge.
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock psycopg2 so importing database_postgres never touches a real driver
# (same guard the sibling extended-ports tests use).
_mock_psycopg2 = MagicMock()
_mock_psycopg2.pool = MagicMock()
sys.modules.setdefault("psycopg2", _mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", _mock_psycopg2.pool)

from src import config_postgres as cfg  # noqa: E402


def _fake_src_file(tmp):
    """A fake config_postgres.py path inside tmp so the config functions
    resolve conf/deploy.ini to tmp/conf/deploy.ini."""
    return os.path.join(tmp, 'src', 'config_postgres.py')


def _write_ini(tmp, body):
    conf_dir = os.path.join(tmp, 'conf')
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, 'deploy.ini'), 'w') as f:
        f.write(body)


def test_get_platform_folder_prefers_deploy_ini():
    with tempfile.TemporaryDirectory() as tmp:
        _write_ini(tmp, "PLTF_FOLDER=acme-cloud\nPLTF_NAME=Acme Cloud\n")
        with patch.object(cfg.os.path, 'abspath', return_value=_fake_src_file(tmp)):
            assert cfg.get_platform_folder() == 'acme-cloud'
            assert cfg.get_platform_name() == 'Acme Cloud'


def test_get_platform_folder_canonical_fallback_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        # No conf/deploy.ini written -> fallback path.
        with patch.object(cfg.os.path, 'abspath', return_value=_fake_src_file(tmp)):
            assert cfg.get_platform_folder() == 'opcp-explorer'


def test_folder_fallback_matches_database_manager_default():
    """The config fallback and the database manager module default must agree
    so every entry point resolves to the same canonical name."""
    from src import database_postgres as dbp
    assert dbp.NAME_OF_APPLICATION == 'opcp-explorer'
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(cfg.os.path, 'abspath', return_value=_fake_src_file(tmp)):
            assert cfg.get_platform_folder() == dbp.NAME_OF_APPLICATION
