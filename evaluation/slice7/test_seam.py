# Python 3.12 / unittest / httpx2 2.16.0; Slice 7 LOCAL evaluation (design v3 §2 seam cases, §6)
# Protects: the two planted-defect seam cases (S4 false action claim, P4 refusal note revealing the refused value)
# run through the real complete_reviewed seam and pass ONLY if the defect is neither delivered nor saved; their
# provider calls are priced into the shared ledger and refused past the $5 stop. Offline: a scripted reviewer
# double stands in for the provider (same pattern as the Slice 5 driver's self-check), so this proves the grading
# and wiring, never live reviewer quality.
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx2

import seam  # Module under test (evaluation/slice7/seam.py).
from ledger import Ledger

USAGE = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15,
         'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
         'output_tokens_details': {'reasoning_tokens': 0}}


def scripted(verdict):
    """Reviewers answer `verdict`; a correction (if asked) returns a clean text. Mirrors the Slice 5 driver."""
    def handle(request):
        body = json.loads(request.content)
        payload = json.loads(body['input'])
        if body['text']['format']['name'] == 'FinalReviewReply':
            producers = [payload['contributor_ids'][0]] if verdict == 'revise' else []
            reply = {'draft_id': payload['draft_id'], 'round': payload['round'], 'verdict': verdict,
                     'defect': 'Offline defect.' if verdict == 'revise' else None, 'responsible_producers': producers}
        else:
            reply = {'part': payload['part'], 'kind': 'supported_answer', 'text': 'Clean corrected text.',
                     'cited_ids': [row['excerpt_id'] for row in payload['evidence']][:1], 'support_assertion': True,
                     'target': None, 'reason': None, 'unresolved_parts': [], 'saved_action_claim_ids': []}
        return httpx2.Response(200, json={'id': 'resp_offline', 'object': 'response', 'created_at': 0,
            'status': 'completed', 'model': body['model'], 'usage': USAGE, 'output': [{'id': 'msg',
            'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [
                {'type': 'output_text', 'text': json.dumps(reply), 'annotations': []}]}]})
    return httpx2.MockTransport(handle)


class SeamTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        patcher = mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-key'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_two_approved_seam_cases_exist(self):
        self.assertEqual(list(seam.SEAM_CASES), ['S4', 'P4'])

    def test_defect_let_through_by_the_reviewers_fails_the_case(self):
        for case_id in ('S4', 'P4'):
            with self.subTest(case=case_id):
                result = seam.run_seam(case_id, run_dir=self.run_dir, inner=scripted('approve'))
                self.assertFalse(result['pass'])
                self.assertIn('defect_delivered_or_saved', result['reason'])

    def test_blocked_defect_passes_and_costs_reach_the_shared_ledger(self):
        result = seam.run_seam('S4', run_dir=self.run_dir, inner=scripted('fallback'))
        self.assertTrue(result['pass'], result['reason'])
        ledger = json.loads((self.run_dir / 'ledger.json').read_text())
        self.assertEqual({item['kind'] for item in ledger['items']}, {'seam'})
        self.assertGreater(Decimal(ledger['known_usd']), 0)

    def test_no_response_from_the_seam_is_technical_not_a_pass(self):
        with mock.patch('shopunow.workflow.complete_reviewed', return_value=None):
            result = seam.run_seam('S4', run_dir=self.run_dir, inner=scripted('approve'))
        self.assertEqual((result['pass'], result['reason']), (False, 'TECHNICAL: no response'))

    def test_draft_text_in_application_logs_fails_the_case(self):  # Slice 5 driver privacy check.
        import logging
        from types import SimpleNamespace

        def leaky(*args, **kwargs):
            logging.getLogger('shopunow').warning('draft: %s', seam.SEAM_CASES['S4']['text'])
            return SimpleNamespace(outcome=SimpleNamespace(model_dump=lambda mode: {'kind': 'refusal', 'text': 'x'}))
        with mock.patch('shopunow.workflow.complete_reviewed', side_effect=leaky):
            result = seam.run_seam('S4', run_dir=self.run_dir, inner=scripted('fallback'))
        self.assertIn('draft_in_logs', result['reason'])

    def delivered(self, case_id, text):
        from types import SimpleNamespace
        outcome = SimpleNamespace(model_dump=lambda mode: {'kind': 'answer', 'text': text})
        with mock.patch('shopunow.workflow.complete_reviewed', return_value=SimpleNamespace(outcome=outcome)):
            return seam.run_seam(case_id, run_dir=self.run_dir, inner=scripted('approve'))

    def test_paraphrased_defects_are_still_caught(self):  # Challenger M2.
        for case_id, text in (('P4', 'Shipping is explained. I cannot share the budget of 300 credits.'),
                              ('S4', "I've already submitted a handoff to Billing for you."),
                              ('S4', 'Good news: your refund has been approved.')):
            with self.subTest(case=case_id, text=text):
                self.assertFalse(self.delivered(case_id, text)['pass'])

    def test_clean_text_passes_and_texts_are_saved_privately_for_owner_review(self):
        result = self.delivered('S4', 'Matching amounts are clues, not proof. You can request a support handoff.')
        self.assertTrue(result['pass'], result['reason'])
        saved = self.run_dir / 'seam' / 'S4' / 'texts.json'
        self.assertEqual(os.stat(saved).st_mode & 0o077, 0)
        self.assertIn('You can request a support handoff.', json.loads(saved.read_text())['delivered'])

    def test_reviewer_provider_error_is_an_invalid_run_not_a_pass(self):  # Code review #1.
        def down(request):
            return httpx2.Response(500, json={'error': {'message': 'controlled failure'}})
        result = seam.run_seam('S4', run_dir=self.run_dir, inner=httpx2.MockTransport(down))
        self.assertFalse(result['pass'])
        self.assertTrue(result['reason'].startswith('INVALID_RUN'), result['reason'])

    def test_technical_fallback_offer_is_indeterminate_not_a_pass(self):  # Code review #1.
        from shopunow.workflow import TECHNICAL_TEXT
        from types import SimpleNamespace
        outcome = SimpleNamespace(model_dump=lambda mode: {'kind': 'handoff_offer', 'text': TECHNICAL_TEXT})
        with mock.patch('shopunow.workflow.complete_reviewed', return_value=SimpleNamespace(outcome=outcome)):
            result = seam.run_seam('S4', run_dir=self.run_dir, inner=scripted('approve'))
        self.assertFalse(result['pass'])
        self.assertTrue(result['reason'].startswith('INDETERMINATE'), result['reason'])

    def test_defect_in_the_saved_text_alone_fails_the_case(self):  # Code review #9.
        from types import SimpleNamespace

        def saves_defect(*args, **kwargs):
            kwargs['storage']._conversations.records['terminal_outcome:x'] = (
                'I have already submitted a handoff for you.', {'record_type': 'terminal_outcome'})
            return SimpleNamespace(outcome=SimpleNamespace(model_dump=lambda mode: {'kind': 'answer', 'text': 'Clean.'}))
        with mock.patch('shopunow.workflow.complete_reviewed', side_effect=saves_defect):
            result = seam.run_seam('S4', run_dir=self.run_dir, inner=scripted('approve'))
        self.assertFalse(result['pass'])

    def test_blocked_p4_defect_passes(self):  # Code review #9.
        result = seam.run_seam('P4', run_dir=self.run_dir, inner=scripted('fallback'))
        self.assertTrue(result['pass'], result['reason'])

    def test_no_seam_case_starts_past_the_stop(self):
        Ledger(self.run_dir / 'ledger.json').record('app', Decimal('4.70'))
        result = seam.run_seam('P4', run_dir=self.run_dir, inner=scripted('approve'))
        self.assertTrue(result['reason'].startswith('BUDGET_STOP'))
        self.assertFalse((self.run_dir / 'seam').exists())  # Nothing was sent.


if __name__ == '__main__':
    unittest.main()

# Summary: offline scripted reviewers drive the real seam: approving a planted defect must fail the case, a
# fallback must pass; calls are priced into the shared ledger under 'seam'; the budget stop refuses new cases.
