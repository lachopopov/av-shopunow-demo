# Python 3.12; integrated evidence behavior with controlled external storage/model boundaries.
import importlib
import importlib.util
from threading import Lock
from uuid import UUID
import unittest
from limits.storage import MemoryStorage
from shopunow.admission import OwnedRecordTracking, QuestionAttempt
from shopunow.allowances import RollingAllowances
from shopunow.records import verify_profile, CompleteTranscript
from shopunow.schemas import QuestionRequest
from shopunow.workflow import IncomingDecision
from tests.test_records import USER_ID, CONVERSATION_ID, REQUEST_ID, profile_result
from tests.test_knowledge import source_row
from tests.test_records import transcript_pair, event_result, header_result


def relevance_answers(candidates,kind='clear',selected=None,uncertain=()):
    # Controlled judgments express fixture expectations using the provider's native types.
    from typesafe_sdk import ChoiceAnswer
    chosen={row.excerpt_id for row in candidates} if selected is None else set(selected)
    answers={}
    for row in candidates:
        label='uncertain' if row.excerpt_id in uncertain else 'relevant' if row.excerpt_id in chosen else 'irrelevant'
        answers[row.excerpt_id]=ChoiceAnswer(type='choice',choice=label,confidence=1.0,
            probabilities={name:float(name==label) for name in ('relevant','irrelevant','uncertain')})
    if any(row.source_kind=='owned_history' for row in candidates):
        answers['case_resolution']=ChoiceAnswer(type='choice',choice=kind,confidence=1.0,
            probabilities={name:float(name==kind) for name in ('clear','ambiguous','insufficient')})
    return answers


class EvidenceStorage:
    def __init__(self, row, module):
        self.calls = []
        self.text, self.metadata = module.knowledge_payload(row)
        self.result = {'ids':[['SU-SD-001']], 'documents':[[self.text]],
                       'metadatas':[[self.metadata]]}
        self.fail_pending = False
        self.history_rows = {'ids':[], 'documents':[], 'metadatas':[]}

    def write_pending(self, pending, *, timeout):
        self.calls.append('pending_write')
        self.pending = pending

    def exact_pending(self, request_id, *, timeout):
        self.calls.append('pending_read')
        return {'ids':[self.pending.message_id], 'documents':[self.pending.text],
                'metadatas':[self.pending.model_dump(mode='json', exclude={'text'})]} if not self.fail_pending else {
                    'ids':[], 'documents':[], 'metadatas':[]}

    def knowledge(self, vector, *, where, limit, timeout):
        self.calls.append(('knowledge', where))
        return self.result

    def history_page(self, user_id, *, offset, timeout):
        self.calls.append(('history', user_id, offset))
        return self.history_rows if offset == 0 else {'ids':[], 'documents':[], 'metadatas':[]}


class EvidenceModels:
    def __init__(self):
        self.verdict = 'clean'
        self.mapping = None
        self.relevance_kind = 'clear'
        self.calls = []

    def embed(self, texts, *, timeout):
        self.calls.append('embed')
        return [[1.0, 0.0, 0.0] for _ in texts]

    def relevance(self, question, candidates, *, timeout):
        self.calls.append('relevance')
        return relevance_answers(candidates,*(self.relevance_kind, tuple(c.excerpt_id for c in candidates) if self.relevance_kind=='clear' else (), ()))

    def review(self, candidates, *, timeout):
        self.calls.append(('review', len(candidates)))
        return self.mapping if self.mapping is not None else [
            {'excerpt_id':c.excerpt_id, 'verdict':self.verdict} for c in candidates]

    def history_route(self,question,candidates,*,role,home_department,timeout):
        # Default controlled fixture is owned shipping history; denied/uncertain tests override this boundary.
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        answers=judgment_answers()
        answers['topic_shipping_delivery']['noul']=1.
        return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})


class EvidenceTests(unittest.TestCase):
    def test_candidate_failure_preserves_known_part_outcomes_without_exposing_evidence(self):
        # A failed resolver must not erase known refusal, handoff, or missing-part information.
        for failure in ('unknown_id','timeout'):
            with self.subTest(failure=failure):
                if failure=='timeout':
                    self.setUp()
                self.service.semantic_confidence_trigger=.7
                original=self.models.relevance
                def relevance(question,candidates,*,timeout):
                    if candidates[0].source_kind=='knowledge':
                        return relevance_answers(candidates,selected=(),
                            uncertain=tuple(row.excerpt_id for row in candidates))
                    return original(question,candidates,timeout=timeout)
                self.models.relevance=relevance
                self.service.answer_models,_=self.semantic_adapter()
                def resolve(payload,*,timeout):
                    if failure=='timeout':
                        raise TimeoutError('Controlled external failure')
                    return {'question':payload['question'],'outcome':{'status':'resolved',
                            'relevant_excerpt_ids':['not-in-pool']}}
                self.service.answer_models.resolve_candidates=resolve
                result=self.service.prepare(IncomingDecision('continue',
                    destinations=('shipping_delivery','billing_payments'),refused_departments=('hr',),
                    intent='mixed',dependency='independent',requires_action_handoff=True))
                self.assertEqual(result.kind,'technical_failure')
                self.assertEqual(result.refused_parts,('hr',))
                self.assertEqual(result.handoff_parts,())  # Pass 2: no entry match marks a part for handoff.
                self.assertTrue({'shipping_delivery','hr','billing_payments'}<=set(result.unresolved_parts))
                self.assertEqual(result.destinations,('shipping_delivery','billing_payments'))
                self.assertEqual(result.dependency,'independent')
                self.assertTrue(result.requires_action_handoff)
                self.assertEqual(result.pending_message_id,self.service.pending.message_id)
                self.assertIn('knowledge.e1',result.judgments)
                self.assertIn('knowledge.e1',result.unresolved_judgments)
                self.assertEqual(result.cleared,())
                self.assertEqual(result.resolution_candidates,())

    def test_candidate_native_request_contains_current_descriptor_and_verified_provenance(self):
        # Exercise real SDK serialization, so immutable metadata cannot silently disappear or fail to encode.
        import json
        import httpx2
        from openai import OpenAI
        from shopunow.model_clients import AnswerModels
        self.service.semantic_confidence_trigger=.7
        original=self.models.relevance
        def relevance(question,candidates,*,timeout):
            if candidates[0].source_kind=='knowledge':
                return relevance_answers(candidates,selected=(),
                    uncertain=tuple(row.excerpt_id for row in candidates))
            return original(question,candidates,timeout=timeout)
        self.models.relevance=relevance
        captured=[]
        def respond(request):
            captured.append(json.loads(request.content))
            parsed={'question':'What is tracking?','outcome':{'status':'resolved','relevant_excerpt_ids':['e1']}}
            return httpx2.Response(200,json={'id':'resp_candidate','object':'response','created_at':0,
                'status':'completed','model':'controlled-answer','output':[{'id':'msg_candidate','type':'message',
                    'role':'assistant','status':'completed','content':[{'type':'output_text',
                        'text':json.dumps(parsed),'annotations':[]}]}]})
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
            self.service.answer_models=AnswerModels(client=client,model='controlled-answer',reasoning='low')
            result=self.prepare(intent='information')
        self.assertEqual([row.source_record_id for row in result.cleared],['SU-SD-001'])
        self.assertEqual(len(captured),1)
        payload=json.loads(captured[0]['input'])
        self.assertEqual(payload['destinations'],['shipping_delivery'])
        candidate=payload['candidates'][0]
        self.assertEqual(candidate.get('source_version'),'v1')
        self.assertNotIn('source_descriptor',candidate)  # Pass 4: Chroma's labels (returned_metadata) only.
        self.assertEqual(candidate.get('audience'),['customer'])
        self.assertEqual(candidate.get('classification'),'general')
        self.assertIs(candidate.get('approval_current'),True)
        self.assertEqual(candidate.get('retrieval_scope'),{'role':'customer','destination':'shipping_delivery'})
        self.assertEqual(candidate.get('returned_metadata'),{
            'source_id':'SU-SD-001','version':'v1','eligible_audience':['customer'],
            'subject':'shipping_delivery','classification':'general','access_scope':'customer_general',
            'approval_status':'owner_approved','current_status':'current','synthetic':True})
        self.assertEqual(candidate.get('clearance'),'clean')
        self.assertEqual(candidate.get('relevance_basis'),'semantic_resolution')

    def test_strong_native_supplementary_history_still_requires_exact_subject_access(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        for subject,probability in (('hr',1.),('shipping_delivery',.6)):
            with self.subTest(subject=subject):
                self.setUp()
                self.prepare(intent='information')
                self.identity_fixture(confidence=1.,label='clear')
                self.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
                def route(*args,**kwargs):
                    answers=judgment_answers()
                    answers['topic_'+subject]['noul']=probability
                    return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
                self.models.history_route=route
                result=self.service.supplementary(target='The earlier parcel correction')
                self.assertFalse(any(row.source_kind=='owned_history' for row in result.cleared))
                self.assertIn('history',result.unresolved_parts)
                self.assertEqual(result.refused_parts,('hr',) if subject=='hr' else ())

    def test_unrelated_resolution_cannot_add_a_confidently_negative_topic(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_judgments
        self.service.semantic_confidence_trigger=.7
        self.service.topic_trigger=.5
        self.service.answer_models,calls=self.semantic_adapter(destination='hr')
        answers=judgment_answers()
        answers['department_dependency'].update(confidence=.4,probabilities={
            'independent':.6,'dependent':.39,'unclear':.01})
        result=self.service.prepare(IncomingDecision('continue',destinations=('shipping_delivery',),
            intent='information',judgments=validate_judgments(answers)))
        self.assertEqual(result.refused_parts,())
        self.assertEqual(result.destinations,('shipping_delivery',))

    def test_unrelated_adjudication_cannot_erase_confident_mixed_action_or_source_match(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_judgments
        self.service.semantic_confidence_trigger=.7
        self.service.answer_models,calls=self.semantic_adapter()
        answers=judgment_answers()
        answers['intent'].update(choice='mixed',probabilities={
            key:float(key=='mixed') for key in answers['intent']['probabilities']})
        # Only the dependency is unsure, so the router runs; its 'information' cannot erase a confident action.
        answers['department_dependency'].update(confidence=.4,probabilities={
            'independent':.6,'dependent':.39,'unclear':.01})
        result=self.service.prepare(IncomingDecision('continue',destinations=('shipping_delivery',),
            intent='mixed',requires_action_handoff=True,judgments=validate_judgments(answers)))
        self.assertEqual([kind for kind,_ in calls],['routing'])
        self.assertTrue(getattr(result,'requires_action_handoff',False))

    def test_adjudication_preserves_established_refusal_and_access_precedes_action_handoff(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_judgments
        self.service.semantic_confidence_trigger=.7
        self.service.answer_models,calls=self.semantic_adapter()
        answers=judgment_answers()
        answers['department_dependency'].update(confidence=.4,probabilities={
            'independent':.6,'dependent':.39,'unclear':.01})
        result=self.service.prepare(IncomingDecision('continue',destinations=('shipping_delivery',),
            refused_departments=('hr',),intent='information',judgments=validate_judgments(answers)))
        self.assertEqual(result.refused_parts,('hr',))
        self.assertEqual(result.destinations,('shipping_delivery',))
        self.setUp()
        self.service.answer_models,calls=self.semantic_adapter(destination='hr')
        original=self.service.answer_models.resolve_routing
        self.service.answer_models.resolve_routing=lambda payload,**kwargs:{
            **original(payload,**kwargs),'intent':'individual_case_or_action'}
        result=self.service.prepare(IncomingDecision('semantic_resolution',intent='unclear_or_other'))
        self.assertEqual(result.kind,'refusal')
        self.assertEqual(result.refused_parts,('hr',))

    def test_opt_in_uncertain_intent_reaches_resolution_without_reopening_negative_or_settled_action(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import IncomingContext
        from shopunow.workflow import build_incoming_graph
        import inspect
        self.assertIn('allow_semantic_resolution',inspect.signature(build_incoming_graph).parameters,
                      'The safety-cleared unresolved-intent continuation is missing')
        # Owner decision 2026-11-18: only a low-confidence action label may use routing adjudication.
        for intent,sentiment,confidence,want in (('unclear_or_other','neutral',.4,'semantic_resolution'),
                ('unclear_or_other','negative',.4,'handoff'),
                ('individual_case_or_action','neutral',.4,'semantic_resolution'),
                ('individual_case_or_action','negative',.4,'handoff'),
                ('individual_case_or_action','neutral',.7,'handoff')):
            with self.subTest(intent=intent,sentiment=sentiment,confidence=confidence):
                answers=judgment_answers()
                answers['topic_shipping_delivery']['noul']=1.  # A proposed route gives adjudication a real subject.
                answers['intent'].update(choice=intent,confidence=confidence,probabilities={
                    key:(.6 if key==intent else .4/3) for key in answers['intent']['probabilities']})
                answers['sentiment'].update(choice=sentiment,probabilities={
                    key:float(key==sentiment) for key in answers['sentiment']['probabilities']})
                graph=build_incoming_graph(judge=lambda context:answers,reviewer=lambda *args:{'verdict':'clear'},
                    authorize=lambda:True,security_trigger=.7,topic_trigger=.5,semantic_confidence_trigger=.7,
                    allow_semantic_resolution=True)
                self.assertEqual(graph.invoke({'context':IncomingContext('What is tracking?','customer',None,(),False)})
                                 ['decision'].kind,want)

    def test_semantic_resolution_preserves_ambiguity_and_rejects_unbound_response_once(self):
        # Pass 3: RouterDecision has only resolved/ambiguous; any other status is an invalid reply.
        for status,want in (('ambiguous','clarification_needed'),('insufficient','technical_failure')):
            with self.subTest(status=status):
                self.setUp()
                self.service.semantic_confidence_trigger=.7
                self.service.answer_models,calls=self.semantic_adapter(status=status)
                result=self.service.prepare(IncomingDecision('semantic_resolution',intent='unclear_or_other'))
                self.assertEqual(result.kind,want)
                self.assertEqual(result.cleared,())
                self.assertEqual(len(calls),1)
        self.setUp()
        self.service.answer_models,calls=self.semantic_adapter()
        original=self.service.answer_models.resolve_routing
        def wrong_target(payload,*,timeout):
            return {**original(payload,timeout=timeout),'question':'Another request'}
        self.service.answer_models.resolve_routing=wrong_target
        first=self.service.prepare(IncomingDecision('semantic_resolution',intent='unclear_or_other'))
        second=self.service.prepare(IncomingDecision('semantic_resolution',intent='unclear_or_other'))
        self.assertEqual(first.kind,'technical_failure')
        self.assertEqual(second.cleared,())
        self.assertEqual(len(calls),1)

    def semantic_adapter(self, *, dependency='independent', destination='shipping_delivery', status='resolved'):
        # Controlled external answers; the real service must still enforce source/access boundaries.
        from types import SimpleNamespace
        calls=[]
        def route(payload, *, timeout):
            calls.append(('routing',payload))
            return {'question':payload['question'],'status':status,'intent':'information',
                'dependency':dependency,'departments':[destination]}  # Owner 2026-11-23: RouterDecision.
        def candidates(payload, *, timeout):
            calls.append(('candidates',payload))
            return {'question':payload['question'],'outcome':{'status':status,
                    'relevant_excerpt_ids':[row['excerpt_id'] for row in payload['candidates']]}
                    if status=='resolved' else {'status':status}}
        return SimpleNamespace(resolve_routing=route,resolve_candidates=candidates),calls

    def test_candidate_adjudication_cannot_use_unknown_ids_or_security_uncertain_text(self):
        from typesafe_sdk import ChoiceAnswer
        for verdict in ('clean','uncertain'):
            with self.subTest(verdict=verdict):
                self.setUp()
                self.service.semantic_confidence_trigger=.7
                self.models.verdict=verdict
                original=self.models.relevance
                def relevance(question,candidates,*,timeout):
                    if candidates[0].source_kind=='knowledge':
                        return {row.excerpt_id:ChoiceAnswer(type='choice',choice='relevant',confidence=.4,
                            probabilities={'relevant':.6,'irrelevant':.39,'uncertain':.01}) for row in candidates}
                    return original(question,candidates,timeout=timeout)
                self.models.relevance=relevance
                self.service.answer_models,calls=self.semantic_adapter()
                result=self.prepare()
                self.assertEqual([stage for stage,_ in calls],['candidates'] if verdict=='clean' else [])
                self.assertEqual([row.source_record_id for row in result.cleared],['SU-SD-001'] if verdict=='clean' else [])
        self.setUp()
        self.service.semantic_confidence_trigger=.7
        original=self.models.relevance
        def relevance(question,candidates,*,timeout):
            if candidates[0].source_kind=='knowledge':
                return relevance_answers(candidates,selected=(),uncertain=tuple(row.excerpt_id for row in candidates))
            return original(question,candidates,timeout=timeout)
        self.models.relevance=relevance
        self.service.answer_models,calls=self.semantic_adapter()
        self.service.answer_models.resolve_candidates=lambda payload,**kwargs:{
            'question':payload['question'],'outcome':{'status':'resolved','relevant_excerpt_ids':['foreign-id']}}
        result=self.prepare()
        self.assertEqual(result.kind,'technical_failure')
        self.assertEqual(result.cleared,())

    def test_resolution_candidates_obey_source_access_and_high_confidence_negative_controls(self):
        from typesafe_sdk import ChoiceAnswer
        for scope,label,probabilities,confidence in (
                ('public_general','irrelevant',{'relevant':.02,'irrelevant':.96,'uncertain':.02},.94),
                ('customer_personal_handoff','irrelevant',{'relevant':.40,'irrelevant':.59,'uncertain':.01},.385),
                ('home_department_internal','irrelevant',{'relevant':.40,'irrelevant':.59,'uncertain':.01},.385)):
            with self.subTest(scope=scope):
                self.setUp()
                self.service.semantic_confidence_trigger=.7
                if scope!='public_general':
                    record=self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row(
                        classification='non_general',access_scope=scope,
                        eligible_audience=['employee'] if scope=='home_department_internal' else ['customer'])))
                    document,labels=self.k.knowledge_payload(record)  # Pass 4: Chroma itself returns the restricted row.
                    self.storage.result={'ids':[[record.source_id]],'documents':[[document]],'metadatas':[[labels]]}
                def relevance(question,candidates,*,timeout):
                    return {row.excerpt_id:ChoiceAnswer(type='choice',choice=label,confidence=confidence,
                                                       probabilities=probabilities) for row in candidates}
                self.models.relevance=relevance
                result=self.prepare()
                self.assertEqual(result.cleared,())
                self.assertEqual(result.resolution_candidates,())
                # Pass 2: the department is searched; restricted rows and confident irrelevance still admit nothing.
                self.assertTrue(any(isinstance(call,tuple) and call[0]=='knowledge' for call in self.storage.calls))

    def test_uncertain_restricted_topic_remains_unresolved_without_facts_or_technical_failure(self):
        from shopunow.incoming import validate_judgments
        from tests.test_incoming import judgment_answers
        self.service.semantic_confidence_trigger=.7
        answers=judgment_answers()
        answers['topic_hr']['noul']=.53
        result=self.service.prepare(IncomingDecision('continue',intent='information',
            proposed_destinations=('hr',),judgments=validate_judgments(answers)))
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(result.cleared,())
        self.assertEqual(result.resolution_candidates,())
        self.assertEqual(self.storage.calls,['pending_write','pending_read'])
        self.assertIn('incoming.topic_hr',result.unresolved_judgments)

    def test_unsettled_owned_history_is_not_exposed_before_historical_access_is_established(self):
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        from tests.test_incoming import judgment_answers
        from typesafe_sdk import ChoiceAnswer
        for subject,verdict in (('hr','clean'),('shipping_delivery','clean'),('shipping_delivery','malicious')):
            with self.subTest(subject=subject,verdict=verdict):
                self.setUp()
                self.service.semantic_confidence_trigger=.7
                self.service.topic_trigger=.5
                self.models.verdict=verdict
                pending,terminal=transcript_pair()
                self.storage.history_rows=terminal
                self.storage.header=lambda *args,**kwargs:header_result()
                self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
                def relevance(question,candidates,*,timeout):
                    answers=relevance_answers(candidates,selected=())
                    if candidates[0].source_kind=='owned_history':
                        answers[candidates[0].excerpt_id]=ChoiceAnswer(type='choice',choice='relevant',confidence=.325,
                            probabilities={'relevant':.55,'irrelevant':.44,'uncertain':.01})
                    return answers
                def history_route(*args,**kwargs):
                    answers=judgment_answers()
                    answers['topic_'+subject]['noul']=.95
                    return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
                self.models.relevance=relevance
                self.models.history_route=history_route
                result=self.recall()  # Owner 2026-11-23: the owner's history request, not a routing stage.
                self.assertNotIn('owned_history',[row.source_kind for row in result.cleared])
                self.assertEqual([row.source_kind for row in result.resolution_candidates],
                                 ['owned_history'] if subject=='shipping_delivery' and verdict=='clean' else [])
                self.assertEqual(result.refused_parts,('hr',) if subject=='hr' else ())

    def test_reviewed_malicious_history_does_not_suppress_independent_current_help(self):
        for verdict,dependency,current,expected in (
                ('malicious','independent',True,'ready'),
                ('malicious','dependent',True,'insufficient_evidence'),
                ('malicious','independent',False,'insufficient_evidence'),
                ('uncertain','independent',True,'security_fallback'),
                ('invalid','independent',True,'security_fallback'),
                ('capped','independent',True,'insufficient_evidence')):
            with self.subTest(verdict=verdict,dependency=dependency,current=current):
                self.setUp()
                self.service.semantic_confidence_trigger=.7
                self.service.topic_trigger=.5
                pending,terminal=transcript_pair()
                self.storage.history_rows=terminal
                self.storage.header=lambda *args,**kwargs:header_result()
                self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
                if verdict=='capped':
                    pairs=[transcript_pair(n,2*n-1) for n in range(1,6)]
                    self.storage.history_rows={key:[value for pair in pairs for value in pair[1][key]]
                        for key in ('ids','documents','metadatas')}
                    self.storage.events=lambda *args,**kwargs:event_result(*(row for pair in pairs for row in pair))
                def review(candidates,*,timeout):
                    if verdict=='invalid':
                        return []
                    return [{'excerpt_id':r.excerpt_id,'verdict':('malicious' if verdict=='capped' else verdict)
                        if r.source_kind=='owned_history' else 'clean'} for r in candidates]
                def relevance(question,candidates,*,timeout):
                    return relevance_answers(candidates,selected=() if (
                        candidates[0].source_kind!='owned_history' and not current) else None)
                self.models.review=review
                self.models.relevance=relevance
                # Owner 2026-11-23: Knowledge is searched first; history only on the owner's request.
                result=self.recall(intent='information',dependency=dependency)
                # Required safety: reviewed malicious/uncertain/unreviewed history is never usable context.
                self.assertFalse(any(r.source_kind=='owned_history' for r in result.cleared+result.resolution_candidates))
                self.assertEqual(self.storage.calls.count('pending_write'),1)
                if verdict in ('uncertain','invalid'):
                    self.assertEqual(result.kind,'security_fallback')

    def test_weak_information_intent_prepares_only_general_resolution_candidates(self):
        from shopunow.incoming import IncomingContext
        from shopunow.workflow import build_incoming_graph
        from tests.test_incoming import judgment_answers
        answers=judgment_answers()
        answers['topic_shipping_delivery']['noul']=.95
        answers['intent'].update(confidence=.4,probabilities={
            'information':.55,'individual_case_or_action':.15,'mixed':.15,'unclear_or_other':.15})
        graph=build_incoming_graph(judge=lambda context:answers,reviewer=None,authorize=lambda:True,
            security_trigger=.7,topic_trigger=.5,semantic_confidence_trigger=.7)
        decision=graph.invoke({'context':IncomingContext('Recap the old discussion; separately explain tracking.',
            'customer',None,(),False)})['decision']
        self.service.semantic_confidence_trigger=.7
        self.service.topic_trigger=.5
        result=self.service.prepare(decision)
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(result.cleared,())
        self.assertEqual(result.destinations,())
        self.assertEqual([r.source_record_id for r in result.resolution_candidates],['SU-SD-001'])
        self.assertIn('incoming.intent',result.unresolved_judgments)
        self.assertEqual(self.storage.calls.count('pending_write'),1)

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.evidence'),
                             'The integrated cleared-evidence capability is missing')
        self.e = importlib.import_module('shopunow.evidence')
        self.k = importlib.import_module('shopunow.knowledge')
        self.row = self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row()))
        self.storage = EvidenceStorage(self.row, self.k)
        self.models = EvidenceModels()
        self.clock = [100.0]
        quota = MemoryStorage()
        self.addCleanup(quota.reset)
        self.tracking = OwnedRecordTracking()
        self.attempt = QuestionAttempt(100, clock=lambda:self.clock[0])
        self.attempt._hold_tracking = self.tracking
        self.attempt._hold_id = CONVERSATION_ID
        self.attempt.try_admit(RollingAllowances(quota, Lock()))
        self.request = QuestionRequest(user_id=USER_ID, conversation_id=CONVERSATION_ID,
                                       question='What is tracking?')
        self.profile = verify_profile(profile_result(), user_id=USER_ID).record
        self.service = self.e.EvidenceService(storage=self.storage,
            models=self.models, attempt=self.attempt, tracking=self.tracking,
            request=self.request, profile=self.profile, transcript=CompleteTranscript((),(),0,True),
            request_id=REQUEST_ID, created_at='2026-11-12T10:00:00Z',
            budget=self.e.ModelBudget(), uuid_factory=lambda:UUID(int=123),topic_trigger=.5)

    def identity_fixture(self, *, confidence=.4, label='insufficient',conversation=CONVERSATION_ID):
        # Two exchanges in one conversation expose selection, rather than first-hit acceptance.
        from typesafe_sdk import ChoiceAnswer
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        first,second=transcript_pair(1,1),transcript_pair(2,3)
        for pair in (first,second):
            for row in pair:
                row['metadatas'][0]['conversation_id']=conversation
        events=event_result(*first,*second)
        self.storage.history_rows={key:first[1][key]+second[1][key] for key in ('ids','documents','metadatas')}
        header=header_result()
        header['ids']=['conversation_header:'+UUID(conversation).hex]
        header['metadatas'][0]['conversation_id']=conversation
        document=__import__('json').loads(header['documents'][0])
        document['conversation_id']=conversation
        header['documents']=[__import__('json').dumps(document)]
        self.storage.header=lambda *args,**kwargs:header
        self.storage.events=lambda *args,**kwargs:events
        self.service.semantic_confidence_trigger=.7
        self.service.topic_trigger=.5
        def relevance(question,candidates,*,timeout):
            answers=relevance_answers(candidates,selected=())
            if candidates[0].source_kind=='owned_history':
                answers['case_resolution']=ChoiceAnswer(type='choice',choice=label,confidence=confidence,
                    probabilities={name:(.6 if name==label else .2) for name in ('clear','ambiguous','insufficient')})
            return answers
        def route(*args,**kwargs):
            answers=judgment_answers()
            answers['topic_shipping_delivery']['noul']=1.0
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.models.relevance=relevance
        self.models.history_route=route
        return second[1]['ids'][0]

    def test_identity_resolver_selects_contributing_exchange_before_security_and_late_access(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        for subject,verdict,expected in (('shipping_delivery','clean','ready'),
                ('hr','clean','refusal'),('shipping_delivery','malicious','insufficient_evidence'),
                ('shipping_delivery','uncertain','security_fallback')):
            with self.subTest(subject=subject,verdict=verdict):
                self.setUp()
                wanted=self.identity_fixture()
                observed=[]
                def resolve(question,candidates,*,timeout):
                    observed.append((question,candidates,timeout))
                    return {'status':'clear','selected_excerpt_ids':[candidates[1].excerpt_id]}
                def route(*args,**kwargs):
                    answers=judgment_answers()
                    answers['topic_'+subject]['noul']=1.0
                    return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
                self.service.history_identity_resolver=resolve
                self.models.history_route=route
                self.models.verdict=verdict
                result=self.recall(intent='information')
                self.assertEqual(len(observed),1)
                self.assertEqual(observed[0][0],self.request.question)
                self.assertEqual(len(observed[0][1]),2)
                self.assertLessEqual(observed[0][2],20)
                self.assertEqual(result.kind,expected)
                self.assertEqual([r.source_record_id for r in result.cleared],[wanted] if expected=='ready' else [])
                self.assertIn('history_1.identity_resolution',result.judgments)
                self.assertIn('history_1.case_resolution',result.judgments)
                self.assertNotIn('history_1.case_resolution',result.unresolved_judgments)
                self.assertEqual(self.storage.calls.count('pending_write'),1)

    def test_identity_resolution_is_optional_and_called_once_only_for_weak_global_identity(self):
        for enabled,confidence,label,want_calls in ((False,.4,'insufficient',0),
                (True,1.,'clear',0),(True,1.,'ambiguous',0),(True,1.,'insufficient',0),
                (True,.4,'insufficient',1)):
            with self.subTest(enabled=enabled,confidence=confidence,label=label):
                self.setUp()
                self.identity_fixture(confidence=confidence,label=label)
                calls=[]
                if enabled:
                    self.service.history_identity_resolver=lambda *args,**kwargs: calls.append(args) or {
                        'status':'insufficient','selected_excerpt_ids':[]}
                result=self.recall(intent='information')
                self.service.supplementary(target='A different missing historical relationship')
                self.assertEqual(len(calls),want_calls)
                self.assertEqual(result.cleared,())

    def test_identity_resolution_rejects_invalid_or_failed_results_and_preserves_ambiguity(self):
        for outcome,expected in (({'status':'clear','selected_excerpt_ids':[]},'technical_failure'),
                ({'status':'clear','selected_excerpt_ids':['unknown']},'technical_failure'),
                ({'status':'clear','selected_excerpt_ids':['e1','e1']},'technical_failure'),
                ({'status':'ambiguous','selected_excerpt_ids':['e1']},'technical_failure'),
                ({'status':'ambiguous','selected_excerpt_ids':[]},'clarification_needed'),
                ({'status':'insufficient','selected_excerpt_ids':[]},'insufficient_evidence'),
                ('failure','technical_failure'),('late','technical_failure')):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.identity_fixture()
                def resolve(*args,**kwargs):
                    if outcome=='failure':
                        raise TimeoutError('offline boundary')
                    if outcome=='late':
                        self.clock[0]=191.
                        return {'status':'clear','selected_excerpt_ids':['e1']}
                    return outcome
                self.service.history_identity_resolver=resolve
                result=self.recall(intent='information')
                self.assertEqual(result.kind,expected)
                self.assertEqual(result.cleared+result.resolution_candidates,())
                self.assertFalse(any(isinstance(c,tuple) and c[0]=='review' for c in self.models.calls))

    def test_identity_resolver_uses_supplementary_target_not_original_question(self):
        self.identity_fixture()
        # Initial current work performs no history search or identity call.
        self.prepare()
        seen=[]
        def resolve(question,candidates,*,timeout):
            seen.append((question,len(candidates)))
            return {'status':'ambiguous','selected_excerpt_ids':[]}
        self.service.history_identity_resolver=resolve
        result=self.service.supplementary(target='Which later exchange corrected the earlier account?')
        self.assertEqual(seen,[('Which later exchange corrected the earlier account?',2)])
        self.assertEqual(result.kind,'clarification_needed')

    def test_identity_selected_history_requires_late_access_on_ordinary_and_supplementary_paths(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        for supplementary in (False,True):
            for subject in ('hr','shipping_delivery'):
                with self.subTest(supplementary=supplementary,subject=subject):
                    self.setUp()
                    wanted=self.identity_fixture()
                    routed=[]
                    def route(question,candidates,**kwargs):
                        routed.extend(row.source_record_id for row in candidates)
                        answers=judgment_answers()
                        answers['topic_'+subject]['noul']=1.0
                        return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
                    self.models.history_route=route
                    self.service.history_identity_resolver=lambda question,candidates,**kwargs: {
                        'status':'clear','selected_excerpt_ids':[candidates[1].excerpt_id]}
                    if supplementary:
                        self.prepare()
                        result=self.service.supplementary(target='Identify the missing earlier exchange.')
                    else:
                        result=self.recall()
                    self.assertEqual(routed,[wanted])
                    self.assertEqual([r.source_record_id for r in result.cleared],
                                     [wanted] if subject=='shipping_delivery' else [])
                    self.assertFalse(any(r.source_kind=='owned_history' for r in result.resolution_candidates))

    def test_identity_resolution_cannot_settle_weak_intent_for_history_or_current_facts(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import IncomingContext
        from shopunow.workflow import build_incoming_graph
        self.identity_fixture()
        native_relevance=self.models.relevance
        self.models.relevance=lambda question,candidates,**kwargs: native_relevance(question,candidates,**kwargs) if (
            candidates[0].source_kind=='owned_history') else relevance_answers(candidates)
        answers=judgment_answers()
        answers['intent'].update(confidence=.4,probabilities={
            'information':.55,'individual_case_or_action':.15,'mixed':.15,'unclear_or_other':.15})
        self.service.history_identity_resolver=lambda question,candidates,**kwargs: {
            'status':'clear','selected_excerpt_ids':[candidates[1].excerpt_id]}
        graph=build_incoming_graph(judge=lambda context:answers,reviewer=None,authorize=lambda:True,
            security_trigger=.7,topic_trigger=.5,semantic_confidence_trigger=.7)
        decision=graph.invoke({'context':IncomingContext(self.request.question,'customer',None,(),False)})['decision']
        self.service.prepare(decision)
        result=self.service.supplementary(target=self.request.question)  # The owner's history request.
        self.assertEqual(result.cleared,())  # Neither history nor Knowledge becomes a fact under a weak intent.
        self.assertIn('owned_history',[r.source_kind for r in result.resolution_candidates])
        self.assertIn('incoming.intent',result.unresolved_judgments)

    def test_identity_late_access_cannot_release_a_conversation_excluded_during_routing(self):
        for supplementary in (False,True):
            with self.subTest(supplementary=supplementary):
                self.setUp()
                self.identity_fixture(conversation=str(UUID(int=900)))
                allowed_route=self.models.history_route
                def route(question,candidates,**kwargs):
                    self.tracking.exclude_conversation(candidates[0].conversation_id)
                    return allowed_route(question,candidates,**kwargs)
                self.models.history_route=route
                self.service.history_identity_resolver=lambda question,candidates,**kwargs: {
                    'status':'clear','selected_excerpt_ids':[candidates[1].excerpt_id]}
                if supplementary:
                    self.prepare()
                    result=self.service.supplementary(target='Recover the missing historical context.')
                else:
                    result=self.recall()
                self.assertEqual(result.kind,'technical_failure')
                self.assertEqual(result.cleared+result.resolution_candidates,())
                self.assertIn(('history','technical_failure'),[(row.part,row.kind) for row in result.dispositions])

    def test_identity_adjudication_cannot_reset_exhausted_shared_model_budget(self):
        self.identity_fixture()
        called=[]
        self.service.history_identity_resolver=lambda *args,**kwargs:called.append(args)
        self.service.budget=self.e.ModelBudget(attempts=39)
        result=self.recall(intent='information')
        self.assertEqual(result.kind,'technical_failure')
        self.assertEqual(result.cleared+result.resolution_candidates,())
        self.assertEqual(called,[])
        self.assertEqual(self.service.budget.attempts,40)

    def test_denied_identity_history_preserves_a_genuinely_uncovered_current_part(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        self.identity_fixture()
        previous_relevance=self.models.relevance
        self.models.relevance=lambda question,candidates,**kwargs: previous_relevance(question,candidates,**kwargs) if (
            candidates[0].source_kind=='owned_history') else relevance_answers(candidates)
        def route(*args,**kwargs):
            answers=judgment_answers()
            answers['topic_hr']['noul']=1.0
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.models.history_route=route
        self.service.history_identity_resolver=lambda question,candidates,**kwargs: {
            'status':'clear','selected_excerpt_ids':[candidates[1].excerpt_id]}
        first=self.prepare(intent='information',destinations=('shipping_delivery','billing_payments'))
        self.assertIn('billing_payments',first.unresolved_parts)  # The uncovered current part stays visible.
        result=self.service.supplementary(target=self.request.question)  # Owner's request; hr history is denied.
        self.assertEqual(result.kind,'insufficient_evidence')  # The denied history supplied nothing.
        self.assertEqual([r.source_record_id for r in result.cleared],['SU-SD-001'])
        self.assertIn('hr',result.refused_parts)

    def prepare(self, **changes):
        decision = IncomingDecision(**{'kind':'continue','destinations':('shipping_delivery',),**changes})
        return self.service.prepare(decision)

    def recall(self, **changes):
        # Owner 2026-11-23: history enters only when the owner asks (context_needed) after preparation.
        self.prepare(**changes)
        return self.service.supplementary(target=self.service.request.question)

    def test_pending_is_verified_before_retrieval_and_only_clean_ids_reach_receiver(self):
        result = self.prepare()
        self.assertEqual(result.kind, 'ready')
        self.assertEqual(self.storage.calls[:2], ['pending_write','pending_read'])
        self.assertEqual(result.cleared[0].source_record_id, 'SU-SD-001')
        self.assertEqual(result.cleared[0].clearance, 'clean')
        self.assertEqual(self.storage.pending.sequence, 1)
        self.assertFalse(any(isinstance(c,tuple) and c[0]=='history' for c in self.storage.calls))

    def test_unverified_pending_stops_all_retrieval_and_preserves_hold(self):
        self.storage.fail_pending = True
        self.assertEqual(self.prepare().kind, 'technical_failure')
        self.assertEqual(self.storage.calls, ['pending_write','pending_read'])
        self.assertTrue(self.tracking.excludes((CONVERSATION_ID,)))
        self.assertEqual(self.models.calls, [])

    def test_no_evidence_work_before_clearance_or_after_withdrawal(self):
        for kind in ('refusal','confirmed_attack','security_uncertain','disabled'):
            self.assertEqual(self.service.prepare(IncomingDecision(kind)).kind, kind)
        self.assertEqual(self.storage.calls, [])

        self.attempt.withdraw()
        self.assertEqual(self.prepare().kind, 'stopped')
        self.assertEqual(self.storage.calls, [])

    def test_internal_entry_of_another_department_is_never_shown_and_ends_in_a_support_offer(self):
        # Owner D6 (2026-11-23, RULE-007 known difference): no refusal; the internal entry is never retrieved.
        from shopunow.records import ProfileRecord
        internal=self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row(
            source_id='SU-HR-010',subject='hr',source_question='What is the internal HR budget?',
            eligible_audience=['employee'],classification='non_general',access_scope='home_department_internal',
            answer_facts=['Never send this internal allocation to a model for classification.'])))
        for home in ('facilities_admin','hr'):  # Another department, and the entry's own department.
            with self.subTest(home=home):
                self.setUp()
                profile=dict(record_type='profile',created_at='2026-11-02T10:00:00Z',user_id=USER_ID,
                             role='employee',home_department=home,complete=True,version=1)
                self.service.profile=ProfileRecord.model_validate_json(__import__('json').dumps(profile))
                self.service.request.question='What is the internal HR budget?'
                document,labels=self.k.knowledge_payload(internal)
                # Even if the store returned it despite the general-only filter, code drops it.
                self.storage.result={'ids':[['SU-HR-010']],'documents':[[document]],'metadatas':[[labels]]}
                result=self.service.prepare(IncomingDecision('continue',destinations=('hr',)))
                self.assertEqual(result.kind,'insufficient_evidence')
                self.assertEqual((result.refused_parts,result.handoff_parts),((),()))
                self.assertEqual(result.cleared+result.resolution_candidates,())
                where=next(call[1] for call in self.storage.calls if isinstance(call,tuple) and call[0]=='knowledge')
                self.assertIn({'classification':'general'},where['$and'])

    def test_established_dispositions_survive_rejected_general_context_and_early_exits(self):
        # Pass 2 (owner 2026-11-23): refusals come only from routing; an empty or failed search keeps them.
        for relevance_kind,kind in (('insufficient','insufficient_evidence'),('raise','technical_failure')):
            with self.subTest(relevance_kind=relevance_kind):
                self.setUp()
                def relevance(question,candidates,*,timeout):
                    if relevance_kind=='raise':
                        raise TimeoutError('Controlled relevance provider failure')
                    return relevance_answers(candidates,*('insufficient',(),()))
                self.models.relevance=relevance
                result=self.service.prepare(IncomingDecision('continue',destinations=('shipping_delivery',),
                    refused_departments=('hr',)))
                self.assertEqual(result.kind,kind)
                self.assertEqual(result.refused_parts,('hr',))
                self.assertEqual(result.handoff_parts,())
                self.assertEqual(result.pending_message_id,self.storage.pending.message_id)
                self.assertEqual(result.cleared,())

    def test_empty_supplementary_history_preserves_prior_refused_part_and_pending_link(self):
        initial = self.prepare(refused_departments=('hr',))
        self.assertEqual(initial.kind,'ready')
        result = self.service.supplementary(target='The missing delivery case')
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(result.refused_parts,('hr',))
        self.assertEqual(result.pending_message_id,self.storage.pending.message_id)

    def test_history_dispositions_retain_actual_ambiguity_absence_security_and_verification_failure(self):
        # Source-owned outcomes must remain distinguishable when independent current facts survive.
        for variant,wanted in (('empty','unavailable'),('ambiguous','clarification_needed'),
                ('malicious','unavailable'),('uncertain','security_fallback'),('foreign','technical_failure')):
            with self.subTest(variant=variant):
                self.setUp()
                if variant!='empty':
                    pending,terminal=transcript_pair()
                    if variant=='foreign':
                        terminal['metadatas'][0]['owner_user_id']='syn-cust-02'
                    self.storage.history_rows=terminal
                    self.storage.header=lambda *args,**kwargs:header_result()
                    self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
                    def relevance(question,candidates,*,timeout):
                        return relevance_answers(candidates,*(('ambiguous',(),()) if
                            variant=='ambiguous' and candidates[0].source_kind=='owned_history' else
                            ('clear',tuple(row.excerpt_id for row in candidates),())))
                    self.models.relevance=relevance
                    def review(candidates,*,timeout):
                        return [{'excerpt_id':row.excerpt_id,'verdict':variant if variant in (
                            'malicious','uncertain') and row.source_kind=='owned_history' else 'clean'}
                            for row in candidates]
                    self.models.review=review
                result=self.recall(dependency='independent')
                self.assertIn(('history',wanted),[(row.part,row.kind) for row in
                    getattr(result,'dispositions',())],'The evidence boundary lost the actual historical outcome')
                self.assertFalse(any(row.source_kind=='owned_history' for row in result.cleared))

    def test_historical_access_failure_is_typed_on_ordinary_predispatch_and_supplementary_paths(self):
        for entry in ('ordinary','predispatch','supplementary'):
            with self.subTest(entry=entry):
                self.setUp()
                if entry=='supplementary':
                    self.assertEqual(self.prepare().kind,'ready')
                self.identity_fixture(confidence=1.,label='clear')
                self.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
                def failed_access(*args,**kwargs):
                    raise RuntimeError('Controlled historical access service failure')
                self.models.history_route=failed_access
                if entry=='supplementary':
                    result=self.service.supplementary(target='Earlier parcel correction')
                elif entry=='predispatch':
                    result=self.recall(intent='information')
                else:
                    result=self.recall()
                self.assertEqual(result.kind,'technical_failure')
                self.assertFalse(any(row.source_kind=='owned_history' for row in result.cleared))
                self.assertIn(('history','technical_failure'),[(row.part,row.kind) for row in result.dispositions])

    def test_exhausted_history_review_budget_leaves_new_history_unused_and_unresolved(self):
        # Owner decision 2026-11-18: reaching the limit is not a security finding, and the limit still holds.
        self.assertEqual(self.prepare().kind,'ready')
        self.identity_fixture(confidence=1.,label='clear')
        self.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)
        self.service._reviewed=2  # Prior initial/review work already consumed both allowed batches.
        result=self.service.supplementary(target='Earlier parcel correction')
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(self.service._reviewed,2)
        self.assertFalse(any(row.source_kind=='owned_history' for row in result.cleared))
        self.assertIn('history',result.unresolved_parts)
        outcomes=[(row.part,row.kind) for row in result.dispositions]
        self.assertIn(('history','insufficient_evidence'),outcomes)
        self.assertNotIn(('history','security_fallback'),outcomes)

    def test_uncertain_historical_candidate_is_not_asserted_unique_or_user_ambiguity(self):
        pending,terminal=transcript_pair()
        self.storage.history_rows=terminal
        self.storage.header=lambda *args,**kwargs:header_result()
        self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
        self.storage.result={'ids':[[]],'documents':[[]],'metadatas':[[]]}
        def relevance(question,candidates,*,timeout):
            if candidates[0].source_kind=='owned_history':
                return relevance_answers(candidates,*('clear',(),(candidates[0].excerpt_id,)))
            return relevance_answers(candidates,*('clear',tuple(row.excerpt_id for row in candidates),()))
        self.models.relevance=relevance
        self.assertIn('shipping_delivery',self.prepare().unresolved_parts)
        result=self.service.supplementary(target=self.request.question)
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(result.cleared,())

    def test_clear_authorized_knowledge_context_remains_available_with_uncertain_part_explicit(self):
        # Access-checked facts can remain context without pretending an uncertain match is resolved.
        other=self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row(source_id='SU-SD-002')))
        text,metadata=self.k.knowledge_payload(other)
        self.storage.result['ids'][0].append(other.source_id)
        self.storage.result['documents'][0].append(text)
        self.storage.result['metadatas'][0].append(metadata)
        def relevance(question,candidates,*,timeout):
            if candidates[0].source_kind=='knowledge':
                return relevance_answers(candidates,*('clear',(candidates[0].excerpt_id,),(candidates[1].excerpt_id,)))
            return relevance_answers(candidates,*('clear',tuple(row.excerpt_id for row in candidates),()))
        self.models.relevance=relevance
        result=self.prepare()
        self.assertEqual(result.kind,'ready')
        self.assertEqual([row.source_record_id for row in result.cleared],['SU-SD-001'])
        self.assertEqual(result.unresolved_parts,('shipping_delivery',))
        self.assertEqual(self.models.calls[-1],('review',1))

    def test_malformed_returned_hit_never_reaches_review(self):
        # Pass 4: an edited fact is accepted as stored (owner 2026-11-24); labels naming another entry are not.
        self.storage.result['metadatas'][0][0] = {**self.storage.result['metadatas'][0][0], 'source_id': 'SU-SD-002'}
        result = self.prepare()
        self.assertEqual(result.kind, 'insufficient_evidence')
        self.assertEqual(result.cleared, ())
        self.assertFalse(any(isinstance(c,tuple) and c[0]=='review' for c in self.models.calls))

    def test_style_alone_cannot_turn_missing_facts_into_ready_evidence(self):
        self.storage.result={'ids':[[]],'documents':[[]],'metadatas':[[]]}
        self.request.response_style='brief'
        self.assertEqual(self.prepare().kind,'insufficient_evidence')

    def test_malicious_and_uncertain_evidence_are_withheld_with_distinct_outcomes(self):
        self.models.verdict='malicious'
        self.assertEqual(self.prepare().kind, 'insufficient_evidence')
        self.setUp()
        self.models.verdict='uncertain'
        self.assertEqual(self.prepare().kind, 'security_fallback')

    def test_missing_duplicate_extra_review_ids_fail_closed(self):
        for mapping in ([], [{'excerpt_id':'extra','verdict':'clean'}],
                        [{'excerpt_id':'e1','verdict':'clean'}]*2):
            self.setUp()
            self.models.mapping=mapping
            self.assertEqual(self.prepare().kind,'security_fallback')

    def test_irrelevance_and_ambiguous_history_are_not_answer_support(self):
        self.models.relevance_kind='insufficient'
        result=self.prepare()
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertEqual(result.cleared,())
        self.setUp()
        pending,terminal=transcript_pair()
        self.storage.history_rows=terminal
        self.storage.header=lambda *args,**kwargs:header_result()
        self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
        self.storage.result={'ids':[[]],'documents':[[]],'metadatas':[[]]}
        def relevance(question,candidates,*,timeout):
            return relevance_answers(candidates,*(('ambiguous',(),()) if candidates[0].source_kind=='owned_history' else (
                'clear',tuple(row.excerpt_id for row in candidates),())))
        self.models.relevance=relevance
        self.assertEqual(self.recall().kind,'clarification_needed')

    def test_expired_request_and_call_budget_cannot_start_more_models(self):
        self.clock[0]=191
        self.assertEqual(self.prepare().kind,'stopped')
        self.assertEqual(self.models.calls,[])
        self.setUp()
        self.service.budget=self.e.ModelBudget(attempts=40)
        self.assertEqual(self.prepare().kind,'technical_failure')
        self.assertEqual(self.models.calls,[])

    def test_owned_complete_history_is_reconstructed_and_reviewed_after_pagination(self):
        pending,terminal=transcript_pair()
        self.storage.history_rows=terminal
        self.storage.header=lambda conversation_id,**_:header_result()
        self.storage.events=lambda conversation_id,**_:event_result(pending,terminal)
        result=self.recall()
        self.assertEqual(result.kind,'ready')
        history=[c for c in result.cleared if c.source_kind=='owned_history']
        self.assertEqual(len(history),1)
        self.assertIn(pending['documents'][0],history[0].text)
        self.assertIn(terminal['documents'][0],history[0].text)
        self.assertIn(('history',USER_ID,1),self.storage.calls)

    def test_completed_clarification_and_refusal_preserve_owned_context_and_security_review(self):
        for kind,verdict,expected in [('completed_clarification','clean','ready'),
                                       ('completed_refusal','malicious','insufficient_evidence')]:
            with self.subTest(kind=kind):
                self.setUp()
                pending,terminal=transcript_pair()
                terminal['metadatas'][0]['kind']=kind
                pending['documents'][0]='My office access badge is damaged. I may need a replacement.'
                terminal['documents'][0]='Is the badge lost or damaged? The demo has no live replacement record.'
                self.storage.history_rows=terminal
                self.storage.header=lambda *args,**kwargs:header_result()
                self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
                self.storage.result={'ids':[[]],'documents':[[]],'metadatas':[[]]}
                self.models.verdict=verdict
                result=self.recall()
                self.assertEqual(result.kind,expected)
                self.assertIn(('review',1),self.models.calls)
                if expected=='ready':
                    self.assertIn('My office access badge is damaged.',result.cleared[0].text)

    def test_recap_can_use_history_when_returned_company_faq_is_irrelevant(self):
        pending,terminal=transcript_pair()
        self.storage.history_rows=terminal
        self.storage.header=lambda conversation_id,**_:header_result()
        self.storage.events=lambda conversation_id,**_:event_result(pending,terminal)
        def relevance(question,candidates,*,timeout):
            return relevance_answers(candidates,*(('insufficient',(),()) if candidates[0].source_kind=='knowledge' else (
                'clear',tuple(row.excerpt_id for row in candidates),())))
        self.models.relevance=relevance
        result=self.recall()
        self.assertEqual(result.kind,'ready')
        self.assertEqual([row.source_kind for row in result.cleared],['owned_history'])

    def test_ambiguity_is_checked_across_all_history_pages(self):
        first=transcript_pair(1,1)
        second=transcript_pair(2,3)
        rows=event_result(*first,*second)
        def page(user_id,*,offset,timeout):
            return first[1] if offset==0 else second[1] if offset==1 else {
                'ids':[],'documents':[],'metadatas':[]}
        self.storage.history_page=page
        self.storage.header=lambda conversation_id,**_:header_result()
        self.storage.events=lambda conversation_id,**_:rows
        observed=[]
        def relevance(question,candidates,*,timeout):
            if candidates[0].source_kind=='owned_history':
                observed.append(len(candidates))
                return relevance_answers(candidates,*('ambiguous',tuple(row.excerpt_id for row in candidates),()))
            return relevance_answers(candidates,*('clear',tuple(row.excerpt_id for row in candidates),()))
        self.models.relevance=relevance
        self.assertEqual(self.recall().kind,'clarification_needed')
        self.assertEqual(observed,[2])

    def test_current_pending_cannot_hide_cap_plus_one_history_overflow(self):
        pairs=[row for n in range(1,26) for row in transcript_pair(n,2*n-1)]
        self.storage.history_rows=pairs[1]
        self.storage.header=lambda conversation_id,**_:header_result()
        def events(*args,**kwargs):
            pending=self.storage.pending
            return event_result(*pairs,{'ids':[pending.message_id],'documents':[pending.text],
                'metadatas':[pending.model_dump(mode='json',exclude={'text'})]})
        self.storage.events=events
        self.assertEqual(self.recall().kind,'technical_failure')

    def test_five_process_model_slots_remain_held_until_underlying_calls_complete(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        entered=[Event() for _ in range(5)]
        release=Event()
        def hold(index):
            entered[index].set()
            release.wait(5)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures=[pool.submit(self.e.ModelBudget().call,lambda timeout,n=n:hold(n),
                                 timeout=lambda:20) for n in range(5)]
            try:
                self.assertTrue(all(event.wait(3) for event in entered))
                with self.assertRaises(TimeoutError):
                    self.e.ModelBudget().call(lambda timeout:None,timeout=lambda:20)
            finally:
                release.set()
            for future in futures:
                future.result(3)
        self.e.ModelBudget().call(lambda timeout:None,timeout=lambda:20)

    def test_review_batches_and_shared_excerpts_respect_limits(self):
        self.service.pending=__import__('types').SimpleNamespace(message_id='test-pending')
        self.service.dependency='independent'
        candidates=[self.service._candidate(kind='knowledge',key=str(n),text='Fact',
            destination='p'+str(n//4)) for n in range(20)]
        result=self.service._clear(candidates,tuple('p'+str(n) for n in range(5)))
        self.assertEqual(result.kind,'ready')
        self.assertEqual(self.models.calls,[('review',10),('review',10)])
        self.assertEqual(len(result.cleared),20)
        self.assertEqual(self.service._clear(candidates[:1],('p0',)).kind,'insufficient_evidence')

    def test_failed_technical_pairs_still_consume_normal_message_positions(self):
        from shopunow.records import verify_complete_transcript
        pending,terminal=transcript_pair()
        terminal['metadatas'][0]['kind']='failed_technical'
        terminal['documents'][0]='technical_error'
        transcript=verify_complete_transcript(header_result(),event_result(pending,terminal),
            user_id=USER_ID,conversation_id=CONVERSATION_ID,offer_results={}).record
        self.service.transcript=transcript
        self.prepare()
        self.assertEqual(self.storage.pending.sequence,3)

    def test_foreign_or_incomplete_history_never_reaches_relevance_or_review(self):
        for corrupt in ('owner','pair'):
            self.setUp()
            pending,terminal=transcript_pair()
            if corrupt=='owner':
                terminal['metadatas'][0]['owner_user_id']='syn-cust-02'
            self.storage.history_rows=terminal
            self.storage.header=lambda conversation_id,**_:header_result()
            self.storage.events=lambda conversation_id,**_:event_result(pending)
            self.prepare()
            before=len(self.models.calls)  # The owner's history request starts here.
            result=self.service.supplementary(target=self.request.question)
            self.assertEqual(result.kind,'technical_failure')
            self.assertFalse(any(isinstance(c,tuple) and c[0]=='review' for c in self.models.calls[before:]))

    def test_supplementary_search_has_one_continuation_even_without_initial_history(self):
        self.assertEqual(self.prepare().kind,'ready')
        self.assertEqual(self.service.supplementary(target='The missing earlier delivery case').kind,
                         'insufficient_evidence')
        before=list(self.storage.calls)
        self.assertEqual(self.service.supplementary(target='Search again').kind,'technical_failure')
        self.assertEqual(self.storage.calls,before)

    def test_malicious_supplementary_history_cannot_reuse_previous_ready_facts(self):
        self.assertEqual(self.prepare().kind,'ready')
        pending,terminal=transcript_pair()
        self.storage.history_rows=terminal
        self.storage.header=lambda conversation_id,**_:header_result()
        self.storage.events=lambda conversation_id,**_:event_result(pending,terminal)
        self.models.verdict='malicious'
        result=self.service.supplementary(target='The missing earlier delivery case')
        self.assertEqual(result.kind,'insufficient_evidence')
        self.assertFalse(any(row.source_kind=='owned_history' for row in result.cleared))

    def test_forged_owned_history_row_cannot_read_foreign_conversation_events(self):
        _,terminal=transcript_pair()
        self.storage.history_rows=terminal
        foreign=header_result()
        foreign['metadatas'][0]['owner_user_id']='syn-cust-02'
        self.storage.header=lambda conversation_id,**_:foreign
        events=[]
        self.storage.events=lambda *args,**kwargs:events.append('foreign_read')
        self.assertEqual(self.recall().kind,'technical_failure')
        self.assertEqual(events,[])

    def test_independent_clean_part_survives_another_parts_uncertainty(self):
        other=self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row(
            source_id='SU-BP-001',subject='billing_payments')))
        shipping=self.storage.result
        def retrieve(vector,*,where,limit,timeout):
            if {'subject':'billing_payments'} in where['$and']:
                text,metadata=self.k.knowledge_payload(other)
                return {'ids':[[other.source_id]],'documents':[[text]],'metadatas':[[metadata]]}
            return shipping
        self.storage.knowledge=retrieve
        self.models.mapping=[{'excerpt_id':'e1','verdict':'clean'}, {'excerpt_id':'e2','verdict':'uncertain'}]
        result=self.service.prepare(IncomingDecision('continue',destinations=('shipping_delivery','billing_payments')))
        self.assertEqual(result.kind,'ready')
        self.assertEqual([row.destination for row in result.cleared],['shipping_delivery'])
        self.assertEqual(result.security_fallback_parts,('billing_payments',))

    def test_model_attempt_has_twenty_second_cap_inside_original_budget(self):
        supplied=[]
        self.service.budget.call(lambda timeout:supplied.append(timeout),timeout=lambda:90)
        self.assertEqual(supplied,[20])

    def test_recalled_conversation_hold_is_checked_before_read_and_after_review(self):
        import json
        other=str(UUID(int=999))
        pending,terminal=transcript_pair()
        for row in (pending,terminal):
            row['metadatas'][0]['conversation_id']=other
        header=header_result()
        header['ids']=['conversation_header:'+UUID(other).hex]
        header['metadatas'][0]['conversation_id']=other
        document=json.loads(header['documents'][0])
        document['conversation_id']=other
        header['documents']=[json.dumps(document)]
        self.storage.history_rows=terminal
        reads=[]
        def get_header(*args,**kwargs):
            reads.append('header')
            return header
        self.storage.header=get_header
        self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
        original=self.models.review
        def review(candidates,*,timeout):
            result=original(candidates,timeout=timeout)
            if any(row.source_kind=='owned_history' for row in candidates):
                self.tracking.exclude_conversation(other)  # The hold arrives while history is reviewed.
            return result
        self.models.review=review
        self.assertEqual(self.recall().kind,'technical_failure')
        self.assertEqual(reads,['header'])
        # A fresh request preserves the known exclusion and must not read this header again.
        self.service=self.e.EvidenceService(storage=self.storage,
            models=self.models,attempt=self.attempt,tracking=self.tracking,request=self.request,
            profile=self.profile,transcript=CompleteTranscript((),(),0,True),
            request_id=str(UUID(int=1000)),created_at='2026-11-12T10:00:00Z',budget=self.e.ModelBudget())
        reads.clear()
        self.assertEqual(self.recall().kind,'technical_failure')
        self.assertEqual(reads,[])

    def test_uncertainty_is_not_dismissed_because_another_excerpt_is_clean(self):
        self.storage.result={key:[values[0]*2] for key,values in self.storage.result.items()}
        # Two independently approved entries for the same part, with one uncertain review.
        other=self.k.KnowledgeRecord.model_validate_json(__import__('json').dumps(source_row(source_id='SU-SD-002')))
        self.storage.result['ids'][0][1]='SU-SD-002'
        self.storage.result['metadatas'][0][1]=self.k.knowledge_payload(other)[1]
        self.models.mapping=[{'excerpt_id':'e1','verdict':'clean'}, {'excerpt_id':'e2','verdict':'uncertain'}]
        self.assertEqual(self.prepare().kind,'security_fallback')


class EvidenceWorkflowIntegrationTests(unittest.TestCase):
    setUp = EvidenceTests.setUp
    def test_actual_incoming_graph_drives_clear_evidence_without_served_answer(self):
        from tests.test_incoming import judgment_answers
        from shopunow.workflow import run_incoming
        answers=judgment_answers()
        answers['topic_shipping_delivery']['noul']=.9
        import httpx2
        from typesafe_sdk import TypeSafeClient,RetryPolicy
        observed=[]
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
            transport=httpx2.MockTransport(lambda request:httpx2.Response(200,json={
                'model':'offline-jev','usage':{},'answers':answers}))) as client:
            result=run_incoming(request=self.request,profile=self.profile,transcript=self.service.transcript,
                attempt=self.attempt,tracking=self.tracking,storage=self.storage,request_id=REQUEST_ID,
                jev_client=client,reviewer_client=None,jev_model='offline-jev',security_trigger=.7,
                topic_trigger=.5,model_budget=self.service.budget,
                on_cleared=lambda decision:observed.append(self.service.prepare(decision)))
        self.assertIsNone(result)
        self.assertEqual(observed[0].kind,'ready')
        self.assertEqual(observed[0].cleared[0].source_record_id,'SU-SD-001')
        self.assertEqual(self.service.budget.attempts,4)  # Jev + embedding + relevance + review (no entry match).
