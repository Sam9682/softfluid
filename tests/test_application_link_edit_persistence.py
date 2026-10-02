"""Bug condition exploration test for the "application link edit persistence" bugfix.

Bug: When an administrator edits an application's link via PUT /api/applications/{id},
the handler `api_application_actions` updates only `applications.url` and never touches
the per-user `user_applications.url` rows. Because the dashboard / "Authorized
Applications" page fall back to `user_applications.url` (via `effective_app_url`) when
`applications.url` is empty, the edited link never appears and the stale link
(e.g. https://opcp-psmc.com:6109/) keeps being displayed.

This is a BUG CONDITION EXPLORATION test. It is EXPECTED TO FAIL on the UNFIXED code.
A failure CONFIRMS the bug exists. It encodes the expected behavior, so it will pass
once the fix propagates the edited link to `user_applications.url` in both the
ID-unchanged path and the ID-changing (deferred-constraint transaction) path.

Property 1: Bug Condition - Edited link fails to persist to user_applications.
Scoped to concrete failing cases where:
    isBugCondition(input) = linkChanged
                            AND hasUserApplicationRows
                            AND userApplicationsUrl(applicationId) != newLink

**Validates: Requirements 1.1, 1.2, 1.3**
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from src.routes.api_routes import api_bp
from src.routes.main_routes import effective_app_url


STALE_URL = 'https://opcp-psmc.com:6109/'


class FakeDB:
    """A tiny in-memory model of the two tables the handler touches.

    Tables:
      - applications:      {app_id: {'url': str, 'name': str, ...}}
      - user_applications: list of {'application_id': int, 'url': str, 'user_id': int}

    It interprets exactly the SQL the PUT handler issues (both via
    `execute_query` and via the cursor inside the deferred-constraint
    transaction), so that the test observes the real read-back behavior.
    """

    def __init__(self):
        self.applications = {}
        self.user_applications = []

    # ---- shared SQL interpretation -------------------------------------
    def _apply(self, query, params):
        q = ' '.join(query.split()).lower()

        # Admin check: SELECT username FROM users WHERE id = %s
        if q.startswith('select username from users'):
            return ('admin',)  # treat the session user as admin

        # Uniqueness check: SELECT id FROM applications WHERE id = %s
        if q.startswith('select id from applications where id'):
            target = params[0]
            return (target,) if target in self.applications else None

        # Current-link read: SELECT url FROM applications WHERE id = %s
        # (used by the fix to detect whether the link actually changed)
        if q.startswith('select url from applications where id'):
            app_id = params[0]
            if app_id in self.applications:
                return (self.applications[app_id].get('url', ''),)
            return None

        # ID-unchanged path: UPDATE applications SET name=..., url=..., ... WHERE id = %s
        if q.startswith('update applications set name') and 'where id = %s' in q:
            app_id = params[-1]
            url = params[1]
            if app_id in self.applications:
                self.applications[app_id]['url'] = url
            return 1

        # ID-changing path: UPDATE applications SET id=%s, name=%s, url=%s, ... WHERE id=%s
        if q.startswith('update applications set id') and 'where id = %s' in q:
            target_id = params[0]
            url = params[2]
            old_id = params[-1]
            row = self.applications.pop(old_id, {'url': ''})
            row['url'] = url
            self.applications[target_id] = row
            return 1

        # Re-point rows: UPDATE user_applications SET application_id = %s WHERE application_id = %s
        if q.startswith('update user_applications set application_id'):
            new_id, old_id = params[0], params[1]
            for row in self.user_applications:
                if row['application_id'] == old_id:
                    row['application_id'] = new_id
            return 1

        # THE FIX (absent on unfixed code):
        # UPDATE user_applications SET url = %s WHERE application_id = %s
        if q.startswith('update user_applications set url'):
            url, app_id = params[0], params[1]
            for row in self.user_applications:
                if row['application_id'] == app_id:
                    row['url'] = url
            return 1

        # Re-point other tables in the ID-change transaction: no-op for this model.
        if q.startswith('update deployments set application_id') or \
           q.startswith('update application_costs set application_id') or \
           q.startswith('update billing_activities set application_id'):
            return 1

        return None

    # ---- execute_query -------------------------------------------------
    def execute_query(self, query, params=None, fetch_one=False, fetch_all=False):
        result = self._apply(query, params)
        if fetch_one or fetch_all:
            return result
        return result if result is not None else 0

    # ---- transaction path: get_db_connection() / conn.cursor() ---------
    @contextmanager
    def get_db_connection(self):
        db = self

        class _Cursor:
            def __enter__(self_c):
                return self_c

            def __exit__(self_c, *a):
                return False

            def execute(self_c, query, params=None):
                # SET CONSTRAINTS ALL DEFERRED and similar are ignored.
                if params is None:
                    return None
                return db._apply(query, params)

        class _Conn:
            def cursor(self_conn):
                return _Cursor()

            def commit(self_conn):
                pass

            def rollback(self_conn):
                pass

        yield _Conn()

    # ---- helpers for assertions ----------------------------------------
    def user_applications_url(self, app_id):
        """userApplicationsUrl(app_id): the per-user url read by the dashboard."""
        for row in self.user_applications:
            if row['application_id'] == app_id:
                return row['url']
        return None

    def applications_url(self, app_id):
        return self.applications.get(app_id, {}).get('url')


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.register_blueprint(api_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _admin_session(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['is_admin'] = True


def _put_edit(client, app_id, *, name, url, new_id=None):
    payload = {'name': name, 'url': url}
    if new_id is not None:
        payload['new_id'] = new_id
    return client.put(
        f'/api/applications/{app_id}',
        data=json.dumps(payload),
        content_type='application/json',
    )


# Scoped failing cases derived from isBugCondition:
#   (label, app_id, new_id, new_link)
# Both cases start from applications.url = '' and user_applications.url = STALE_URL
# so the bug condition (linkChanged AND hasUserApplicationRows AND
# userApplicationsUrl != newLink) holds.
SCOPED_CASES = [
    # ID-unchanged link edit: target_id == app_id
    ('id_unchanged', 10, None, 'https://ai-shai.example.com/'),
    # ID-changing link edit: target_id != app_id
    ('id_changing', 20, 21, 'https://new-link.example.com/'),
]


@pytest.mark.parametrize('label,app_id,new_id,new_link', SCOPED_CASES,
                         ids=[c[0] for c in SCOPED_CASES])
def test_edited_link_persists_to_user_applications(client, label, app_id, new_id, new_link):
    """Property 1 (Bug Condition): after editing the link, the per-user
    user_applications.url MUST equal the new link, and the displayed link
    (effective_app_url) MUST equal the new link.

    EXPECTED ON UNFIXED CODE: FAILS because user_applications.url is never
    updated, so it retains the stale STALE_URL value.

    Validates: Requirements 1.1, 1.2, 1.3
    """
    db = FakeDB()
    # Seed: empty applications.url, stale user_applications.url
    db.applications[app_id] = {'url': '', 'name': 'ai-shai-web-interface'}
    db.user_applications.append(
        {'application_id': app_id, 'url': STALE_URL, 'user_id': 1}
    )

    target_id = new_id if new_id is not None else app_id

    with patch('src.routes.api_routes.db_manager', db):
        _admin_session(client)
        resp = _put_edit(client, app_id, name='ai-shai-web-interface',
                         url=new_link, new_id=new_id)

    assert resp.status_code == 200, resp.get_json()

    # userApplicationsUrl(target_id) = newLink
    persisted = db.user_applications_url(target_id)
    assert persisted == new_link, (
        f"[{label}] BUG: user_applications.url for id {target_id} is "
        f"{persisted!r}, expected {new_link!r}"
    )

    # displayedLink(target_id) = newLink
    displayed = effective_app_url(db.applications_url(target_id), persisted)
    assert displayed == new_link, (
        f"[{label}] BUG: displayed link for id {target_id} is "
        f"{displayed!r}, expected {new_link!r}"
    )
