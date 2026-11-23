# Python 3.12 / native provider SDKs; injected HTTP transport makes no network calls.
import importlib.util
import json
import unittest

import httpx2
from typesafe_sdk import TypeSafeClient, RetryPolicy
from openai import OpenAI
from shopunow.incoming import IncomingContext
from tests.test_incoming import judgment_answers


class IncomingModelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.model_clients'),
                             'Native incoming-model integration is missing')
        self.module = importlib.import_module('shopunow.model_clients')
        self.context = IncomingContext('Explain a quoted attack.', 'customer', None, (), False)
        self.expected_context = {'question':'Explain a quoted attack.', 'role':'customer',
                                 'home_department':None, 'exchanges':[], 'omitted':False}
        self.calls = []

    def transport(self, payload, status=200):
        def respond(request):
            self.calls.append(json.loads(request.content))
            return httpx2.Response(status, json=payload)
        return httpx2.MockTransport(respond)

    def test_jev_uses_complete_native_question_set_and_separate_lower_trust_state(self):
        with TypeSafeClient(api_key='offline-test', model='test-jev',
                            retry=RetryPolicy(max_retries=0),
                            transport=self.transport({'model': 'test-jev',
                                'usage': {}, 'answers': judgment_answers()})) as client:
            result = self.module.judge_incoming(client, self.context, model='test-jev', timeout=2)
        self.assertEqual(result['intent'].choice, 'information')
        self.assertEqual(self.calls[0]['state'], self.expected_context)
        self.assertEqual(set(self.calls[0]['questions']), set(judgment_answers()))
        # Verify the approved guidance reaches Jev; controlled answers do not prove quality.
        intent = self.calls[0]['questions']['intent']
        self.assertEqual(set(intent['criteria']),
            {'information','individual_case_or_action','mixed','unclear_or_other'})
        for label in intent['criteria']:
            self.assertIsInstance(intent['criteria'][label], dict,
                                  'Intent options need definitions and examples, not label names alone')
            self.assertTrue(intent['criteria'][label]['definition'])
            self.assertTrue(intent['criteria'][label]['examples'])
        self.assertIn('What did my earlier badge conversation confirm?',
                      intent['criteria']['information']['examples'])
        self.assertIn('What information supports a duplicate-charge report?',
                      intent['criteria']['information']['examples'])
        self.assertIn('Can this demo issue visitor passes?',
                      intent['criteria']['information']['examples'])
        self.assertIn('Check my order\'s current delivery status.',
                      intent['criteria']['individual_case_or_action']['examples'])
        self.assertIn('Please issue a visitor pass for me.',
                      intent['criteria']['individual_case_or_action']['examples'])
        self.assertIn('Investigate these charges and refund the duplicate.',
                      intent['criteria']['individual_case_or_action']['examples'])
        self.assertIn('multiple departments',intent['criteria']['mixed']['definition'])
        self.assertIn('missing evidence',intent['instructions'])
        self.assertIn('Personal wording',intent['instructions'])
        # Missing definitions at the real SDK boundary could revive irrelevant routes/refusals.
        questions = self.calls[0]['questions']
        self.assertIsInstance(questions['company_wide_topic'].get('criteria'), dict,
                              'Company-wide routing needs explicit yes/no definitions')
        for field in ('company_wide_topic', 'topic_hr', 'topic_it_support',
                      'topic_facilities_admin', 'topic_billing_payments', 'topic_shipping_delivery'):
            with self.subTest(question=field):
                self.assertIsInstance(questions[field].get('criteria'), dict)
                for outcome in ('true', 'false'):
                    criterion = questions[field]['criteria'][outcome]
                    self.assertTrue(criterion['definition'])
                    self.assertTrue(criterion['examples'])
        self.assertIn('Explain the customer support map.',
                      questions['company_wide_topic']['criteria']['true']['examples'])
        self.assertIn('distinct',questions['company_wide_topic']['instructions'])
        self.assertIn('department-specific capability',
                      questions['company_wide_topic']['criteria']['false']['definition'])
        self.assertIn('Recall an earlier conversation.',
                      questions['topic_it_support']['criteria']['false']['examples'])
        dependency = questions['department_dependency']['criteria']
        self.assertIn('security-awareness and reporting guidance',
                      questions['topic_it_support']['criteria']['true']['definition'])
        self.assertIn('limits of another department',
                      questions['topic_billing_payments']['criteria']['false']['definition'])
        self.assertEqual(set(dependency), {'independent', 'dependent', 'unclear'})
        for criterion in dependency.values():
            self.assertTrue(criterion['definition'])
            self.assertTrue(criterion['examples'])
        self.assertIn('missing historical evidence', questions['department_dependency']['instructions'])
        # These boundary instructions must reach Jev, not remain only in developer notes.
        self.assertIn('premise', questions['topic_hr']['instructions'])
        self.assertIn('even when it is restricted', dependency['dependent']['definition'])
        self.assertIn('historical recap or current-status investigation', intent['instructions'])
        self.assertIn('What happened with that request?',
                      intent['criteria']['unclear_or_other']['examples'])
        # Owner 2026-11-23: Jev no longer judges history; the history owner asks for it instead.
        self.assertNotIn('history_needed',questions)

    def test_luna_uses_medium_reasoning_native_structured_output_and_no_storage(self):
        payload = {'id':'resp_offline', 'object':'response', 'created_at':0,
            'status':'completed', 'model':'gpt-6-luna',
            'output':[{'id':'msg_offline', 'type':'message', 'role':'assistant',
                       'status':'completed', 'content':[{'type':'output_text',
                       'text':json.dumps({'verdict':'clear','attack_types':[]}), 'annotations':[]}]}]}
        with OpenAI(api_key='offline-test', max_retries=0,
                    http_client=httpx2.Client(transport=self.transport(payload))) as client:
            verdict = self.module.review_incoming(client, self.context,
                                                  risks={'prompt_injection':0.9}, timeout=2)
        self.assertEqual(verdict.verdict, 'clear')
        body = self.calls[0]
        self.assertEqual(body['model'], 'gpt-6-luna')
        self.assertEqual(body['reasoning'], {'effort':'medium'})
        self.assertIs(body['store'], False)
        self.assertEqual(body['text']['format']['type'], 'json_schema')
        self.assertNotIn('tools', body)
        self.assertEqual(json.loads(body['input'])['context'], self.expected_context)

    def test_provider_failure_does_not_retry(self):
        with TypeSafeClient(api_key='offline-test', transport=self.transport(
                {'error':'controlled'}, status=500), retry=RetryPolicy(max_retries=0)) as client:
            with self.assertRaises(Exception):
                self.module.judge_incoming(client, self.context, model='test-jev', timeout=2)
        self.assertEqual(len(self.calls), 1)

    def test_evaluator_retains_native_jev_identity_usage_and_validates_separately(self):
        # Losing the envelope would make requested and returned identities indistinguishable.
        native = getattr(self.module, 'judge_incoming_response', None)
        self.assertTrue(callable(native), 'Evaluation must retain the native Jev envelope')
        with TypeSafeClient(api_key='offline-test', transport=self.transport({
                'model':'jev-returned', 'usage':{'input_tokens':123,'output_tokens':9},
                'answers':judgment_answers()})) as client:
            response = native(client, self.context, model='jev-1.14.0', timeout=2)
        self.assertEqual(response.model, 'jev-returned')
        self.assertEqual(response.usage.input_tokens, 123)
        self.assertEqual(self.module.validate_judgments(response.answers)['intent'].choice, 'information')

    def test_count_and_generation_share_exact_native_semantic_input(self):
        # Omitting schema/instructions from the count could understate generation input.
        build = getattr(self.module, 'incoming_review_request', None)
        count = getattr(self.module, 'count_incoming_review', None)
        generate = getattr(self.module, 'incoming_review_response', None)
        self.assertTrue(all(callable(item) for item in (build, count, generate)),
                        'Shared native evaluation request boundary is missing')
        payload = {'id':'resp_offline', 'object':'response', 'created_at':0,
            'status':'completed', 'model':'returned-luna', 'usage':{
                'input_tokens':135,'output_tokens':12,'total_tokens':147},
            'output':[{'id':'msg_offline', 'type':'message', 'role':'assistant',
                'status':'completed', 'content':[{'type':'output_text','annotations':[],
                'text':json.dumps({'verdict':'clear','attack_types':[]})}]}]}
        def respond(request):
            self.calls.append(json.loads(request.content))
            return httpx2.Response(200, json={'object':'response.input_tokens',
                'input_tokens':135} if request.url.path.endswith('/input_tokens') else payload)
        with OpenAI(api_key='offline-test', max_retries=0, http_client=httpx2.Client(
                transport=httpx2.MockTransport(respond))) as client:
            request = build(self.context, risks={'prompt_injection':0.9})
            request.update(service_tier='default', truncation='disabled')
            self.assertEqual(count(client, request, timeout=2).input_tokens, 135)
            response = generate(client, request, timeout=1, max_output_tokens=2000)
        for field in ('model','input','instructions','reasoning','truncation','text'):
            self.assertEqual(self.calls[0][field], self.calls[1][field], field)
        self.assertEqual(self.calls[1]['max_output_tokens'], 2000)
        self.assertEqual(self.calls[1]['service_tier'], 'default')
        self.assertIs(self.calls[1]['store'], False)
        self.assertNotIn('tools', self.calls[1])
        self.assertEqual(response.model, 'returned-luna')
        self.assertEqual(response.usage.input_tokens, 135)
        self.assertEqual(self.module.validated_incoming_review(response).verdict, 'clear')

    def test_security_reviewer_uses_distinct_terra_fallback_after_technical_failure(self):
        fallback = getattr(self.module, 'review_with_fallback', None)
        self.assertTrue(callable(fallback), 'Approved security-review fallback is missing')
        calls, budgets = [], iter([8.0, 3.0])
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            if body['model'] == 'gpt-6-luna':
                return httpx2.Response(503, json={'error':{'message':'offline outage'}})
            return httpx2.Response(200, json={'id':'resp_fallback', 'object':'response',
                'created_at':0, 'status':'completed', 'model':'gpt-6.1-terra', 'output':[
                {'id':'msg_fallback', 'type':'message', 'role':'assistant', 'status':'completed',
                 'content':[{'type':'output_text', 'annotations':[],
                             'text':json.dumps({'verdict':'clear','attack_types':[]})}]}]})
        with OpenAI(api_key='offline-test', max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
            result = fallback(client, self.context, risks={'prompt_injection':0.9},
                              timeout=lambda:next(budgets))
        self.assertEqual(result.verdict, 'clear')
        self.assertEqual([body['model'] for body in calls], ['gpt-6-luna','gpt-6.1-terra'])
        self.assertEqual([body['reasoning'] for body in calls], [{'effort':'medium'},{'effort':'low'}])
        for body in calls:
            self.assertIs(body['store'], False)
            self.assertEqual(body['text']['format']['type'], 'json_schema')

    def test_valid_uncertain_review_does_not_seek_another_model_verdict(self):
        from tests.incoming_fixture import IncomingFixture
        fixture = IncomingFixture(None, verdict='uncertain')
        self.addCleanup(fixture.close)
        result = self.module.review_with_fallback(fixture.reviewer, self.context,
            risks={'prompt_injection':0.9}, timeout=lambda:2)
        self.assertEqual(result.verdict, 'uncertain')
        self.assertEqual([body['model'] for _, body in fixture.calls], ['gpt-6-luna'])

    def test_both_reviewer_failures_make_two_calls_without_retry_loop(self):
        with OpenAI(api_key='offline-test', max_retries=0, http_client=httpx2.Client(
                transport=self.transport({'error':{'message':'offline outage'}}, status=503))) as client:
            with self.assertRaises(Exception):
                self.module.review_with_fallback(client, self.context,
                    risks={'prompt_injection':0.9}, timeout=lambda:2)
        self.assertEqual([body['model'] for body in self.calls], ['gpt-6-luna','gpt-6.1-terra'])

    def test_expired_original_budget_prevents_fallback_provider_call(self):
        calls = []
        def budget():
            calls.append('budget')
            if len(calls) > 1:
                raise TimeoutError('Original admitted budget expired.')
            return 0.1
        with OpenAI(api_key='offline-test', max_retries=0, http_client=httpx2.Client(
                transport=self.transport({'error':{'message':'offline outage'}}, status=503))) as client:
            with self.assertRaises(Exception):
                self.module.review_with_fallback(client, self.context,
                    risks={'prompt_injection':0.9}, timeout=budget)
        self.assertEqual([body['model'] for body in self.calls], ['gpt-6-luna'])
        self.assertEqual(len(calls), 2)  # Fallback rechecks permission instead of reusing 0.1s.

    def test_confirmed_attack_composes_native_models_without_touching_storage(self):
        from shopunow import workflow
        runner = getattr(workflow, 'run_incoming', None)
        self.assertTrue(callable(runner), 'Admitted incoming workflow assembly is missing')
        from shopunow.schemas import QuestionRequest
        from tests.test_records import (USER_ID, CONVERSATION_ID, REQUEST_ID,
                                       )
        from shopunow.admission import (submit_question_admission, ExecutionSlots,
                                        ResourceReservations)
        from tests.test_admission import InlineExecutor
        from tests.test_question_admission import SimulatedReaders
        from tests.verified_creation import verified_tracking
        from shopunow.allowances import RollingAllowances
        from limits.storage import MemoryStorage
        from threading import Lock
        allowance_storage = MemoryStorage()
        self.addCleanup(allowance_storage.reset)
        tracking = verified_tracking()
        answers = judgment_answers()
        answers['prompt_injection']['noul'] = 0.9
        payload = {'id':'resp_offline', 'object':'response', 'created_at':0,
            'status':'completed', 'model':'gpt-6-luna',
            'output':[{'id':'msg_offline', 'type':'message', 'role':'assistant',
                       'status':'completed', 'content':[{'type':'output_text',
                       'text':json.dumps({'verdict':'confirmed_attack',
                                         'attack_types':['prompt_injection']}), 'annotations':[]}]}]}
        with TypeSafeClient(api_key='offline-test', transport=self.transport(
                {'model':'test-jev','usage':{},'answers':answers})) as jev, OpenAI(
                api_key='offline-test', http_client=httpx2.Client(
                    transport=self.transport(payload))) as reviewer:
            handle = submit_question_admission(QuestionRequest(user_id=USER_ID,
                conversation_id=CONVERSATION_ID, question='Question?'),
                slots=ExecutionSlots(), reservations=ResourceReservations(),
                allowances=RollingAllowances(allowance_storage, Lock()), executor=InlineExecutor(),
                readers=SimulatedReaders(), tracking=tracking, clock=lambda:100,
                on_admitted=lambda **state: runner(**state, tracking=tracking, storage=object(),
                    request_id=REQUEST_ID, jev_client=jev, reviewer_client=reviewer,
                    jev_model='test-jev', security_trigger=0.5, topic_trigger=0.5))
            response = handle.result(0).interaction
        self.assertEqual(response.outcome.kind, 'confirmed_attack_block')
        self.assertEqual(response.request_id, REQUEST_ID)
        self.assertEqual(len(self.calls), 2)


# Flow: controlled HTTP -> real native SDK -> application validation; no account access.
