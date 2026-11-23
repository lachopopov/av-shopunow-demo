# Python 3.12 / FastAPI 0.143.2; Slice 5 reviewed delivery through the real admitted HTTP path.
# Controlled external boundaries only: local model replies and in-memory Chroma-shaped collections.
import asyncio
import json
import unittest
from uuid import UUID
from shopunow import workflow
from shopunow.model_clients import AnswerModels
from shopunow.records import verify_pending_terminal_pair
from tests.private_workflow_fixture import PrivateWorkflowFixture
from tests import test_question_http as http_fixtures
from tests import test_records

PARTIAL_SENTENCE = ('For anything not answered here, you can ask in this chat, or in a new conversation later, '
                    'to be put in touch with our support team.')  # Owner 2026-11-21 (CD-098).


class ReviewedFixture(PrivateWorkflowFixture):
    """Adds the two final reviewers and the final-record mapping; used only by Slice 5 tests."""
    def __init__(self, tracking, **options):
        super().__init__(tracking, **options)
        self.verdicts = {'security': {'verdict': 'approve', 'defect': None, 'responsible_producers': []},
                         'policy': {'verdict': 'approve', 'defect': None, 'responsible_producers': []}}
        self.review_payloads = []
        self.review_hook = lambda reviewer, payload: None  # Optional side effect during a review call.
        self.pass_budget = True  # False: the caller supplies no shared model budget.
        for name in ('write_terminal', 'exact_terminal'):
            setattr(self.storage, name, getattr(self.native_storage, name))
        self.review_models = AnswerModels(client=self.reviewer, model='controlled-reviewer', reasoning='high',
                                          observe_response=self.observe_response)

    def review(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith('/responses') and body['text']['format']['name'] == 'FinalReviewReply':
            payload = json.loads(body['input'])
            reviewer = 'security' if 'S1 ' in body['instructions'] else 'policy'
            self.review_payloads.append((reviewer, payload))
            self.calls.append(('FinalReviewReply', body))  # Wire body, for configuration checks.
            self.review_hook(reviewer, payload)
            verdict = self.verdicts[reviewer]  # One verdict, or a list indexed by review round.
            verdict = verdict[payload['round']] if isinstance(verdict, list) else verdict
            reply = dict(verdict, draft_id=payload['draft_id'], round=payload['round'])
            return self._reply(body, reply)
        return super().review(request)

    @staticmethod
    def _reply(body, reply):
        import httpx2
        return httpx2.Response(200, json={'id': 'resp_final', 'object': 'response', 'created_at': 0,
            'status': 'completed', 'model': body['model'],
            'usage': {'input_tokens': 9, 'output_tokens': 4, 'total_tokens': 13},
            'output': [{'id': 'msg_final', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': json.dumps(reply), 'annotations': []}]}]})

    def continue_admitted(self, *, request_id, **state):
        from datetime import datetime, timezone
        from shopunow.evidence import ModelBudget
        self.budget = ModelBudget()
        self.request_id = request_id
        try:
            self.result = workflow.run_reviewed_workflow(**state, storage=self.storage, tracking=self.tracking,
                request_id=request_id, catalog=self.catalog, evidence_models=self.evidence_models,
                answer_models=self.answer_models, review_models=self.review_models, jev_client=self.jev,
                reviewer_client=self.reviewer, jev_model='offline-jev', security_trigger=self.security_trigger,
                topic_trigger=.5, semantic_confidence_trigger=self.semantic_confidence_trigger,
                model_budget=self.budget if self.pass_budget else None, observe_response=self.observe_response,
                utc_now=lambda: datetime(2026, 11, 16, tzinfo=timezone.utc))
            return self.result
        except Exception as error:
            self.error = error
            raise


class ReviewedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = http_fixtures.QuestionHTTPTests(methodName='runTest')
        await self.http.asyncSetUp()
        self.addCleanup(self.http.doCleanups)
        self.fixture = ReviewedFixture(self.http.tracking)
        self.addCleanup(self.fixture.close)

    async def asyncTearDown(self):
        await self.http.asyncTearDown()

    async def post(self):
        application = self.http.app(on_admitted=self.fixture.continue_admitted)
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            response = await self.http.post(application)
        if self.fixture.error:
            raise self.fixture.error
        self.http.assert_joined(application)
        self.logs = '\n'.join(captured.output)
        return response

    def saved_pair(self, body):
        storage = self.fixture.storage
        return verify_pending_terminal_pair(
            storage.exact_pending(body['request_id'], timeout=1.), storage.exact_terminal(body['request_id'], timeout=1.),
            user_id=body['user_id'], conversation_id=body['conversation_id'], request_id=body['request_id'],
            sequence=self.fixture.tracking.pending_intent(body['request_id']).sequence)

    async def test_draft_approved_by_both_reviewers_is_saved_read_back_and_delivered_unchanged(self):
        # Break caught: delivery without both approvals, without a verified saved pair, or with altered text.
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'answer')
        self.assertEqual(body['outcome']['text'], 'Private supported draft text')
        self.assertEqual(sorted(reviewer for reviewer, _ in self.fixture.review_payloads), ['policy', 'security'])
        self.assertEqual({payload['draft_id'] for _, payload in self.fixture.review_payloads}.__len__(), 1)
        pair = await asyncio.to_thread(self.saved_pair, body)  # The storage bridge runs its own loop.
        self.assertEqual(pair.status, 'verified')
        self.assertEqual((pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('completed_answer', 'Private supported draft text'))
        self.assertEqual(tuple(body['outcome']['cited_ids']), pair.record.terminal.cleared_citation_ids)
        self.assertNotIn('Private supported draft text', self.logs)
        self.assertNotIn(test_records.PROFILE['user_id'] + ' asked', self.logs)

    async def test_failed_or_fallback_verdict_never_delivers_the_draft_and_saves_a_linked_offer(self):
        # Break caught: one approval is enough, a failed review leaks the draft, or the offer is unsaved/unlinked.
        cases = {'security_fallback': ('security', {'verdict': 'fallback', 'defect': None, 'responsible_producers': []}),
                 'policy_malformed': ('policy', {'verdict': 'approve', 'defect': 'x', 'responsible_producers': []})}
        for name, (reviewer, verdict) in cases.items():
            with self.subTest(name):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                self.fixture.verdicts[reviewer] = verdict
                response = await self.post()
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body['outcome']['kind'], 'handoff_offer')
                self.assertNotIn('Private supported draft text', response.text + self.logs)
                pair = await asyncio.to_thread(self.saved_pair, body)
                terminal = pair.record.terminal
                self.assertEqual((pair.status, terminal.kind, terminal.verified_offer_id),
                                 ('verified', 'completed_offer', body['outcome']['offer_id']))
                self.assertEqual(terminal.approved_text_or_code,
                                 'Something went wrong while answering. You can request a support handoff.')
                self.assertEqual(body['outcome']['text'], terminal.approved_text_or_code)  # LLD 2026-11-21.
                stored = self.fixture.conversations.raw()['documents'] + self.fixture.offers.raw()['documents']
                self.assertFalse(any('Private supported draft text' in document for document in stored))
                offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'], timeout=1.)
                self.assertEqual(UUID(offer['metadatas'][0]['request_id']), UUID(body['request_id']))

    async def test_named_producer_corrects_and_both_reviewers_approve_the_new_snapshot(self):
        # Break caught: no correction, an unnamed or extra producer reruns, the corrected draft skips a
        # reviewer or keeps the old ID/round, or the rejected text is delivered, saved or logged.
        revise = {'verdict': 'revise', 'defect': 'Mentions an internal team name.',
                  'responsible_producers': ['shipping_delivery']}
        approve = {'verdict': 'approve', 'defect': None, 'responsible_producers': []}
        self.fixture.verdicts['security'] = [revise, approve]
        self.fixture.producer_hook = lambda payload, reply: dict(reply, text='Corrected supported text') \
            if payload.get('correction') else reply
        response = await self.post()
        body = response.json()
        self.assertEqual((body['outcome']['kind'], body['outcome']['text']), ('answer', 'Corrected supported text'))
        calls = self.fixture.producer_calls
        self.assertEqual([call['part'] for call in calls], ['shipping_delivery', 'shipping_delivery'])
        self.assertEqual(calls[1]['correction'], ['Mentions an internal team name.'])
        rounds = sorted((payload['round'], reviewer) for reviewer, payload in self.fixture.review_payloads)
        self.assertEqual(rounds, [(0, 'policy'), (0, 'security'), (1, 'policy'), (1, 'security')])
        ids = {payload['round']: payload['draft_id'] for _, payload in self.fixture.review_payloads}
        self.assertNotEqual(ids[0], ids[1])
        self.assertTrue(all(payload['full_text'] == 'Corrected supported text'
                            for _, payload in self.fixture.review_payloads if payload['round'] == 1))
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual(pair.record.terminal.approved_text_or_code, 'Corrected supported text')
        self.assertNotIn('Private supported draft text', response.text + self.logs)
        stored = self.fixture.conversations.raw()['documents']
        self.assertFalse(any('Private supported draft text' in document for document in stored))

    async def test_specialist_correction_preserves_other_findings_and_central_support_rebuilds(self):
        # Break caught: synthesis keeps the rejected finding, an unnamed specialist reruns or loses its
        # finding, or CentralSupport receives a correction nobody asked of it.
        self.fixture = ReviewedFixture(self.http.tracking, source_ids=('SU-BP-004', 'SU-SD-002'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_billing_payments']['noul'] = 1.
        self.fixture.destinations = ('billing_payments', 'shipping_delivery')

        def produce(payload, reply):
            if payload['part'] == 'central_support':
                return dict(reply, text=' + '.join(row['text'] for row in payload['contributions']))
            text = 'Corrected billing' if payload.get('correction') else 'Finding for ' + payload['part']
            return dict(reply, text=text)
        self.fixture.producer_hook = produce
        self.fixture.verdicts['policy'] = [
            {'verdict': 'revise', 'defect': 'Quotes a card number.', 'responsible_producers': ['billing_payments']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['text'], 'Corrected billing + Finding for shipping_delivery')
        parts = [call['part'] for call in self.fixture.producer_calls]
        self.assertEqual(sorted(parts), ['billing_payments', 'billing_payments', 'central_support',
                                         'central_support', 'shipping_delivery'])
        central = [call for call in self.fixture.producer_calls if call['part'] == 'central_support']
        self.assertNotIn('correction', central[1])
        rounds = sorted((payload['round'], reviewer) for reviewer, payload in self.fixture.review_payloads)
        self.assertEqual(rounds, [(0, 'policy'), (0, 'security'), (1, 'policy'), (1, 'security')])
        self.assertNotIn('Finding for billing_payments', response.text + self.logs)

    async def test_three_revised_rounds_without_approval_end_in_a_saved_offer(self):
        # Break caught: a fourth correction, an unbounded loop, or any rejected draft delivered or saved.
        self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Still names a staff member.',
                                             'responsible_producers': ['shipping_delivery']}
        self.fixture.producer_hook = lambda payload, reply: dict(
            reply, text='Correction %d' % len(self.fixture.producer_calls))
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual(len(self.fixture.producer_calls), 4)  # The draft plus three corrections.
        self.assertEqual(sorted({payload['round'] for _, payload in self.fixture.review_payloads}), [0, 1, 2, 3])
        self.assertEqual(len(self.fixture.review_payloads), 8)
        offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'], timeout=1.)
        self.assertEqual(offer['metadatas'][0]['reason_code'], 'revision_rounds_exhausted')
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual(pair.record.terminal.approved_text_or_code,  # Owner 2026-11-20: not a technical failure.
                         'Our support team can help with this. You can request a support handoff.')
        self.assertEqual(body['outcome']['text'], pair.record.terminal.approved_text_or_code)  # Live = reopen.
        stored = self.fixture.conversations.raw()['documents'] + self.fixture.offers.raw()['documents']
        self.assertFalse(any('Correction' in text for text in [response.text, self.logs, *stored]))

    async def test_a_failed_reviewer_outranks_a_revise_and_no_correction_runs(self):
        # Break caught: revise wins over a failed review, so a correction runs on an unsafe-to-trust round.
        self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Names a staff member.',
                                             'responsible_producers': ['shipping_delivery']}
        self.fixture.verdicts['policy'] = {'verdict': 'approve', 'defect': None,
                                           'responsible_producers': ['shipping_delivery']}  # Malformed reply.
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual(len(self.fixture.producer_calls), 1)
        self.assertEqual({payload['round'] for _, payload in self.fixture.review_payloads}, {0})
        offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'], timeout=1.)
        self.assertEqual(offer['metadatas'][0]['reason_code'], 'technical_failure')

    async def test_a_review_round_starts_only_when_all_its_model_calls_fit_the_budget(self):
        # Break caught: a round starts with one call left, or a correction runs that cannot be re-reviewed.
        revise = {'verdict': 'revise', 'defect': 'Names a staff member.',
                  'responsible_producers': ['shipping_delivery']}
        for used, security, kind, reviews, producers in ((38, None, 'answer', 2, 1), (39, None, 'handoff_offer', 0, 1),
                                                          (36, revise, 'handoff_offer', 2, 1)):
            with self.subTest(used=used, revise=security is not None):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                if security:
                    self.fixture.verdicts['security'] = security

                def produce(payload, reply, used=used):
                    self.fixture.budget.attempts = used  # Calls already spent when the draft is ready.
                    return reply
                self.fixture.producer_hook = produce
                body = (await self.post()).json()
                self.assertEqual(body['outcome']['kind'], kind)
                self.assertEqual(len(self.fixture.review_payloads), reviews)
                self.assertEqual(len(self.fixture.producer_calls), producers)

    async def test_a_correction_that_leaves_a_dependent_part_unresolved_is_never_synthesized(self):
        # Break caught: a correction bypasses the assembly gate and a dependent request gets a
        # complete-looking conclusion built on an unresolved part.
        self.fixture = ReviewedFixture(self.http.tracking, source_ids=('SU-BP-004', 'SU-SD-002'))
        self.addCleanup(self.fixture.close)
        labels = ('independent', 'dependent', 'unclear')
        self.fixture.answers['department_dependency'] = self.fixture.choice('dependent', labels)
        self.fixture.dependency = 'dependent'
        self.fixture.answers['topic_billing_payments']['noul'] = 1.
        self.fixture.destinations = ('billing_payments', 'shipping_delivery')

        def produce(payload, reply):
            if payload.get('correction'):
                return dict(reply, text='Corrected billing', unresolved_parts=[payload['part']])
            return reply
        self.fixture.producer_hook = produce
        self.fixture.verdicts['policy'] = {'verdict': 'revise', 'defect': 'Quotes a card number.',
                                           'responsible_producers': ['billing_payments']}
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual([call['part'] for call in self.fixture.producer_calls].count('central_support'), 1)
        self.assertNotIn('Corrected billing', response.text + self.logs)
        # Review 2, M2: an evidence gap, not a technical failure, with the support text.
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual(pair.record.terminal.approved_text_or_code,
                         'Our support team can help with this. You can request a support handoff.')
        offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'], timeout=1.)
        self.assertEqual(offer['metadatas'][0]['reason_code'], 'insufficient_evidence')

    async def test_a_correction_is_discarded_when_its_recalled_conversation_becomes_unresolved(self):
        # Break caught: a correction keeps using owned history that was excluded meanwhile (sibling
        # parity with production's recalled-context check).
        self.fixture.answers['history_needed']['noul'] = 1.
        recalled = self.fixture.history()

        def produce(payload, reply):
            if payload.get('correction'):
                self.fixture.tracking.exclude_conversation(recalled)  # Unresolved during the correction.
                return dict(reply, text='Corrected with history')
            return reply
        self.fixture.producer_hook = produce
        self.fixture.verdicts['security'] = [
            {'verdict': 'revise', 'defect': 'Names a staff member.', 'responsible_producers': ['shipping_delivery']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        response = await self.post()
        self.assertTrue(any(row['source_kind'] == 'owned_history'
                            for row in self.fixture.producer_calls[0]['evidence']))
        self.assertEqual(response.json()['outcome']['kind'], 'handoff_offer')
        self.assertNotIn('Corrected with history', response.text + self.logs)

    async def test_an_approved_answer_is_not_saved_when_its_recalled_conversation_became_unresolved(self):
        # Break caught: the final save trusts history that was excluded while the reviewers ran.
        self.fixture.answers['history_needed']['noul'] = 1.
        recalled = self.fixture.history()
        self.fixture.review_hook = lambda reviewer, payload: self.fixture.tracking.exclude_conversation(recalled)
        response = await self.post()
        self.assertEqual(response.json()['outcome']['kind'], 'handoff_offer')
        self.assertNotIn('Private supported draft text', response.text + self.logs)
        stored = self.fixture.conversations.raw()['documents']
        self.assertFalse(any('Private supported draft text' in document for document in stored))

    async def test_a_correction_after_the_specialist_cutoff_uses_the_original_request_deadline(self):
        # Break caught: corrections inherit the 60 s specialist cutoff and fail inside the 90 s deadline.
        admitted = self.http.now

        def advance(reviewer, payload):
            if payload['round'] == 0:
                self.http.now = admitted + 61.  # Past the 60 s cutoff, still 29 s (>= 28 s floor) left.
        self.fixture.review_hook = advance
        self.fixture.verdicts['security'] = [
            {'verdict': 'revise', 'defect': 'Names a staff member.', 'responsible_producers': ['shipping_delivery']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        self.fixture.producer_hook = lambda payload, reply: dict(reply, text='Late corrected text') \
            if payload.get('correction') else reply
        body = (await self.post()).json()
        self.assertEqual((body['outcome']['kind'], body['outcome'].get('text')), ('answer', 'Late corrected text'))

    async def test_an_oversized_correction_is_never_truncated_saved_or_delivered(self):
        # Break caught: text over 8,192 UTF-8 bytes is cut down, saved or shown instead of failing closed.
        oversized = 'é' * 4097  # 8,194 bytes in UTF-8, under 8,192 characters.
        self.fixture.verdicts['security'] = [
            {'verdict': 'revise', 'defect': 'Names a staff member.', 'responsible_producers': ['shipping_delivery']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        self.fixture.producer_hook = lambda payload, reply: dict(reply, text=oversized) \
            if payload.get('correction') else reply
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual({payload['round'] for _, payload in self.fixture.review_payloads}, {0})
        stored = self.fixture.conversations.raw()['documents'] + self.fixture.offers.raw()['documents']
        self.assertFalse(any('éé' in text for text in [response.text, self.logs, *stored]))

    async def test_non_draft_results_after_the_saved_question_end_in_their_saved_table_outcome(self):
        # Break caught: a clarification or a production failure leaves the saved question without a
        # verified final record, or is delivered with a different kind or text than was saved.
        def ambiguous_history(fixture):
            fixture.answers['history_needed']['noul'] = 1.
            fixture.history()
            fixture.history_kind = fixture.routing_status = 'ambiguous'

        def failed_producer(fixture):
            fixture.producer_hook = lambda payload, reply: dict(reply, kind='technical_failure', text=None,
                cited_ids=[], support_assertion=False)
        clarification = 'Could you add more detail so I can help with this question?'
        for name, setup, saved_kind, live_kind in (
                ('clarification', ambiguous_history, 'completed_clarification', 'clarification'),
                ('production_failure', failed_producer, 'completed_offer', 'handoff_offer')):
            with self.subTest(name):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                setup(self.fixture)
                body = (await self.post()).json()
                self.assertEqual(body['outcome']['kind'], live_kind)
                pair = await asyncio.to_thread(self.saved_pair, body)
                self.assertEqual((pair.status, pair.record.terminal.kind), ('verified', saved_kind))
                if live_kind == 'clarification':
                    self.assertEqual((body['outcome']['text'], pair.record.terminal.approved_text_or_code),
                                     (clarification, clarification))
                else:
                    self.assertEqual(pair.record.terminal.verified_offer_id, body['outcome']['offer_id'])
                self.assertEqual(self.fixture.review_payloads, [])  # No draft, so no final review.

    async def test_gate_refusal_and_clarification_save_the_question_then_their_fixed_outcome(self):
        # Break caught: a gate outcome leaves no saved question, no verified final record, or a text that
        # differs from the saved fixed text (reopen would not match what the user saw).
        intents = ('information', 'individual_case_or_action', 'mixed', 'unclear_or_other')

        def refused(fixture):
            fixture.answers['topic_shipping_delivery']['noul'] = 0.
            fixture.answers['topic_hr']['noul'] = 1.  # A customer asks about an internal department.

        def unclear(fixture):
            fixture.answers['intent'] = fixture.choice('unclear_or_other', intents)
        for name, setup, saved_kind, text in (
                ('refusal', refused, 'completed_refusal', "I can't help with that request."),
                ('clarification', unclear, 'completed_clarification',
                 'Could you add more detail so I can help with this question?')):
            with self.subTest(name):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                setup(self.fixture)
                body = (await self.post()).json()
                self.assertEqual((body['outcome']['kind'], body['outcome']['text']), (name, text))
                pair = await asyncio.to_thread(self.saved_pair, body)
                self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                                 ('verified', saved_kind, text))
                self.assertEqual(self.fixture.producer_calls, [])

    async def test_non_technical_offer_rows_save_a_linked_offer_with_the_support_text(self):
        # Break caught: these rows save no offer, mislabel it as a technical failure, or link it wrongly.
        def negative(fixture):
            fixture.answers['sentiment'] = fixture.choice('negative', ('positive', 'neutral', 'negative'))

        def unrouted(fixture):
            fixture.answers['intent'] = {'type': 'choice', 'choice': 'unclear_or_other', 'confidence': .4,
                'probabilities': {'unclear_or_other': .5, 'information': .3, 'individual_case_or_action': .1,
                                  'mixed': .1}}
            fixture.answers['topic_shipping_delivery']['noul'] = .35
            fixture.destinations = ()

        def uncertain_history(fixture):
            fixture.answers['history_needed']['noul'] = 1.
            fixture.history()
            fixture.history_verdict = 'uncertain'
        text = 'Our support team can help with this. You can request a support handoff.'
        for reason, setup in (('handoff_needed', negative), ('insufficient_evidence', unrouted),
                              ('security_uncertain', uncertain_history)):
            with self.subTest(reason):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                setup(self.fixture)
                body = (await self.post()).json()
                self.assertEqual(body['outcome']['kind'], 'handoff_offer')
                pair = await asyncio.to_thread(self.saved_pair, body)
                terminal = pair.record.terminal
                self.assertEqual((pair.status, terminal.kind, terminal.approved_text_or_code, terminal.verified_offer_id),
                                 ('verified', 'completed_offer', text, body['outcome']['offer_id']))
                offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'],
                                                timeout=1.)
                self.assertEqual(offer['metadatas'][0]['reason_code'], reason)
                self.assertEqual(self.fixture.producer_calls, [])

    async def test_a_deadline_after_the_saved_question_leaves_a_verified_technical_record(self):
        # Break caught: a stop after the question was saved leaves it without a final record (unreadable
        # conversation), or the unreviewed draft is saved or shown.
        admitted = self.http.now

        def expire(reviewer, payload):
            self.http.now = admitted + 91.  # The 90 s request deadline passes during the review.
        self.fixture.review_hook = expire
        response = await self.post()
        self.assertNotIn('Private supported draft text', response.text + self.logs)
        body = {'request_id': self.fixture.request_id, 'user_id': http_fixtures.USER_ID,
                'conversation_id': http_fixtures.CONVERSATION_ID}
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('verified', 'failed_technical', 'technical_error'))

    async def test_a_deadline_during_drafting_leaves_a_verified_technical_record(self):
        # Break caught (review 2, S1): the saved-question read-back needs time the stopped request no
        # longer has, so the conversation keeps its question with no final record.
        admitted = self.http.now

        def expire(payload, reply):
            self.http.now = admitted + 91.  # The 90 s request deadline passes while the answer is drafted.
            return reply
        self.fixture.producer_hook = expire
        response = await self.post()
        self.assertNotIn('Private supported draft text', response.text + self.logs)
        body = {'request_id': self.fixture.request_id, 'user_id': http_fixtures.USER_ID,
                'conversation_id': http_fixtures.CONVERSATION_ID}
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('verified', 'failed_technical', 'technical_error'))

    async def test_a_deadline_during_a_correction_leaves_a_verified_technical_record(self):
        # Stop 2 gap (lesson: test a stop at every phase): the deadline passes while a producer corrects.
        admitted = self.http.now
        self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Names a staff member.',
                                             'responsible_producers': ['shipping_delivery']}

        def expire(payload, reply):
            if payload.get('correction'):
                self.http.now = admitted + 91.  # The 90 s request deadline passes during the correction.
            return reply
        self.fixture.producer_hook = expire
        response = await self.post()
        self.assertNotIn('Private supported draft text', response.text + self.logs)
        body = {'request_id': self.fixture.request_id, 'user_id': http_fixtures.USER_ID,
                'conversation_id': http_fixtures.CONVERSATION_ID}
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('verified', 'failed_technical', 'technical_error'))

    async def test_a_client_disconnect_during_review_leaves_a_verified_technical_record(self):
        # Stop 2 gap (plan §1): a disconnect after the question was saved still ends in a saved technical
        # record, and nothing unapproved is sent, logged or saved.
        from threading import Event
        started, release = Event(), Event()
        self.http.release_events.append(release)

        def hold(reviewer, payload):
            started.set()
            if not release.wait(8):
                raise AssertionError('Review was not released')
        self.fixture.review_hook = hold
        application = self.http.app(on_admitted=self.fixture.continue_admitted)
        with self.assertLogs('shopunow.entry', level='INFO') as captured:
            host, messages, sent, state = await self.http.start_wire(application)
            await self.http.thread_event(started)
            messages.put_nowait({'type': 'http.disconnect'})
            await asyncio.wait_for(host, 3)
            release.set()
            result = await state['handle'].lease.wait(3)
        self.assertIsNone(self.fixture.error)
        self.assertEqual(result.status, 'stopped')
        self.assertNotIn('Private supported draft text', json.dumps(sent, default=str) + '\n'.join(captured.output))
        body = {'request_id': self.fixture.request_id, 'user_id': http_fixtures.USER_ID,
                'conversation_id': http_fixtures.CONVERSATION_ID}
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('verified', 'failed_technical', 'technical_error'))

    async def test_an_unknown_final_write_excludes_the_conversation_and_claims_no_success(self):
        # Break caught: an unconfirmed save is treated as success, retried as a second final record, or
        # leaves the conversation open for new questions.
        writes = []
        landed = self.fixture.storage.write_terminal

        def lost_reply(terminal, *, timeout):
            writes.append(terminal.kind)
            landed(terminal, timeout=timeout)  # The write lands, but its confirmation is lost.
            raise TimeoutError('Storage reply lost.')
        self.fixture.storage.write_terminal = lost_reply
        application = self.http.app(on_admitted=self.fixture.continue_admitted)
        response = await self.http.post(application)
        self.assertIsInstance(self.fixture.error, TimeoutError)
        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn('Private supported draft text', response.text)
        self.assertEqual(writes, ['completed_answer'])  # No cleanup or second final record.
        self.assertTrue(self.fixture.tracking.excludes((http_fixtures.CONVERSATION_ID,)))

    async def test_an_unconfirmed_offer_is_held_and_the_request_ends_in_a_saved_technical_record(self):
        # Break caught: an unconfirmed offer is shown or linked, or the saved question is left without
        # a final record.
        self.fixture.verdicts['security'] = {'verdict': 'fallback', 'defect': None, 'responsible_producers': []}
        landed = self.fixture.storage.write_offer

        def lost_reply(offer, *, timeout):
            landed(offer, timeout=timeout)
            raise TimeoutError('Storage reply lost.')
        self.fixture.storage.write_offer = lost_reply
        body = (await self.post()).json()
        self.assertEqual(body['outcome'], {'kind': 'fallback_error', 'offer_id': None,
                                           'text': 'The request could not be completed safely.'})
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.status, pair.record.terminal.kind, pair.record.terminal.verified_offer_id),
                         ('verified', 'failed_technical', None))

    async def test_the_request_waits_for_a_slow_reviewer_after_the_other_one_fails(self):
        # Break caught: the worker returns (and releases its reservation) while a reviewer call still runs.
        import time
        finished = []

        def slow_policy(reviewer, payload):
            if reviewer == 'policy':
                time.sleep(.3)
                finished.append(reviewer)
        self.fixture.review_hook = slow_policy
        self.fixture.verdicts['security'] = {'verdict': 'fallback', 'defect': None, 'responsible_producers': []}
        observed = []
        run = self.fixture.continue_admitted

        def continue_admitted(**state):
            outcome = run(**state)
            observed.append(list(finished))  # What had finished when the worker gave its result back.
            return outcome
        self.fixture.continue_admitted = continue_admitted
        body = (await self.post()).json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual(observed, [['policy']])

    async def test_the_served_wiring_sends_the_evaluated_models_and_settings_to_providers(self):
        # Break caught (CD-099): the served app uses different models, efforts or thresholds than the
        # evaluated configuration, or the factory skips the reviewed workflow.
        from shopunow import wiring
        fixture = self.fixture
        admitted = wiring.answering(openai_client=fixture.reviewer, jev_client=fixture.jev,
                                    catalog=fixture.catalog)(storage=fixture.storage, tracking=fixture.tracking)
        fixture.continue_admitted = admitted
        response = await self.post()
        self.assertEqual(response.json()['outcome']['kind'], 'answer')
        sent = {}
        for schema, body in fixture.calls:
            if isinstance(body, dict) and 'model' in body:
                sent.setdefault(schema, (body['model'], (body.get('reasoning') or {}).get('effort')))
        self.assertEqual(sent['ProducerReply'], ('gpt-6.1-sol', 'medium'))
        self.assertEqual(sent['FinalReviewReply'], ('gpt-6-luna', 'high'))
        self.assertEqual(sent['incoming'][0], 'jev-1.14.0')
        self.assertEqual((wiring.SECURITY_TRIGGER, wiring.TOPIC_TRIGGER, wiring.SEMANTIC_CONFIDENCE_TRIGGER),
                         (.7, .5, .7))

    async def test_an_approved_draft_marked_unresolved_is_delivered_once_without_an_offer(self):
        # Break caught: code overrides two approvals because of the (possibly wrong) unresolved label,
        # attaches an offer to a partial answer (CD-093), or adds the support sentence after review (CD-098).
        self.fixture.producer_hook = lambda payload, reply: dict(
            reply, unresolved_parts=[payload['part']], reason='Delivery times for islands are missing.')
        body = (await self.post()).json()
        expected = 'Private supported draft text\n\n' + PARTIAL_SENTENCE
        self.assertEqual((body['outcome']['kind'], body['outcome']['text']), ('answer', expected))
        self.assertTrue(all(payload['unresolved_parts'] == ['shipping_delivery'] and payload['full_text'] == expected
                            for _, payload in self.fixture.review_payloads))  # Both reviewers saw the sentence.
        self.assertEqual(self.fixture.offers.raw()['ids'], [])
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual((pair.record.terminal.kind, pair.record.terminal.approved_text_or_code),
                         ('completed_answer', expected))

    async def test_a_corrected_draft_that_stays_partial_keeps_one_support_sentence(self):
        # Break caught: the rebuilt snapshot drops the sentence, or repeats it after a correction.
        self.fixture.verdicts['security'] = [
            {'verdict': 'revise', 'defect': 'Names a staff member.', 'responsible_producers': ['shipping_delivery']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        self.fixture.producer_hook = lambda payload, reply: dict(
            reply, text='Corrected text' if payload.get('correction') else reply['text'],
            unresolved_parts=[payload['part']], reason='Island times are missing.')
        body = (await self.post()).json()
        expected = 'Corrected text\n\n' + PARTIAL_SENTENCE
        self.assertEqual(body['outcome']['text'], expected)
        self.assertEqual({payload['full_text'] for _, payload in self.fixture.review_payloads if payload['round'] == 1},
                         {expected})

    async def test_a_partial_draft_too_large_with_the_sentence_is_never_reviewed_or_truncated(self):
        # Break caught: the sentence pushes a complete text over 8,192 bytes and it is truncated or reviewed.
        self.fixture.producer_hook = lambda payload, reply: dict(
            reply, text='x' * 8150, unresolved_parts=[payload['part']], reason='Island times are missing.')
        response = await self.post()
        body = response.json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual(self.fixture.review_payloads, [])
        self.assertNotIn('x' * 100, response.text)
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual(pair.record.terminal.approved_text_or_code,
                         'Something went wrong while answering. You can request a support handoff.')

    async def test_a_correction_that_needs_a_handoff_saves_the_support_offer(self):
        # Break caught: a non-answer correction is mislabelled as a technical failure.
        self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Names a staff member.',
                                             'responsible_producers': ['shipping_delivery']}
        self.fixture.producer_hook = lambda payload, reply: dict(reply, kind='handoff_needed', text=None,
            cited_ids=[], support_assertion=False, target=payload['part'], reason='Needs a person.') \
            if payload.get('correction') else reply
        body = (await self.post()).json()
        offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'], timeout=1.)
        self.assertEqual(offer['metadatas'][0]['reason_code'], 'handoff_needed')
        pair = await asyncio.to_thread(self.saved_pair, body)
        self.assertEqual(pair.record.terminal.approved_text_or_code,
                         'Our support team can help with this. You can request a support handoff.')

    async def test_a_correction_without_enough_evidence_saves_the_support_offer_not_the_technical_text(self):
        # Break caught (pass 8, sp-ship): a correction that lacks evidence or needs clarification showed the
        # technical error text, which the 2026-11-20 owner amendment reserves for real failures. A real
        # technical failure in a correction must keep the technical text (regression guard).
        support = 'Our support team can help with this. You can request a support handoff.'
        technical = 'Something went wrong while answering. You can request a support handoff.'
        for kind, reason, text in (('context_needed', 'insufficient_evidence', support),
                                   ('clarification_needed', 'insufficient_evidence', support),
                                   ('technical_failure', 'technical_failure', technical)):
            with self.subTest(kind):
                await self.asyncTearDown()
                self.http.doCleanups()
                await self.asyncSetUp()
                self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Names another customer.',
                                                     'responsible_producers': ['shipping_delivery']}
                self.fixture.producer_hook = lambda payload, reply, kind=kind: dict(
                    reply, kind=kind, text=None, cited_ids=[], support_assertion=False,
                    target='the order shipping stage' if kind == 'context_needed' else None,
                    reason='The evidence does not establish the order stage.') \
                    if payload.get('correction') else reply
                body = (await self.post()).json()
                self.assertEqual(body['outcome']['kind'], 'handoff_offer')
                offer = await asyncio.to_thread(self.fixture.storage.exact_offer, body['outcome']['offer_id'],
                                                timeout=1.)
                self.assertEqual(offer['metadatas'][0]['reason_code'], reason)
                pair = await asyncio.to_thread(self.saved_pair, body)
                self.assertEqual(pair.record.terminal.approved_text_or_code, text)

    async def test_a_request_without_a_supplied_budget_still_completes_under_one_shared_budget(self):
        # Break caught: the composer crashes after the question was saved, leaving no final record.
        self.fixture.pass_budget = False
        body = (await self.post()).json()
        self.assertEqual(body['outcome']['kind'], 'answer')

    async def test_no_review_round_starts_with_less_than_28_seconds_left(self):
        # Owner 2026-11-20 (A = 28 s): a round needs time for one reviewer call plus one final write.
        admitted = self.http.now

        def late(reviewer, payload):
            self.http.now = admitted + 70.  # 20 s left after the first review.
        self.fixture.review_hook = late
        self.fixture.verdicts['security'] = {'verdict': 'revise', 'defect': 'Names a staff member.',
                                             'responsible_producers': ['shipping_delivery']}
        body = (await self.post()).json()
        self.assertEqual(body['outcome']['kind'], 'handoff_offer')
        self.assertEqual({payload['round'] for _, payload in self.fixture.review_payloads}, {0})
        self.assertEqual(len(self.fixture.producer_calls), 1)  # No correction that could not be re-reviewed.

    async def test_no_final_record_is_written_for_a_question_that_cannot_be_read_back(self):
        # Break caught: a final record is linked to a question that was retained but never stored (LLD:
        # failed_technical only after an exactly verified pending/terminal prefix).
        def drop_question(payload, reply):
            records = self.fixture.conversations.records
            for key in [key for key in records if key.startswith('pending_message:')]:
                del records[key]  # As if the pending write had never happened.
            return reply
        self.fixture.producer_hook = drop_question
        application = self.http.app(on_admitted=self.fixture.continue_admitted)
        response = await self.http.post(application)
        self.assertNotEqual(response.json().get('outcome', {}).get('kind'), 'answer')
        stored = self.fixture.conversations.raw()['metadatas']
        self.assertFalse(any(meta['record_type'] == 'terminal_outcome' for meta in stored))

    async def test_central_support_rebuild_can_cite_the_excerpt_a_correction_newly_relies_on(self):
        # Break caught: synthesis rebuilds from stale inputs, so a valid correction ends in an offer.
        self.fixture = ReviewedFixture(self.http.tracking, source_ids=('SU-BP-001', 'SU-BP-004', 'SU-SD-002'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_billing_payments']['noul'] = 1.
        self.fixture.destinations = ('billing_payments', 'shipping_delivery')

        def produce(payload, reply):
            if payload['part'] == 'central_support':
                cited = [identity for row in payload['contributions'] for identity in row['cited_ids']]
                return dict(reply, text='Synthesis', cited_ids=list(dict.fromkeys(cited)))
            ids = [row['excerpt_id'] for row in payload['evidence']]
            return dict(reply, cited_ids=ids[-1:] if payload.get('correction') else ids[:1])
        self.fixture.producer_hook = produce
        self.fixture.verdicts['policy'] = [
            {'verdict': 'revise', 'defect': 'Cites the wrong policy.', 'responsible_producers': ['billing_payments']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        body = (await self.post()).json()
        billing = [call for call in self.fixture.producer_calls if call['part'] == 'billing_payments']
        self.assertGreater(len(billing[0]['evidence']), 1)  # The setup really offers a second excerpt.
        self.assertEqual(body['outcome']['kind'], 'answer')
        self.assertIn(billing[0]['evidence'][-1]['excerpt_id'], body['outcome']['cited_ids'])

    async def test_central_support_rebuild_drops_the_excerpt_a_correction_stopped_citing(self):
        # Break caught (review 2, M1): synthesis may still cite a source the corrected specialist dropped.
        self.fixture = ReviewedFixture(self.http.tracking, source_ids=('SU-BP-001', 'SU-BP-004', 'SU-SD-002'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_billing_payments']['noul'] = 1.
        self.fixture.destinations = ('billing_payments', 'shipping_delivery')

        def produce(payload, reply):
            if payload['part'] == 'central_support':
                return dict(reply, text='Synthesis')
            ids = [row['excerpt_id'] for row in payload['evidence']]
            return dict(reply, cited_ids=ids[-1:] if payload.get('correction') else ids[:1])
        self.fixture.producer_hook = produce
        self.fixture.verdicts['policy'] = [
            {'verdict': 'revise', 'defect': 'Cites the wrong policy.', 'responsible_producers': ['billing_payments']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        await self.post()
        billing = [call for call in self.fixture.producer_calls if call['part'] == 'billing_payments']
        central = [call for call in self.fixture.producer_calls if call['part'] == 'central_support']
        dropped = billing[0]['evidence'][0]['excerpt_id']
        self.assertIn(dropped, [row['excerpt_id'] for row in central[0]['evidence']])  # Citable before.
        self.assertNotIn(dropped, [row['excerpt_id'] for row in central[1]['evidence']])

    async def test_central_support_rebuild_is_told_which_parts_a_correction_left_unresolved(self):
        # Break caught (review 2, M2): synthesis after a correction presents an unresolved part as answered.
        self.fixture = ReviewedFixture(self.http.tracking, source_ids=('SU-BP-004', 'SU-SD-002'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_billing_payments']['noul'] = 1.
        self.fixture.destinations = ('billing_payments', 'shipping_delivery')

        def produce(payload, reply):
            if payload.get('correction'):
                return dict(reply, text='Corrected billing', unresolved_parts=[payload['part']])
            return reply
        self.fixture.producer_hook = produce
        self.fixture.verdicts['policy'] = [
            {'verdict': 'revise', 'defect': 'Quotes a card number.', 'responsible_producers': ['billing_payments']},
            {'verdict': 'approve', 'defect': None, 'responsible_producers': []}]
        await self.post()
        central = [call for call in self.fixture.producer_calls if call['part'] == 'central_support']
        self.assertEqual(len(central), 2)  # The independent request still rebuilds its synthesis.
        self.assertIn('billing_payments', central[1]['unresolved_parts'])


if __name__ == '__main__':
    unittest.main()
