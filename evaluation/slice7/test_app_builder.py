# Python 3.12 / unittest / httpx2 2.16.0; Slice 7 LOCAL evaluation (design v3 §6 driver, plan D14)
# Protects: the evaluated app is built as hosted (same validated settings, fixed provider endpoints, no retries),
# every provider call goes through the recording transport, Knowledge is the only Cloud collection requested,
# and operational records land in fresh local stand-ins. No network: provider and Cloud are controlled doubles,
# so these tests prove the wiring only, never provider or Cloud behaviour.
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx2  # MockTransport stands in for the provider network (as in the Slice 4 V4 driver's offline mode).

from app_builder import real_build  # Module under test (evaluation/slice7/app_builder.py).
from case_runner import run_case

ENV = {'CHROMA_HOST': 'chroma.invalid', 'CHROMA_API_KEY': 'chroma-secret', 'CHROMA_TENANT': 't',
       'CHROMA_DATABASE': 'd', 'SHOPUNOW_API_TOKEN': 'eval-token', 'SHOPUNOW_COLLECTIONS': 'cp,cc,ce',
       'SHOPUNOW_KNOWLEDGE_COLLECTION': 'kn', 'OPENAI_API_KEY': 'openai-secret', 'TYPESAFE_API_KEY': 'jev-secret'}


class FakeKnowledge:
    name, metadata = 'kn', {'embedding_model': 'text-embedding-3-small', 'embedding_dimensions': 1536}

    async def get(self, **kwargs):
        return {'ids': [], 'documents': [], 'metadatas': []}

    async def query(self, **kwargs):
        return {'ids': [[]], 'documents': [[]], 'metadatas': [[]], 'distances': [[]]}


class FakeCloud:
    def __init__(self):
        self.requested = []  # Every collection name the app asked Cloud for.

    async def get_collection(self, name, *, embedding_function=None):
        self.requested.append(name)
        return FakeKnowledge()


class RealBuildTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.spec = {'wire_dir': str(self.folder / 'wire'), 'ledger': str(self.folder / 'ledger.json')}
        self.cloud, self.sent = FakeCloud(), []

        def providers(request):  # Jev: controlled Shipping routing (V4 fixture); OpenAI: every call fails.
            self.sent.append(request)
            if request.url.path.endswith('/systemone'):
                from tests.test_incoming import judgment_answers  # Backend suite's controlled Jev answers.
                state = json.loads(request.content)['state']
                answers = {row['excerpt_id']: {'type': 'choice', 'choice': 'relevant', 'confidence': 1.,  # V4 shape.
                                               'probabilities': {'relevant': 1., 'irrelevant': 0., 'uncertain': 0.}}
                           for row in state['excerpts']} if 'excerpts' in state else judgment_answers()
                if 'excerpts' not in state:
                    answers['topic_shipping_delivery']['noul'] = 1.
                return httpx2.Response(200, json={'model': 'jev-1.14.0', 'answers': answers,
                                                  'usage': {'input_tokens': 100, 'output_tokens': 10}})
            return httpx2.Response(500, json={'error': {'message': 'controlled failure'}})
        self.inner = httpx2.MockTransport(providers)

        async def connect():
            return self.cloud
        self.connect = connect

    def build(self, environ=ENV):
        return real_build(self.spec, environ=environ, cloud_connect=self.connect, inner=self.inner)

    def test_question_runs_on_the_hosted_wiring_with_recorded_calls_and_local_records(self):
        app, local, evidence = self.build()
        result = asyncio.run(run_case(app, token='eval-token', user_id='syn-cust-01',
                                      turns=['Where is my parcel?']))
        self.assertEqual(result['turns'][0]['status'], 200)  # A safe outcome despite failing providers.
        self.assertEqual(self.cloud.requested, ['kn'])  # Only Knowledge reaches Cloud; never cp/cc/ce.
        self.assertEqual({request.url.host for request in self.sent},
                         {'api.openai.com', 'api.typesafe.ai'})  # Both provider clients were used ...
        for request in self.sent:  # ... only on the fixed public hosts, carrying the .env credentials.
            self.assertIn(request.url.host, {'api.openai.com', 'api.typesafe.ai'})
            secret = 'openai-secret' if request.url.host == 'api.openai.com' else 'jev-secret'
            self.assertTrue(any(secret in value for value in request.headers.values()))
        calls = evidence()['wire_calls']
        self.assertEqual(len(calls), len(self.sent))  # Each sent request was recorded once.
        self.assertEqual([call['http_status'] for call in calls],
                         [200 if call['endpoint'] == '/v1/systemone' else 500 for call in calls])
        bodies = [json.dumps(call['request'], sort_keys=True) for call in calls]
        self.assertEqual(len(bodies), len(set(bodies)))  # max_retries=0: no failed request was resent.
        self.assertTrue(json.loads((self.folder / 'ledger.json').read_text())['items'])  # Priced into the ledger.
        types = {meta['record_type'] for _, meta in local['conversations'].records.values()}
        self.assertIn('conversation_header', types)  # The conversation was written to the local stand-in.

    def test_provider_override_settings_are_refused_as_in_hosting(self):
        with self.assertRaisesRegex(ValueError, 'OPENAI_BASE_URL'):  # CD-099: config frozen as evaluated.
            self.build({**ENV, 'OPENAI_BASE_URL': 'https://elsewhere.invalid/v1'})
        with self.assertRaisesRegex(ValueError, 'TYPESAFE_API_KEY'):
            self.build({key: value for key, value in ENV.items() if key != 'TYPESAFE_API_KEY'})


if __name__ == '__main__':
    unittest.main()

# Summary: build the evaluated app from a settings dict with controlled provider/Cloud doubles (Jev routes to
# Shipping, OpenAI fails), run one question,
# and check hosts, credentials, recording, no retries, ledger, Cloud names and local writes; overrides refused.
