# Streamlit 1.67.0 AppTest + HTTPX 0.28.1 MockTransport; progress bar while a question runs (owner 2026-11-23).
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest import mock

from functools import partial

import httpx
import api_client
import test_app  # Reuse the controlled backend and page helpers (module import: its tests are not re-run here).
from test_app import CONV_A, Backend


class ProgressBackend(Backend):
    """The controlled backend plus GET /progress reporting a fixed step."""

    def __init__(self, step=2):
        super().__init__()
        self.step = step

    def __call__(self, request):
        if request.url.path == '/progress':
            params = dict(request.url.params)
            self.calls.append((request.method, request.url.path, params))
            return httpx.Response(200, json={'user_id': params['user_id'],
                                             'conversation_id': params['conversation_id'], 'step': self.step})
        return super().__call__(request)


class ClientProgressTests(unittest.TestCase):
    def client(self, backend):
        return api_client.Client('http://backend.test', 'token', transport=httpx.MockTransport(backend))

    def test_the_client_reads_the_step_and_treats_any_failure_as_unknown(self):
        # Break caught: a progress failure breaks the question, or the step is not read.
        self.assertEqual(self.client(ProgressBackend(3)).progress('syn-cust-01', CONV_A), 3)
        self.assertIsNone(self.client(Backend()).progress('syn-cust-01', CONV_A))  # 404: older backend.

    def run_with(self, seconds):
        updates = []
        with mock.patch.object(api_client, 'NOTICE_SECONDS', 0.1), ThreadPoolExecutor(1) as pool:
            future = pool.submit(lambda: time.sleep(seconds) or 'done')
            value = api_client.wait_with_progress(future, lambda: updates.append(True), interval=0.1)
        return value, updates

    def test_a_slow_question_updates_the_progress_repeatedly_and_still_returns_its_result(self):
        # Break caught: the bar is drawn once and never moves, or the wait gives up early.
        value, updates = self.run_with(0.6)
        self.assertEqual(value, 'done')
        self.assertGreaterEqual(len(updates), 2)

    def test_a_fast_question_shows_no_progress(self):
        self.assertEqual(self.run_with(0.0), ('done', []))


class PageProgressTests(unittest.TestCase):
    app, signed_in, chat = test_app.AppTests.app, test_app.AppTests.signed_in, test_app.AppTests.chat

    def setUp(self):
        self.backend = ProgressBackend(step=2)
        patcher = mock.patch.object(api_client, 'Client', partial(api_client.Client,
                                                                   transport=httpx.MockTransport(self.backend)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_running_question_shows_its_current_step_and_name(self):
        # Break caught: the page shows only the generic notice, or the wrong step text.
        at = self.chat()
        state = at.session_state
        current = (state['user_id'], state['conversation_id'], state['generation'])
        at.session_state['pending'] = {'sel': current, 'timer': {}, 'future': Future()}  # Still running.
        at.run()
        bars = at.get('progress')
        self.assertEqual(len(bars), 1)
        self.assertIn('Step 2 of 5', bars[0].proto.text)
        self.assertIn('Finding the right department', bars[0].proto.text)


if __name__ == '__main__':
    unittest.main()
