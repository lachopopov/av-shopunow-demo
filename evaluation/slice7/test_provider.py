# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §6 provider, test 7; §7 budget)
# Protects: only the dataset query and profile reach the app (evaluator data leaking in would inflate results);
# a case never starts past the $5 stop; Promptfoo gets the in-process latency, and cost/tokens only when every
# call's usage is known (Promptfoo would add a missing cost as $0).
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import provider  # Module under test (evaluation/slice7/provider.py).
from cases import rows
from ledger import Ledger

QUERY = rows()['FACT-008']['query']


def context(case='FACT-008', attempt=1, query=QUERY, profile=None):
    profile = profile or rows()[case]['__metadata:profile_id']
    return {'vars': {'query': query, 'profile_id': profile, 'case_id': case, 'attempt': attempt,
                     'expected_answer': 'EVALUATOR-ONLY'}}  # Must never reach the app.


def call(model, usd, usage):
    return {'endpoint': '/v1/responses', 'model': model, 'http_status': 200, 'usd': usd, 'usage': usage}


TRACE = {'offer_reason': None, 'producer_parts': ['facilities_admin'], 'cited_sources': ['SU-FA-002']}


def result(calls, kind='answer'):
    return {'turns': [{'status': 200, 'latency_ms': 1234.6,
                       'body': {'outcome': {'kind': kind, 'text': 'Bring the host name.', 'cited_ids': ['e1']}}}],
            'wire_calls': calls, 'trace': TRACE}


class SpecTests(unittest.TestCase):
    def test_only_query_and_profile_reach_the_app(self):
        spec = provider.case_spec(QUERY, context()['vars'], run_dir=Path('/tmp/run'), token='t',
                                  builder='app_builder:real_build')
        self.assertEqual(spec['turns'], [QUERY])
        self.assertEqual(spec['user_id'], rows()['FACT-008']['__metadata:profile_id'])
        self.assertEqual(set(spec), {'builder', 'token', 'user_id', 'turns', 'wire_dir', 'ledger'})
        self.assertNotIn('EVALUATOR-ONLY', repr(spec))

    def test_two_turn_case_sends_both_turns_and_the_fictitious_contact(self):
        from two_turn import kept
        case = kept()
        variables = {'query': case['turn1'], 'profile_id': case['profile_id'], 'case_id': 'TWO-TURN', 'attempt': 1}
        spec = provider.case_spec(case['turn1'], variables, run_dir=Path('/tmp/run'), token='t', builder='x:y')
        self.assertEqual(spec['turns'], [case['turn1'], case['turn2']])
        self.assertEqual(spec['handoff'], case['contact'])
        self.assertEqual(spec['user_id'], 'syn-cust-02')

    def test_prompt_or_profile_differing_from_the_dataset_is_refused(self):
        for variables, prompt in ((context()['vars'], 'Another question'),
                                  (context(profile='syn-cust-01')['vars'], QUERY)):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                provider.case_spec(prompt, variables, run_dir=Path('/tmp/run'), token='t', builder='x:y')


class ResponseTests(unittest.TestCase):
    def test_latency_cost_and_tokens_when_usage_is_complete(self):
        usage = {'input_tokens': 100, 'output_tokens': 20}
        response = provider.respond(result([call('gpt-6.1-sol', '0.0004', usage), call('gpt-6-luna', '0.0001', usage)]))
        self.assertEqual(response['latencyMs'], 1235)
        self.assertEqual(response['output']['latency_ms'], 1234.6)  # For layer 1's budget check (design §3 #10).
        self.assertEqual(Decimal(str(response['cost'])), Decimal('0.0005'))
        self.assertEqual(response['tokenUsage'], {'prompt': 200, 'completion': 40, 'total': 240, 'numRequests': 2})
        self.assertEqual(response['output']['kind'], 'answer')
        self.assertEqual(response['output']['text'], 'Bring the host name.')
        self.assertEqual(response['output']['trace'], TRACE)

    def test_cost_and_tokens_are_omitted_when_any_usage_is_unknown(self):
        response = provider.respond(result([call('gpt-6.1-sol', '0.0004', {'input_tokens': 1, 'output_tokens': 1}),
                                            call('gpt-6-luna', None, None)]))
        self.assertNotIn('cost', response)
        self.assertNotIn('tokenUsage', response)
        self.assertEqual(response['output']['unknown_cost_calls'], 1)

    def test_error_body_code_is_kept_for_the_mapping(self):
        response = provider.respond({'turns': [{'status': 503, 'latency_ms': 5,
                                                'body': {'error': {'code': 'execution_capacity'}}}],
                                     'wire_calls': [], 'trace': TRACE})
        self.assertEqual((response['output']['http_status'], response['output']['error_code']),
                         (503, 'execution_capacity'))


class TwoTurnResponseTests(unittest.TestCase):
    def test_first_turn_and_handoff_are_reported(self):
        two = result([])
        two['turns'].insert(0, {'status': 200, 'latency_ms': 50.0,
                                'body': {'outcome': {'kind': 'answer', 'text': 'Part. Sentence.'}}})
        two['handoff'] = {'status': 200, 'body': {'receipt_id': 'r-1', 'offer_id': 'o-1'}}
        output = provider.respond(two)['output']
        self.assertEqual(output['first_turn'], {'http_status': 200, 'kind': 'answer', 'text': 'Part. Sentence.',
                                                'offer_id': None, 'error_code': None})
        self.assertEqual(output['handoff'], {'status': 200, 'receipt_id': 'r-1'})
        self.assertEqual(output['latency_ms'], 1234.6)  # Turn 2's POST /interactions (design §3a).


class ProviderErrorTests(unittest.TestCase):  # Challenger H1/M4.
    def test_only_completed_http_errors_make_provider_errors_and_timeouts_stay_visible(self):
        timed_out = {**call('gpt-6-luna', None, None), 'http_status': None, 'transport_error': 'ReadTimeout'}
        output = provider.respond(result([call('gpt-6.1-sol', '0.0004', {'input_tokens': 1, 'output_tokens': 1}),
                                          timed_out]))['output']
        self.assertEqual((output['provider_errors'], output['transport_errors']), (0, ['ReadTimeout']))
        failed = {**call('gpt-6-luna', None, None), 'http_status': 500}
        self.assertEqual(provider.respond(result([failed]))['output']['provider_errors'], 1)

    def test_raw_5xx_without_json_body_is_still_graded(self):
        output = provider.respond({'turns': [{'status': 500, 'latency_ms': 3, 'body': None}], 'wire_calls': [],
                                   'trace': TRACE})['output']
        self.assertEqual((output['http_status'], output['kind'], output['error_code']), (500, None, None))

    def test_failed_conversation_setup_is_reported(self):
        output = provider.respond({'conversation': {'status': 503, 'body': None}, 'turns': [], 'setup_failed': True,
                                   'wire_calls': [], 'trace': TRACE})['output']
        self.assertTrue(output['setup_failed'])


class BudgetTests(unittest.TestCase):
    def test_no_case_starts_past_the_stop(self):
        run = Path(self.enterContext(tempfile.TemporaryDirectory()))
        Ledger(run / 'ledger.json').record('app', Decimal('4.70'))  # 4.70 + 0.40 reservation > 5.00.
        with mock.patch.dict(os.environ, {'SLICE7_RUN_DIR': str(run), 'SHOPUNOW_API_TOKEN': 't'}), \
                mock.patch.object(provider, 'run_in_subprocess') as runner:
            response = provider.call_api(QUERY, {'config': {}}, context())
        runner.assert_not_called()
        self.assertTrue(response['error'].startswith('BUDGET_STOP'))


if __name__ == '__main__':
    unittest.main()

# Summary: case_spec carries only the dataset query/profile (bound to the case ID); respond() maps the subprocess
# JSON to Promptfoo's output/latencyMs/cost/tokenUsage without inventing zero cost; the ledger stops new cases.
