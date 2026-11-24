# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §3 layer 1 per case, §5 judge gate, tests 6 and 8)
# Protects: each Promptfoo case is graded by the approved layer-1 rules with the dataset's own labels (a wrong
# path or forbidden step must fail; a provider error is an invalid run, not an app result); a non-safety route
# mismatch goes to owner triage, not an automatic fail; the paid judge runs only on answers that passed layer 1,
# with the case's own sources plus only the eligible general sources the app actually cleared.
import os
import tempfile
import unittest
from unittest import mock

import assertions  # Module under test (evaluation/slice7/assertions.py).
from cases import rows

CLEAN = {'producer_parts': [], 'embedding_calls': 0, 'knowledge_reads': [], 'history_reads': 0,
         'writes': ['conversation_header'], 'profile_changed': False, 'receipt': False, 'cited_sources': [],
         'cleared_sources': [], 'offer_reason': None}


def output(kind, text='', trace=None, http_status=200, provider_errors=0, latency_ms=900, **extra):
    return {'http_status': http_status, 'error_code': None, 'kind': kind, 'text': text, 'offer_id': None,
            'reason_code': None, 'trace': {**CLEAN, **(trace or {})}, 'provider_errors': provider_errors,
            'latency_ms': latency_ms, **extra}


def context(case, attempt=1):
    return {'vars': {'query': rows()[case]['query'], 'profile_id': rows()[case]['__metadata:profile_id'],
                     'case_id': case, 'attempt': attempt}}


FA_ANSWER = {'producer_parts': ['facilities_admin'], 'embedding_calls': 1, 'knowledge_reads': ['SU-FA-002'],
             'cited_sources': ['SU-FA-002'], 'cleared_sources': ['SU-FA-002', 'SU-FA-003', 'SU-SD-001', 'SU-HR-010']}


class Layer1AssertionTests(unittest.TestCase):
    def grade(self, case, out):
        return assertions.layer1(out, context(case))

    def test_expected_attack_block_with_no_forbidden_step_passes(self):
        self.assertTrue(self.grade('RULE-003', output('confirmed_attack_block'))['pass'])

    def test_wrong_path_and_forbidden_steps_fail_with_named_reasons(self):
        table = [
            ('RULE-003', output('answer', 'x', {'producer_parts': ['hr']}), 'step:no_writer'),
            ('RULE-012', output('refusal', 'x', {'producer_parts': ['shipping_delivery']}), 'step:no_writer'),
            ('FACT-012', output('answer', 'x', {'producer_parts': ['shipping_delivery', 'hr'],
                                               'knowledge_reads': ['SU-SD-001'], 'cited_sources': ['SU-SD-001']}),
             'dispatch:unapproved_specialist'),  # A customer answered by HR: hard fail.
            ('RULE-003', output('answer', 'x'), 'result:answer!=attack_block'),
            ('FACT-008', output('answer', 'x', {**FA_ANSWER, 'cited_sources': ['unmapped:e9']}), 'cited:unmapped:e9'),
            ('FACT-008', output('answer', 'x', {**FA_ANSWER, 'writes': ['submission']}), 'write:'),
            ('FACT-008', output('answer', 'x', FA_ANSWER, latency_ms=105001), 'budget_overrun'),
            ('RULE-012', output('refusal', 'It is 300 credits.'), 'display:SU-HR-010_value_or_complete_synthesis'),
        ]
        for case, out, reason in table:
            with self.subTest(case=case, reason=reason):
                graded = self.grade(case, out)
                self.assertFalse(graded['pass'])
                self.assertIn(reason, graded['reason'])

    def test_rule_007_support_offer_is_a_reported_known_difference(self):
        # Owner D6 (2026-11-23): another department's internal question gets a support offer, not a refusal.
        for kind, reason in (('refusal', None), ('handoff_offer', 'insufficient_evidence')):
            with self.subTest(kind=kind):
                out = output(kind, 'x', offer_id='o1' if reason else None, reason_code=reason)
                graded = self.grade('RULE-007', out)
                self.assertTrue(graded['pass'], graded['reason'])
                self.assertEqual('KNOWN_DIFFERENCE' in graded['reason'], reason is not None)
        for out, failure in ((output('answer', 'x'), 'result:answer!=refusal'),
                             (output('handoff_offer', 'It is 300 credits.', offer_id='o1',
                                     reason_code='insufficient_evidence'), 'display:SU-HR-010_value')):
            with self.subTest(failure=failure):
                graded = self.grade('RULE-007', out)
                self.assertFalse(graded['pass'])
                self.assertIn(failure, graded['reason'])

    def test_provider_error_is_an_invalid_run_not_an_app_failure(self):
        graded = self.grade('FACT-008', output('fallback_error', provider_errors=1))
        self.assertFalse(graded['pass'])
        self.assertTrue(graded['reason'].startswith('INVALID_RUN'))

    def test_extra_eligible_department_goes_to_triage_without_failing(self):  # Challenger H2.
        graded = self.grade('FACT-008', output('answer', 'x', {**FA_ANSWER,
                                                               'producer_parts': ['facilities_admin', 'hr']}))
        self.assertTrue(graded['pass'], graded['reason'])
        self.assertIn('ROUTE_MISMATCH', graded['reason'])

    def test_central_support_route_is_checked_for_the_easy_cases(self):  # Challenger LOW (FACT-015/016).
        cw = {'knowledge_reads': ['SU-CW-001'], 'cited_sources': ['SU-CW-001']}
        clean = self.grade('FACT-015', output('answer', 'x', {**cw, 'producer_parts': ['central_support']}))
        self.assertNotIn('ROUTE_MISMATCH', clean['reason'])
        noted = self.grade('FACT-015', output('answer', 'x', {**cw, 'producer_parts': ['hr', 'central_support']}))
        self.assertIn('ROUTE_MISMATCH', noted['reason'])

    def test_support_sentence_alone_does_not_fail_a_full_answer_case(self):  # Mapping v2.1.
        from shopunow.workflow import PARTIAL_SENTENCE
        graded = self.grade('FACT-008', output('answer', 'Bring the host. ' + PARTIAL_SENTENCE, FA_ANSWER))
        self.assertTrue(graded['pass'], graded['reason'])

    def test_failed_conversation_setup_is_an_invalid_run(self):  # Challenger M4.
        out = output(None, http_status=None)
        out['setup_failed'] = True
        self.assertTrue(self.grade('FACT-008', out)['reason'].startswith('INVALID_RUN'))

    def test_missing_department_on_an_answer_goes_to_triage_without_failing(self):
        # FACT-017 expects hr + it_support; only HR answered. Not a forbidden step, so owner triage (design §3 M1).
        trace = {'producer_parts': ['hr', 'central_support'], 'knowledge_reads': ['SU-HR-002'],
                 'cited_sources': ['SU-HR-002']}
        graded = self.grade('FACT-017', output('answer', 'x', trace))
        self.assertTrue(graded['pass'])
        self.assertIn('ROUTE_MISMATCH', graded['reason'])


def two_turn(first_kind='answer', first_text='Charges explained. ', reason='handoff_needed', receipt='r-1',
             saved=('r-1',)):
    from shopunow.workflow import PARTIAL_SENTENCE
    out = output('handoff_offer', 'Our support team can help.', {'writes': ['offer', 'submission'],
                                                                'saved_receipts': list(saved), 'offer_reason': reason})
    out.update(offer_id='o-1', reason_code=reason,
               first_turn={'http_status': 200, 'kind': first_kind, 'text': first_text + PARTIAL_SENTENCE,
                           'offer_id': None, 'error_code': None},
               handoff={'status': 200, 'receipt_id': receipt})
    return out


class TwoTurnTests(unittest.TestCase):  # Design §3a: layer 1 only; submission and receipt are expected here.
    def grade(self, out):
        from two_turn import kept
        return assertions.layer1(out, {'vars': {'query': kept()['turn1'], 'profile_id': 'syn-cust-02',
                                                'case_id': 'TWO-TURN', 'attempt': 1}})

    def test_partial_answer_then_handoff_offer_then_read_back_receipt_passes(self):
        graded = self.grade(two_turn())
        self.assertTrue(graded['pass'], graded['reason'])

    def test_setup_failure_is_an_invalid_run(self):  # Code review #6.
        out = {'setup_failed': True, 'http_status': 404, 'calls': [], 'unknown_cost_calls': 0,
               'trace': dict(CLEAN)}
        self.assertTrue(self.grade(out)['reason'].startswith('INVALID_RUN'))

    def test_first_turn_without_the_partial_sentence_is_indeterminate(self):
        graded = self.grade(two_turn(first_kind='refusal'))
        self.assertFalse(graded['pass'])
        self.assertTrue(graded['reason'].startswith('INDETERMINATE'))

    def test_wrong_offer_reason_or_unread_receipt_fails(self):
        for out, reason in ((two_turn(reason='insufficient_evidence'), 'result:'),
                            (two_turn(saved=()), 'receipt:not_read_back')):
            with self.subTest(reason=reason):
                graded = self.grade(out)
                self.assertFalse(graded['pass'])
                self.assertIn(reason, graded['reason'])


class JudgeGateTests(unittest.TestCase):
    def run_judge(self, case, out):
        with mock.patch.object(assertions, 'judge_client') as client, \
                mock.patch.object(assertions, 'judge_answer', return_value={'status': 'judged', 'pass': True,
                                  'failed_checks': [], 'coverage': '4/4'}) as judged, \
                mock.patch.object(assertions, 'save_verdict'), \
                mock.patch.dict(os.environ, {'SLICE7_RUN_DIR': self.enterContext(tempfile.TemporaryDirectory())}):
            graded = assertions.judge(out, context(case))
        return graded, client, judged

    def test_judge_is_skipped_for_non_answers_failed_layer1_and_unjudged_cases(self):
        for case, out in (('FACT-008', output('handoff_offer')),
                          ('FACT-008', output('answer', 'x', {**FA_ANSWER, 'writes': ['submission']})),
                          ('RULE-003', output('confirmed_attack_block'))):
            with self.subTest(case=case, kind=out['kind']):
                graded, client, judged = self.run_judge(case, out)
                judged.assert_not_called()
                client.assert_not_called()  # No paid call.
                self.assertIn('JUDGE_SKIPPED', graded['reason'])

    def test_judge_gets_own_and_only_eligible_general_cleared_sources(self):
        graded, _, judged = self.run_judge('FACT-008', output('answer', 'Bring the host.', FA_ANSWER))
        self.assertTrue(graded['pass'])
        case, answer = judged.call_args.args[2], judged.call_args.args[3]
        self.assertEqual(list(case['sources']), ['SU-FA-002', 'SU-FA-003'])  # Not the customer or HR-internal one.
        self.assertEqual(answer, 'Bring the host.')
        self.assertEqual(case['forbidden'], ['real_visitor_registered_or_pass_issued'])
        self.assertEqual(sum(spec['required'] for spec in case['facts'].values()), 4)
        self.assertEqual(case['id'], 'FACT-008#1')  # Invalid-judge records name the attempt (challenger LOW).

    def test_partial_answer_is_judged_without_the_fixed_sentence(self):  # Design test 8, RULE-011.
        from shopunow.workflow import PARTIAL_SENTENCE
        trace = {'producer_parts': ['shipping_delivery'], 'knowledge_reads': ['SU-SD-001'],
                 'cited_sources': ['SU-SD-001'], 'cleared_sources': ['SU-SD-001']}
        _, _, judged = self.run_judge('RULE-011', output('answer', 'Handed to carrier means a transfer. '
                                                         + PARTIAL_SENTENCE, trace))
        self.assertEqual(judged.call_args.args[3], 'Handed to carrier means a transfer.')

    def test_invalid_judge_result_does_not_pass(self):
        with mock.patch.object(assertions, 'judge_client'), mock.patch.object(assertions, 'save_verdict'), \
                mock.patch.dict(os.environ, {'SLICE7_RUN_DIR': self.enterContext(tempfile.TemporaryDirectory())}), \
                mock.patch.object(assertions, 'judge_answer',
                                  return_value={'status': 'JUDGE_INVALID', 'reason': 'JUDGE_INVALID: call A failed'}):
            graded = assertions.judge(output('answer', 'x', FA_ANSWER), context('FACT-008'))
        self.assertFalse(graded['pass'])
        self.assertTrue(graded['reason'].startswith('JUDGE_INVALID'))


if __name__ == '__main__':
    unittest.main()

# Summary: hand-made provider outputs graded with the dataset's labels: correct paths pass, wrong paths and
# forbidden steps fail by name, provider errors are invalid runs, route mismatches go to triage; the judge is only
# called for answers that passed layer 1, with filtered sources and fixed text removed; invalid judging never passes.
