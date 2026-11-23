# Python 3.12 / Pydantic 2.14.1; real verified transcripts, no provider calls.
import importlib  # Discover an absent capability as an explicit behavioral failure.
import unittest
from copy import deepcopy
from uuid import UUID

from shopunow.records import verify_complete_transcript, verify_profile
from shopunow.schemas import QuestionRequest
from tests.test_records import (
    USER_ID, CONVERSATION_ID, event_result, header_result, profile_result,
    transcript_pair, transcript_marker, transcript_offer,
)


class IncomingContextTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.incoming'),
                             'Owned incoming-context selection is missing')
        self.incoming = importlib.import_module('shopunow.incoming')
        self.profile = verify_profile(profile_result(), user_id=USER_ID).record

    def context(self, rows=(), *, question='Current question', offers=None, **changes):
        transcript = verify_complete_transcript(
            header_result(), event_result(*rows), user_id=USER_ID,
            conversation_id=CONVERSATION_ID, offer_results=offers or {},
        ).record  # Verify storage-shaped input through existing code first.
        self.assertIsNotNone(transcript)
        request = QuestionRequest(**dict(user_id=USER_ID, conversation_id=CONVERSATION_ID,
                                        question=question, **changes))
        return self.incoming.build_incoming_context(
            transcript, profile=self.profile, request=request)

    def test_empty_context_captures_stored_binding_and_exact_question(self):
        context = self.context(question='  Why?  ')
        self.assertEqual(context.question, '  Why?  ')
        self.assertEqual((context.role, context.home_department), ('customer', None))
        self.assertEqual(context.exchanges, ())
        self.assertFalse(context.omitted)

    def test_latest_six_complete_exchanges_remain_in_chronological_order(self):
        rows = [row for number in range(1, 9)
                for row in transcript_pair(number, 2 * number - 1)]
        context = self.context(rows)
        self.assertEqual([exchange.assistant_text for exchange in context.exchanges],
                         [f'Stored answer {number}' for number in range(3, 9)])
        self.assertTrue(context.omitted)

    def test_budget_includes_question_and_never_splits_an_exchange(self):
        first, second = transcript_pair(1, 1), transcript_pair(2, 3)
        first[0]['documents'][0] = 'a' * 3000
        second[0]['documents'][0] = 'b' * 3000
        context = self.context((*first, *second), question='q' * 2000)
        self.assertEqual(len(context.exchanges), 1)
        self.assertEqual(context.exchanges[0].user_text, 'b' * 3000)
        self.assertTrue(context.omitted)

    def test_oversized_latest_exchange_does_not_substitute_older_context(self):
        old, latest = transcript_pair(1, 1), transcript_pair(2, 3)
        latest[0]['documents'][0] = 'z' * 7000
        context = self.context((*old, *latest), question='q' * 2000)
        self.assertEqual(context.exchanges, ())
        self.assertTrue(context.omitted)

    def test_technical_and_preclearance_notices_never_supply_question_text(self):
        failed = transcript_pair(1, 1, 'failed_technical')
        marker = transcript_marker(2)
        offer = transcript_offer(2, reason='security_uncertain')
        context = self.context((*failed, marker), offers={str(UUID(int=1002)): offer})
        self.assertEqual(context.exchanges, ())

    def test_mismatched_stored_profile_cannot_become_model_context(self):
        self.profile = self.profile.model_copy(update={'user_id': 'syn-cust-02'})
        with self.assertRaises(ValueError):
            self.context()


def judgment_answers():
    # Provider-native answer shape; labels are evaluator-side fixtures, never model inputs.
    choices = {'intent': ('information', ['information', 'individual_case_or_action', 'mixed', 'unclear_or_other']),
               'sentiment': ('neutral', ['positive', 'neutral', 'negative']),
               'department_dependency': ('independent', ['independent', 'dependent', 'unclear'])}
    result = {key: {'type': 'choice', 'choice': choice, 'confidence': 1.0,
                   'probabilities': {label: float(label == choice) for label in labels}}
              for key, (choice, labels) in choices.items()}
    for key in ('company_wide_topic', 'topic_hr', 'topic_it_support', 'topic_facilities_admin',
                'topic_billing_payments', 'topic_shipping_delivery', 'history_needed',
                'prompt_injection', 'sql_injection', 'profile_override_attempt', 'access_concern'):
        result[key] = {'type': 'noul', 'noul': 0.0}
    result['severity'] = {'type': 'score', 'score': 0.0, 'confidence': 1.0,
                          'legend': {0: 'minor', 1: 'disruption', 2: 'major'},
                          'probabilities': {0: 1.0, 1: 0.0, 2: 0.0}}
    return result


class IncomingJudgmentTests(unittest.TestCase):
    def parse(self, answers):
        from shopunow import incoming
        parser = getattr(incoming, 'validate_judgments', None)
        self.assertTrue(callable(parser), 'Obligatory Jev validation is missing')
        return parser(answers)

    def test_complete_valid_judgments_preserve_independent_signals(self):
        answers = judgment_answers()
        answers['company_wide_topic']['noul'] = 0.8
        answers['topic_hr']['noul'] = 0.9
        result = self.parse(answers)
        self.assertEqual(result['company_wide_topic'].noul, 0.8)
        self.assertEqual(result['topic_hr'].noul, 0.9)

    def test_each_missing_obligatory_judgment_withholds_validated_result(self):
        for key in judgment_answers():
            answers = judgment_answers()
            del answers[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(answers)

    def test_wrong_types_labels_and_nonfinite_values_are_rejected(self):
        for key, value in [('intent', {'type': 'noul', 'noul': 0.1}),
                           ('sentiment', {'type': 'choice', 'choice': 'happy',
                                          'confidence': 1.0, 'probabilities': {'happy': 1.0}}),
                           ('severity', {'type': 'score', 'score': 3.0, 'confidence': 1.0}),
                           ('prompt_injection', {'type': 'noul', 'noul': float('nan')}),
                           ('access_concern', {'type': 'noul', 'noul': True})]:
            answers = deepcopy(judgment_answers())
            answers[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(answers)

    def test_reviewer_confirmation_requires_valid_attack_types(self):
        from shopunow import incoming
        decide = getattr(incoming, 'reviewer_disposition', None)
        self.assertTrue(callable(decide), 'Validated incoming reviewer disposition is missing')
        for raw, expected in [
            ({'verdict': 'clear', 'attack_types': []}, 'clear'),
            ({'verdict': 'confirmed_attack', 'attack_types': ['prompt_injection']}, 'confirmed_attack'),
            ({'verdict': 'confirmed_attack', 'attack_types': []}, 'uncertain'),
            ({'verdict': 'clear', 'attack_types': ['sql_injection']}, 'uncertain'),
            ({'verdict': 'confirmed_attack', 'attack_types': ['other']}, 'uncertain'),
            ({'verdict': 'clear', 'extra': 'ignore access'}, 'uncertain'),
            (None, 'uncertain'),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(decide(raw), expected)

    def test_severity_cannot_contradict_its_native_rubric_distribution(self):
        answers = judgment_answers()
        answers['severity']['score'] = 2.0
        with self.assertRaises(ValueError):
            self.parse(answers)


# Flow: native fixtures -> existing verifier -> owned bounded context -> literal assertions.
