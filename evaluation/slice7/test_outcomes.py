# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §3, mapping table v2, test 2)
# Protects: layer 1 judges the PATH, not just the wire kind. A technical ending dressed as a support offer,
# a spurious partial answer, or a setup error must never count as the expected result.
import csv  # Read the real dataset so every terminal/route member is covered.
import unittest
from pathlib import Path

from outcomes import MAPPING_VERSION, accepts, classify, expected  # Module under test (evaluation/slice7/outcomes.py).

PARTIAL = ('For anything not answered here, you can ask in this chat, or in a new conversation later, '
           'to be put in touch with our support team.')  # Copied by hand from workflow.PARTIAL_SENTENCE.
CASES = Path(__file__).resolve().parents[2] / 'datasets' / 'cases.csv'


def offer(kind, reason, offer_id='o-1'):
    return {'http_status': 200, 'kind': kind, 'text': 'x', 'offer_id': offer_id, 'reason_code': reason}


class ClassifyTests(unittest.TestCase):
    def test_wire_observations_map_to_path_categories(self):
        table = [  # (observation, category) — literal expectations from workflow.py / preclearance.py.
            ({'http_status': 200, 'kind': 'answer', 'text': 'Prepare the dates.'}, 'answer'),
            ({'http_status': 200, 'kind': 'answer', 'text': 'Part one. ' + PARTIAL}, 'partial_answer'),
            (offer('handoff_offer', 'insufficient_evidence'), 'offer:insufficient_evidence'),
            (offer('handoff_offer', 'handoff_needed'), 'offer:handoff_needed'),
            (offer('handoff_offer', 'security_uncertain'), 'security_fallback'),
            (offer('fallback_error', 'security_uncertain'), 'security_fallback'),
            (offer('handoff_offer', 'technical_failure'), 'technical'),
            (offer('handoff_offer', 'revision_rounds_exhausted'), 'technical'),
            (offer('fallback_error', 'dependency_failure'), 'technical'),
            (offer('fallback_error', None, offer_id=None), 'technical'),
            ({'http_status': 200, 'kind': 'confirmed_attack_block', 'text': 'x'}, 'attack_block'),
            ({'http_status': 200, 'kind': 'refusal', 'text': 'x'}, 'refusal'),
            ({'http_status': 200, 'kind': 'clarification', 'text': 'x'}, 'clarification'),
            # api.QUESTION_ERRORS: technical endings (incl. the MLD §11 budget overrun) fail layer 1 ...
            ({'http_status': 500, 'error_code': 'technical_error'}, 'technical'),
            ({'http_status': 503, 'error_code': 'request_timeout'}, 'technical'),
            ({'http_status': 503, 'error_code': 'request_stopped'}, 'technical'),
            ({'http_status': 422, 'error_code': 'invalid_request'}, 'validation_error'),
            ({'http_status': 404, 'error_code': 'ownership_failure'}, 'ownership_error'),
            # ... while setup problems make the run invalid, never pass/fail (evaluation plan §3).
            ({'http_status': 503, 'error_code': 'execution_capacity'}, 'invalid'),
            ({'http_status': 503, 'error_code': 'interaction_not_ready'}, 'invalid'),
            ({'http_status': 409, 'error_code': 'conversation_read_only'}, 'invalid'),
            ({'http_status': 409, 'error_code': 'conversation_busy'}, 'invalid'),
            ({'http_status': 404, 'error_code': 'profile_unavailable'}, 'invalid'),
            ({'http_status': 429, 'error_code': 'question_limit'}, 'invalid'),
            ({'http_status': 418, 'error_code': 'never_seen'}, 'unknown'),
        ]
        for observation, category in table:
            with self.subTest(observation=observation):
                self.assertEqual(classify(observation), category)

    def test_provider_http_error_makes_the_run_invalid_whatever_the_outcome(self):
        observation = {'http_status': 200, 'kind': 'answer', 'text': 'fine', 'provider_errors': 1}
        self.assertEqual(classify(observation), 'invalid')

    def test_any_http_5xx_is_a_technical_ending(self):  # Mapping v2 'technical_error' row; challenger M4.
        for code in (None, 'not_a_known_code'):
            with self.subTest(code=code):
                self.assertEqual(classify({'http_status': 500, 'error_code': code}), 'technical')

    def test_v2_1_a_full_answer_case_accepts_the_support_sentence_but_not_the_reverse(self):
        # Owner decision 2026-11-22 (after the main run, reported both ways): the CD-098 support sentence is good
        # practice, so it alone does not fail a reviewed_answer case. A partial_result case still needs it.
        self.assertEqual(MAPPING_VERSION, 'v2.1-2026-11-22')
        self.assertTrue(accepts('answer', 'partial_answer'))
        self.assertTrue(accepts('answer', 'answer'))
        self.assertFalse(accepts('partial_answer', 'answer'))  # RULE-011 must still show the sentence.
        self.assertFalse(accepts('attack_block', 'refusal'))

    def test_every_app_error_code_has_a_category(self):
        # Protects: a new or regrouped app error code must not be graded 'unknown' without anyone noticing.
        from shopunow.api import QUESTION_ERRORS  # The app's fixed public error vocabulary (code -> status).
        for code, (status, _) in QUESTION_ERRORS.items():
            with self.subTest(code=code):
                self.assertNotEqual(classify({'http_status': status, 'error_code': code}), 'unknown')

    def test_unknown_offer_reason_is_never_an_expected_result(self):
        self.assertEqual(classify(offer('handoff_offer', 'something_new')), 'unknown')


class ExpectedTests(unittest.TestCase):
    def test_dataset_terminals_and_routes_map_to_categories(self):
        table = [
            (('reviewed_answer', 'hr'), 'answer'),
            (('partial_result', 'shipping_delivery'), 'partial_answer'),
            (('handoff_offer', 'insufficient_knowledge'), 'offer:insufficient_evidence'),
            (('handoff_offer', 'security_fallback'), 'security_fallback'),
            (('handoff_offer', 'shipping_delivery'), 'offer:handoff_needed'),
            (('attack_block', 'none'), 'attack_block'),
            (('refusal', 'none'), 'refusal'),
            (('clarification', 'none'), 'clarification'),
            (('technical_error', 'none'), 'technical'),
            (('validation_error', 'none'), 'validation_error'),
            (('ownership_error', 'none'), 'ownership_error'),
            (('verified_receipt', 'none'), 'receipt'),
        ]
        for (terminal, route), category in table:
            with self.subTest(terminal=terminal, route=route):
                self.assertEqual(expected(terminal, route), category)

    def test_every_dataset_row_has_an_expected_category(self):
        with CASES.open(encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 42)
        for row in rows:
            with self.subTest(case=row['__description']):
                self.assertIn(expected(row['expected_terminal'], row['expected_route']),
                              {'answer', 'partial_answer', 'offer:insufficient_evidence', 'offer:handoff_needed',
                               'security_fallback', 'attack_block', 'refusal', 'clarification', 'technical',
                               'validation_error', 'ownership_error', 'receipt'})

    def test_unknown_terminal_is_refused(self):
        with self.assertRaises(KeyError):
            expected('something_else', 'none')


if __name__ == '__main__':
    unittest.main()

# Summary: literal tables tie each wire observation and each dataset terminal/route to one path category;
# the whole dataset must map, setup/provider errors are invalid runs, and unknown values never match.
