"""Integration tests for the /dashboard route URL fallback behavior.

Covers task 2.2: when the stored applications.url (a.url) is an Empty_Value
(None, empty string, or whitespace-only), the Dashboard_Route must fall back
to the per-user derived user_applications.url (ua.url) for app[2].

Conventions follow existing tests/ files: pytest, unittest.mock.patch on
db_manager, Flask test client with session_transaction() to set user_id.

Requirements: 4.2, 1.2
"""

from unittest.mock import patch

import pytest
from flask import Flask

from src.routes.main_routes import main_bp


# Dashboard SELECT column order (row shape the mock must return):
# a.id, a.name, ua.url, a.description, a.git_url, a.git_local_url,
# a.git_repo_size, a.docker_build_duration, a.docker_start_duration,
# a.docker_stop_duration, a.docker_ps_duration, d.swautomorph_url, a.url
DERIVED_URL = ':6109'  # ua.url


def _make_row(stored_url):
    """Build a dashboard result row where ua.url is DERIVED_URL and a.url is stored_url."""
    return (
        1,                # a.id
        'demo-app',       # a.name
        DERIVED_URL,      # ua.url (derived)
        'a description',  # a.description
        'git://url',      # a.git_url
        'git-local',      # a.git_local_url
        50,               # a.git_repo_size
        20,               # a.docker_build_duration
        30,               # a.docker_start_duration
        10,               # a.docker_stop_duration
        5,                # a.docker_ps_duration
        'http://sw',      # d.swautomorph_url
        stored_url,       # a.url (stored, trailing column)
    )


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.register_blueprint(main_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize('stored_url', [None, '', '   '])
@patch('src.routes.main_routes.render_template')
@patch('src.routes.main_routes.db_manager')
def test_dashboard_falls_back_to_derived_url_when_empty(
    mock_db, mock_render, stored_url, client
):
    """When a.url is an Empty_Value, app[2] equals the derived ua.url. (Req 4.2, 1.2)"""
    # First execute_query call: username lookup (fetch_one).
    # Second call: applications query (fetch_all) -> list of rows.
    mock_db.execute_query.side_effect = [
        ('alice',),
        [_make_row(stored_url)],
    ]
    mock_render.return_value = 'OK'

    with client.session_transaction() as sess:
        sess['user_id'] = 1

    response = client.get('/dashboard')
    assert response.status_code == 200

    # render_template was called with the applications list; inspect app[2].
    mock_render.assert_called_once()
    applications = mock_render.call_args.kwargs['applications']
    assert len(applications) == 1
    app_tuple = applications[0]
    assert app_tuple[2] == DERIVED_URL
