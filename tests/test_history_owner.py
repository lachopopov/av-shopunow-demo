# Python 3.12; owner decision 2026-11-23: one history owner per question, history only via context_needed.
from dataclasses import replace
import unittest
from tests import test_production_workflow as base


def context_needed(reply):
    # Turn a supported fixture reply into a request for one earlier case.
    reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                 target='The earlier delivery windows answer')
    return reply


class HistoryOwnerTests(unittest.TestCase):
    # Reuse the real production graph and evidence service; only model replies are controlled.
    Base=base.ProductionWorkflowTests
    setUp,produce,run_production=Base.setUp,Base.produce,Base.run_production

    def no_knowledge(self,destinations):
        # The routed parts found no Knowledge: today they would never reach a producer.
        self.service.destinations=destinations
        self.evidence=replace(self.evidence,kind='insufficient_evidence',destinations=destinations,cleared=(),
                              unresolved_parts=destinations,dependency='independent')

    def history_search(self):
        from tests.test_evidence import relevance_answers
        self.fixture.identity_fixture(confidence=1.,label='clear')
        self.fixture.models.relevance=lambda question,candidates,**kwargs:relevance_answers(candidates)

    def test_single_department_owner_without_knowledge_requests_history_and_answers_completely(self):
        self.history_search()
        self.no_knowledge(('shipping_delivery',))
        def produce(payload,*,timeout):
            if payload['evidence']:
                return self.produce(payload,timeout=timeout)
            self.calls.append(payload)
            return context_needed({'part':payload['part'],'unresolved_parts':[],'reason':None,
                                   'saved_action_claim_ids':[]})
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual([row['history_owner'] for row in self.calls],[True,True])
        self.assertEqual(self.service.searches,1)
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(result.draft.unresolved_parts,())  # A complete history answer is not partial (B2).

    def test_owner_without_evidence_may_only_request_history(self):
        self.no_knowledge(('shipping_delivery',))
        def produce(payload,*,timeout):
            self.calls.append(payload)
            return {'part':payload['part'],'kind':'clarification_needed','text':None,'cited_ids':[],
                    'support_assertion':False,'target':None,'reason':'unclear','unresolved_parts':[],
                    'saved_action_claim_ids':[]}
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result.kind,'insufficient_evidence')  # Unsupported -> support offer, as before (H3).

    def test_several_departments_only_central_support_owns_history(self):
        self.history_search()
        self.service.destinations=('billing_payments','shipping_delivery')
        bill=replace(self.evidence.cleared[0],excerpt_id='bill',destination='billing_payments')
        self.evidence=replace(self.evidence,destinations=self.service.destinations,
                              cleared=(bill,*self.evidence.cleared),dependency='independent')
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            return context_needed(reply) if payload['part']=='billing_payments' else reply
        self.models.produce=produce
        result=self.run_production()
        owners={row['part']:row['history_owner'] for row in self.calls}
        self.assertEqual(owners,{'billing_payments':False,'shipping_delivery':False,'central_support':True})
        self.assertEqual(self.service.searches,0)  # A non-owner's request is never searched.
        self.assertIn(('billing_payments','insufficient_evidence'),
                      [(row.part,row.kind) for row in result.dispositions])

    def test_central_support_with_one_department_has_exactly_one_owner(self):
        self.history_search()
        self.no_knowledge(('central_support','shipping_delivery'))
        def produce(payload,*,timeout):
            self.calls.append(payload)
            return context_needed({'part':payload['part'],'unresolved_parts':[],'reason':None,
                                   'saved_action_claim_ids':[]})
        self.models.produce=produce
        self.run_production()
        owners=[(row['part'],row['history_owner']) for row in self.calls]
        self.assertEqual({part for part,owner in owners if owner},{'central_support'})
        self.assertNotIn(('shipping_delivery',True),owners)

    def test_central_support_owner_without_knowledge_requests_history_and_answers(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        self.history_search()
        def route(*args,**kwargs):
            # Company-wide history: today's customer release rule (kept, owner 2026-11-23) allows it.
            answers=judgment_answers()
            answers['company_wide_topic']['noul']=1.
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.fixture.models.history_route=route
        self.no_knowledge(('central_support',))
        def produce(payload,*,timeout):
            self.calls.append(payload)
            if not any(row['source_kind']=='owned_history' for row in payload['evidence']):
                return context_needed({'part':payload['part'],'unresolved_parts':[],'reason':None,
                                       'saved_action_claim_ids':[]})
            return {'part':payload['part'],'kind':'supported_answer','text':'Earlier windows',
                    'cited_ids':[payload['evidence'][0]['excerpt_id']],'support_assertion':True,
                    'target':None,'reason':None,'unresolved_parts':[],'saved_action_claim_ids':[]}
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(self.service.searches,1)
        self.assertEqual(result.kind,'draft_ready')
        self.assertEqual(result.draft.unresolved_parts,())

    def recalled_hr_history(self):
        # The customer's earlier conversation is about HR, a topic customers are refused.
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import validate_history_route,HISTORY_ROUTE_FIELDS
        self.history_search()
        def route(*args,**kwargs):
            answers=judgment_answers()
            answers['topic_hr']['noul']=1.
            return validate_history_route({key:answers[key] for key in HISTORY_ROUTE_FIELDS})
        self.fixture.models.history_route=route

    def test_refused_history_in_a_dependent_question_is_a_refusal_without_a_resumed_answer(self):
        # Challenge T2: the specialist owner asks; the recalled history is refused; nothing is answered.
        self.recalled_hr_history()
        self.service.dependency='dependent'  # The prepared request and its evidence agree on dependency.
        self.evidence=replace(self.evidence,dependency='dependent')
        def produce(payload,*,timeout):
            return context_needed(self.produce(payload,timeout=timeout))
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(len(self.calls),1)  # No resumed producer call.
        self.assertEqual(result.kind,'refusal')
        self.assertIsNone(result.draft)

    def test_resumed_owner_keeps_a_refused_part_unresolved(self):
        # Challenge T3: the owner resumes with history while the customer's refused hr part stays visible.
        self.history_search()
        self.evidence=replace(self.evidence,refused_parts=('hr',))
        def produce(payload,*,timeout):
            reply=self.produce(payload,timeout=timeout)
            return context_needed(reply) if len(self.calls)==1 else reply
        self.models.produce=produce
        result=self.run_production()
        self.assertEqual(len(self.calls),2)
        self.assertIn({'part':'hr','outcome':'refusal'},self.calls[1]['part_outcomes'])
        self.assertIn('hr',result.draft.unresolved_parts)


class NoDepartmentRoutingTests(unittest.TestCase):
    # Q-c (owner 2026-11-23): no department + information -> central_support; unclear -> clarification; action -> offer.
    def decide(self,intent,**topics):
        from tests.test_incoming import judgment_answers
        from shopunow.workflow import build_incoming_graph
        from shopunow.incoming import IncomingContext
        answers=judgment_answers()
        answers['intent'].update(choice=intent,probabilities={label:float(label==intent)
                                                              for label in answers['intent']['probabilities']})
        for key,value in topics.items():
            answers[key]['noul']=value
        graph=build_incoming_graph(judge=lambda context:answers,reviewer=None,authorize=lambda:True,
                                   security_trigger=.7,topic_trigger=.5)
        return graph.invoke({'context':IncomingContext('Any news?','customer',None,(),False)})['decision']

    def test_information_without_department_goes_to_central_support(self):
        decision=self.decide('information')
        self.assertEqual((decision.kind,decision.destinations),('continue',('central_support',)))

    def test_unclear_or_action_without_department_keeps_todays_outcome(self):
        self.assertEqual(self.decide('unclear_or_other').kind,'clarification')
        self.assertEqual(self.decide('individual_case_or_action').kind,'handoff')

    def test_customer_question_for_an_employee_only_department_is_still_refused(self):
        self.assertEqual(self.decide('information',topic_hr=.9).kind,'refusal')

    def test_jev_no_longer_judges_history(self):
        from shopunow.model_clients import incoming_questions
        self.assertNotIn("history_needed",list(incoming_questions()))


if __name__=='__main__':
    unittest.main()
