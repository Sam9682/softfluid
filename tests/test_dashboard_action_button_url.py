"""Example/integration tests for the /dashboard route action-button URL.

Spec: app-action-button-uses-stored-url-port

These tests exercise the `/dashboard` route in `src/routes/main_routes.py` with a
mocked `db_manager`. The route builds a per-application tuple consumed positionally
by `templates/dashboard.html`; index `app[2]` feeds the "Open Application" action
button `href`.

The dashboard SELECT returns rows in this column order (the shape the mock returns):

    a.id, a.name, ua.url, a.description, a.git_url, a.git_local_url,
    a.git_repo_size, a.docker_build_duration, a.docker_start_duration,
    a.docker_stop_duration, a.docker_ps_duration, d.swautomorph_url, a.url

`a.url` (the trailing column) is the Stored_URL; `ua.url` (index 2 of the row) is
the Derived_URL. The route sets `app[2]` to the stored URL when it is non-empty,
otherwise to the derived URL.
"""

from unittest.mock import patch

import pytest
from flask import Flask

from src.routes.main_routes import main_bp


# Row column order as returned by the dashboard SELECT (trailing column is a.url).
def _make_row(derived_url, stored_url):
    return (
        1,                       # a.id
        'ai-shai',               # a.name
        derived_url,             # ua.url  (Derived_URL)
        'desc',                  # a.description
        'https://git.example/repo.git',  # a.git_url
        '/srv/git/repo',         # a.git_local_url
        50,                      # a.git_repo_size
        120,                     # a.docker_build_duration
        30,                      # a.docker_start_duration
        10,                      # a.docker_stop_duration
        5,                       # a.docker_ps_duration
        'https://swautomorph.example/app',  # d.swautomorph_url
        stored_url,              # a.url   (Stored_URL, trailing column)
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


def test_dashboard_uses_stored_url_when_set(client):
    """When a.url (stored) is non-empty, app[2] equals the stored value exactly,
    not the derived ua.url value.

    Stored a.url = ':6124', derived ua.url = ':6109' -> app[2] == ':6124'.

    Validates: Requirements 4.1, 1.1, 1.3
    """
    derived_url = ':6109'
    stored_url = ':6124'

    # First execute_query call -> username lookup (fetch_one).
    # Second execute_query call -> applications rows (fetch_all).
    def execute_query(query, params=None, fetch_one=False, fetch_all=False):
        if 'FROM users' in query:
            return ('admin',)
        return [_make_row(derived_url, stored_url)]

    captured = {}

    def fake_render_template(template_name, **context):
        captured['template'] = template_name
        captured['applications'] = context.get('applications')
        return 'OK'

    with patch('src.routes.main_routes.db_manager') as mock_db, \
            patch('src.routes.main_routes.render_template', side_effect=fake_render_template):
        mock_db.execute_query.side_effect = execute_query
        with client.session_transaction() as sess:
            sess['user_id'] = 1
        resp = client.get('/dashboard')

    assert resp.status_code == 200
    applications = captured['applications']
    assert applications is not None and len(applications) == 1
    app_tuple = applications[0]
    # app[2] is the action-button href source.
    assert app_tuple[2] == stored_url
    assert app_tuple[2] != derived_url
    # Tuple shape stays at 12 elements (indices unchanged).
    assert len(app_tuple) == 12
