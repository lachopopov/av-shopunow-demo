# Python 3.12 / FastAPI 0.143.2 / HTTPX 0.28.1; answer progress steps (owner request 2026-11-23).
# The UI shows which step a running question has reached; the backend keeps only a step number per
# (user, conversation) in memory, never question or answer content.
import unittest

import httpx  # ASGI transport: calls the app in-process, no server.

from shopunow.progress import Progress  # In-memory step store shared by the workflow and the route.
from shopunow.storage_app import create_storage_app  # The served app that hosts GET /progress.

TOKEN = 'synthetic-test-token'
USER, CONV = 'syn-cust-01', '0000000a-0000-4000-8000-00000000000a'


class ProgressStoreTests(unittest.TestCase):
    def test_a_step_never_moves_back_and_is_gone_after_the_question_ends(self):
        # Break caught: a late supplementary search moves the bar backwards, or a finished question
        # keeps showing a step to the next question in the same conversation.
        progress = Progress()
        self.assertIsNone(progress.get(USER, CONV))
        progress.report(USER, CONV, 4)
        progress.report(USER, CONV, 3)
        self.assertEqual(progress.get(USER, CONV), 4)
        self.assertIsNone(progress.get(USER, 'other-conversation'))
        progress.clear(USER, CONV)
        self.assertIsNone(progress.get(USER, CONV))


class ProgressRouteTests(unittest.IsolatedAsyncioTestCase):
    async def get(self, progress, token=TOKEN, params=None):
        async def connect():  # Never awaited: the progress route needs no storage.
            raise AssertionError('storage must not be used')
        app = create_storage_app(connect=connect, api_token=TOKEN, collection_names=('p', 'c', 'e'),
                                 progress=progress)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/progress', params=params or {'user_id': USER, 'conversation_id': CONV},
                                    headers={'x-api-token': token})

    async def test_the_current_step_is_returned_for_the_asking_conversation_only_with_the_token(self):
        # Break caught: progress readable without the API token, or the UI cannot read the step.
        progress = Progress()
        progress.report(USER, CONV, 2)
        response = await self.get(progress)
        self.assertEqual((response.status_code, response.json()['step']), (200, 2))
        self.assertEqual((response.json()['user_id'], response.json()['conversation_id']), (USER, CONV))
        self.assertEqual((await self.get(progress, token='wrong')).status_code, 401)
        progress.clear(USER, CONV)
        self.assertIsNone((await self.get(progress)).json()['step'])


if __name__ == '__main__':
    unittest.main()
