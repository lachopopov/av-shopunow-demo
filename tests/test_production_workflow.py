# Python 3.12; coordinator behavior with real admission/evidence components and controlled model calls.
from dataclasses import replace
from threading import Event,Thread,Lock
from types import SimpleNamespace
import unittest
from tests import test_evidence as evidence_fixtures


class ProductionWorkflowTests(unittest.TestCase):
    def test_final_central_missing_information_rechecks_dependency_before_any_draft_observation(self):
        for dependency,wanted in (('dependent','insufficient_evidence'),('unclear','clarification_needed')):
            with self.subTest(dependency=dependency):
                self.setUp()
                self.service.destinations=('central_support',)
                self.evidence=replace(self.evidence,destinations=self.service.destinations,dependency=dependency,
                    cleared=(replace(self.evidence.cleared[0],destination='central_support'),))
                def produce(payload,*,timeout):
                    reply=self.produce(payload,timeout=timeout)
                    reply['unresolved_parts']=['central_support']
                    return reply
                self.models.produce=produce
                observed=[]
                result=self.run_production(observe_draft=observed.append)
                self.assertEqual(result.kind,wanted)
                self.assertIsNone(result.draft)
                self.assertEqual(observed,[])
                self.assertIn(('central_support','insufficient_evidence'),
                              [(row.part,row.kind) for row in result.dispositions])

    def test_historical_clarification_without_current_support_starts_no_producer(self):
        from shopunow.evidence import PartDisposition
        self.evidence=replace(self.evidence,kind='clarification_needed',cleared=(),
            unresolved_parts=('history',),dispositions=(PartDisposition('history','clarification_needed'),))
        result=self.run_production()
        self.assertEqual(result.kind,'clarification_needed')
        self.assertEqual(self.calls,[])
        self.assertEqual(result.frozen_contributors,())
        self.assertIn(('history','clarification_needed'),[(row.part,row.kind) for row in result.dispositions])

    def test_known_security_fallback_part_never_dispatches_alongside_independent_help(self):
        self.service.destinations=('billing_payments','shipping_delivery')
        bill=replace(self.evidence.cleared[0],excerpt_id='bill',destination='billing_payments')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,cleared=(bill,),
            dependency='independent',security_fallback_parts=('shipping_delivery',))
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual([row['part'] for row in self.calls],['billing_payments','central_support'])  # Owner decision 2026-11-19: an unfinished other part sends the answer through synthesis.
        self.assertEqual(result.frozen_contributors,('billing_payments',))
        self.assertEqual(result.draft.unresolved_parts,('shipping_delivery',))
        self.assertIn(('shipping_delivery','security_fallback'),[(row.part,row.kind) for row in result.dispositions])

    def test_actual_admitted_lease_owns_late_child_through_cutoff_and_withdrawal(self):
        from shopunow import admission,production
        from shopunow.evidence import EvidenceService,ModelBudget
        from shopunow.workflow import IncomingDecision
        from tests.test_admission import InlineExecutor
        from tests.test_question_admission import QuestionAdmissionTests
        from tests.test_records import CONVERSATION_ID,REQUEST_ID
        owner=QuestionAdmissionTests(methodName='runTest')
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        started,release,observed,other_release=Event(),Event(),Event(),Event()
        self.addCleanup(lambda:(release.set(),other_release.set()))
        snapshots=[]
        def produce(payload,*,timeout):
            started.set()
            if not release.wait(5):
                raise AssertionError('Admitted late child was not released')
            return self.produce(payload,timeout=timeout)
        def continuation(**state):
            service=EvidenceService(storage=self.service.storage,catalog=self.service.catalog,
                models=self.fixture.models,tracking=owner.tracking,request_id=REQUEST_ID,
                created_at='2026-11-16T10:00:00Z',budget=ModelBudget(),topic_trigger=.5,**state)
            evidence=service.prepare(IncomingDecision('continue',destinations=('shipping_delivery',),intent='information'))
            production.run_production(evidence=evidence,service=service,models=SimpleNamespace(produce=produce),
                observe_result=lambda row:(snapshots.append(row),observed.set()))
            return None  # Private boundary never becomes a terminal interaction response.
        handle=owner.submit(executor=owner.executor,on_admitted=continuation)
        self.assertTrue(started.wait(3))
        owner.now=160.
        self.assertTrue(observed.wait(3))
        handle.withdraw()
        self.assertFalse(handle.lease.capacity_released)
        lease=owner.slots.submit_with_lease(InlineExecutor(),lambda worker:
            owner.reservations.reserve(worker,('conversation',CONVERSATION_ID)))
        with self.assertRaises(admission.ReservationConflict):
            lease.result(0)
        others=[owner.slots.submit(owner.executor,other_release.wait,5) for _ in range(4)]
        with self.assertRaises(admission.AdmissionBusy):
            owner.slots.submit(InlineExecutor(),lambda:None)
        other_release.set()
        for lease in others:
            lease.result(2)
        release.set()
        self.assertEqual(handle.result(3).status,'stopped')
        self.assertTrue(handle.lease.capacity_released)
        lease=owner.slots.submit_with_lease(InlineExecutor(),lambda worker:
            owner.reservations.reserve(worker,('conversation',CONVERSATION_ID)))
        self.assertIsNone(lease.result(0))
        self.assertEqual(snapshots[0].contexts,())
        self.assertEqual(snapshots[0].frozen_contributors,())

    def test_at_most_one_history_search_per_question_and_only_by_the_owner(self):
        # Owner 2026-11-23 (challenge T1): every producer asks; only central support (owner) is searched, once.
        from tests.test_evidence import relevance_answers
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        self.fixture.identity_fixture(confidence=1.,label='clear')
        self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
        def route(*args,**kwargs):
            answers=judgment_answers()
            answers['topic_billing_payments']['noul']=answers['topic_shipping_delivery']['noul']=1.
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.fixture.models.history_route=route
        self.service.destinations=('billing_payments','shipping_delivery')
        bill=replace(self.evidence.cleared[0],excerpt_id='bill',destination='billing_payments')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
                              cleared=(bill,*self.evidence.cleared),dependency='independent')
        counts={}
        def produce(payload,*,timeout):
            part=payload['part']
            counts[part]=counts.get(part,0)+1
            if counts[part]==1:  # Every producer asks once (central support starts with no evidence).
                self.calls.append(payload)
                return {'part':part,'kind':'context_needed','text':None,'cited_ids':[],'support_assertion':False,
                        'target':'The earlier parcel correction','reason':None,'unresolved_parts':[],
                        'saved_action_claim_ids':[]}
            return self.produce(payload,timeout=timeout)
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(counts,{'billing_payments':1,'shipping_delivery':1,'central_support':2})
        self.assertEqual(self.service.searches,1)  # One history search (it may read several pages).
        self.assertIn(('billing_payments','insufficient_evidence'),
                      [(row.part,row.kind) for row in result.dispositions])
        central=next(row for row in result.contexts if row.part=='central_support')
        self.assertIn('owned_history',[row.source_kind for row in central.evidence])

    def test_central_support_can_continue_after_specialist_cutoff_using_original_deadline(self):
        from tests.test_evidence import relevance_answers
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        self.fixture.identity_fixture(confidence=1.,label='clear')
        self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
        def route(*args,**kwargs):
            answers=judgment_answers()
            answers['company_wide_topic']['noul']=1.
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.fixture.models.history_route=route
        self.service.destinations=('central_support',)
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
            cleared=(replace(self.evidence.cleared[0],destination='central_support'),))
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            if len(self.calls)==1:
                self.fixture.clock[0]=161.
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier company guidance')
            return reply
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual([row['part'] for row in self.calls],['central_support','central_support'])
        self.assertEqual(result.frozen_contributors,())
        self.assertEqual(result.draft.contributor_ids,('central_support',))
        self.assertEqual([row.source_kind for row in result.contexts[0].evidence],
                         ['knowledge','owned_history','owned_history'])
        self.assertEqual(result.contributions[0].text,'Tracking facts')
        self.assertEqual(self.service.searches,1)

    def test_late_continuation_is_absent_from_pre_drain_findings_and_correction_inputs(self):
        from tests.test_evidence import relevance_answers
        self.fixture.identity_fixture(confidence=1.,label='clear')
        self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
        started,release,observed=Event(),Event(),Event()
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            if len(self.calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            else:
                started.set()
                if not release.wait(5):
                    raise AssertionError('Late continuation was not released')
            return reply
        self.models.produce=produce
        snapshots,results=[],[]
        worker=Thread(target=lambda:results.append(self.run_production(
            observe_result=lambda row:(snapshots.append(row),observed.set()))))
        worker.start()
        self.addCleanup(lambda:(release.set(),worker.join(6)))
        self.assertTrue(started.wait(3))
        self.fixture.clock[0]=160.
        self.assertTrue(observed.wait(3))
        self.assertTrue(worker.is_alive())
        self.assertIsNone(snapshots[0].draft)
        self.assertEqual(snapshots[0].frozen_contributors,())
        self.assertEqual(snapshots[0].contributions,())
        self.assertEqual(snapshots[0].contexts,())
        self.assertIn(('shipping_delivery','incomplete_at_cutoff'),
                      [(row.part,row.kind) for row in snapshots[0].dispositions])
        release.set()
        worker.join(3)
        self.assertEqual(results,snapshots)

    def test_partial_draft_retains_responsible_context_and_failure_without_false_authorship(self):
        # A failed specialist remains a frozen outcome, never an author of another specialist's text.
        self.service.destinations=('billing_payments','shipping_delivery')
        bill=replace(self.evidence.cleared[0],excerpt_id='bill',destination='billing_payments')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
                              cleared=(bill,*self.evidence.cleared),dependency='independent')
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            if payload['part']=='shipping_delivery':
                reply.update(kind='technical_failure',text=None,cited_ids=[],support_assertion=False)
            return reply
        self.models.produce=produce
        result=self.run_production()
        # Owner decision 2026-11-19: another failed part sends the answer through synthesis.
        self.assertEqual(result.draft.contributor_ids,('billing_payments','central_support'))
        self.assertEqual(result.frozen_contributors,('billing_payments','shipping_delivery'))
        self.assertEqual([(row.part,row.kind) for row in result.contributions],
                         [('billing_payments','supported_answer'),('shipping_delivery','technical_failure'),
                          ('central_support','supported_answer')])
        contexts={row.part:row for row in result.contexts}
        self.assertEqual(contexts['billing_payments'].evidence,(bill,))
        self.assertEqual(contexts['shipping_delivery'].evidence,self.evidence.cleared[1:])
        self.assertIn(('shipping_delivery','technical_failure'),
                      [(row.part,row.kind) for row in result.dispositions])

    def test_full_boundary_is_observable_before_late_work_drains_and_cannot_change(self):
        # Future reviews need correction inputs while the admitted parent still owns a late child.
        self.assertIn('observe_result',__import__('inspect').signature(
            __import__('shopunow.production',fromlist=['run_production']).run_production).parameters,
            'The pre-drain observation must include retained correction inputs, not only draft text')
        # One department: its specialist owns history (owner 2026-11-23); its search outlives the cutoff.
        started,release,observed,returned=Event(),Event(),Event(),Event()
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            if payload['part']=='shipping_delivery':
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            return reply
        self.models.produce=produce
        def history(user_id,*,offset,timeout):
            started.set()
            if not release.wait(5):
                raise AssertionError('Late supplementary read was not released')
            return {'ids':[],'documents':[],'metadatas':[]}
        self.service.storage.history_page=history
        snapshots,results=[],[]
        def run():
            try:
                results.append(self.run_production(observe_result=lambda row:(snapshots.append(row),observed.set())))
            finally:
                returned.set()
        worker=Thread(target=run)
        worker.start()
        self.addCleanup(lambda:(release.set(),worker.join(6)))
        self.assertTrue(started.wait(3))
        self.fixture.clock[0]=160.
        self.assertTrue(observed.wait(3))
        self.assertFalse(returned.is_set())
        snapshot=snapshots[0]
        self.assertIsNone(snapshot.draft)
        self.assertIn(('shipping_delivery','incomplete_at_cutoff'),
                      [(row.part,row.kind) for row in snapshot.dispositions])
        release.set()
        worker.join(3)
        self.assertEqual(results,[snapshot])
        self.assertNotIn('shipping_delivery',snapshot.frozen_contributors)

    def test_supplementary_nonready_outcomes_keep_their_kind_without_resumption(self):
        from shopunow.evidence import EvidenceResult
        for part in ('shipping_delivery','central_support'):
            for kind in ('technical_failure','clarification_needed','security_fallback'):
                with self.subTest(part=part,kind=kind):
                    self.setUp()
                    if part=='central_support':
                        self.service.destinations=(part,)
                        self.evidence=replace(self.evidence,destinations=(part,),
                            cleared=(replace(self.evidence.cleared[0],destination=part),))
                    def produce(payload,*,timeout):
                        reply=self.produce(payload,timeout=timeout)
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier parcel correction')
                        return reply
                    self.models.produce=produce
                    self.service.supplementary=lambda **kwargs:EvidenceResult(kind)
                    result=self.run_production()
                    self.assertEqual(result.kind,kind)
                    self.assertIsNone(result.draft)
                    self.assertEqual(len(self.calls),1)

    def test_one_successful_targeted_continuation_reuses_cleared_history_and_original_budget(self):
        from tests.test_evidence import relevance_answers
        self.fixture.identity_fixture(confidence=1.,label='clear')
        self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
        before=self.service.budget.attempts
        def produce(payload,*,timeout):
            result=self.produce(payload,timeout=timeout)
            if len(self.calls)==1:
                result.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                              target='The earlier parcel correction')
            return result
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.service.searches,1)
        self.assertEqual(self.service.budget.attempts-before,5)
        self.assertEqual([row['source_kind'] for row in self.calls[1]['evidence']],
                         ['knowledge','owned_history','owned_history'])

    def test_supplementary_refusal_is_preserved_and_security_fallback_stops_assembly(self):
        from tests.test_evidence import relevance_answers
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        for verdict in ('clean','uncertain'):
            with self.subTest(verdict=verdict):
                if verdict=='uncertain':
                    self.setUp()
                self.fixture.identity_fixture(confidence=1.,label='clear')
                self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
                self.fixture.models.verdict=verdict
                def route(*args,**kwargs):
                    answers=judgment_answers()
                    answers['topic_hr']['noul']=1.
                    return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
                self.fixture.models.history_route=route
                def produce(payload,*,timeout):
                    reply=self.produce(payload,timeout=timeout)
                    if len(self.calls)==1:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier parcel correction')
                    return reply
                self.models.produce=produce
                result=self.run_production()
                self.assertFalse(any(row['source_kind']=='owned_history' for payload in self.calls
                                     for row in payload['evidence']))
                if verdict=='clean':
                    self.assertIn('hr',result.unresolved_parts)
                    self.assertIn('history',result.unresolved_parts)
                else:
                    self.assertEqual(result.kind,'security_fallback')
                    self.assertIsNone(result.draft)
                    self.assertEqual(len(self.calls),1)

    def test_five_distinct_specialists_run_at_most_two_at_once_then_attribute_synthesis(self):
        # Controlled stored employee profile/evidence isolates scheduling from native semantic quality.
        self.service.profile=self.service.profile.model_copy(update={'role':'employee','home_department':'hr'})
        parts=('hr','it_support','facilities_admin','billing_payments','shipping_delivery')
        self.service.destinations=parts
        self.evidence=replace(self.evidence,destinations=parts,cleared=tuple(
            replace(self.evidence.cleared[0],excerpt_id='e'+str(index),destination=part)
            for index,part in enumerate(parts)),dependency='independent')
        two_started,release=Event(),Event()
        guard=Lock()
        active=[0]
        maximum=[0]
        def produce(payload,*,timeout):
            if payload['part']=='central_support':
                return self.produce(payload,timeout=timeout)
            with guard:
                active[0]+=1
                maximum[0]=max(maximum[0],active[0])
                if active[0]==2:
                    two_started.set()
            try:
                if not release.wait(5):
                    raise AssertionError('Test did not release specialists')
                return self.produce(payload,timeout=timeout)
            finally:
                with guard:
                    active[0]-=1
        self.models.produce=produce
        results=[]
        worker=Thread(target=lambda:results.append(self.run_production()))
        worker.start()
        self.addCleanup(lambda:(release.set(),worker.join(6)))
        self.assertTrue(two_started.wait(3))
        self.assertEqual(maximum[0],2)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(maximum[0],2)
        self.assertEqual(sorted(row['part'] for row in self.calls),sorted((*parts,'central_support')))
        self.assertEqual(results[0].draft.contributor_ids,(*parts,'central_support'))
        self.assertEqual(results[0].frozen_contributors,parts)

    def test_exhausted_original_attempt_budget_starts_no_producer_request(self):
        from shopunow.evidence import ModelBudget
        self.service.budget=ModelBudget(attempts=40)
        result=self.run_production()
        self.assertIsNone(result.draft)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.service.budget.attempts,40)

    def test_recalled_hold_before_or_during_production_prevents_a_draft(self):
        from shopunow.evidence import EvidenceCandidate
        for when in ('before','during'):
            with self.subTest(when=when):
                if when=='during':
                    self.setUp()
                recalled='99999999-9999-4999-8999-999999999999'
                history=EvidenceCandidate('h1','owned_history','terminal-old',None,'Owned earlier facts',
                    'history',owner_user_id=self.service.profile.user_id,conversation_id=recalled,
                    clearance='clean',relevance_basis='typed_current_request')
                self.evidence=replace(self.evidence,cleared=(*self.evidence.cleared,history))
                original=self.models.produce
                if when=='before':
                    self.service.tracking.exclude_conversation(recalled)
                else:
                    def hold(payload,*,timeout):
                        self.service.tracking.exclude_conversation(recalled)
                        return original(payload,timeout=timeout)
                    self.models.produce=hold
                result=self.run_production()
                self.assertIsNone(result.draft)
                self.assertEqual(len(self.calls),0 if when=='before' else 1)

    def test_company_wide_author_is_attributed_without_inventing_a_specialist(self):
        self.service.destinations=('central_support',)
        self.evidence=replace(self.evidence,destinations=('central_support',),
            cleared=(replace(self.evidence.cleared[0],destination='central_support'),))
        result=self.run_production()
        self.assertEqual(result.draft.contributor_ids,('central_support',))
        self.assertEqual(result.frozen_contributors,())
        self.assertEqual([row['part'] for row in self.calls],['central_support'])

    def test_company_wide_producer_keeps_already_admitted_owned_history(self):
        from shopunow.evidence import EvidenceCandidate
        self.service.destinations=('central_support',)
        history=EvidenceCandidate('history-admitted','owned_history','old-terminal',None,'Earlier company facts',
            'history',owner_user_id=self.service.profile.user_id,conversation_id=self.service.request.conversation_id,
            clearance='clean',relevance_basis='typed_current_request')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
            cleared=(replace(self.evidence.cleared[0],destination='central_support'),history))
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual([row['excerpt_id'] for row in self.calls[0]['evidence']],['e1','history-admitted'])
        self.assertEqual(result.contexts[0].evidence,self.evidence.cleared)
        self.assertEqual(self.service.searches,0)

    def test_synthesis_citations_are_limited_to_frozen_supported_findings(self):
        from shopunow.evidence import EvidenceCandidate
        self.service.destinations=('billing_payments','shipping_delivery')
        bill=replace(self.evidence.cleared[0],excerpt_id='bill',destination='billing_payments')
        history=EvidenceCandidate('uncited-history','owned_history','old-terminal',None,'Additional case facts',
            'history',owner_user_id=self.service.profile.user_id,conversation_id=self.service.request.conversation_id,
            clearance='clean',relevance_basis='typed_current_request')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
                              cleared=(bill,*self.evidence.cleared,history),dependency='independent')
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        central=next(row for row in self.calls if row['part']=='central_support')
        self.assertEqual({row['excerpt_id'] for row in central['evidence']},{'bill','e1'})
        self.assertEqual(len(result.contexts),3)
        self.assertEqual(result.contexts[-1].part,'central_support')
        self.assertEqual(result.contexts[-1].contributions,result.contributions[:-1])

    def setUp(self):
        from shopunow import production
        self.assertTrue(hasattr(production,'run_production'),'Bounded production graph missing')
        self.fixture=evidence_fixtures.EvidenceTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service=self.fixture.service
        self.evidence=self.fixture.prepare(intent='information')
        self.calls=[]
        self.models=SimpleNamespace(produce=self.produce)

    def produce(self,payload,*,timeout):
        self.calls.append(payload)
        return {'part':payload['part'],'kind':'supported_answer','text':'Tracking facts',
            'cited_ids':[payload['evidence'][0]['excerpt_id']],'support_assertion':True,
            'target':None,'reason':None,'unresolved_parts':[],'saved_action_claim_ids':[]}

    def run_production(self,**kwargs):
        from shopunow import production
        self.assertTrue(hasattr(production,'run_production'),'Bounded production graph missing')
        return production.run_production(evidence=self.evidence,service=self.service,models=self.models,**kwargs)

    def test_single_specialist_passes_unchanged_without_synthesis_or_delivery(self):
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(result.draft.full_text,'Tracking facts')
        self.assertEqual(result.draft.contributor_ids,('shipping_delivery',))
        self.assertEqual(result.draft.cited_ids,('e1',))
        self.assertEqual([row['part'] for row in self.calls],['shipping_delivery'])
        self.assertEqual(self.service.storage.calls.count('pending_write'),1)

    def test_mixed_action_and_refusal_survive_an_empty_model_unresolved_list(self):
        self.evidence=replace(self.evidence,dependency='independent',requires_action_handoff=True,
                              refused_parts=('hr',))
        result=self.run_production()
        self.assertEqual(result.draft.unresolved_parts,('hr','action'))
        self.assertEqual(result.draft.full_text,'Tracking facts')

    def test_supported_finding_cannot_erase_a_known_missing_subpart_in_its_department(self):
        self.evidence=replace(self.evidence,dependency='independent',unresolved_parts=('shipping_delivery',))
        result=self.run_production()
        self.assertEqual(result.kind,'draft_ready')
        self.assertIn('shipping_delivery',result.draft.unresolved_parts)
        self.assertIn(('shipping_delivery','supported_answer'),[(row.part,row.kind) for row in result.dispositions])
        self.assertIn(('shipping_delivery','insufficient_evidence'),[(row.part,row.kind) for row in result.dispositions])

    def test_dependent_missing_part_cannot_become_a_complete_looking_draft(self):
        self.evidence=replace(self.evidence,dependency='dependent',unresolved_parts=('billing_payments',))
        result=self.run_production()
        self.assertIsNone(result.draft)
        self.assertEqual(result.kind,'insufficient_evidence')

    def test_cutoff_assembles_without_waiting_for_late_work_but_parent_retains_ownership(self):
        # Advancing the admitted clock is deterministic; barriers govern child lifetime, not wall-clock guesses.
        started=Event()
        release=Event()
        frozen=Event()
        returned=Event()
        snapshots=[]
        def slow(payload,*,timeout):
            started.set()
            if not release.wait(5):
                raise AssertionError('Test did not release the late child')
            return self.produce(payload,timeout=timeout)
        self.models.produce=slow
        results=[]
        def run():
            try:
                results.append(self.run_production(observe_freeze=lambda value:(snapshots.append(value),frozen.set())))
            finally:
                returned.set()
        worker=Thread(target=run)
        worker.start()
        self.addCleanup(lambda:(release.set(),worker.join(6)))
        self.assertTrue(started.wait(3))
        self.fixture.clock[0]=160.0  # Exactly60 seconds after the original admission.
        self.assertTrue(frozen.wait(3))
        self.assertFalse(returned.is_set())
        self.assertEqual(snapshots[0],())
        release.set()
        worker.join(3)
        self.assertTrue(returned.is_set())
        self.assertIsNone(results[0].draft)
        self.assertEqual(results[0].frozen_contributors,())


if __name__=='__main__':
    unittest.main()
