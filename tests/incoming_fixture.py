# TypeSafe SDK 0.7.5 / OpenAI 3.46.0 / Chroma mapping 1.5.9; entirely offline.
import json  # Inspect the real native SDK request bodies at their external boundary.
from contextlib import ExitStack  # Close both explicitly owned native HTTP clients.
from datetime import datetime, timezone
import httpx2  # Both provider SDKs support its public MockTransport.
from openai import OpenAI
from typesafe_sdk import TypeSafeClient, RetryPolicy
from shopunow.sdk_writes import SDKRecords
from shopunow.workflow import run_incoming
from tests.test_incoming import judgment_answers
from tests.test_preclearance_writes import ImmediateBridge


class StoredCollection:
    """Controlled public collection boundary; preserves actual SDKRecords add/get mapping."""

    def __init__(self, name, owner):
        self.name, self.owner, self.records = name, owner, {}

    async def add(self, **payload):
        self.owner.storage_calls.append((self.name, 'add', payload))
        self.owner.storage_hook(self.name, 'add')
        for key, document, metadata in zip(payload['ids'], payload['documents'], payload['metadatas']):
            if key in self.records:
                raise ValueError('Duplicate immutable record.')
            self.records[key] = (document, metadata)

    async def get(self, *, ids, include):
        self.owner.storage_calls.append((self.name, 'get', ids))
        self.owner.storage_hook(self.name, 'get')
        return self.raw(ids)

    def raw(self, ids=None):
        keys = [key for key in (self.records if ids is None else ids) if key in self.records]
        return {'ids':keys, 'documents':[self.records[key][0] for key in keys],
                'metadatas':[self.records[key][1] for key in keys], 'embeddings':None,
                'included':['metadatas','documents']}


class IncomingFixture:
    """Actual native providers and graph, with controlled external model/storage results."""

    def __init__(self, tracking, *, answers=None, verdict='uncertain', trigger=0.5):
        self.tracking, self.trigger = tracking, trigger  # Provisional numbers are fixtures only.
        self.answers = judgment_answers() if answers is None else answers
        self.verdict = verdict
        self.calls, self.storage_calls = [], []
        self.error = None  # Test-only diagnostic; never part of public responses or telemetry.
        self.result = None
        self.model_hook = lambda name: None
        self.storage_hook = lambda name, operation: None
        self.stack = ExitStack()
        self.jev = self.stack.enter_context(TypeSafeClient(api_key='offline-test',
            retry=RetryPolicy(max_retries=0), transport=httpx2.MockTransport(self.judge)))
        self.reviewer = self.stack.enter_context(OpenAI(api_key='offline-test', max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.review))))
        self.offers = StoredCollection('offers', self)
        self.conversations = StoredCollection('conversations', self)
        self.storage = SDKRecords(ImmediateBridge(), profiles=None,
            conversations=self.conversations, escalations=self.offers)

    def close(self):
        self.stack.close()

    def judge(self, request):
        self.calls.append(('judge', json.loads(request.content)))
        self.model_hook('judge')
        return httpx2.Response(200, json={'model':'offline-jev', 'usage':{}, 'answers':self.answers})

    def review(self, request):
        self.calls.append(('review', json.loads(request.content)))
        self.model_hook('review')
        verdict = {'verdict':self.verdict, 'attack_types':
                   ['prompt_injection'] if self.verdict == 'confirmed_attack' else []}
        return httpx2.Response(200, json={'id':'resp_offline', 'object':'response', 'created_at':0,
            'status':'completed', 'model':'gpt-6-luna', 'output':[{'id':'msg_offline',
            'type':'message', 'role':'assistant', 'status':'completed', 'content':[
                {'type':'output_text', 'text':json.dumps(verdict), 'annotations':[]}]}]})

    def continue_admitted(self, *, request_id, **state):
        try:
            self.result = run_incoming(**state, tracking=self.tracking, storage=self.storage,
                request_id=request_id, jev_client=self.jev, reviewer_client=self.reviewer,
                jev_model='offline-jev', security_trigger=self.trigger, topic_trigger=self.trigger,
                utc_now=lambda:datetime(2026, 11, 9, tzinfo=timezone.utc))
            return self.result
        except Exception as error:
            self.error = error
            raise


# Flow: actual admitted callback -> native clients/graph -> native add/get mappings -> API.
