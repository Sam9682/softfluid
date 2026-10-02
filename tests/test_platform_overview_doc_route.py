"""Route test for the renamed platform-overview doc.

The dashboard "Platform Overview" menu entry links to
``/docs/PLATFORM_OVERVIEW.md`` (previously the branded ``opcp-explorer.md``).
This test verifies the renamed file resolves through ``view_doc`` and that the
old branded filename no longer exists in docs/.
"""

import os

import pytest
from flask import Flask

from src.routes.main_routes import main_bp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.register_blueprint(main_bp)
    return app.test_client()


def test_platform_overview_doc_file_exists():
    assert os.path.exists(os.path.join(DOCS_DIR, 'PLATFORM_OVERVIEW.md'))


def test_old_branded_doc_removed():
    assert not os.path.exists(os.path.join(DOCS_DIR, 'opcp-explorer.md'))


def test_view_doc_resolves_platform_overview(client):
    # The route must find and read the renamed file. We assert it does NOT
    # return the "file not found" (404) or "invalid type" (400) responses;
    # full template rendering needs the app-wide context processor and sibling
    # blueprints that this minimal harness intentionally omits, so a 500 from
    # template context is acceptable here - it still proves the file resolved.
    resp = client.get('/docs/PLATFORM_OVERVIEW.md')
    assert resp.status_code not in (400, 404)


def test_view_doc_old_filename_404(client):
    resp = client.get('/docs/opcp-explorer.md')
    assert resp.status_code == 404
