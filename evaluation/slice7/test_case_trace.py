# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §3 checks 2-6, §6 route evidence)
# Protects: layer 1 sees what the app actually did. Wrong extraction would hide a forbidden specialist, a
# restricted read, an unwanted write or a mis-cited source, or would fail a correct case.
import asyncio
import json
import unittest

from stand_ins import LocalCollection
from case_trace import build_trace  # Module under test (evaluation/slice7/case_trace.py).

PRODUCE = 'Produce only the supplied authorized part, echoing part exactly. other_parts lists ...'


def producer(part, evidence=()):
    payload = {'part': part, 'question': 'q', 'evidence': [
        {'excerpt_id': excerpt, 'source_kind': kind, 'source_record_id': source} for excerpt, kind, source in evidence]}
    return {'endpoint': '/v1/responses', 'model': 'gpt-6.1-sol', 'http_status': 200,
            'request': {'model': 'gpt-6.1-sol', 'instructions': PRODUCE, 'input': json.dumps(payload)}}


REVIEW = {'endpoint': '/v1/responses', 'model': 'gpt-6-luna', 'http_status': 200,
          'request': {'model': 'gpt-6-luna', 'instructions': 'You are the final security reviewer.',
                      'input': json.dumps({'part': 'hr', 'draft': 'x'})}}
EMBED = {'endpoint': '/v1/embeddings', 'model': 'text-embedding-3-small', 'http_status': 200,
         'request': {'model': 'text-embedding-3-small', 'input': ['q']}}


def stores(conversation_records=(), escalation_records=(), history_scans=0):
    local = {name: LocalCollection(name) for name in ('profiles', 'conversations', 'escalations')}
    asyncio.run(local['profiles'].add(ids=['profile:syn-cust-01'], documents=['{}'],
                                      metadatas=[{'record_type': 'profile', 'user_id': 'syn-cust-01'}]))
    for index, meta in enumerate(conversation_records):
        local['conversations'].records['c%d' % index] = ('', meta)
    for index, meta in enumerate(escalation_records):
        local['escalations'].records['e%d' % index] = ('', meta)
    for _ in range(history_scans):
        local['conversations'].calls.append({'operation': 'get', 'ids': None, 'where': {'$and': [
            {'owner_user_id': 'syn-cust-01'}, {'record_type': 'terminal_outcome'},
            {'kind': {'$in': ['completed_answer']}}, {'complete': True}]}})
    return local


def answer(cited=('e1',)):
    return [{'status': 200, 'body': {'outcome': {'kind': 'answer', 'text': 'x', 'cited_ids': list(cited)}}}]


class TraceTests(unittest.TestCase):
    def build(self, calls, local, turns, reads=(), profiles_before=None):
        before = profiles_before if profiles_before is not None else dict(local['profiles'].records)
        return build_trace(calls, knowledge_reads=list(reads), local=local, profiles_before=before, turns=turns)

    def test_producer_parts_embeddings_and_cited_sources_come_from_the_wire(self):
        calls = [EMBED, producer('hr', [('e1', 'knowledge', 'SU-HR-002'), ('e2', 'history', 'req-9')]),
                 producer('it_support', [('e3', 'knowledge', 'SU-IT-002')]), REVIEW,
                 producer('central_support'), producer('hr', [('e1', 'knowledge', 'SU-HR-002')])]
        trace = self.build(calls, stores(), answer(cited=('e1', 'e3')), reads=['SU-HR-002', 'SU-IT-002'])
        self.assertEqual(trace['producer_parts'], ['hr', 'it_support', 'central_support', 'hr'])  # Not the reviewer.
        self.assertEqual(trace['embedding_calls'], 1)
        self.assertEqual(trace['knowledge_reads'], ['SU-HR-002', 'SU-IT-002'])
        self.assertEqual(trace['cited_sources'], ['SU-HR-002', 'SU-IT-002'])
        self.assertEqual(trace['cleared_sources'], ['SU-HR-002', 'SU-IT-002'])  # Knowledge given to writers, once.

    def test_unmapped_cited_id_is_kept_visible(self):
        trace = self.build([producer('hr', [('e1', 'knowledge', 'SU-HR-002')])], stores(), answer(cited=('e7',)))
        self.assertEqual(trace['cited_sources'], ['unmapped:e7'])  # Never silently dropped.

    def test_new_local_writes_history_scans_and_offer_reason_are_extracted(self):
        local = stores(conversation_records=[{'record_type': 'conversation_header'},
                                             {'record_type': 'pending_message'}],
                       escalation_records=[{'record_type': 'offer', 'offer_id': 'o-1', 'reason_code': 'handoff_needed'}],
                       history_scans=2)
        turns = [{'status': 200, 'body': {'outcome': {'kind': 'handoff_offer', 'text': 'x', 'offer_id': 'o-1'}}}]
        trace = self.build([], local, turns)
        self.assertEqual(sorted(trace['writes']), ['conversation_header', 'offer', 'pending_message'])
        self.assertEqual(trace['history_reads'], 2)
        self.assertEqual(trace['offer_reason'], 'handoff_needed')
        self.assertFalse(trace['profile_changed'])
        self.assertFalse(trace['receipt'])

    def test_profile_change_and_receipt_are_detected(self):
        local = stores()
        before = dict(local['profiles'].records)
        local['profiles'].records['profile:syn-cust-01'] = ('{"role": "employee"}', {'record_type': 'profile'})
        turns = [{'status': 200, 'body': {'outcome': {'kind': 'answer', 'text': 'x', 'cited_ids': []},
                                          'receipt_id': 'r-1'}}]
        trace = self.build([], local, turns, profiles_before=before)
        self.assertTrue(trace['profile_changed'])
        self.assertTrue(trace['receipt'])

    def test_producer_prefix_matches_the_app_answer_writer_instructions(self):  # Code review #2.
        from unittest import mock
        from case_trace import PRODUCE_PREFIX
        from shopunow.model_clients import AnswerModels
        captured = []
        with mock.patch.object(AnswerModels, '_call', side_effect=lambda *args, **kwargs: captured.append(args[3])):
            AnswerModels(client=None, model='gpt-6.1-sol', reasoning='medium').produce(
                {'part': 'hr', 'unresolved_parts': []}, timeout=1)
        self.assertTrue(captured[0].startswith(PRODUCE_PREFIX))  # Else every writer check silently sees nothing.

    def test_turn_without_json_body_is_traced(self):  # A raw 5xx (challenger M4).
        trace = self.build([], stores(), [{'status': 500, 'body': None}])
        self.assertEqual((trace['receipt'], trace['cited_sources'], trace['offer_reason']), (False, [], None))

    def test_saved_submission_receipts_are_read_back_from_the_stand_in(self):  # Two-turn case (design §3a).
        local = stores()
        local['escalations'].records['submission:1'] = (json.dumps({'receipt_id': 'r-77', 'name': 'x'}),
                                                        {'record_type': 'submission', 'offer_id': 'o-1'})
        self.assertEqual(self.build([], local, answer())['saved_receipts'], ['r-77'])


if __name__ == '__main__':
    unittest.main()

# Summary: hand-built wire calls and stand-in contents check that the trace names the answer-writer parts
# (not reviewers), embeddings, Knowledge reads, cited sources (unmapped kept visible), writes, history scans,
# the saved offer reason, profile changes and receipts exactly.
