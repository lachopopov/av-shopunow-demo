# Python 3.12 / unittest / httpx 0.28.1; Slice 7 LOCAL evaluation (design v3 §6, test 3a)
# Protects: each case runs the REAL storage app through its lifespan (else /interactions answers 503
# execution_capacity), against fresh local stand-ins with the seeded synthetic profile, in its own process
# (CD-044: one client/loop/lifespan per process), and returns JSON with per-turn latency.
import asyncio
import unittest

from case_runner import run_case, run_in_subprocess  # Module under test (evaluation/slice7/case_runner.py).
from routing import RoutingClient
from stand_ins import LocalCollection, seed_profiles

TOKEN = 'synthetic-eval-token'
CALLS = []  # Profiles the stub answer step saw (in-process test only).


def stub_build(spec):
    """Real create_storage_app with local stand-ins and a fixed refusal instead of model calls (no network)."""
    from shopunow.profiles import load_profiles
    from shopunow.schemas import InteractionResponse, RefusalOutcome
    from shopunow.storage_app import create_storage_app
    local = {name: LocalCollection(name) for name in ('profiles', 'conversations', 'escalations')}
    asyncio.run(seed_profiles(local['profiles'], load_profiles()))

    async def connect():
        return RoutingClient(None, knowledge_name='not-used', local=local)

    def answering(*, storage, tracking):
        def admitted(*, request_id, request, profile, transcript, attempt):
            CALLS.append(profile.user_id)
            return InteractionResponse(request_id=request_id, user_id=request.user_id,
                                       conversation_id=request.conversation_id,
                                       outcome=RefusalOutcome(kind='refusal', text='Fixed stub refusal.'))
        return admitted

    app = create_storage_app(connect=connect, api_token=TOKEN, collection_names=tuple(local),
                             answering=answering)
    return app, local, lambda: {'wire_calls': [], 'knowledge_reads': []}  # No providers in the stub.


class CaseRunnerTests(unittest.TestCase):
    def test_case_runs_through_the_app_lifespan_with_the_seeded_profile(self):
        CALLS.clear()
        app, local, _ = stub_build({})
        result = asyncio.run(run_case(app, token=TOKEN, user_id='syn-cust-01', turns=['Where is my parcel?']))
        self.assertEqual(result['conversation']['status'], 200)
        self.assertEqual(result['turns'][0]['status'], 200)  # 503 execution_capacity if no lifespan ran.
        self.assertEqual(result['turns'][0]['body']['outcome']['kind'], 'refusal')
        self.assertGreater(result['turns'][0]['latency_ms'], 0)
        self.assertEqual(CALLS, ['syn-cust-01'])  # The profile came from the seeded local stand-in.
        types = {meta['record_type'] for _, meta in local['conversations'].records.values()}
        self.assertIn('conversation_header', types)  # Real create wrote to the stand-in, not to Cloud.

    def test_failed_conversation_creation_stops_the_case_as_a_setup_failure(self):  # Challenger M4.
        app, _, _ = stub_build({})
        result = asyncio.run(run_case(app, token=TOKEN, user_id='syn-unknown-99', turns=['Where is my parcel?']))
        self.assertNotEqual(result['conversation']['status'], 200)
        self.assertTrue(result['setup_failed'])
        self.assertEqual(result['turns'], [])  # No question was sent on a conversation that does not exist.

    def test_non_json_body_is_kept_as_none(self):
        import httpx
        from case_runner import _json
        self.assertIsNone(_json(httpx.Response(500, content=b'')))
        self.assertEqual(_json(httpx.Response(200, json={'a': 1})), {'a': 1})

    def test_case_runs_in_its_own_process_and_returns_json(self):
        result = run_in_subprocess({'builder': 'test_case_runner:stub_build', 'token': TOKEN,
                                    'user_id': 'syn-cust-02', 'turns': ['Is this a refund?']}, timeout=60)
        self.assertEqual(result['turns'][0]['status'], 200)
        self.assertEqual(result['turns'][0]['body']['outcome']['kind'], 'refusal')
        self.assertNotEqual(result['pid'], __import__('os').getpid())  # A separate process ran the case.
        self.assertEqual(result['wire_calls'], [])  # Provider calls come back for cost and the judge's sources.
        self.assertIn('conversation_header', result['trace']['writes'])  # Layer-1 trace built in the subprocess.
        self.assertFalse(result['trace']['profile_changed'])  # Compared with the seeded profiles before the run.


if __name__ == '__main__':
    unittest.main()

# Summary: the real storage app (stub answer step, local stand-ins, seeded profiles) must answer 200 only
# because the runner ran its lifespan; a second test proves the same case runs in a separate process as JSON.
