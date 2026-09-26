"""Preservation tests for the "start action not launching app" bugfix.

Spec: .kiro/specs/start-action-not-launching-app

Property 2: Preservation - Non-Bug Inputs Unchanged
---------------------------------------------------
For any operator request where the bug condition does NOT hold
(``isBugCondition`` returns false), the fixed code SHALL produce the same
result as the original code, preserving:

  - executing-prompt construction when the context file exists (Req 3.1),
  - billing for genuinely executed START/STOP successes (Req 3.2),
  - the Virtual Advisor Q&A fallback for genuinely invalid/empty actions
    (Req 3.3),
  - no billing for non-billable actions such as LOGS/PS/SPECIFY (Req 3.4).

Bug condition (design ``isBugCondition``):
    (isRecognizedExecutableAction(detected_action) AND NOT context_file_exists)
        OR (detected_action IN {START, STOP} AND returncode == 0
            AND NOT action_executed)

The non-bug domain exercised here therefore restricts inputs so that neither
clause of ``isBugCondition`` can hold:

  - recognized actions are only combined with ``context_exists = True`` (the
    first clause requires a *missing* context), and
  - START/STOP are only "executed" when their context file is present, so the
    "billed without execution" clause (``returncode == 0 AND NOT
    action_executed``) is never entered.

Methodology (observation-first)
-------------------------------
These tests were written by first running the UNFIXED code (see the bugfix
task notes) and recording the actual outputs, then asserting exactly those
outputs. They are EXPECTED TO PASS on the unfixed code - they capture the
baseline behavior the fix must preserve.

Conventions follow the existing pytest + unittest.mock + Flask test-client +
hypothesis suite (see tests/test_serverless_routes.py,
tests/test_extended_ports_preservation.py, and the sibling
tests/test_start_action_bug_condition.py).

_Requirements: 3.1, 3.2, 3.3, 3.4_
"""

import json
import logging
import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.routes.genai_routes import (  # noqa: E402
    genai_bp,
    return_prompt_for_operator,
)


# --------------------------------------------------------------------------
# Domain constants (mirror the design glossary and dashboard operator dropdown)
# --------------------------------------------------------------------------

# Recognized executable operator actions (MODIFY_CODE is redirected to the
# Developer agent before prompt building and is therefore excluded).
RECOGNIZED_OPERATOR_ACTIONS = ['START', 'STOP', 'LOGS', 'PS', 'SPECIFY', 'RESTORE_DATABASE']

# Recognized AND billable actions.
BILLABLE_ACTIONS = ['START', 'STOP']

# Recognized but NON-billable actions.
NON_BILLABLE_ACTIONS = ['LOGS', 'PS', 'SPECIFY', 'RESTORE_DATABASE']

# Genuinely invalid / unrecognized actions. These include empty/whitespace,
# unrecognized words, mixed case that does not map to a recognized action, and
# path-traversal attempts - all of which legitimately use the advisor fallback.
INVALID_ACTIONS = [
    '',
    '   ',
    'FOObar',
    'launch-now',
    'not_a_real_action',
    '../../etc/passwd',
    '..\\..\\windows',
    '###bad###',
    'START; rm -rf /',
    '%2e%2e%2f',
]

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


def _resolve_prompt_with_context(app, action, context_template,
                                 application_name='ai-shai-web-interface',
                                 application_folder='/some/folder',
                                 user_name='Test User',
                                 user_email='test@example.com'):
    """Call `return_prompt_for_operator` on the UNFIXED code with a present
    context file whose contents are `context_template`.

    Returns the resolved prompt string.
    """
    fake_file = MagicMock()
    fake_file.__enter__.return_value.read.return_value = context_template
    with app.test_request_context():
        with patch('src.routes.genai_routes.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=fake_file), \
             patch('src.database_postgres.load_deploy_config', return_value={}), \
             patch('src.routes.genai_routes.db_manager') as db:
            # No application row found -> application_id falls back to 0 (does
            # not affect the operator prompt, which has no ID placeholder).
            db.execute_query.return_value = None
            # The resolver now returns a tuple ``(l_prompt, is_executing)``.
            # Unpack it so resolver-level assertions inspect the prompt string.
            l_prompt, _is_executing = return_prompt_for_operator(
                action,
                application_name=application_name,
                application_folder=application_folder,
                user_name=user_name,
                user_email=user_email,
            )
            return l_prompt


def _resolve_prompt_missing_context(app, action,
                                    application_name='ai-shai-web-interface',
                                    application_folder='/some/folder',
                                    user_name='Test User',
                                    user_email='test@example.com'):
    """Call `return_prompt_for_operator` on the UNFIXED code with the context
    file absent. Returns the resolved prompt string.
    """
    with app.test_request_context():
        with patch('src.routes.genai_routes.os.path.exists', return_value=False):
            # The resolver now returns a tuple ``(l_prompt, is_executing)``.
            # Unpack it so resolver-level assertions inspect the prompt string.
            l_prompt, _is_executing = return_prompt_for_operator(
                action,
                application_name=application_name,
                application_folder=application_folder,
                user_name=user_name,
                user_email=user_email,
            )
            return l_prompt


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


def _drive_operator_generate(client, action, context_exists, returncode,
                             application_name='ai-shai-web-interface'):
    """Drive the operator `generate()` SSE flow on the UNFIXED code.

    Returns (billing_call_count, final_done_event).

    `context_exists` controls whether `return_prompt_for_operator` loads a
    real context file (executing prompt) or the advisor fallback. `returncode`
    is the simulated engine exit code.
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

    # A present context file loads a template via open(); provide a benign one.
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
    return billing_mock.call_count, final


# --------------------------------------------------------------------------
# Observation-first oracle for the billing/success behavior
# --------------------------------------------------------------------------

def _expected_billing_recorded(action, context_exists, returncode):
    """The observed baseline: on the UNFIXED code the operator `generate()`
    records billing IFF the action is START/STOP AND returncode == 0 AND an
    application_name is set. Within the non-bug domain, START/STOP are only
    "executed" when the context is present, so we require context_exists too.
    """
    return (
        action in BILLABLE_ACTIONS
        and returncode == 0
        and context_exists
    )


# --------------------------------------------------------------------------
# Req 3.1 - executing-prompt construction preserved when context exists
# --------------------------------------------------------------------------

class TestExecutingPromptPreserved:
    """With a present context file, the recognized action builds the executing
    prompt with all placeholders substituted (never the advisor fallback)."""

    def test_start_context_present_builds_executing_prompt(self, app):
        template = (
            'EXECUTE START for {{USER_NAME}} <{{USER_EMAIL}}> '
            'app={{APPLICATION_NAME}} folder={{APPLICATION_FOLDER}} '
            'uid={{USER_ID}} tail={{TAIL_LINES}}'
        )
        result = _resolve_prompt_with_context(app, 'START', template)

        # Observed baseline: placeholders are substituted; user_id defaults to
        # 0 in a bare request context; TAIL_LINES is the literal '100'.
        expected = (
            'EXECUTE START for Test User <test@example.com> '
            'app=ai-shai-web-interface folder=/some/folder uid=0 tail=100'
        )
        assert result == expected
        assert not _is_advisor_fallback(result)

    @pytest.mark.parametrize('action', RECOGNIZED_OPERATOR_ACTIONS)
    def test_recognized_action_context_present_is_not_advisor(self, app, action):
        template = 'EXECUTE {{APPLICATION_NAME}} via ' + action
        result = _resolve_prompt_with_context(app, action, template)
        # The executing prompt is the substituted template, not the advisor.
        assert result == 'EXECUTE ai-shai-web-interface via ' + action
        assert not _is_advisor_fallback(result)


# --------------------------------------------------------------------------
# Req 3.3 - advisor fallback preserved for genuinely invalid/empty actions
# --------------------------------------------------------------------------

class TestAdvisorFallbackPreserved:
    """Genuinely invalid/empty actions still resolve to the advisor Q&A
    fallback and record no billing."""

    @pytest.mark.parametrize('action', INVALID_ACTIONS)
    def test_invalid_action_returns_advisor_fallback(self, app, action):
        result = _resolve_prompt_missing_context(app, action)
        assert _is_advisor_fallback(result), (
            f"Invalid action {action!r} should resolve to the advisor "
            f"fallback. Got: {result!r:.200}"
        )

    @pytest.mark.parametrize('action', INVALID_ACTIONS)
    def test_invalid_action_records_no_billing(self, client, action):
        # Invalid action -> advisor path (context missing); returncode 0.
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=False, returncode=0,
        )
        assert billing_count == 0, (
            f"Invalid action {action!r} unexpectedly recorded billing "
            f"{billing_count} time(s)."
        )


# --------------------------------------------------------------------------
# Req 3.2 - billing preserved for genuinely executed START/STOP successes
# --------------------------------------------------------------------------

class TestGenuineBillingPreserved:
    """A genuinely executed START/STOP (context present, returncode == 0)
    still records billing exactly once and reports success."""

    @pytest.mark.parametrize('action', BILLABLE_ACTIONS)
    def test_executed_billable_action_records_billing(self, client, action):
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=True, returncode=0,
        )
        assert billing_count == 1, (
            f"Genuine {action} success should record billing exactly once, "
            f"got {billing_count}."
        )
        assert final is not None
        assert final.get('success') is True

    @pytest.mark.parametrize('action', BILLABLE_ACTIONS)
    def test_failed_billable_action_records_no_billing(self, client, action):
        # returncode != 0 -> the engine failed; baseline records no billing
        # and reports success == False.
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=True, returncode=1,
        )
        assert billing_count == 0
        assert final is not None
        assert final.get('success') is False


# --------------------------------------------------------------------------
# Req 3.4 - non-billable actions complete without billing
# --------------------------------------------------------------------------

class TestNonBillableActionsPreserved:
    """LOGS/PS/SPECIFY/RESTORE_DATABASE complete without recording billing."""

    @pytest.mark.parametrize('action', NON_BILLABLE_ACTIONS)
    def test_non_billable_action_records_no_billing(self, client, action):
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=True, returncode=0,
        )
        assert billing_count == 0, (
            f"Non-billable action {action} should not record billing, "
            f"got {billing_count}."
        )
        assert final is not None
        assert final.get('success') is True


# --------------------------------------------------------------------------
# Property-based preservation tests
# --------------------------------------------------------------------------

# Strategy over action strings: recognized, unrecognized, empty, mixed case,
# and path-traversal attempts, for stronger coverage of the resolver branch.
_recognized_action_strategy = st.sampled_from(RECOGNIZED_OPERATOR_ACTIONS)
_invalid_action_strategy = st.sampled_from(INVALID_ACTIONS)
_mixed_case_recognized = st.sampled_from(
    [a.lower() for a in RECOGNIZED_OPERATOR_ACTIONS]
    + [a.capitalize() for a in RECOGNIZED_OPERATOR_ACTIONS]
)


class TestPreservationProperties:
    """Property-based tests capturing the observed non-bug patterns."""

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(action=_recognized_action_strategy)
    def test_property_recognized_action_context_present_never_advisor(self, app, action):
        """For any recognized action with a PRESENT context (non-bug), the
        resolver returns the executing prompt, never the advisor fallback."""
        template = 'EXEC {{APPLICATION_NAME}} :: ' + action
        result = _resolve_prompt_with_context(app, action, template)
        assert result == 'EXEC ai-shai-web-interface :: ' + action
        assert not _is_advisor_fallback(result)

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(action=_invalid_action_strategy)
    def test_property_invalid_action_always_advisor(self, app, action):
        """For any genuinely invalid/empty action (non-bug), the resolver
        returns the advisor fallback."""
        result = _resolve_prompt_missing_context(app, action)
        assert _is_advisor_fallback(result)

    @settings(max_examples=80, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        action=st.sampled_from(RECOGNIZED_OPERATOR_ACTIONS),
        returncode=st.sampled_from([0, 1, 2, -1]),
    )
    def test_property_billing_iff_billable_executed_success(self, client, action, returncode):
        """Restricted to the non-bug domain (recognized action WITH context
        present so START/STOP are genuinely executed): billing is recorded IFF
        the action is START/STOP AND returncode == 0. success == (rc == 0)."""
        context_exists = True  # non-bug domain: recognized action + context present
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=context_exists, returncode=returncode,
        )
        expected_billing = _expected_billing_recorded(action, context_exists, returncode)
        assert (billing_count == 1) == expected_billing, (
            f"action={action} rc={returncode}: expected billing="
            f"{expected_billing}, got count={billing_count}"
        )
        assert final is not None
        assert final.get('success') is (returncode == 0)

    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(action=st.sampled_from(NON_BILLABLE_ACTIONS),
           returncode=st.sampled_from([0, 1]))
    def test_property_non_billable_never_bills(self, client, action, returncode):
        """Non-billable recognized actions never record billing regardless of
        returncode (non-bug domain, context present)."""
        billing_count, final = _drive_operator_generate(
            client, action, context_exists=True, returncode=returncode,
        )
        assert billing_count == 0
        assert final is not None
        assert final.get('success') is (returncode == 0)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    sys.exit(pytest.main([__file__, '-v']))
