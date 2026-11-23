# Python 3.12 / unittest; Slice 7 LOCAL evaluation — judge (design v3 §5, test 5, 6, 8)
# Protects: a judge failure never looks like an app failure (JUDGE_INVALID), the pass rule fails only on
# contradicted/unsupported claims, missing REQUIRED facts, forbidden claims or irrelevance, and the judge never
# sees code-owned fixed text or runs on a result that already failed layer 1.
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from judge import JUDGE_INVALID, decide, judge_answer, prepare_answer  # Module under test (judge.py).
from ledger import Ledger

PARTIAL = ('For anything not answered here, you can ask in this chat, or in a new conversation later, '
           'to be put in touch with our support team.')
FACTS = {'f1': {'text': 'first and last dates', 'required': True},
         'f2': {'text': 'cannot submit or approve', 'required': False}}


def call_a(*statuses, forbidden_present=False):
    return {'claims': [{'reason': 'r', 'claim': 'c%d' % index, 'status': status, 'source_id': 'SU-HR-002'}
                       for index, status in enumerate(statuses)],
            'forbidden': [{'reason': 'r', 'label': 'leave_request_submitted_or_approved',
                           'present': forbidden_present}]}


def call_b(f1=True, f2=True, relevant=True):
    return {'facts': {'f1': {'reason': 'r', 'covered': f1}, 'f2': {'reason': 'r', 'covered': f2}},
            'relevant': {'reason': 'r', 'verdict': relevant}}


class DecideTests(unittest.TestCase):
    def test_pass_rule(self):
        table = [  # (call A, call B, pass?, failed checks)
            (call_a('supported'), call_b(), True, []),
            (call_a('supported', 'contradicted'), call_b(), False, ['correct']),
            (call_a('unsupported'), call_b(), False, ['grounded']),
            (call_a('supported'), call_b(f1=False), False, ['required_facts']),
            (call_a('supported'), call_b(f2=False), True, []),  # A reported-only fact never fails the case.
            (call_a('supported', forbidden_present=True), call_b(), False, ['forbidden_claims']),
            (call_a('supported'), call_b(relevant=False), False, ['relevant']),
        ]
        for a, b, passed, failed in table:
            with self.subTest(a=a, b=b):
                verdict = decide(a, b, FACTS)
                self.assertIs(verdict['pass'], passed)
                self.assertEqual(verdict['failed_checks'], failed)

    def test_coverage_fraction_counts_all_facts(self):
        self.assertEqual(decide(call_a('supported'), call_b(f2=False), FACTS)['coverage'], '1/2')


class FakeResponses:
    """External OpenAI boundary double: returns queued replies or raises queued errors."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def create(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def completed(payload):
    return NS(status='completed', output_text=json.dumps(payload),
              output=[NS(type='message', content=[NS(type='output_text')])])


REFUSED = NS(status='completed', output_text='', output=[NS(type='message', content=[NS(type='refusal')])])
CUT_OFF = NS(status='incomplete', output_text='{"claims": [', output=[])


class JudgeAnswerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.directory.name) / 'ledger.json')
        self.case = {'id': 'FACT-002', 'query': 'What should I prepare?', 'facts': FACTS,
                     'forbidden': ['leave_request_submitted_or_approved'], 'sources': {'SU-HR-002': 'source text'},
                     'expected_answer': 'Prepare the dates.'}

    def tearDown(self):
        self.directory.cleanup()

    def run_judge(self, replies):
        client = NS(responses=FakeResponses(replies))
        return judge_answer(client, self.ledger, self.case, 'Prepare the first and last dates.'), client

    def test_two_valid_calls_give_a_verdict(self):
        verdict, client = self.run_judge([completed(call_a('supported')), completed(call_b())])
        self.assertTrue(verdict['pass'])
        self.assertEqual(len(client.responses.requests), 2)
        self.assertEqual({request['model'] for request in client.responses.requests}, {'gpt-6.1-terra'})
        self.assertNotIn('Prepare the dates.', client.responses.requests[0]['input'])  # No expected_answer in A.
        self.assertIn('Prepare the dates.', client.responses.requests[1]['input'])     # Coverage only (L3).

    def test_refusal_cut_off_schema_mismatch_and_api_error_are_judge_invalid_after_one_retry(self):
        bad_replies = (REFUSED, CUT_OFF, completed({'claims': 'not a list'}), RuntimeError('api down'))
        for bad in bad_replies:
            with self.subTest(reply=bad):
                verdict, client = self.run_judge([bad, bad])
                self.assertEqual(verdict['status'], JUDGE_INVALID)
                self.assertNotIn('pass', verdict)  # Never reported as an app pass or an app fail.
                self.assertEqual(len(client.responses.requests), 2)  # One retry, then stop.

    def test_missing_fact_id_in_call_b_is_judge_invalid(self):
        partial_b = {'facts': {'f1': {'reason': 'r', 'covered': True}}, 'relevant': {'reason': 'r', 'verdict': True}}
        verdict, _ = self.run_judge([completed(call_a('supported')), completed(partial_b), completed(partial_b)])
        self.assertEqual(verdict['status'], JUDGE_INVALID)

    def test_forbidden_list_missing_the_case_label_is_judge_invalid(self):  # Challenger M1.
        empty_a = {**call_a('supported'), 'forbidden': []}
        # Without the check, A is accepted and B then succeeds on the third reply: a wrong 'judged' verdict.
        verdict, _ = self.run_judge([completed(empty_a), completed(empty_a), completed(call_b())])
        self.assertEqual(verdict['status'], JUDGE_INVALID)

    def test_judge_call_refused_by_the_budget_sends_nothing_and_is_invalid(self):  # Code review #3.
        self.ledger = Ledger(Path(self.directory.name) / 'capped.json', judge_cap=0)
        verdict, client = self.run_judge([completed(call_a('supported')), completed(call_b())])
        self.assertEqual(verdict['status'], JUDGE_INVALID)
        self.assertEqual(client.responses.requests, [])  # No paid call was sent.

    def test_a_maxed_judge_call_fits_its_reservation(self):  # Code review #7: the $5 stop holds.
        from ledger import JUDGE_RESERVATION_USD
        from wire import price
        from judge import MAX_OUTPUT_TOKENS
        inputs = 3000  # Above the largest calibration input (670 tokens); assumption.
        worst = price('gpt-6.1-terra', {'input_tokens': inputs, 'output_tokens': MAX_OUTPUT_TOKENS,
                                        'total_tokens': inputs + MAX_OUTPUT_TOKENS,
                                        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}})
        self.assertLessEqual(worst, JUDGE_RESERVATION_USD)

    def test_judge_invalid_is_counted_in_the_ledger(self):
        self.run_judge([REFUSED, REFUSED])
        self.ledger.judge_invalid('x')
        self.ledger.judge_invalid('y')
        self.ledger.judge_invalid('z')
        self.assertTrue(self.ledger.run_invalid())  # 1 from the judge + 3 here > 3.


class PrepareAnswerTests(unittest.TestCase):
    def test_judge_is_skipped_unless_layer1_passed_on_an_answer(self):
        self.assertIsNone(prepare_answer({'kind': 'refusal', 'text': 'x'}, layer1_passed=True))
        self.assertIsNone(prepare_answer({'kind': 'answer', 'text': 'x'}, layer1_passed=False))

    def test_fixed_partial_sentence_is_removed_before_judging(self):
        text = prepare_answer({'kind': 'answer', 'text': 'Handed to carrier means a transfer. ' + PARTIAL},
                              layer1_passed=True)
        self.assertEqual(text, 'Handed to carrier means a transfer.')


if __name__ == '__main__':
    unittest.main()

# Summary: literal judge replies drive the pass rule (required vs reported facts), the coverage fraction, the
# JUDGE_INVALID path (refusal, cut-off, schema mismatch, API error; one retry; counted in the ledger), the
# A/B input separation (expected_answer only in call B), the skip rule and fixed-text removal.
