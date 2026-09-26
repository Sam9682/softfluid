"""Targeted unit and integration tests for "start action not launching app".

Spec: .kiro/specs/start-action-not-launching-app (Task 4)

These tests target the fixed behavior directly (not property-based). They cover
the resolver (`return_prompt_for_operator`) and the operator `generate()` SSE
flow inside `api_request_ops_ai_for_app`.

Expected behavior under test:
  - A recognized action with a MISSING context file returns an explicit failure
    ``(None, False)`` and logs a clear error - it never degrades to the advisor
    Q&A fallback (Req 2.1, 2.2).
  - A genuinely invalid/empty action returns the advisor fallback with
    ``is_executing == False`` (Req 3.3).
  - A recognized action with a PRESENT context file returns the executing prompt
    with placeholders substituted and ``is_executing == True`` (Req 3.1).
  - Operator `generate()` skips billing and reports ``success == False`` when a
    recognized action fails to resolve a context (Req 2.3, 2.4).
  - Operator `generate()` records billing and reports ``success == True`` only
    for a genuinely executed START/STOP with ``returncode == 0`` (Req 3.2).
  - Integration: POST START with no context -> clear error + done(success=False)
    and no billing row (Req 2.1, 2.2, 2.3, 2.4).
  - Integration: POST START with a present context -> executing path runs and,
    on genuine success, a billing row is created (Req 3.1, 3.2).
  - Integration: POST LOGS -> completes with no billing and no advisor
    degradation (Req 3.4).

Conventions follow the sibling tests
(tests/test_start_action_bug_condition.py,
tests/test_start_action_preservation.py): pytest + unittest.mock + a Flask
test client, with the engine subprocess and path/DB helpers patched at their
source modules.

_Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4_
"""

import json
import logging
import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.routes import genai_routes  # noqa: E402
from src.routes.genai_routes import (  # noqa: E402
    genai_bp,
    return_prompt_for_operator,
)


# --------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------

RECOGNIZED_OPERATOR_ACTIONS = ['START', 'STOP', 'LOGS', 'PS', 'SPECIFY', 'RESTORE_DATABASE']
BILLABLE_ACTIONS = ['START', 'STOP']
NON_BILLABLE_ACTIONS = ['LOGS', 'PS', 'SPECIFY', 'RESTORE_DATABASE']

ADVISOR_MARKER = 'You are a helpful Virtual Advisor assistant.'
ADVISOR_NO_EXEC_MARKER = 'Do not execute any commands or modify any files.'


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create a Flask app with the genai blueprint for testing."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.register_blueprint(genai_bp)
    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _is_advisor_fallback(prompt):
    """Return True if `prompt` is the Virtual Advisor Q&A fallback."""
    if not isinstance(prompt, str):
        return False
    return ADVISOR_MARKER in prompt and ADVISOR_NO_EXEC_MARKER in prompt


def _sse_events(response):
    """Parse an SSE Response body into a list of decoded JSON event dicts."""
    body = response.get_data(as_text=True)
    events = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[len('data:'):].strip()
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    return events


def _resolve_operator(action, context_exists, context_template=None,
                      application_name='ai-shai-web-interface',
                      application_folder='/some/folder',
                      user_name='Test User',
                      user_email='test@example.com'):
    """Call `return_prompt_for_operator` with a controlled context-file state.

    Returns the ``(l_prompt, is_executing)`` tuple. When ``context_exists`` is
    True, ``open()`` yields ``context_template`` and the application-id lookup
    returns no row (application_id defaults to 0).
    """
    patchers = [patch('src.routes.genai_routes.os.path.exists', return_value=context_exists)]
    if context_exists:
        fake_file = MagicMock()
        fake_file.__enter__.return_value.read.return_value = context_template or ''
        patchers.extend([
            patch('builtins.open', return_value=fake_file),
            patch('src.database_postgres.load_deploy_config', return_value={}),
            patch('src.routes.genai_routes.db_manager'),
        ])
    # Enter all patchers, then call inside a request context.
    started = [p.start() for p in patchers]
    try:
        if context_exists:
            # The db_manager mock is the last started patcher; make its
            # application lookup return no row so application_id -> 0.
            started[-1].execute_query.return_value = None
        # A bare request context provides session.get('user_id', 0) -> 0.
        # Build a minimal app just to obtain a request context.
        _app = Flask(__name__)
        with _app.test_request_context():
            return return_prompt_for_operator(
                action,
                application_name=application_name,
                application_folder=application_folder,
                user_name=user_name,
                user_email=user_email,
            )
    finally:
        for p in patchers:
            p.stop()


def _drive_operator_generate(client, action, context_exists, returncode,
                             application_name='ai-shai-web-interface'):
    """Drive the operator `generate()` SSE flow.

    Returns (billing_call_count, final_done_event, events).
    """
    lines = iter(['engine output line\n', ''])
    fake_process = MagicMock()
    fake_process.stdout.readline.side_effect = lambda: next(lines)
    fake_process.returncode = returncode
    fake_process.poll.return_value = returncode
    fake_process.wait.return_value = returncode

    billing_mock = MagicMock()

    with client.session_transaction() as sess:
        sess['user_id'] = 1

    def fake_execute_query(query, params=None, **kwargs):
        if 'FROM users' in query:
            return ('tester', 'test@example.com', 'Test', 'User')
        return None

    fake_file = MagicMock()
    fake_file.__enter__.return_value.read.return_value = 'EXECUTING CONTEXT for {{APPLICATION_NAME}}'

    with patch('src.routes.genai_routes.os.path.exists', return_value=context_exists), \
         patch('os.setsid', MagicMock(name='setsid'), create=True), \
         patch('builtins.open', return_value=fake_file), \
         patch('src.config_postgres.get_qchat_paths', return_value='/usr/bin/qchat'), \
         patch('src.config_postgres.get_shai_paths', return_value='/usr/bin/shai'), \
         patch('src.routes.billing_routes.record_billing_activity', billing_mock), \
         patch('subprocess.Popen', return_value=fake_process), \
         patch('src.database_postgres.load_deploy_config', return_value={}), \
         patch('src.routes.genai_routes.db_manager') as mock_db:
        mock_db.execute_query.side_effect = fake_execute_query
        response = client.post(
            '/api/request_ops_ai_for_app',
            data=json.dumps({
                'message': 'perform the action',
                'application_name': application_name,
                'application_folder': '/some/folder',
                'action_operation': action,
                'agentic_engine': 'shai',
            }),
            content_type='application/json',
        )
        events = _sse_events(response)

    done_events = [e for e in events if e.get('done')]
    final = done_events[-1] if done_events else None
    return billing_mock.call_count, final, events


# ==========================================================================
# UNIT TESTS - return_prompt_for_operator
# ==========================================================================

class TestResolverRecognizedMissingContext:
    """Req 2.1, 2.2: recognized action + missing context -> explicit failure
    (None, False) and a clear error is logged (never the advisor fallback)."""

    @pytest.mark.parametrize('action', RECOGNIZED_OPERATOR_ACTIONS)
    def test_recognized_missing_context_returns_explicit_failure(self, action):
        prompt, is_executing = _resolve_operator(action, context_exists=False)
        assert prompt is None, (
            f"Recognized action {action!r} with missing context must return an "
            f"explicit failure (None), got: {prompt!r:.200}"
        )
        assert is_executing is False
        assert not _is_advisor_fallback(prompt)

    def test_recognized_missing_context_logs_clear_error(self):
        # The module logger has propagate=False, so patch it directly rather
        # than relying on caplog.
        with patch.object(genai_routes.logger, 'error') as mock_error:
            prompt, is_executing = _resolve_operator('START', context_exists=False)

        assert prompt is None
        assert is_executing is False
        assert mock_error.called, "A clear error must be logged for a recognized action with missing context"
        logged = ' '.join(str(c.args[0]) for c in mock_error.call_args_list)
        # The error names the action and communicates the missing context.
        assert 'START' in logged
        assert 'context' in logged.lower()


class TestResolverInvalidAction:
    """Req 3.3: genuinely invalid/empty action -> advisor fallback,
    is_executing == False."""

    @pytest.mark.parametrize('action', ['', '   ', 'FOObar', '###bad###', '../../etc/passwd'])
    def test_invalid_action_returns_advisor_fallback(self, action):
        prompt, is_executing = _resolve_operator(action, context_exists=False)
        assert _is_advisor_fallback(prompt), (
            f"Invalid action {action!r} should resolve to the advisor fallback, "
            f"got: {prompt!r:.200}"
        )
        assert is_executing is False


class TestResolverContextPresent:
    """Req 3.1: recognized action + present context -> executing prompt with
    placeholders substituted, is_executing == True."""

    def test_context_present_substitutes_placeholders(self):
        template = (
            'EXECUTE START for {{USER_NAME}} <{{USER_EMAIL}}> '
            'app={{APPLICATION_NAME}} folder={{APPLICATION_FOLDER}} '
            'uid={{USER_ID}} tail={{TAIL_LINES}}'
        )
        prompt, is_executing = _resolve_operator(
            'START', context_exists=True, context_template=template,
        )
        expected = (
            'EXECUTE START for Test User <test@example.com> '
            'app=ai-shai-web-interface folder=/some/folder uid=0 tail=100'
        )
        assert prompt == expected
        assert is_executing is True
        assert not _is_advisor_fallback(prompt)

    @pytest.mark.parametrize('action', RECOGNIZED_OPERATOR_ACTIONS)
    def test_context_present_is_executing_for_all_recognized(self, action):
        template = 'EXEC {{APPLICATION_NAME}} :: ' + action
        prompt, is_executing = _resolve_operator(
            action, context_exists=True, context_template=template,
        )
        assert prompt == 'EXEC ai-shai-web-interface :: ' + action
        assert is_executing is True


# ==========================================================================
# UNIT TESTS - operator generate() billing/success gating
# ==========================================================================

class TestGenerateBillingGating:
    """Req 2.3, 2.4, 3.2: generate() skips billing and reports success=False
    when a recognized action fails to resolve a context; it records billing and
    reports success=True only for a genuinely executed START/STOP (rc == 0)."""

    @pytest.mark.parametrize('action', BILLABLE_ACTIONS)
    def test_missing_context_skips_billing_and_reports_failure(self, client, action):
        billing_count, final, events = _drive_operator_generate(
            client, action, context_exists=False, returncode=0,
        )
        assert billing_count == 0, (
            f"{action} with missing context must not record billing, got {billing_count}"
        )
        assert final is not None
        assert final.get('success') is False
        # A clear error event must be emitted.
        errors = [e for e in events if 'error' in e]
        assert errors, f"Expected a clear error event, got: {events}"

    @pytest.mark.parametrize('action', BILLABLE_ACTIONS)
    def test_genuine_execution_records_billing_and_reports_success(self, client, action):
        billing_count, final, events = _drive_operator_generate(
            client, action, context_exists=True, returncode=0,
        )
        assert billing_count == 1, (
            f"Genuine {action} success should record billing exactly once, got {billing_count}"
        )
        assert final is not None
        assert final.get('success') is True

    @pytest.mark.parametrize('action', BILLABLE_ACTIONS)
    def test_executed_but_failed_returncode_skips_billing(self, client, action):
        billing_count, final, events = _drive_operator_generate(
            client, action, context_exists=True, returncode=1,
        )
        assert billing_count == 0
        assert final is not None
        assert final.get('success') is False


# ==========================================================================
# INTEGRATION TESTS - POST /api/request_ops_ai_for_app
# ==========================================================================

class TestIntegrationStartMissingContext:
    """Req 2.1, 2.2, 2.3, 2.4: POST START with no context -> clear error, a
    done event with success == False, and no billing row created."""

    def test_start_missing_context_emits_error_and_failure_no_billing(self, client):
        billing_count, final, events = _drive_operator_generate(
            client, 'START', context_exists=False, returncode=0,
        )
        # A clear error is emitted.
        errors = [e for e in events if 'error' in e]
        assert errors, f"Expected a clear error event in the SSE stream, got: {events}"
        assert any('not launched' in e['error'].lower() or 'context' in e['error'].lower()
                   for e in errors)
        # done event with success == False.
        assert final is not None
        assert final.get('done') is True
        assert final.get('success') is False
        # No billing row created.
        assert billing_count == 0


class TestIntegrationStartContextPresent:
    """Req 3.1, 3.2: POST START with a present context -> executing path runs
    and, on genuine success, a billing row is created."""

    def test_start_context_present_runs_and_bills_on_success(self, client):
        billing_count, final, events = _drive_operator_generate(
            client, 'START', context_exists=True, returncode=0,
        )
        # The executing path ran (engine output streamed, no explicit failure).
        chunks = [e for e in events if 'chunk' in e]
        assert chunks, f"Expected streamed engine chunks, got: {events}"
        assert not any('not launched' in e.get('error', '').lower() for e in events)
        # Genuine success -> billing recorded and success reported.
        assert billing_count == 1
        assert final is not None
        assert final.get('success') is True


class TestIntegrationLogsAction:
    """Req 3.4: POST LOGS -> completes with no billing and no advisor
    degradation."""

    def test_logs_action_completes_without_billing_or_degradation(self, client):
        billing_count, final, events = _drive_operator_generate(
            client, 'LOGS', context_exists=True, returncode=0,
        )
        assert billing_count == 0, (
            f"LOGS is non-billable and must not record billing, got {billing_count}"
        )
        assert final is not None
        assert final.get('done') is True
        assert final.get('success') is True
        # No advisor degradation: no advisor fallback text leaked into a chunk.
        assert not any(_is_advisor_fallback(e.get('chunk', '')) for e in events)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    sys.exit(pytest.main([__file__, '-v']))
