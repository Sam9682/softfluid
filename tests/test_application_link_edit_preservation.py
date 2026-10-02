"""Preservation property tests for the "application link edit persistence" bugfix.

These tests capture the behavior that the fix MUST NOT change (Property 2:
Preservation). Following the observation-first methodology, they are written
against the UNFIXED code and are EXPECTED TO PASS - they establish the baseline
behavior to preserve. They cover the non-bug-condition domain, i.e. inputs where

    isBugCondition(input) = linkChanged
                            AND hasUserApplicationRows
                            AND userApplicationsUrl(applicationId) != newLink

is FALSE (the link is unchanged, or there are no user_applications rows, or the
request is not an authorized link-changing edit).

Observed baseline behavior (from `api_application_actions` PUT/DELETE branches
and `effective_app_url`):
  - Non-link field edits update `applications` and leave `user_applications.url`
    untouched (Requirement 3.1).
  - Changing the application ID re-points `user_applications`, `deployments`,
    `application_costs`, and `billing_activities` within the deferred-constraint
    transaction (Requirement 3.2).
  - A non-admin request is rejected with 403 (Admin access required); an
    unauthenticated request is rejected with 401 (Requirement 3.3).
  - Applications with a non-empty `applications.url`, and applications not
    edited, display their unchanged link via `effective_app_url` (Requirements
    3.4, 3.5).

Property 2: Preservation - Non-link edits, ID propagation, and authorization
unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from flask import Flask
from hypothesis import HealthCheck, given, settings, strategies as st

from src.routes.api_routes import api_bp
from src.routes.main_routes import effective_app_url


STALE_URL = 'https://opcp-psmc.com:6109/'


class FakeDB:
    """In-memory model of the tables the PUT/DELETE handler touches.

    Tables:
      - applications:      {app_id: {'url', 'name', 'description', 'git_url',
                                     'git_repo_size', 'docker_build_duration',
                                     'docker_start_duration',
                                     'docker_stop_duration',
                                     'docker_ps_duration'}}
      - user_applications: list of {'application_id', 'url', 'user_id'}
      - deployments/application_costs/billing_activities: list of
                           {'application_id', ...} used to observe ID re-pointing

    It interprets exactly the SQL the handler issues so the test observes real
    read-back behavior on the UNFIXED code.
    """

    def __init__(self):
        self.applications = {}
        self.user_applications = []
        self.deployments = []
        self.application_costs = []
        self.billing_activities = []
        self.admin_username = 'admin'  # what SELECT username returns

    # ---- shared SQL interpretation -------------------------------------
    def _apply(self, query, params):
        q = ' '.join(query.split()).lower()

        # Admin check: SELECT username FROM users WHERE id = %s
        if q.startswith('select username from users'):
            return (self.admin_username,) if self.admin_username is not None else None

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
            # params: (name, url, description, git_url, git_repo_size,
            #          docker_build, docker_start, docker_stop, docker_ps, app_id)
            app_id = params[-1]
            if app_id in self.applications:
                row = self.applications[app_id]
                row['name'] = params[0]
                row['url'] = params[1]
                row['description'] = params[2]
                row['git_url'] = params[3]
                row['git_repo_size'] = params[4]
                row['docker_build_duration'] = params[5]
                row['docker_start_duration'] = params[6]
                row['docker_stop_duration'] = params[7]
                row['docker_ps_duration'] = params[8]
            return 1

        # ID-changing path: UPDATE applications SET id=%s, name=%s, url=%s, ... WHERE id=%s
        if q.startswith('update applications set id') and 'where id = %s' in q:
            # params: (target_id, name, url, description, git_url, git_repo_size,
            #          docker_build, docker_start, docker_stop, docker_ps, old_id)
            target_id = params[0]
            old_id = params[-1]
            row = self.applications.pop(old_id, {})
            row['name'] = params[1]
            row['url'] = params[2]
            row['description'] = params[3]
            row['git_url'] = params[4]
            row['git_repo_size'] = params[5]
            row['docker_build_duration'] = params[6]
            row['docker_start_duration'] = params[7]
            row['docker_stop_duration'] = params[8]
            row['docker_ps_duration'] = params[9]
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

        # Re-point the other related tables in the ID-change transaction.
        if q.startswith('update deployments set application_id'):
            new_id, old_id = params[0], params[1]
            for row in self.deployments:
                if row['application_id'] == old_id:
                    row['application_id'] = new_id
            return 1
        if q.startswith('update application_costs set application_id'):
            new_id, old_id = params[0], params[1]
            for row in self.application_costs:
                if row['application_id'] == old_id:
                    row['application_id'] = new_id
            return 1
        if q.startswith('update billing_activities set application_id'):
            new_id, old_id = params[0], params[1]
            for row in self.billing_activities:
                if row['application_id'] == old_id:
                    row['application_id'] = new_id
            return 1

        # DELETE FROM applications WHERE id = %s
        if q.startswith('delete from applications where id'):
            app_id = params[0]
            self.applications.pop(app_id, None)
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
                if params is None:
                    return None  # SET CONSTRAINTS ALL DEFERRED etc.
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
        for row in self.user_applications:
            if row['application_id'] == app_id:
                return row['url']
        return None

    def applications_url(self, app_id):
        return self.applications.get(app_id, {}).get('url')

    def app_ids_in(self, table):
        return sorted(row['application_id'] for row in getattr(self, table))


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


def _plain_session(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 2


def _put(client, app_id, payload):
    return client.put(
        f'/api/applications/{app_id}',
        data=json.dumps(payload),
        content_type='application/json',
    )


def _seed_app(db, app_id, *, url, name='app'):
    db.applications[app_id] = {
        'url': url,
        'name': name,
        'description': 'orig-desc',
        'git_url': 'https://git.example.com/orig',
        'git_repo_size': 50,
        'docker_build_duration': 1,
        'docker_start_duration': 2,
        'docker_stop_duration': 3,
        'docker_ps_duration': 4,
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Field values that never look like a URL edit; the link (url) stays the SAME
# as what is already stored, so isBugCondition is false (linkChanged = False).
names = st.text(min_size=1, max_size=20).filter(lambda s: s.strip() != '')
descriptions = st.text(max_size=40)
git_urls = st.text(max_size=40)
sizes = st.integers(min_value=1, max_value=1000)
durations = st.integers(min_value=0, max_value=3600)


# ---------------------------------------------------------------------------
# 3.1 Non-link field edits update applications and leave user_applications.url
#     untouched (link unchanged => isBugCondition False).
# ---------------------------------------------------------------------------
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    name=names,
    description=descriptions,
    git_url=git_urls,
    git_repo_size=sizes,
    d_build=durations,
    d_start=durations,
    d_stop=durations,
    d_ps=durations,
    has_ua=st.booleans(),
    stored_url=st.sampled_from(['', 'https://kept.example.com/']),
)
def test_non_link_field_edit_preserves_user_applications_url(
    client, name, description, git_url, git_repo_size,
    d_build, d_start, d_stop, d_ps, has_ua, stored_url,
):
    """3.1: Editing non-link fields updates `applications` and leaves
    `user_applications.url` byte-for-byte identical (the link is unchanged, so
    the bug condition does not hold).

    Validates: Requirements 3.1
    """
    app_id = 100
    db = FakeDB()
    _seed_app(db, app_id, url=stored_url, name='orig-name')
    if has_ua:
        db.user_applications.append(
            {'application_id': app_id, 'url': STALE_URL, 'user_id': 7}
        )
    ua_before = db.user_applications_url(app_id)

    # Submit the SAME url that is already stored -> link unchanged.
    payload = {
        'name': name,
        'url': stored_url,
        'description': description,
        'git_url': git_url,
        'git_repo_size': git_repo_size,
        'docker_build_duration': d_build,
        'docker_start_duration': d_start,
        'docker_stop_duration': d_stop,
        'docker_ps_duration': d_ps,
    }

    with patch('src.routes.api_routes.db_manager', db):
        _admin_session(client)
        resp = _put(client, app_id, payload)

    assert resp.status_code == 200, resp.get_json()

    # Non-link fields updated on applications.
    row = db.applications[app_id]
    assert row['name'] == name
    assert row['description'] == description
    assert row['git_url'] == git_url
    assert row['git_repo_size'] == git_repo_size
    assert row['docker_build_duration'] == d_build
    assert row['docker_start_duration'] == d_start
    assert row['docker_stop_duration'] == d_stop
    assert row['docker_ps_duration'] == d_ps
    # applications.url unchanged (same value re-submitted).
    assert row['url'] == stored_url

    # user_applications.url is untouched on the unfixed code.
    assert db.user_applications_url(app_id) == ua_before


# ---------------------------------------------------------------------------
# 3.2 ID-change re-points user_applications, deployments, application_costs,
#     and billing_activities within the deferred-constraint transaction.
# ---------------------------------------------------------------------------
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    old_id=st.integers(min_value=1, max_value=500),
    delta=st.integers(min_value=1, max_value=500),
    n_ua=st.integers(min_value=0, max_value=3),
    n_dep=st.integers(min_value=0, max_value=3),
    n_cost=st.integers(min_value=0, max_value=3),
    n_bill=st.integers(min_value=0, max_value=3),
)
def test_id_change_repoints_all_related_tables(
    client, old_id, delta, n_ua, n_dep, n_cost, n_bill,
):
    """3.2: Changing the application ID re-points every related table
    (user_applications, deployments, application_costs, billing_activities)
    from the old id to the new id within the transaction.

    Validates: Requirements 3.2
    """
    new_id = old_id + delta  # guaranteed different and (assumed) unique
    db = FakeDB()
    _seed_app(db, old_id, url='https://kept.example.com/', name='orig')
    for _ in range(n_ua):
        db.user_applications.append(
            {'application_id': old_id, 'url': STALE_URL, 'user_id': 1}
        )
    for _ in range(n_dep):
        db.deployments.append({'application_id': old_id})
    for _ in range(n_cost):
        db.application_costs.append({'application_id': old_id})
    for _ in range(n_bill):
        db.billing_activities.append({'application_id': old_id})

    payload = {
        'name': 'orig',
        'url': 'https://kept.example.com/',  # link unchanged
        'new_id': new_id,
    }

    with patch('src.routes.api_routes.db_manager', db):
        _admin_session(client)
        resp = _put(client, old_id, payload)

    assert resp.status_code == 200, resp.get_json()

    # applications row moved to new_id.
    assert new_id in db.applications
    assert old_id not in db.applications

    # Every related table re-pointed old_id -> new_id, none left under old_id.
    for table, n in (
        ('user_applications', n_ua),
        ('deployments', n_dep),
        ('application_costs', n_cost),
        ('billing_activities', n_bill),
    ):
        ids = db.app_ids_in(table)
        assert ids == [new_id] * n, f"{table} not re-pointed: {ids}"


# ---------------------------------------------------------------------------
# 3.3 Authorization / authentication preserved.
# ---------------------------------------------------------------------------
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    new_link=st.text(max_size=30),
    change_id=st.booleans(),
)
def test_non_admin_rejected_with_403(client, new_link, change_id):
    """3.3: A logged-in non-admin user is rejected with 403 regardless of the
    payload, and no data is mutated.

    Validates: Requirements 3.3
    """
    app_id = 300
    db = FakeDB()
    db.admin_username = 'notadmin'  # SELECT username returns a non-admin
    _seed_app(db, app_id, url='', name='orig')
    db.user_applications.append(
        {'application_id': app_id, 'url': STALE_URL, 'user_id': 2}
    )

    payload = {'name': 'x', 'url': new_link}
    if change_id:
        payload['new_id'] = app_id + 1

    with patch('src.routes.api_routes.db_manager', db):
        _plain_session(client)
        resp = _put(client, app_id, payload)

    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'Admin access required'
    # Nothing changed.
    assert db.applications[app_id]['name'] == 'orig'
    assert db.user_applications_url(app_id) == STALE_URL


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    new_link=st.text(max_size=30),
    method=st.sampled_from(['PUT', 'DELETE']),
)
def test_unauthenticated_rejected_with_401(client, new_link, method):
    """3.3: An unauthenticated request is rejected with 401 for both PUT and
    DELETE, and no data is mutated.

    Validates: Requirements 3.3
    """
    app_id = 310
    db = FakeDB()
    _seed_app(db, app_id, url='', name='orig')
    db.user_applications.append(
        {'application_id': app_id, 'url': STALE_URL, 'user_id': 2}
    )

    with patch('src.routes.api_routes.db_manager', db):
        # No session set -> unauthenticated.
        if method == 'PUT':
            resp = _put(client, app_id, {'name': 'x', 'url': new_link})
        else:
            resp = client.delete(f'/api/applications/{app_id}')

    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'Authentication required'
    # Nothing changed / nothing deleted.
    assert app_id in db.applications
    assert db.user_applications_url(app_id) == STALE_URL


# ---------------------------------------------------------------------------
# 3.4 / 3.5 Display of unaffected applications is unchanged.
# ---------------------------------------------------------------------------
@settings(max_examples=60, deadline=None)
@given(
    stored_url=st.text(max_size=40),
    derived_url=st.text(max_size=40),
)
def test_effective_app_url_display_preserved(stored_url, derived_url):
    """3.4 / 3.5: `effective_app_url` returns the stored applications.url when it
    is non-empty (after trimming), otherwise falls back to the per-user
    user_applications.url. This display behavior is not modified by the fix.

    Validates: Requirements 3.4, 3.5
    """
    result = effective_app_url(stored_url, derived_url)
    if stored_url is not None and stored_url.strip() != '':
        assert result == stored_url  # 3.4: non-empty stored link is shown as-is
    else:
        assert result == derived_url  # 3.5: fall back to existing per-user link


@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    edited_link=st.text(min_size=1, max_size=30).filter(lambda s: s.strip() != ''),
    other_stored=st.sampled_from(['https://a.example.com/', 'https://b.example.com/']),
)
def test_editing_one_app_leaves_other_apps_display_unchanged(
    client, edited_link, other_stored,
):
    """3.5: Editing one application does not change the displayed link of a
    different, unaffected application.

    Validates: Requirements 3.5
    """
    edited_id, other_id = 400, 401
    db = FakeDB()
    _seed_app(db, edited_id, url='', name='edited')
    db.user_applications.append(
        {'application_id': edited_id, 'url': STALE_URL, 'user_id': 1}
    )
    # An unrelated application with a stored link and its own user row.
    _seed_app(db, other_id, url=other_stored, name='other')
    other_ua = 'https://other-ua.example.com/'
    db.user_applications.append(
        {'application_id': other_id, 'url': other_ua, 'user_id': 1}
    )

    other_display_before = effective_app_url(
        db.applications_url(other_id), db.user_applications_url(other_id)
    )

    with patch('src.routes.api_routes.db_manager', db):
        _admin_session(client)
        resp = _put(client, edited_id, {'name': 'edited', 'url': edited_link})

    assert resp.status_code == 200, resp.get_json()

    other_display_after = effective_app_url(
        db.applications_url(other_id), db.user_applications_url(other_id)
    )
    assert other_display_after == other_display_before == other_stored
    # The other app's per-user row is untouched too.
    assert db.user_applications_url(other_id) == other_ua
