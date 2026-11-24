# Python 3.12 / AnyIO v4.15.1; owner 2026-11-24 (cleanup step 1): the question host and the storage (history)
# host share one owned-application runner. Breaks caught: cleanup skips stopping admission, runs unshielded,
# swallows the application's exception, or leaves the state flags unset.
import unittest

import anyio

from shopunow.question_host import run_owned_application


def fresh_state(handle):
    return {'handle': handle, 'cleanup_started': anyio.Event(), 'application_done': False}


class OwnedApplicationTests(unittest.TestCase):
    def run_app(self, application, state):
        stopped = []

        async def stop(handle):
            await anyio.lowlevel.checkpoint()  # A real await: cleanup must still finish under the shield.
            stopped.append(handle)

        async def main():
            await run_owned_application(application, {'type': 'http'}, None, None, state=state, stop_admission=stop)
        return main, stopped

    def test_normal_run_stops_admission_and_marks_the_state(self):
        state = fresh_state('handle-1')

        async def application(scope, receive, send):
            assert state['application_scope'] is not None  # The child runs inside its own cancel scope.

        main, stopped = self.run_app(application, state)
        anyio.run(main)
        self.assertEqual(stopped, ['handle-1'])
        self.assertTrue(state['cleanup_started'].is_set())
        self.assertTrue(state['application_done'])
        self.assertIn('group', state)

    def test_application_error_is_re_raised_after_cleanup(self):
        state = fresh_state('handle-2')

        async def application(scope, receive, send):
            raise RuntimeError('controlled application failure')

        main, stopped = self.run_app(application, state)
        with self.assertRaisesRegex(RuntimeError, 'controlled application failure'):
            anyio.run(main)
        self.assertEqual(stopped, ['handle-2'])  # Cleanup ran before the error surfaced.
        self.assertTrue(state['application_done'])

    def test_cancelled_application_still_stops_admission_under_the_shield(self):
        state = fresh_state('handle-3')

        async def application(scope, receive, send):
            state['application_scope'].cancel()  # As the disconnect observer does.
            await anyio.sleep(10)

        main, stopped = self.run_app(application, state)
        anyio.run(main)
        self.assertEqual(stopped, ['handle-3'])
        self.assertTrue(state['application_done'])

    def test_no_handle_means_nothing_to_stop(self):
        state = fresh_state(None)

        async def application(scope, receive, send):
            return None

        main, stopped = self.run_app(application, state)
        anyio.run(main)
        self.assertEqual(stopped, [])
        self.assertTrue(state['cleanup_started'].is_set())


class ProductionHostWiringTests(unittest.TestCase):
    # Reviewer gap 2026-11-24 (storage_app.py, run_owned_application call): the history tests use test-only host copies, so the production
    # history host's own stop function was unchecked. Break caught: it stops question admission, or nothing.
    def test_the_history_host_stops_history_admission_not_question_admission(self):
        from unittest.mock import patch
        from shopunow import api
        from shopunow.storage_app import create_storage_app

        async def connect():  # Never awaited: no lifespan runs in this test.
            raise AssertionError('storage must not be used')
        app = create_storage_app(connect=connect, api_token='synthetic-test-token', collection_names=('p', 'c', 'e'))
        stopped = []

        async def application(scope, receive, send):
            scope['identity_http']['handle'] = 'history-handle'  # As a successful history submission does.
        app.application = application

        async def history_stop(handle):
            stopped.append(('history', handle))

        async def question_stop(handle):
            stopped.append(('question', handle))

        async def main():
            await app({'type': 'http', 'path': '/history'}, None, None)
        with patch.object(api, 'stop_history_admission', history_stop), \
                patch.object(api, 'stop_question_admission', question_stop):
            anyio.run(main)
        self.assertEqual(stopped, [('history', 'history-handle')])


if __name__ == '__main__':
    unittest.main()

# Flow: each test builds a fresh host state, runs one application through the shared runner, and checks
# admission stop, the cleanup flag, the done flag and error re-raise.
