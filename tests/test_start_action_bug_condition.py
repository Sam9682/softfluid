"""Bug condition exploration test for "start action not launching app".

This is a BUGFIX exploration test. It encodes the EXPECTED (correct) behavior
described in the spec's Correctness Property 1. On the UNFIXED code it is
EXPECTED TO FAIL - the failures are the counterexamples that prove the bug
exists. Do NOT fix the code or the test from within this file; the fix is a
later task.

Bug condition (design `isBugCondition`):
    (isRecognizedExecutableAction(detected_action) AND NOT context_file_exists)
        OR (detected_action IN {START, STOP} AND returncode == 0 AND NOT action_executed)

Conventions follow the existing pytest + unittest.mock + Flask test-client
suite (see tests/test_serverless_routes.py).

Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4
"""

import json
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

from src.routes import genai_routes
from src.routes.genai_routes import genai_bp, return_prompt_for_operator, _create_fallback_prompt


# The recognized executable operator actions (mirrors the dashboard operator
# dropdown; MODIFY_CODE is redirected to the Developer agent before prompt
# building and is therefore excluded here).
RECOGNIZED_OPERATOR_ACTIONS = ['START', 'STOP', 'LOGS', 'PS', 'SPECIFY', 'RESTORE_DATABASE']

# Actions that are recognized AND billable.
BILLABLE_ACTIONS = ['START', 'STOP']


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


def _is_advisor_fallback(prompt):
    """Return True if `prompt` is (or contains) the Virtual Advisor Q&A fallback.

    The advisor fallback is produced by `_create_fallback_prompt`. It is the
    non-executing prompt that the bug incorrectly returns for recognized
    actions.
    """
    if prompt is None:
        return False
    if not isinstance(prompt, str):
        return False
    advisor_marker = 'You are a helpful Virtual Advisor assistant.'
    no_exec_marker = 'Do not execute any commands or modify any files.'
    return advisor_marker in prompt and no_exec_marker in prompt


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


class TestBugConditionRecognizedActionMissingContext:
    """Test case 1: recognized action + missing context must NOT be advisor.

    Property: for all action in RECOGNIZED_OPERATOR_ACTIONS with a missing
    context file, `return_prompt_for_operator` must NOT return the advisor Q&A
    fallback - it must resolve an executing prompt or signal an explicit
    failure.
    """

    @pytest.mark.parametrize('action', RECOGNIZED_OPERATOR_ACTIONS)
    def test_recognized_action_missing_context_is_not_advisor_fallback(self, app, action):
        # Simulate the {ACTION}_context.md file being absent.
        with app.test_request_context():
            with patch('src.routes.genai_routes.os.path.exists', return_value=False):
                result = return_prompt_for_operator(
                    action,
                    application_name='ai-shai-web-interface',
                    application_folder='/some/folder',
                    user_name='Test User',
                    user_email='test@example.com',
                )

        # `return_prompt_for_operator` returns a tuple ``(l_prompt, is_executing)``
        # after the fix. Unpack it so the assertions inspect the actual prompt
        # string and the executing flag rather than the tuple wrapper.
        prompt, is_executing = result

        # A recognized action with missing context must either resolve an
        # executing prompt or fail explicitly - it must NEVER be the advisor
        # Q&A fallback. On unfixed code this returned the advisor fallback,
        # so this assertion failed (counterexample proving the bug).
        assert not _is_advisor_fallback(prompt), (
            f"Recognized action {action!r} with missing context silently "
            f"degraded to the Virtual Advisor Q&A prompt instead of executing "
            f"or failing explicitly. Returned: {prompt!r:.200}"
        )

        # The prompt must be either an executing prompt (is_executing True) or
        # an explicit failure signal (None). It must never be a non-executing
        # advisory string that pretends the action ran.
        assert is_executing or prompt is None, (
            f"Recognized action {action!r} with missing context neither "
            f"resolved an executing prompt nor failed explicitly. "
            f"is_executing={is_executing!r}, prompt={prompt!r:.200}"
        )


class TestAdvisorPathBillingAndSuccess:
    """Test cases 2 & 3: advisor path (STOP, missing context, rc==0).

    The recognized STOP action with a missing context runs the advisor prompt,
    the engine exits 0, and the unfixed code (a) records billing and
    (b) reports session success. Correct behavior: NO billing and success ==
    False because no real action was executed.
    """

    def _drive_generate(self, client, action):
        """POST an operator request and return (events, billing_mock)."""
        # Fake subprocess whose engine exits cleanly (returncode == 0),
        # emulating the advisor Q&A being answered without any real action.
        # generate() consumes output via `iter(process.stdout.readline, '')`,
        # so stdout.readline must yield lines then '' (EOF sentinel).
        lines = iter(['Advisor answer line\n', ''])
        fake_process = MagicMock()
        fake_process.stdout.readline.side_effect = lambda: next(lines)
        fake_process.returncode = 0
        fake_process.poll.return_value = 0
        fake_process.wait.return_value = 0

        billing_mock = MagicMock()

        with client.session_transaction() as sess:
            sess['user_id'] = 1

        # `subprocess`, `os`, `signal`, `time` and the engine-path helpers are
        # imported INSIDE generate()/api_request_ops_ai_for_app, so patch them
        # at their real source modules (the names resolve at call time).
        def fake_execute_query(query, params=None, **kwargs):
            # Only the user-details lookup should return a row; configuration
            # lookups (agentic_engine / agentic_command) must return None so
            # the engine path (agentic_engine='shai') is used rather than the
            # deployControlPlan.sh branch.
            if 'FROM users' in query:
                return ('tester', 'test@example.com', 'Test', 'User')
            return None

        # os.setsid is referenced as Popen(preexec_fn=os.setsid); it does not
        # exist on Windows, so provide a stub. Popen itself is mocked.
        os_setsid_stub = MagicMock(name='setsid')

        with patch('src.routes.genai_routes.os.path.exists', return_value=False), \
             patch('os.setsid', os_setsid_stub, create=True), \
             patch('src.config_postgres.get_qchat_paths', return_value='/usr/bin/qchat'), \
             patch('src.config_postgres.get_shai_paths', return_value='/usr/bin/shai'), \
             patch('src.routes.billing_routes.record_billing_activity', billing_mock), \
             patch('subprocess.Popen', return_value=fake_process), \
             patch('src.routes.genai_routes.db_manager') as mock_db:
            mock_db.execute_query.side_effect = fake_execute_query
            response = client.post(
                '/api/request_ops_ai_for_app',
                data=json.dumps({
                    'message': 'stop the app',
                    'application_name': 'ai-shai-web-interface',
                    'application_folder': '/some/folder',
                    'action_operation': action,
                    'agentic_engine': 'shai',
                }),
                content_type='application/json',
            )
            events = _sse_events(response)

        return events, billing_mock

    def test_stop_advisor_path_does_not_record_billing(self, client):
        events, billing_mock = self._drive_generate(client, 'STOP')

        # Correct behavior: no real STOP executed, so billing must NOT be
        # recorded. On unfixed code billing IS recorded (rc==0), so this fails.
        assert billing_mock.call_count == 0, (
            "STOP advisor path (missing context, no real action) recorded "
            f"billing {billing_mock.call_count} time(s) despite no execution."
        )

    def test_stop_advisor_path_reports_failure_not_success(self, client):
        events, _ = self._drive_generate(client, 'STOP')

        done_events = [e for e in events if e.get('done')]
        assert done_events, f"No completion (done) event emitted. Events: {events}"
        final = done_events[-1]

        # Correct behavior: nothing was executed, so success must be False.
        # On unfixed code success == (returncode == 0) == True, so this fails.
        assert final.get('success') is False, (
            "STOP advisor path reported session success=True although no real "
            f"action was executed. Final event: {final}"
        )


class TestRecognizedVsInvalidConflation:
    """Test case 4 (edge): recognized action and invalid action must differ.

    A recognized START (missing context) and a genuinely invalid action must
    take DIFFERENT branches. On unfixed code both fall through to the same
    advisor fallback, so they are indistinguishable (the bug conflates them).
    """

    def test_recognized_start_and_invalid_action_take_different_branches(self, app):
        invalid_action = '###not-a-real-action###'

        with app.test_request_context():
            with patch('src.routes.genai_routes.os.path.exists', return_value=False):
                start_result = return_prompt_for_operator(
                    'START',
                    application_name='ai-shai-web-interface',
                    application_folder='/some/folder',
                    user_name='Test User',
                    user_email='test@example.com',
                )
                invalid_result = return_prompt_for_operator(
                    invalid_action,
                    application_name='ai-shai-web-interface',
                    application_folder='/some/folder',
                    user_name='Test User',
                    user_email='test@example.com',
                )

        # `return_prompt_for_operator` returns a tuple ``(l_prompt, is_executing)``
        # after the fix. Unpack both results so the branch comparison inspects
        # the resolved prompt/flag rather than the tuple wrapper.
        start_prompt, start_is_executing = start_result
        invalid_prompt, invalid_is_executing = invalid_result

        # The genuinely invalid action legitimately uses the advisor fallback.
        assert _is_advisor_fallback(invalid_prompt), (
            "Baseline expectation: a genuinely invalid action should use the "
            f"advisor fallback. Got: {invalid_prompt!r:.200}"
        )

        # The recognized START must take a DIFFERENT branch from the invalid
        # action. On unfixed code both returned the advisor fallback (identical
        # branch), so this assertion failed (counterexample proving the bug).
        assert start_result != invalid_result, (
            "Recognized START (missing context) took the SAME branch as a "
            "genuinely invalid action - both returned the advisor fallback. "
            "The system fails to differentiate recognized-but-unconfigured "
            "actions from genuinely invalid ones."
        )
        assert not _is_advisor_fallback(start_prompt), (
            "Recognized START (missing context) resolved to the advisor "
            "fallback instead of executing or failing explicitly."
        )
