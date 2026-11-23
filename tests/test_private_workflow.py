# Python 3.12 / FastAPI 0.143.2; full private HTTP composition, controlled external responses.
import unittest
import asyncio
import json
from threading import Event
from unittest import mock
from uuid import UUID
from shopunow import workflow
from shopunow.evidence import EvidenceService
from tests.private_workflow_fixture import PrivateWorkflowFixture
from tests import test_question_http as http_fixtures
from tests import test_records


class PrivateWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(callable(getattr(workflow,'run_private_workflow',None)),
                        'The complete admitted incoming/evidence/production composition is missing')
        self.http=http_fixtures.QuestionHTTPTests(methodName='runTest')
        await self.http.asyncSetUp()
        self.addCleanup(self.http.doCleanups)
        self.fixture=PrivateWorkflowFixture(self.http.tracking)
        self.addCleanup(self.fixture.close)

    async def asyncTearDown(self):
        if hasattr(self,'http'):
            await self.http.asyncTearDown()

    async def post(self):
        self.application=self.http.app(on_admitted=self.fixture.continue_admitted)
        with self.assertLogs('shopunow.entry',level='INFO') as captured:
            response=await self.http.post(self.application)
        if self.fixture.error:
            raise self.fixture.error
        self.http.assert_joined(self.application)
        self.last_logs='\n'.join(captured.output)
        for text in (self.fixture.draft_text,http_fixtures.VALID['question'],
                     *(row.answer_facts[0] for row in self.fixture.catalog.values())):
            self.assertNotIn(text,response.text)
            self.assertNotIn(text,self.last_logs)
        self.assertFalse(any(row[1].get('record_type')=='terminal_outcome'
            for row in self.fixture.conversations.records.values()))
        self.assertFalse(any(self.fixture.draft_text in row[0] for row in self.fixture.conversations.records.values()))
        return response

    async def employee(self,department='it_support'):
        # The stored profile, not message text, decides which internal subjects are restricted.
        original=dict(test_records.PROFILE)
        self.addCleanup(lambda:(test_records.PROFILE.clear(),test_records.PROFILE.update(original)))
        test_records.PROFILE.update(role='employee',home_department=department)
        await self.reset_http()  # The admitted host reads this stored profile during setup.

    def recalled_employee_fixture(self,*,sources=('SU-HR-010','SU-IT-004'),topics=('hr','it_support')):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=sources)
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_shipping_delivery']['noul']=0.
        self.fixture.answers['history_needed']['noul']=1.
        for topic in topics:
            self.fixture.answers['topic_'+topic]['noul']=1.
        self.fixture.history_topics=topics
        self.fixture.destinations=topics
        self.fixture.history()
        return self.fixture

    async def reset_http(self):
        await self.http.asyncTearDown()
        self.http.doCleanups()
        await self.http.asyncSetUp()

    async def test_employee_history_is_released_only_on_a_confident_not_restricted_judgment(self):
        labels=('restricted','not_restricted','uncertain')
        weak={'type':'choice','choice':'not_restricted','confidence':.4,
              'probabilities':{'restricted':.3,'not_restricted':.6,'uncertain':.1}}
        for name,answer,disposition in (('restricted',PrivateWorkflowFixture.choice('restricted',labels),'refusal'),
                ('uncertain',PrivateWorkflowFixture.choice('uncertain',labels),'insufficient_evidence'),
                ('weak',weak,'insufficient_evidence')):
            with self.subTest(judgment=name):
                await self.employee()
                fixture=self.recalled_employee_fixture()
                fixture.restricted_history=answer
                fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
                await self.post()
                result=fixture.boundaries[0]
                self.assertIn(('history',disposition),[(row.part,row.kind) for row in result.dispositions])
                self.assertTrue(fixture.producer_calls,'Independent current help must still be produced')
                for payload in fixture.producer_calls:
                    self.assertEqual({row['source_kind'] for row in payload['evidence']},{'knowledge'})
                self.assertEqual({row['part'] for row in fixture.producer_calls},{'it_support','central_support'})  # Owner decision 2026-11-19: an unfinished other part sends the answer through synthesis.
                route=next(body for stage,body in fixture.calls if stage=='history_route')
                self.assertEqual([row['source_id'] for row in route['state']['restricted_subjects']],['SU-HR-010'])
                self.assertNotIn(fixture.catalog['SU-HR-010'].answer_facts[0],str(route))

    async def test_unconfigured_confidence_never_releases_employee_history_on_a_weak_judgment(self):
        weak={'type':'choice','choice':'not_restricted','confidence':.4,
              'probabilities':{'restricted':.3,'not_restricted':.6,'uncertain':.1}}
        await self.employee()
        fixture=self.recalled_employee_fixture()
        fixture.semantic_confidence_trigger=None  # A caller that does not configure the confidence setting.
        fixture.restricted_history=weak
        fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
        await self.post()
        self.assertTrue(any('restricted_history_subject' in body['questions']
                            for stage,body in fixture.calls if stage=='history_route'))
        for payload in fixture.producer_calls:
            self.assertNotIn('owned_history',[row['source_kind'] for row in payload['evidence']])
        self.assertIn('history',[row.part for row in fixture.boundaries[0].dispositions])

    async def test_employee_may_recall_general_history_and_own_department_subjects(self):
        for department,asked in (('it_support',True),('hr',False)):
            with self.subTest(department=department):
                await self.employee(department)
                fixture=self.recalled_employee_fixture()
                fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
                await self.post()
                route=next(body for stage,body in fixture.calls if stage=='history_route')
                self.assertEqual('restricted_history_subject' in route['questions'],asked)
                self.assertTrue(any(row['source_kind']=='owned_history'
                                    for payload in fixture.producer_calls for row in payload['evidence']))

    async def test_denied_history_alone_never_makes_a_department_eligible_to_answer(self):
        await self.employee()
        fixture=self.recalled_employee_fixture(sources=('SU-HR-010',),topics=('hr',))
        fixture.restricted_history=fixture.choice('restricted',('restricted','not_restricted','uncertain'))
        fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
        await self.post()
        self.assertEqual(fixture.producer_calls,[])
        self.assertIsNone(fixture.boundaries[0].draft)
        self.assertIn(('history','refusal'),[(row.part,row.kind) for row in fixture.boundaries[0].dispositions])

    async def test_candidate_adjudication_failure_keeps_evidence_already_admitted(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-001','SU-SD-006'))
        self.addCleanup(self.fixture.close)
        admitted=self.fixture.catalog['SU-SD-001'].answer_facts[0]
        self.fixture.current_match_for=lambda text:'relevant' if admitted in text else 'uncertain'
        self.fixture.candidate_reply=lambda payload:{'question':'Another request',
            'outcome':{'status':'resolved','relevant_excerpt_ids':[]}}
        await self.post()
        self.assertEqual(self.fixture.boundaries[0].kind,'draft_ready')
        self.assertEqual([row['source_record_id'] for row in self.fixture.producer_calls[0]['evidence']],['SU-SD-001'])

    async def test_weak_unrelated_route_score_does_not_block_targeted_continuation(self):
        await self.employee()
        self.recalled_employee_fixture(sources=('SU-IT-004',),topics=('it_support',))
        self.fixture.answers['history_needed']['noul']=0.
        self.fixture.history_topic_score=.75
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        self.assertEqual(len(self.fixture.producer_calls),2)
        self.assertIn('owned_history',[row['source_kind'] for row in self.fixture.producer_calls[1]['evidence']])

    async def test_weak_restricted_topic_still_withholds_customer_continuation_history(self):
        self.fixture.history()
        self.fixture.history_topic_scores={'hr':.75}  # The answered topic stays confidently routed.
        def produce(payload,reply):
            reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                         target='The earlier parcel correction')
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        for payload in self.fixture.producer_calls:
            self.assertEqual({row['source_kind'] for row in payload['evidence']},{'knowledge'})

    async def test_producer_is_told_independence_and_the_actual_historical_outcome(self):
        self.fixture.answers['history_needed']['noul']=1.
        self.fixture.history()
        self.fixture.history_kind='ambiguous'
        await self.post()
        payload=self.fixture.producer_calls[0]
        self.assertEqual(payload['dependency'],'independent')
        self.assertIn({'part':'history','outcome':'clarification_needed'},payload['part_outcomes'])

    async def test_empty_search_resumes_independent_help_once_and_keeps_the_missing_target(self):
        target='The preferred period in my earlier planning note'
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,target=target)
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(len(self.fixture.producer_calls),2)
        self.assertEqual(self.fixture.producer_calls[1]['missing_context'],target)
        self.assertEqual(result.kind,'draft_ready')

    async def test_empty_search_never_resumes_dependent_work_and_preserves_the_specific_target(self):
        target='The preferred period in my earlier planning note'
        self.fixture.answers['department_dependency']=self.fixture.choice('dependent',('independent','dependent','unclear'))
        def produce(payload,reply):
            reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,target=target)
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(len(self.fixture.producer_calls),1)
        self.assertIsNone(result.draft)
        self.assertEqual([row.target for row in result.contributions],[target])

    async def test_vague_context_request_resumes_only_after_an_actual_empty_search(self):
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='shipping_delivery')
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        self.assertTrue(any(isinstance(row,tuple) and row[0]=='history' for row in self.fixture.storage.calls))
        self.assertEqual(len(self.fixture.producer_calls),2)
        self.assertEqual(self.fixture.boundaries[0].kind,'draft_ready')

    async def test_history_withheld_once_is_never_released_by_a_later_search(self):
        labels=('restricted','not_restricted','uncertain')
        for first in ('restricted','uncertain'):
            with self.subTest(first=first):
                await self.employee()
                fixture=self.recalled_employee_fixture()
                fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
                fixture.restricted_sequence=[fixture.choice(first,labels)]
                def produce(payload,reply):
                    if len(fixture.producer_calls)==1:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier allocation discussion')
                    return reply
                fixture.producer_hook=produce
                await self.post()
                for payload in fixture.producer_calls:
                    self.assertEqual({row['source_kind'] for row in payload['evidence']},{'knowledge'})
                self.assertEqual(fixture.boundaries[0].kind,'draft_ready')

    async def test_resumed_producer_may_name_the_missing_item_without_losing_its_answer(self):
        target='The preferred period in my earlier planning note'
        for named in ('history',):  # Owner decision 2026-11-19: only code-accepted labels are expressible.
            with self.subTest(named=named):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                def produce(payload,reply):
                    if len(self.fixture.producer_calls)==1:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,target=target)
                    else:
                        reply['unresolved_parts']=[named]
                    return reply
                self.fixture.producer_hook=produce
                await self.post()
                result=self.fixture.boundaries[0]
                self.assertEqual(result.kind,'draft_ready')
                self.assertEqual(result.draft.unresolved_parts,('history',))
                outcomes=[row.kind for row in result.dispositions if row.part=='shipping_delivery']
                self.assertEqual(outcomes,['supported_answer'])

    async def test_failed_adjudication_of_history_never_looks_like_a_complete_draft(self):
        self.fixture.answers['history_needed']['noul']=1.
        self.fixture.history()
        self.fixture.history_match='uncertain'
        self.fixture.candidate_reply=lambda payload:{'question':'Another request',
            'outcome':{'status':'resolved','relevant_excerpt_ids':[]}}
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertIn('history',result.unresolved_parts)
        self.assertIn(('history','technical_failure'),[(row.part,row.kind) for row in result.dispositions])
        for payload in self.fixture.producer_calls:
            self.assertEqual({row['source_kind'] for row in payload['evidence']},{'knowledge'})

    async def test_central_support_resumes_once_after_an_empty_search(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-CW-003',))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_shipping_delivery']['noul']=0.
        self.fixture.answers['company_wide_topic']['noul']=1.
        self.fixture.destinations=('central_support',)
        target='The earlier support note'
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,target=target)
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(len(self.fixture.producer_calls),2)
        self.assertEqual(self.fixture.producer_calls[1]['missing_context'],target)
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual([row.kind for row in result.dispositions if row.part=='central_support'],['supported_answer'])

    async def test_settled_search_hint_is_not_reported_as_an_unresolved_part(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-001','SU-CW-003'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['company_wide_topic']['noul']=.21
        self.fixture.descriptor_matches={'source_SU_CW_003':'irrelevant'}
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertNotIn('central_support',result.unresolved_parts)
        self.assertNotIn('central_support',[row.part for row in result.dispositions])

    def request_context_once(self,target):
        # The first producer asks for earlier context; its resumed call answers normally.
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,target=target)
            return reply
        self.fixture.producer_hook=produce

    async def test_settled_history_refusal_survives_routing_adjudication(self):
        self.fixture.answers['topic_shipping_delivery']['noul']=0.
        self.fixture.answers['history_needed']['noul']=1.
        self.fixture.history()
        self.fixture.history_topics=('hr','shipping_delivery')
        self.fixture.history_company_score=.6  # One unrelated weak field triggers adjudication.
        await self.post()
        result=self.fixture.boundaries[0]
        outcomes=[(row.part,row.kind) for row in result.dispositions]
        self.assertIn(('hr','refusal'),outcomes)
        self.assertIn(('history','refusal'),outcomes)
        for payload in self.fixture.producer_calls:
            self.assertNotIn('owned_history',[row['source_kind'] for row in payload['evidence']])
        routing=[json.loads(body['input']) for stage,body in self.fixture.calls if stage=='RoutingResolution']
        self.assertEqual([(row['history'],row['history_status']) for row in routing],[([],'withheld')])

    async def test_rejected_history_candidate_is_reported_as_unresolved(self):
        replies=(({'status':'ambiguous'},'clarification_needed'),({'status':'insufficient'},'insufficient_evidence'),
                 ({'status':'resolved','relevant_excerpt_ids':[]},'insufficient_evidence'))
        for outcome,wanted in replies:
            with self.subTest(outcome=outcome['status'],wanted=wanted):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                self.fixture.answers['history_needed']['noul']=1.
                self.fixture.history()
                self.fixture.history_match='uncertain'
                self.fixture.candidate_reply=lambda payload:{'question':payload['question'],'outcome':outcome}
                await self.post()
                result=self.fixture.boundaries[0]
                self.assertIn('history',result.unresolved_parts)
                self.assertIn(('history',wanted),[(row.part,row.kind) for row in result.dispositions])

    async def test_settled_dependent_refusal_survives_an_unrelated_weak_route_field(self):
        labels=('independent','dependent','unclear')
        weak={'type':'choice','choice':'independent','confidence':.4,
              'probabilities':{'independent':.6,'dependent':.3,'unclear':.1}}
        for name,incoming,adjudicated in (('adjudicator independent',None,'independent'),
                                          ('adjudicator dependent',None,'dependent'),
                                          ('weak incoming dependency',weak,'independent')):
            with self.subTest(name=name):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                fixture=self.fixture
                fixture.answers['topic_shipping_delivery']['noul']=0.
                fixture.answers['history_needed']['noul']=1.
                if incoming is not None:
                    fixture.answers['department_dependency']=incoming
                fixture.history()
                fixture.history_topics=('hr','shipping_delivery')
                fixture.history_company_score=.6
                fixture.dependency=adjudicated
                def settle_route(stage):
                    # Incoming has been judged; the history route then confidently says dependent.
                    if stage=='relevance':
                        fixture.answers['department_dependency']=fixture.choice('dependent',labels)
                fixture.model_hook=settle_route
                await self.post()
                self.assertEqual(fixture.boundaries[0].kind,'refusal')
                self.assertEqual(fixture.producer_calls,[])

    async def test_settled_dependent_refusal_does_not_depend_on_the_routing_status(self):
        labels=('independent','dependent','unclear')
        for status in ('resolved','ambiguous','insufficient'):
            with self.subTest(status=status):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                fixture=self.fixture
                fixture.answers['topic_shipping_delivery']['noul']=0.
                fixture.answers['history_needed']['noul']=1.
                fixture.history()
                fixture.history_topics=('hr','shipping_delivery')
                fixture.history_company_score=.6
                fixture.routing_status=status
                def settle_route(stage):
                    if stage=='relevance':
                        fixture.answers['department_dependency']=fixture.choice('dependent',labels)
                fixture.model_hook=settle_route
                await self.post()
                result=fixture.boundaries[0]
                self.assertEqual(result.kind,'refusal')
                self.assertEqual(fixture.producer_calls,[])
                self.assertIn(('hr','refusal'),[(row.part,row.kind) for row in result.dispositions])
                self.assertIn(('history','refusal'),[(row.part,row.kind) for row in result.dispositions])

    async def test_history_judged_malicious_stays_excluded_from_a_later_search(self):
        self.fixture.answers['history_needed']['noul']=1.
        self.fixture.history()
        self.fixture.history_verdict='malicious'
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                self.fixture.history_verdict='clean'  # A later inconsistent review cannot reverse the exclusion.
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            return reply
        self.fixture.producer_hook=produce
        original=EvidenceService.supplementary
        def with_review_capacity(service,**kwargs):
            service._reviewed=1  # Keep one approved batch free so only the exclusion can stop this row.
            return original(service,**kwargs)
        with mock.patch.object(EvidenceService,'supplementary',with_review_capacity):
            await self.post()
        self.assertEqual(len(self.fixture.producer_calls),2)
        for payload in self.fixture.producer_calls:
            self.assertEqual({row['source_kind'] for row in payload['evidence']},{'knowledge'})
        self.assertIn(('history','unavailable'),[(row.part,row.kind) for row in self.fixture.boundaries[0].dispositions])

    async def test_customer_continuation_ignores_only_unrelated_weak_route_fields(self):
        for name,company,scores,released in (('unrelated weak company score',.6,{},True),
                                             ('unrelated weak department score',None,{'billing_payments':.35},True),
                                             ('another answered topic is weak',None,{'billing_payments':.75},False)):
            with self.subTest(name=name):
                await self.reset_http()
                answered=name.startswith('another')
                self.fixture=PrivateWorkflowFixture(self.http.tracking,
                    source_ids=('SU-BP-001','SU-SD-001') if answered else ('SU-SD-001',))
                self.addCleanup(self.fixture.close)
                fixture=self.fixture
                if answered:
                    fixture.answers['topic_billing_payments']['noul']=1.
                    fixture.destinations=('billing_payments','shipping_delivery')
                fixture.history()
                fixture.history_company_score=company
                fixture.history_topic_scores=scores
                def produce(payload,reply):
                    asked=sum(row['part']=='shipping_delivery' for row in fixture.producer_calls)
                    if payload['part']=='shipping_delivery' and asked==1:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier parcel correction')
                    return reply
                fixture.producer_hook=produce
                await self.post()
                kinds={row['source_kind'] for payload in fixture.producer_calls for row in payload['evidence']}
                self.assertEqual('owned_history' in kinds,released)

    async def test_unrouted_search_hint_with_an_uncertain_source_is_not_reported_unresolved(self):
        cases=((('SU-BP-004','SU-SD-002'),'shipping_delivery','topic_shipping_delivery','billing_payments',.3),
               (('SU-SD-004','SU-BP-005'),'billing_payments','topic_billing_payments','shipping_delivery',.62),
               (('SU-BP-004','SU-CW-002'),'central_support','company_wide_topic','billing_payments',.25))
        labels=('independent','dependent','unclear')
        for sources,hint,field,main,score in cases:
            for dependency in labels:
                for reply in ('omitted','uncertain','irrelevant'):
                    with self.subTest(hint=hint,dependency=dependency,reply=reply):
                        await self.reset_http()
                        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=sources)
                        self.addCleanup(self.fixture.close)
                        fixture=self.fixture
                        fixture.answers['topic_shipping_delivery']['noul']=0.
                        fixture.answers['topic_'+main]['noul']=1.
                        fixture.answers[field]['noul']=score
                        fixture.answers['department_dependency']=fixture.choice(dependency,labels)
                        fixture.dependency=dependency
                        fixture.destinations=(main,)
                        fixture.descriptor_matches={'source_'+sources[1].replace('-','_'):'uncertain'}
                        fixture.routing_matches={} if reply=='omitted' else {sources[1]:reply}
                        await self.post()
                        result=fixture.boundaries[0]
                        self.assertEqual(result.kind,'draft_ready')
                        self.assertNotIn(hint,result.draft.unresolved_parts)
                        self.assertNotIn(hint,[row.part for row in result.dispositions])

    async def test_part_the_adjudicator_routes_stays_unresolved_while_its_source_is_uncertain(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-BP-004','SU-SD-002'))
        self.addCleanup(self.fixture.close)
        fixture=self.fixture
        fixture.answers['topic_shipping_delivery']['noul']=.3
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        fixture.descriptor_matches={'source_SU_SD_002':'uncertain'}
        fixture.routing_matches={'SU-SD-002':'uncertain'}
        await self.post()
        self.assertIn('shipping_delivery',fixture.boundaries[0].unresolved_parts)

    async def test_partly_covered_routed_part_stays_unresolved_and_blocks_dependent_drafts(self):
        labels=('independent','dependent','unclear')
        for dependency in labels:
            with self.subTest(dependency=dependency):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-001','SU-SD-006'))
                self.addCleanup(self.fixture.close)
                fixture=self.fixture
                fixture.answers['department_dependency']=fixture.choice(dependency,labels)
                fixture.dependency=dependency
                fixture.descriptor_matches={'source_SU_SD_006':'uncertain'}  # One source admitted, one undecided.
                fixture.routing_matches={'SU-SD-006':'uncertain'}
                await self.post()
                result=fixture.boundaries[0]
                self.assertIn('shipping_delivery',result.unresolved_parts)
                self.assertEqual(result.draft is not None,dependency=='independent')

    async def test_undecided_search_hint_stays_visible_without_a_routing_decision(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-BP-004','SU-SD-002'))
        self.addCleanup(self.fixture.close)
        fixture=self.fixture
        fixture.answers['topic_shipping_delivery']['noul']=.3
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.descriptor_matches={'source_SU_SD_002':'uncertain'}
        fixture.answer_models=None  # No adjudicator exists, so nothing has settled the hint.
        await self.post()
        self.assertIn('shipping_delivery',fixture.evidence_results[0].unresolved_parts)

    @staticmethod
    def action_label(confidence):
        # A valid native Choice whose winning action label carries the requested confidence.
        return {'type':'choice','choice':'individual_case_or_action','confidence':confidence,'probabilities':{
            'individual_case_or_action':.6,'information':.2,'mixed':.1,'unclear_or_other':.1}}

    async def fresh_fixture(self,**options):
        await self.reset_http()
        self.fixture=PrivateWorkflowFixture(self.http.tracking,**options)
        self.addCleanup(self.fixture.close)
        return self.fixture

    async def test_low_confidence_action_label_can_be_settled_as_information(self):
        for confidence in (.2,.45,.69):
            with self.subTest(confidence=confidence):
                fixture=await self.fresh_fixture()
                fixture.answers['intent']=self.action_label(confidence)
                await self.post()
                result=fixture.boundaries[0]
                self.assertEqual(sum(stage=='RoutingResolution' for stage,_ in fixture.calls),1)
                self.assertEqual(result.kind,'draft_ready')
                self.assertEqual([row['part'] for row in fixture.producer_calls],['shipping_delivery'])
                self.assertNotIn('action',[row.part for row in result.dispositions])

    async def test_unsettled_or_confirmed_action_keeps_its_handoff_outcome(self):
        def failing(stage):
            if stage=='RoutingResolution':
                raise RuntimeError('Controlled routing failure')
        for name,intent,status,hook,produces in (
                ('confirmed action','individual_case_or_action','resolved',None,False),
                ('mixed request','mixed','resolved',None,True),
                ('intent left open',None,'resolved',None,False),
                ('still unclear','unclear_or_other','resolved',None,False),
                ('ambiguous routing','information','ambiguous',None,False),
                ('insufficient routing','information','insufficient',None,False),
                ('routing failure','information','resolved',failing,False)):
            with self.subTest(name=name):
                fixture=await self.fresh_fixture()
                fixture.answers['intent']=self.action_label(.5)
                fixture.routing_intent=intent
                fixture.routing_status=status
                if hook is not None:
                    fixture.model_hook=hook
                await self.post()
                result=fixture.boundaries[0]
                self.assertIn(('action','handoff_needed'),[(row.part,row.kind) for row in result.dispositions])
                self.assertEqual(bool(fixture.producer_calls),produces)

    async def test_low_confidence_mixed_request_keeps_its_action_part_after_adjudication(self):
        fixture=await self.fresh_fixture()
        fixture.answers['intent']={'type':'choice','choice':'mixed','confidence':.5,'probabilities':{
            'mixed':.6,'information':.2,'individual_case_or_action':.1,'unclear_or_other':.1}}
        await self.post()
        self.assertTrue(any(stage=='RoutingResolution' for stage,_ in fixture.calls))
        self.assertIn(('action','handoff_needed'),[(row.part,row.kind) for row in fixture.boundaries[0].dispositions])

    async def test_settled_action_label_never_reaches_routing_adjudication(self):
        for name,confidence,sentiment,topic in (('fully confident',1.,'neutral',1.),
                                                ('exactly at the trigger',.7,'neutral',1.),
                                                ('negative sentiment',.5,'negative',1.),
                                                ('nothing to route',.5,'neutral',0.)):
            with self.subTest(name=name):
                fixture=await self.fresh_fixture()
                fixture.answers['intent']=self.action_label(confidence)
                fixture.answers['topic_shipping_delivery']['noul']=topic
                fixture.answers['sentiment']=fixture.choice(sentiment,('positive','neutral','negative'))
                await self.post()
                self.assertFalse(any(stage=='RoutingResolution' for stage,_ in fixture.calls))
                self.assertEqual(fixture.boundaries[0].kind,'handoff_needed')
                self.assertEqual(fixture.producer_calls,[])

    async def test_restricted_topic_added_by_adjudication_stays_refused_for_customers(self):
        fixture=await self.fresh_fixture()
        fixture.answers['intent']=self.action_label(.5)
        fixture.answers['topic_hr']['noul']=.4  # Weak, so only the adjudicator's route can add it.
        fixture.destinations=('hr','shipping_delivery')
        await self.post()
        result=fixture.boundaries[0]
        self.assertTrue(any(stage=='RoutingResolution' for stage,_ in fixture.calls))
        self.assertIn(('hr','refusal'),[(row.part,row.kind) for row in result.dispositions])
        self.assertEqual([row['part'] for row in fixture.producer_calls],['shipping_delivery'])
        restricted=await self.fresh_fixture()
        restricted.answers['intent']=self.action_label(.5)
        restricted.answers['topic_shipping_delivery']['noul']=0.
        restricted.answers['topic_hr']['noul']=1.
        await self.post()
        self.assertEqual(restricted.boundaries[0].kind,'refusal')
        self.assertEqual(restricted.producer_calls,[])

    async def test_action_cleared_as_information_still_uses_the_history_search(self):
        fixture=await self.fresh_fixture()
        fixture.answers['intent']=self.action_label(.5)
        fixture.answers['history_needed']['noul']=1.
        fixture.history()
        await self.post()
        self.assertTrue(any(isinstance(row,tuple) and row[0]=='history' for row in fixture.storage.calls))
        self.assertIn('owned_history',[row['source_kind'] for payload in fixture.producer_calls
                                       for row in payload['evidence']])
        self.assertNotIn('action',[row.part for row in fixture.boundaries[0].dispositions])

    async def test_unclear_request_settled_as_information_with_no_route_starts_no_work(self):
        fixture=await self.fresh_fixture()
        fixture.answers['intent']={'type':'choice','choice':'unclear_or_other','confidence':.4,'probabilities':{
            'unclear_or_other':.5,'information':.3,'individual_case_or_action':.1,'mixed':.1}}
        fixture.answers['topic_shipping_delivery']['noul']=.35  # Only a weak hint exists.
        fixture.destinations=()  # The adjudicator says information but names no department.
        await self.post()
        self.assertTrue(any(stage=='RoutingResolution' for stage,_ in fixture.calls))
        self.assertEqual(fixture.boundaries[0].kind,'insufficient_evidence')
        self.assertEqual(fixture.producer_calls,[])
        self.assertFalse(any(isinstance(row,tuple) and row[0]=='knowledge' for row in fixture.storage.calls))

    async def test_action_label_is_kept_when_the_information_reply_names_no_route(self):
        for history_needed in (1.,0.):
            with self.subTest(history_needed=history_needed):
                fixture=await self.fresh_fixture()
                fixture.answers['intent']=self.action_label(.55)
                fixture.answers['history_needed']['noul']=history_needed
                fixture.answers['topic_shipping_delivery']['noul']=.35  # Only a weak hint exists.
                fixture.destinations=()  # The adjudicator says information but names no department.
                fixture.history()
                await self.post()
                result=fixture.boundaries[0]
                self.assertEqual(result.kind,'handoff_needed')
                self.assertIn(('action','handoff_needed'),[(row.part,row.kind) for row in result.dispositions])
                self.assertEqual(fixture.producer_calls,[])

    async def test_employee_follow_up_history_needs_a_confident_unrestricted_subject(self):
        labels=('restricted','not_restricted','uncertain')
        weak={'type':'choice','choice':'not_restricted','confidence':.65,
              'probabilities':{'restricted':.2,'not_restricted':.7,'uncertain':.1}}
        for name,answer,outcome in (('restricted',PrivateWorkflowFixture.choice('restricted',labels),'refusal'),
                                    ('uncertain',PrivateWorkflowFixture.choice('uncertain',labels),'insufficient_evidence'),
                                    ('weak',weak,'insufficient_evidence')):
            with self.subTest(judgment=name):
                await self.employee('facilities_admin')
                fixture=self.recalled_employee_fixture(sources=('SU-HR-010','SU-IT-004'),topics=('it_support',))
                fixture.answers['history_needed']['noul']=0.
                fixture.restricted_history=answer
                self.request_context_once('The earlier allocation discussion')
                await self.post()
                self.assertTrue(any('restricted_history_subject' in body['questions']
                                    for stage,body in fixture.calls if stage=='history_route'))
                kinds={row['source_kind'] for payload in fixture.producer_calls for row in payload['evidence']}
                self.assertEqual(kinds,{'knowledge'})
                self.assertIn(('history',outcome),[(row.part,row.kind) for row in fixture.boundaries[0].dispositions])

    async def test_employee_denial_on_the_first_search_survives_the_follow_up_search(self):
        labels=('restricted','not_restricted','uncertain')
        await self.employee()
        fixture=self.recalled_employee_fixture(sources=('SU-HR-010','SU-IT-004'),topics=('it_support',))
        # A mixed request searches history during ordinary preparation, not the pre-dispatch history phase.
        fixture.answers['intent']=fixture.choice('mixed',('information','individual_case_or_action','mixed','unclear_or_other'))
        fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
        fixture.restricted_sequence=[fixture.choice('restricted',labels)]  # Later route calls would allow it.
        self.request_context_once('The earlier allocation discussion')
        await self.post()
        self.assertEqual(sum(stage=='history_route' for stage,_ in fixture.calls),1)
        kinds={row['source_kind'] for payload in fixture.producer_calls for row in payload['evidence']}
        self.assertEqual(kinds,{'knowledge'})

    async def test_central_support_resume_keeps_its_earlier_unresolved_status(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-001','SU-CW-003'))
        self.addCleanup(self.fixture.close)
        fixture=self.fixture
        fixture.answers['company_wide_topic']['noul']=1.
        fixture.destinations=('central_support','shipping_delivery')
        company_fact=fixture.catalog['SU-CW-003'].answer_facts[0]
        fixture.current_match_for=lambda text:'irrelevant' if company_fact in text else 'relevant'
        def produce(payload,reply):
            asked=sum(row['part']=='central_support' for row in fixture.producer_calls)
            if payload['part']=='central_support' and asked==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier support note')
            return reply
        fixture.producer_hook=produce
        await self.post()
        self.assertIn('central_support',fixture.boundaries[0].draft.unresolved_parts)
        self.assertIn('history',fixture.boundaries[0].draft.unresolved_parts)
        self.assertIn('central_support',fixture.producer_calls[-1]['unresolved_parts'])

    async def test_exhausted_review_batches_keep_supported_work_without_a_security_outcome(self):
        self.fixture.history()
        self.request_context_once('The earlier parcel correction')
        original=EvidenceService.supplementary
        reviews=[]
        def exhausted(service,**kwargs):
            service._reviewed=2  # The approved two-batch limit is already spent at this continuation.
            reviews.append(sum(stage=='ExcerptReview' for stage,_ in self.fixture.calls))
            return original(service,**kwargs)
        with mock.patch.object(EvidenceService,'supplementary',exhausted):
            await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(sum(stage=='ExcerptReview' for stage,_ in self.fixture.calls),reviews[0])
        self.assertNotIn('security_fallback',[row.kind for row in result.dispositions])
        self.assertIn('history',result.draft.unresolved_parts)
        self.assertEqual({row['source_kind'] for row in self.fixture.producer_calls[-1]['evidence']},{'knowledge'})

    async def test_failed_search_does_not_mark_another_answered_part_unresolved(self):
        for variant in ('empty','ambiguous'):
            with self.subTest(variant=variant):
                await self.reset_http()
                self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-BP-001','SU-SD-001'))
                self.addCleanup(self.fixture.close)
                self.fixture.answers['topic_billing_payments']['noul']=1.
                self.fixture.destinations=('billing_payments','shipping_delivery')
                if variant=='ambiguous':
                    self.fixture.history()
                    self.fixture.history_kind='ambiguous'
                def produce(payload,reply):
                    asked=sum(row['part']=='shipping_delivery' for row in self.fixture.producer_calls)
                    if payload['part']=='shipping_delivery' and asked==1:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier parcel correction')
                    return reply
                self.fixture.producer_hook=produce
                await self.post()
                result=self.fixture.boundaries[0]
                self.assertEqual([row.kind for row in result.dispositions if row.part=='billing_payments'],
                                 ['supported_answer'])
                self.assertNotIn('billing_payments',result.unresolved_parts)

    async def test_confident_unrestricted_employee_history_on_another_topic_is_released(self):
        await self.employee()
        fixture=self.recalled_employee_fixture(sources=('SU-IT-004',),topics=('it_support',))
        fixture.answers['history_needed']['noul']=0.  # The history is recalled by the producer's continuation.
        fixture.history_topics=('billing_payments',)  # A general subject outside this request's current route.
        self.request_context_once('The earlier parcel correction')
        await self.post()
        self.assertTrue(any(row['source_kind']=='owned_history'
                            for payload in fixture.producer_calls for row in payload['evidence']))
        self.assertNotIn('history',[row.part for row in fixture.boundaries[0].dispositions])

    async def test_resumed_producer_keeps_its_own_unresolved_part_when_the_target_is_vague(self):
        def produce(payload,reply):
            if len(self.fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='shipping_delivery')
            else:
                reply['unresolved_parts']=['shipping_delivery']
            return reply
        self.fixture.producer_hook=produce
        await self.post()
        self.assertIn('shipping_delivery',self.fixture.boundaries[0].draft.unresolved_parts)

    async def test_search_finding_only_withheld_history_keeps_the_refusal_outcome(self):
        await self.employee()
        fixture=self.recalled_employee_fixture()
        labels=('restricted','not_restricted','uncertain')
        fixture.descriptor_matches={'source_SU_HR_010':'irrelevant'}
        fixture.restricted_history=fixture.choice('restricted',labels)
        self.request_context_once('The earlier allocation discussion')
        await self.post()
        self.assertEqual([row.kind for row in fixture.boundaries[0].dispositions if row.part=='history'],['refusal'])

    @staticmethod
    def unresolved_labels(schema):
        # Find the allowed label list for unresolved_parts, wherever the SDK places it.
        def search(node):
            if isinstance(node,dict):
                if 'enum' in node:
                    return node['enum']
                for value in node.values():
                    found=search(value)
                    if found is not None:
                        return found
            if isinstance(node,list):
                for value in node:
                    found=search(value)
                    if found is not None:
                        return found
            return None
        return search(schema['properties']['unresolved_parts'])

    async def test_each_producer_reply_format_allows_only_the_parts_code_accepts(self):
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        def produce(payload,reply):
            asked=sum(row['part']=='shipping_delivery' for row in fixture.producer_calls)
            if payload['part']=='shipping_delivery' and asked==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier delivery note')
            return reply
        fixture.producer_hook=produce
        await self.post()
        bodies=[body for stage,body in fixture.calls if stage=='ProducerReply']
        self.assertEqual(len(bodies),len(fixture.producer_calls))
        self.assertTrue(any(payload['missing_context'] for payload in fixture.producer_calls))
        for body,payload in zip(bodies,fixture.producer_calls):
            schema=body['text']['format']['schema']
            expected={payload['part'],*payload['unresolved_parts']}|({'history'} if payload['missing_context'] else set())
            self.assertEqual(set(self.unresolved_labels(schema)),expected)
            self.assertTrue(schema['properties']['target'].get('description'))

    async def test_failed_second_part_is_synthesized_instead_of_passed_through(self):
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        def produce(payload,reply):
            if payload['part']=='shipping_delivery':
                reply.update(kind='insufficient_evidence',text=None,cited_ids=[],support_assertion=False)
            return reply
        fixture.producer_hook=produce
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual(result.draft.contributor_ids,('billing_payments','central_support'))
        central=[payload for payload in fixture.producer_calls if payload['part']=='central_support']
        self.assertEqual(len(central),1)
        self.assertIn('shipping_delivery',central[0]['unresolved_parts'])
        self.assertIn('shipping_delivery',result.draft.unresolved_parts)

    PAIRED_SOURCES=(  # Every catalog part pairing answerable sources with a restricted or non-general one.
        ('customer','billing_payments','SU-BP-001',('SU-BP-008','SU-BP-009','SU-BP-010'),'handoff_needed'),
        ('customer','shipping_delivery','SU-SD-001',('SU-SD-008','SU-SD-009','SU-SD-010'),'handoff_needed'),
        ('customer','company_wide','SU-CW-002',('SU-CW-001',),'refusal'),
        ('employee','hr','SU-HR-001',('SU-HR-008','SU-HR-009'),'handoff_needed'),
        ('employee','hr','SU-HR-001',('SU-HR-010',),'refusal'),
        ('employee','it_support','SU-IT-001',('SU-IT-008','SU-IT-009'),'handoff_needed'),
        ('employee','it_support','SU-IT-001',('SU-IT-010',),'refusal'),
        ('employee','facilities_admin','SU-FA-001',('SU-FA-008','SU-FA-009'),'handoff_needed'),
        ('employee','facilities_admin','SU-FA-001',('SU-FA-010',),'refusal'),
        ('employee','company_wide','SU-CW-001',('SU-CW-002',),'refusal'))

    async def paired_source_fixture(self,role,subject,answerable,other):
        if role=='employee':
            await self.employee('hr' if subject!='hr' else 'it_support')  # Never the subject's own department.
        fixture=await self.fresh_fixture(source_ids=(answerable,other))
        part='central_support' if subject=='company_wide' else subject
        fixture.answers['topic_shipping_delivery']['noul']=0.
        fixture.answers['company_wide_topic' if subject=='company_wide' else 'topic_'+subject]['noul']=1.
        fixture.destinations=(part,)
        return fixture,part

    async def test_matched_restricted_or_non_general_source_adds_a_note_to_an_answered_part(self):
        # Owner decision 2026-11-19 (R4): admitted answerable evidence answers the part; the other topic is a note.
        for role,subject,answerable,others,note in self.PAIRED_SOURCES:
            for other in others:
                with self.subTest(role=role,other=other):
                    fixture,part=await self.paired_source_fixture(role,subject,answerable,other)
                    await self.post()
                    result=fixture.boundaries[0]
                    self.assertEqual(result.kind,'draft_ready')
                    self.assertIn(part,result.draft.contributor_ids)
                    self.assertIn((part,note),[(row.part,row.kind) for row in result.dispositions])
                    self.assertIn(part,result.draft.unresolved_parts)  # R4 review: the noted topic stays visible.
                    call=next(row for row in fixture.producer_calls if row['part']==part)
                    self.assertEqual(call['own_notes'],[note])
                    self.assertNotIn(part,call['unresolved_parts'])  # R4 review: the agent still answers its share.
                    sources={row['source_record_id'] for row in fixture.producer_calls for row in row['evidence']}
                    self.assertNotIn(other,sources)  # The restricted or non-general source is never used.

    async def test_part_with_only_a_non_general_or_restricted_source_keeps_its_whole_part_outcome(self):
        # R4 applies only when answerable evidence was admitted; otherwise the part is refused or handed off as before.
        for role,subject,_,others,note in (self.PAIRED_SOURCES[1],self.PAIRED_SOURCES[2]):
            with self.subTest(note=note):
                fixture=await self.fresh_fixture(source_ids=(others[0],))
                part='central_support' if subject=='company_wide' else subject
                fixture.answers['topic_shipping_delivery']['noul']=0.
                fixture.answers['company_wide_topic' if subject=='company_wide' else 'topic_'+subject]['noul']=1.
                fixture.destinations=(part,)
                await self.post()
                result=fixture.boundaries[0]
                self.assertIsNone(result.draft)
                self.assertEqual(result.kind,note)
                self.assertEqual([(row.part,row.kind) for row in result.dispositions if row.part==part],[(part,note)])
                self.assertEqual(fixture.producer_calls,[])

    async def test_other_part_with_only_a_non_general_source_stays_handed_off_beside_an_answer(self):
        # Reachable path for the R4 evidence condition: one part answerable, the other only non-general.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-008'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual([row.kind for row in result.dispositions if row.part=='shipping_delivery'],['handoff_needed'])
        self.assertNotIn('shipping_delivery',[row['part'] for row in fixture.producer_calls])

    async def test_noted_part_reason_check_still_rejects_an_echoed_complete_answer(self):
        # R4 review: echoing supplied labels must not satisfy the partial-reason rule for a noted part.
        fixture,part=await self.paired_source_fixture('customer','shipping_delivery','SU-SD-001','SU-SD-008')
        fixture.producer_hook=lambda payload,reply:reply|{'reason':'The facts are not supported.',
                                                          'unresolved_parts':list(payload['unresolved_parts'])}
        await self.post()
        self.assertEqual(fixture.boundaries[0].kind,'technical_failure')

    async def test_partial_answer_may_explain_its_gap_in_reason_without_it_reaching_the_draft(self):
        # Owner decision 2026-11-19 (R4): the live V3 partial-answer shape is accepted; reason stays private.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        secret='PRIVATE-REASON-MARKER'
        def produce(payload,reply):
            if payload['part']=='billing_payments':
                reply.update(reason=secret,unresolved_parts=['billing_payments'])
            return reply
        fixture.producer_hook=produce
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertIn('billing_payments',result.draft.contributor_ids)  # Accepted, not a failure path.
        self.assertIn('billing_payments',result.draft.unresolved_parts)
        self.assertNotIn(secret,result.draft.full_text)
        central=[row for row in fixture.producer_calls if row['part']=='central_support']
        self.assertNotIn(secret,json.dumps(central))

    async def test_supported_answer_with_reason_but_no_own_gap_is_still_rejected(self):
        # A reason on an otherwise complete supported answer may signal a hidden non-answer; keep it strict.
        fixture=await self.fresh_fixture()
        fixture.producer_hook=lambda payload,reply:reply|{'reason':'The facts are not supported.'}
        await self.post()
        self.assertEqual(fixture.boundaries[0].kind,'technical_failure')

    async def test_dependent_request_with_a_matched_restricted_or_non_general_source_is_not_answered(self):
        for role,subject,answerable,others,note in (self.PAIRED_SOURCES[1],self.PAIRED_SOURCES[2]):
            with self.subTest(note=note):
                fixture,part=await self.paired_source_fixture(role,subject,answerable,others[0])
                fixture.dependency='dependent'
                fixture.answers['department_dependency']=fixture.choice('dependent',('independent','dependent','unclear'))
                await self.post()
                result=fixture.boundaries[0]
                self.assertIsNone(result.draft)
                self.assertIn((part,note),[(row.part,row.kind) for row in result.dispositions])
                self.assertEqual(fixture.producer_calls,[])

    async def test_each_specialist_is_told_which_other_parts_other_departments_answer(self):
        # Owner decision 2026-11-19 (R3): a department judges only its own share of a multi-part question.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        await self.post()
        others={row['part']:row['other_parts'] for row in fixture.producer_calls}
        self.assertEqual(others['billing_payments'],['shipping_delivery'])
        self.assertEqual(others['shipping_delivery'],['billing_payments'])
        body=next(body for stage,body in fixture.calls if stage=='ProducerReply')
        # Wording check only; whether the live model follows it is V3 evidence.
        self.assertIn('Judge sufficiency only for your own share',body['instructions'])
        self.assertIn('never return a non-answer result because facts for an other_parts item are missing',
                      body['instructions'])
        # R3 review findings 1-2: other parts may be refused or handed off, and a searchable gap still asks first.
        self.assertIn('handled separately; their status, when known, is in part_outcomes',body['instructions'])
        self.assertNotIn('other producers answer them',body['instructions'])
        # R4: wording only; live compliance is V4 evidence.
        self.assertIn('own_notes lists topics inside your own part that must not be answered',body['instructions'])
        self.assertIn('a partial one (own part in unresolved_parts) may give a short reason',body['instructions'])
        self.assertIn('If something missing from your own share is a specific earlier case or fact that a search '
                      'could supply and missing_context is not set, return context_needed',body['instructions'])

    async def test_partial_specialist_part_stays_unresolved_through_synthesis(self):
        # R3 review finding 3: one department answers partly, the other fully; the gap survives synthesis.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        def produce(payload,reply):
            if payload['part']=='billing_payments':
                reply.update(unresolved_parts=['billing_payments'])
            return reply
        fixture.producer_hook=produce
        await self.post()
        result=fixture.boundaries[0]
        central=[row for row in fixture.producer_calls if row['part']=='central_support']
        self.assertEqual(len(central),1)
        self.assertIn('billing_payments',central[0]['unresolved_parts'])
        self.assertIn('billing_payments',result.draft.unresolved_parts)

    async def test_resumed_specialist_keeps_its_other_parts(self):
        # R3 review finding 4: a producer resumed after the history search keeps its own-share boundary.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        fixture.history()
        def produce(payload,reply):
            if payload['part']=='shipping_delivery' and sum(row['part']=='shipping_delivery'
                                                            for row in fixture.producer_calls)==1:
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            return reply
        fixture.producer_hook=produce
        await self.post()
        shipping=[row for row in fixture.producer_calls if row['part']=='shipping_delivery']
        self.assertEqual(len(shipping),2)
        self.assertEqual([row['other_parts'] for row in shipping],[['billing_payments']]*2)

    async def test_specialist_partial_answer_keeps_its_own_part_unresolved_in_the_draft(self):
        # A department may answer the share its evidence supports; the rest stays visibly unresolved.
        fixture=await self.fresh_fixture()
        def produce(payload,reply):
            reply.update(unresolved_parts=[payload['part']])
            return reply
        fixture.producer_hook=produce
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertIn('shipping_delivery',result.draft.unresolved_parts)

    async def test_uncertain_non_negative_sentiment_does_not_hold_back_evidence(self):
        # Owner decision 2026-11-19 (R3): sentiment only drives negative escalation, never evidence admission.
        fixture=await self.fresh_fixture()
        fixture.answers['sentiment']={'type':'choice','choice':'positive','confidence':.3,
            'probabilities':{'positive':.53,'neutral':.47,'negative':0.}}
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual([row['part'] for row in fixture.producer_calls],['shipping_delivery'])

    async def test_uncertain_negative_sentiment_still_escalates_the_whole_request(self):
        # Frozen design: a request classified as negative escalates, whatever the label's confidence.
        fixture=await self.fresh_fixture()
        fixture.answers['sentiment']={'type':'choice','choice':'negative','confidence':.34,
            'probabilities':{'positive':0.,'neutral':.44,'negative':.56}}
        await self.post()
        self.assertEqual(fixture.boundaries[0].kind,'handoff_needed')
        self.assertEqual(fixture.producer_calls,[])

    async def test_gap_only_synthesis_cannot_start_a_history_search(self):
        # Owner decision 2026-11-19: synthesis added only because another part failed must not search history.
        fixture=await self.fresh_fixture(source_ids=('SU-BP-001','SU-SD-001'))
        fixture.answers['topic_billing_payments']['noul']=1.
        fixture.destinations=('billing_payments','shipping_delivery')
        def produce(payload,reply):
            if payload['part']=='shipping_delivery':
                reply.update(kind='insufficient_evidence',text=None,cited_ids=[],support_assertion=False)
            if payload['part']=='central_support':
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='The earlier parcel correction')
            return reply
        fixture.producer_hook=produce
        searches=[]
        original=EvidenceService.supplementary
        def tracked(service,**kwargs):
            searches.append(kwargs.get('target'))
            return original(service,**kwargs)
        with mock.patch.object(EvidenceService,'supplementary',tracked):
            await self.post()
        result=fixture.boundaries[0]
        self.assertEqual(searches,[])
        self.assertNotIn('history',result.unresolved_parts)

    async def test_single_part_with_missing_history_is_still_passed_through(self):
        fixture=await self.fresh_fixture(source_ids=('SU-SD-006',))
        fixture.answers['history_needed']['noul']=1.
        await self.post()
        result=fixture.boundaries[0]
        self.assertEqual([row['part'] for row in fixture.producer_calls],['shipping_delivery'])
        self.assertEqual(result.draft.contributor_ids,('shipping_delivery',))
        self.assertIn('history',result.draft.unresolved_parts)

    async def test_producer_is_told_department_outcomes_are_not_missing_history(self):
        await self.post()
        body=next(body for stage,body in self.fixture.calls if stage=='ProducerReply')
        self.assertIn('For a department part',body['instructions'])

    async def test_supported_request_reaches_actual_producer_without_publishing_private_work(self):
        response=await self.post()
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['error']['code'],'interaction_not_ready')
        result=self.fixture.boundaries[0]
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(result.draft.full_text,self.fixture.draft_text)
        self.assertEqual(result.draft.contributor_ids,('shipping_delivery',))
        self.assertEqual([row['part'] for row in self.fixture.producer_calls],['shipping_delivery'])
        native=self.fixture.producer_calls[0]
        self.assertEqual(UUID(native['request_id']),UUID(response.json()['error']['request_id']))
        self.assertEqual(native['evidence'][0]['source_record_id'],'SU-SD-001')
        self.assertIn(self.fixture.catalog['SU-SD-001'].answer_facts[0],native['evidence'][0]['text'])
        self.assertEqual(self.fixture.storage.calls[:2],['pending_write','pending_read'])
        self.assertEqual(len(self.fixture.conversations.records),1)
        self.assertEqual(self.fixture.offers.records,{})
        record=next(iter(self.fixture.conversations.records.values()))
        self.assertEqual(record[1]['record_type'],'pending_message')
        self.assertEqual(record[0],http_fixtures.VALID['question'])
        private=(self.fixture.draft_text,http_fixtures.VALID['question'],self.fixture.catalog['SU-SD-001'].answer_facts[0])
        for text in private:
            self.assertNotIn(text,self.last_logs)
            self.assertNotIn(text,response.text)
        self.assertEqual(result.contexts[0].request_id,native['request_id'])
        self.assertEqual(len(self.fixture.usage),self.fixture.budget.attempts)

    async def test_unverified_pending_never_reaches_producer_and_retains_technical_disposition(self):
        self.fixture.storage.fail_pending=True
        response=await self.post()
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.fixture.producer_calls,[])
        self.assertEqual(self.fixture.boundaries[0].kind,'technical_failure')
        self.assertIsNone(self.fixture.boundaries[0].draft)
        self.assertFalse(any(stage in ('embedding','ExcerptReview') for stage,_ in self.fixture.calls))

    async def test_pending_failure_retains_technical_outcome_alongside_an_established_refusal(self):
        self.fixture.answers['topic_hr']['noul']=1.
        self.fixture.storage.fail_pending=True
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(result.kind,'technical_failure')
        self.assertIn(('hr','refusal'),[(row.part,row.kind) for row in result.dispositions])
        self.assertIn(('request','technical_failure'),[(row.part,row.kind) for row in result.dispositions])
        self.assertEqual(self.fixture.producer_calls,[])

    async def test_disabled_and_withdrawn_controls_do_not_invent_a_technical_failure(self):
        for control in ('disabled','stopped'):
            with self.subTest(control=control):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                if control=='disabled':
                    self.fixture.security_trigger=None
                else:
                    self.fixture.model_hook=lambda stage:self.fixture.attempt.withdraw() if stage=='incoming' else None
                await self.post()
                self.assertEqual(self.fixture.boundaries[0].kind,control)
                self.assertEqual(self.fixture.boundaries[0].dispositions,())
                self.assertEqual(self.fixture.producer_calls,[])
                self.assertEqual(self.fixture.storage.calls,[])
                self.assertEqual(len(self.fixture.calls),0 if control=='disabled' else 1)

    async def test_company_wide_and_multiple_parts_call_the_actual_required_authors(self):
        for mode,sources,parts,authors in (
                ('company',('SU-CW-002',),('central_support',),('central_support',)),
                ('multiple',('SU-BP-001','SU-SD-001'),('billing_payments','shipping_delivery'),
                 ('billing_payments','shipping_delivery','central_support'))):
            with self.subTest(mode=mode):
                # A new HTTP-owned tracker avoids reusing the previous request's pending prefix.
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=sources)
                self.addCleanup(self.fixture.close)
                self.fixture.destinations=parts
                self.fixture.answers['topic_shipping_delivery']['noul']=float('shipping_delivery' in parts)
                self.fixture.answers['topic_billing_payments']['noul']=float('billing_payments' in parts)
                self.fixture.answers['company_wide_topic']['noul']=float('central_support' in parts)
                await self.post()
                result=self.fixture.boundaries[0]
                self.assertEqual(result.kind,'draft_ready')
                self.assertEqual(result.draft.contributor_ids,authors)
                calls=[row['part'] for row in self.fixture.producer_calls]
                self.assertEqual(sorted(calls),sorted(authors))
                self.assertEqual(calls[-1],'central_support')
                self.assertEqual(result.frozen_contributors,() if mode=='company' else parts)
                self.assertEqual({row['source_record_id'] for payload in self.fixture.producer_calls
                    for row in payload['evidence']},set(sources))
                self.assertEqual(result.contexts[-1].part,'central_support')

    async def test_historical_limits_do_not_suppress_independently_supported_current_guidance(self):
        for variant,expected_kind,history_kind,produces in (
                ('empty','draft_ready','unavailable',True),
                ('ambiguous','draft_ready','clarification_needed',True),
                ('denied','draft_ready','refusal',True),
                ('malicious','draft_ready','unavailable',True),
                ('uncertain','security_fallback','security_fallback',False),
                ('invalid_review','security_fallback','security_fallback',False),
                ('incomplete','technical_failure','technical_failure',False),
                ('foreign','technical_failure','technical_failure',False)):
            with self.subTest(variant=variant):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-006',))
                self.addCleanup(self.fixture.close)
                self.fixture.answers['history_needed']['noul']=1.
                if variant!='empty':
                    self.fixture.history(foreign=variant=='foreign')
                if variant=='incomplete':
                    self.fixture.storage.events=lambda *args,**kwargs:{'ids':[],'documents':[],'metadatas':[]}
                if variant=='ambiguous':
                    self.fixture.history_kind='ambiguous'
                if variant=='denied':
                    self.fixture.history_topics=('hr','shipping_delivery')
                if variant in ('malicious','uncertain'):
                    self.fixture.history_verdict=variant
                if variant=='invalid_review':
                    self.fixture.history_verdict='not-a-verdict'
                response=await self.post()
                self.assertNotEqual(response.status_code,200)
                result=self.fixture.boundaries[0]
                self.assertEqual(result.kind,expected_kind)
                self.assertIn(('history',history_kind),[(row.part,row.kind) for row in result.dispositions])
                self.assertEqual(bool(self.fixture.producer_calls),produces)
                for payload in self.fixture.producer_calls:
                    self.assertEqual({row['source_record_id'] for row in payload['evidence']},{'SU-SD-006'})
                    self.assertTrue(all(row['source_kind']=='knowledge' for row in payload['evidence']))
                    self.assertIn(self.fixture.catalog['SU-SD-006'].answer_facts[0],payload['evidence'][0]['text'])
                if produces:
                    self.assertIn('history',result.draft.unresolved_parts)
                if variant in ('foreign','incomplete'):
                    self.assertFalse(any(stage=='history_route' or stage=='ExcerptReview'
                                         for stage,_ in self.fixture.calls))

    async def test_clear_owned_recap_and_current_guidance_both_reach_the_actual_producer(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-SD-006',))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['history_needed']['noul']=1.
        recalled=self.fixture.history()
        await self.post()
        self.assertEqual(self.fixture.boundaries[0].kind,'draft_ready')
        payload=self.fixture.producer_calls[0]
        self.assertEqual([row['source_kind'] for row in payload['evidence']],['owned_history','knowledge'])
        self.assertIn('Stored answer 1',payload['evidence'][0]['text'])
        self.assertEqual(payload['evidence'][1]['source_record_id'],'SU-SD-006')
        history=self.fixture.boundaries[0].contexts[0].evidence[0]
        self.assertEqual((history.owner_user_id,history.conversation_id),
                         (http_fixtures.VALID['user_id'],recalled))
        self.assertEqual(self.fixture.boundaries[0].draft.cited_ids,tuple(row['excerpt_id'] for row in payload['evidence']))

    async def test_unpromoted_candidates_never_become_generation_inputs(self):
        self.fixture.current_match='uncertain'
        self.fixture.admit_candidates=False
        await self.post()
        prepared=self.fixture.evidence_results[0]
        self.assertTrue(prepared.resolution_candidates)
        self.assertEqual(prepared.cleared,())
        self.assertEqual(self.fixture.producer_calls,[])
        self.assertIsNone(self.fixture.boundaries[0].draft)

    async def test_genuine_history_ambiguity_without_current_help_stays_clarification_without_generation(self):
        self.fixture.answers['history_needed']['noul']=1.
        self.fixture.history()
        self.fixture.history_kind='ambiguous'
        self.fixture.routing_status='ambiguous'
        await self.post()
        self.assertEqual(self.fixture.producer_calls,[])
        self.assertEqual(len(self.fixture.boundaries),1)
        self.assertEqual(self.fixture.boundaries[0].kind,'clarification_needed')
        self.assertIsNone(self.fixture.boundaries[0].draft)
        self.assertIn(('history','clarification_needed'),
                      [(row.part,row.kind) for row in self.fixture.boundaries[0].dispositions])

    async def test_dependent_missing_information_cannot_reach_a_complete_draft(self):
        self.fixture.answers['department_dependency']=self.fixture.choice('dependent',('independent','dependent','unclear'))
        def missing(payload,reply):
            reply['unresolved_parts']=['shipping_delivery']
            return reply
        self.fixture.producer_hook=missing
        await self.post()
        result=self.fixture.boundaries[0]
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertIsNone(result.draft)
        self.assertIn(('shipping_delivery','insufficient_evidence'),[(row.part,row.kind) for row in result.dispositions])

    async def test_independent_partial_help_survives_a_failed_part_but_dependency_blocks_drafting(self):
        for dependency,wanted in (('independent','draft_ready'),('dependent','insufficient_evidence')):
            with self.subTest(dependency=dependency):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-BP-001','SU-SD-001'))
                self.addCleanup(self.fixture.close)
                self.fixture.answers['topic_billing_payments']['noul']=1.
                self.fixture.answers['department_dependency']=self.fixture.choice(
                    dependency,('independent','dependent','unclear'))
                def produce(payload,reply):
                    if payload['part']=='shipping_delivery':
                        reply.update(kind='technical_failure',text=None,cited_ids=[],support_assertion=False)
                    return reply
                self.fixture.producer_hook=produce
                await self.post()
                result=self.fixture.boundaries[0]
                self.assertEqual(result.kind,wanted)
                self.assertEqual(result.frozen_contributors,('billing_payments','shipping_delivery'))
                self.assertIn(('shipping_delivery','technical_failure'),
                              [(row.part,row.kind) for row in result.dispositions])
                if dependency=='independent':
                    self.assertEqual(result.draft.contributor_ids,('billing_payments','central_support'))
                    self.assertEqual(result.draft.unresolved_parts,('shipping_delivery',))
                else:
                    self.assertIsNone(result.draft)

    async def test_incoming_refusal_and_action_dispositions_do_not_start_normal_work(self):
        for variant,wanted,part in (('refusal','refusal','hr'),('action','handoff_needed','action')):
            with self.subTest(variant=variant):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                if variant=='refusal':
                    self.fixture.answers['topic_shipping_delivery']['noul']=0.
                    self.fixture.answers['topic_hr']['noul']=1.
                else:
                    self.fixture.answers['intent']=self.fixture.choice('individual_case_or_action',
                        ('information','individual_case_or_action','mixed','unclear_or_other'))
                await self.post()
                self.assertEqual(self.fixture.boundaries[0].kind,wanted)
                self.assertIn((part,wanted),[(row.part,row.kind) for row in self.fixture.boundaries[0].dispositions])
                self.assertEqual(self.fixture.producer_calls,[])
                self.assertEqual(self.fixture.storage.calls,[])

    async def test_targeted_continuation_shares_original_accounting_and_resumes_only_once(self):
        for succeeds in (True,False):
            with self.subTest(succeeds=succeeds):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                self.fixture.history()
                def produce(payload,reply):
                    if len(self.fixture.producer_calls)==1 or not succeeds:
                        reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                                     target='The earlier parcel correction')
                    return reply
                self.fixture.producer_hook=produce
                await self.post()
                self.assertEqual([row['part'] for row in self.fixture.producer_calls],
                                 ['shipping_delivery','shipping_delivery'])
                searches=[row for row in self.fixture.storage.calls if isinstance(row,tuple) and row[0]=='history']
                self.assertEqual([row[2] for row in searches],[0,1])  # One complete paginated search.
                self.assertEqual(self.fixture.budget.attempts,len(self.fixture.calls))
                self.assertEqual(len(self.fixture.usage),self.fixture.budget.attempts)
                result=self.fixture.boundaries[0]
                self.assertEqual(result.kind,'draft_ready' if succeeds else 'insufficient_evidence')
                self.assertEqual([row['source_kind'] for row in self.fixture.producer_calls[1]['evidence']],
                                 ['knowledge','owned_history'])
                if succeeds:
                    self.assertEqual([row.excerpt_id for row in result.contexts[0].evidence],
                                     [row['excerpt_id'] for row in self.fixture.producer_calls[1]['evidence']])

    async def test_http_disconnect_keeps_admitted_ownership_and_frozen_boundary_while_history_drains(self):
        self.fixture=PrivateWorkflowFixture(self.http.tracking,source_ids=('SU-BP-001','SU-SD-001'))
        self.addCleanup(self.fixture.close)
        self.fixture.answers['topic_billing_payments']['noul']=1.
        self.fixture.destinations=('billing_payments','shipping_delivery')
        started,release,observed=Event(),Event(),Event()
        self.http.release_events.append(release)
        def history(user_id,*,offset,timeout):
            self.http.now=160.
            started.set()  # The earlier billing receipt is required before shipping's supplementary read.
            if not release.wait(8):
                raise AssertionError('Late history read was not released')
            return {'ids':[],'documents':[],'metadatas':[]}
        self.fixture.storage.history_page=history
        def produce(payload,reply):
            if payload['part']=='shipping_delivery':
                reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                             target='Earlier parcel correction')
            return reply
        self.fixture.producer_hook=produce
        self.fixture.observer_hook=lambda result:observed.set()
        application=self.http.app(on_admitted=self.fixture.continue_admitted)
        with self.assertLogs('shopunow.entry',level='INFO') as captured:
            host,messages,sent,state=await self.http.start_wire(application)
            await self.http.thread_event(started)
            await self.http.thread_event(observed)
            snapshot=self.fixture.boundaries[0]
            self.assertEqual(snapshot.draft.contributor_ids,('billing_payments','central_support'))  # Owner decision 2026-11-19: an unfinished other part sends the answer through synthesis.
            self.assertIn(('shipping_delivery','incomplete_at_cutoff'),
                          [(row.part,row.kind) for row in snapshot.dispositions])
            # Owner decision 2026-11-19: the synthesis input is retained after the specialist input.
            self.assertEqual([row.part for row in snapshot.contexts],['billing_payments','central_support'])
            self.assertFalse(state['handle'].lease.capacity_released)
            self.http.assert_key_busy()
            await self.http.assert_running_worker_owns_capacity()
            messages.put_nowait({'type':'http.disconnect'})
            await asyncio.wait_for(host,3)
            self.assertFalse(state['handle'].lease.capacity_released)
            self.http.assert_key_busy()
            calls_before=list(self.fixture.calls)
            release.set()
            result=await state['handle'].lease.wait(3)
        self.assertEqual(result.status,'stopped')
        self.assertTrue(state['handle'].lease.capacity_released)
        self.http.assert_key_reusable()
        self.assertEqual(self.fixture.boundaries,[snapshot])
        self.assertEqual(self.fixture.calls,calls_before)
        self.assertNotIn(self.fixture.draft_text,json.dumps(sent,default=str))
        self.assertNotIn(self.fixture.draft_text,'\n'.join(captured.output))
        self.assertFalse(any(row[1].get('record_type')=='terminal_outcome'
            for row in self.fixture.conversations.records.values()))

    async def test_preclearance_attack_and_uncertainty_keep_existing_public_behavior(self):
        for verdict,status,private_kind in (('confirmed_attack',200,'confirmed_attack'),
                                            ('uncertain',200,'security_fallback')):
            with self.subTest(verdict=verdict):
                await self.http.asyncTearDown()
                self.http.doCleanups()
                await self.http.asyncSetUp()
                self.fixture=PrivateWorkflowFixture(self.http.tracking)
                self.addCleanup(self.fixture.close)
                self.fixture.answers['prompt_injection']['noul']=1.
                self.fixture.verdict=verdict
                saved_at_observation=[]
                self.fixture.observer_hook=lambda result:saved_at_observation.append(
                    (len(self.fixture.offers.records),len(self.fixture.conversations.records)))
                response=await self.post()
                self.assertEqual(response.status_code,status)
                self.assertEqual(self.fixture.boundaries[0].kind,private_kind)
                if verdict=='confirmed_attack':
                    self.assertEqual(self.fixture.boundaries[0].dispositions,())
                self.assertEqual(self.fixture.evidence_results,[])
                self.assertEqual(self.fixture.producer_calls,[])
                self.assertFalse(any(row[1].get('record_type')=='pending_message'
                    for row in self.fixture.conversations.records.values()))
                self.assertEqual(len(self.fixture.offers.records),0 if verdict=='confirmed_attack' else 1)
                self.assertEqual(saved_at_observation,[(0,0)] if verdict=='confirmed_attack' else [(1,1)])
                self.assertNotIn(self.fixture.draft_text,response.text)


if __name__=='__main__':
    unittest.main()
