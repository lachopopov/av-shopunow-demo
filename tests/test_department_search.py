# Python 3.12; owner decision 2026-11-23 (pass 2): no per-entry matching before the search (step 4 removed);
# each settled routed department is searched as a whole, filtered by code.
import json
import unittest
from tests import test_evidence as base
from tests.test_evidence import source_row


class DepartmentSearchTests(unittest.TestCase):
    Base = base.EvidenceTests
    setUp, prepare = Base.setUp, Base.prepare

    def test_department_is_searched_once_without_entry_matching(self):
        result = self.prepare()
        self.assertEqual(result.kind, 'ready')
        self.assertEqual(self.models.calls.count('relevance'), 1)  # Excerpt relevance only, no entry match.
        where = next(call[1] for call in self.storage.calls if isinstance(call, tuple) and call[0] == 'knowledge')
        # Code filters the whole department: general, current, approved, for this role (challenge M7).
        self.assertIn({'subject': 'shipping_delivery'}, where['$and'])
        self.assertIn({'classification': 'general'}, where['$and'])
        self.assertIn({'eligible_audience': {'$contains': self.profile.role}}, where['$and'])
        self.assertFalse(any('source_id' in clause for clause in where['$and']))

    def test_returned_row_outside_the_department_or_not_general_is_dropped(self):
        # Challenge T4: verify_knowledge is the code-side guard behind the store filter.
        # Pass 4: the hit is checked on its own labels; a label set naming another ID is dropped too.
        for hit, changes in (('SU-BP-001', {'subject': 'billing_payments'}),
                             ('SU-BP-001', {'classification': 'non_general', 'access_scope': 'customer_personal_handoff'}),
                             ('SU-SD-001', {'source_id': 'SU-SD-002'})):
            with self.subTest(changes=changes):
                self.setUp()
                other = self.k.KnowledgeRecord.model_validate_json(json.dumps(source_row(
                    **{'source_id': hit, **changes})))
                document, labels = self.k.knowledge_payload(other)
                self.storage.result = {'ids': [[hit]], 'documents': [[document]], 'metadatas': [[labels]]}
                result = self.prepare()
                self.assertEqual(result.cleared, ())
                self.assertEqual(result.kind, 'insufficient_evidence')  # A support offer, never a refusal.

    def test_four_relevant_rows_and_a_style_preference_still_answer(self):
        # Challenge M1: only Knowledge rows count toward the four-per-department cap.
        rows = [self.k.KnowledgeRecord.model_validate_json(json.dumps(source_row(source_id=f'SU-SD-00{n}')))
                for n in range(1, 5)]
        payloads = [self.k.knowledge_payload(row) for row in rows]
        self.storage.result = {'ids': [[row.source_id for row in rows]], 'documents': [[p[0] for p in payloads]],
                               'metadatas': [[p[1] for p in payloads]]}
        self.request.response_style = 'brief'
        result = self.prepare()
        self.assertEqual(result.kind, 'ready')
        self.assertEqual(len([row for row in result.cleared if row.source_kind == 'knowledge']), 4)

    def test_chroma_hit_absent_from_the_packaged_file_is_answered(self):
        # Pass 4 (owner 2026-11-24): Chroma is the only FAQ store; the app keeps no second copy to compare with.
        row = self.k.KnowledgeRecord.model_validate_json(json.dumps(source_row(source_id='SU-SD-099')))
        document, labels = self.k.knowledge_payload(row)
        self.storage.result = {'ids': [['SU-SD-099']], 'documents': [[document]], 'metadatas': [[labels]]}
        result = self.prepare()
        self.assertEqual(result.kind, 'ready')
        self.assertEqual([row.source_record_id for row in result.cleared], ['SU-SD-099'])

    def test_weak_topic_hint_is_not_searched_without_a_settled_route(self):
        # Pass 2: only settled departments are searched; an unsettled hint costs no search.
        from shopunow.incoming import validate_judgments
        from shopunow.workflow import IncomingDecision
        from tests.test_incoming import judgment_answers
        self.service.semantic_confidence_trigger = .7
        self.service.topic_confidence_trigger = .1  # Topic bar from wiring.py (plan 4f).
        answers = judgment_answers()
        answers['topic_billing_payments']['noul'] = .55
        result = self.service.prepare(IncomingDecision('continue', destinations=(), intent='information',
            proposed_destinations=('billing_payments',), judgments=validate_judgments(answers)))
        self.assertEqual(result.kind, 'insufficient_evidence')
        self.assertFalse(any(isinstance(call, tuple) and call[0] == 'knowledge' for call in self.storage.calls))


if __name__ == '__main__':
    unittest.main()
