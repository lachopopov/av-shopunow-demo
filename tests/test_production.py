# Python 3.12 / Pydantic 2.14.1; controlled producer calls do not prove native semantic quality.
import importlib.util
from types import SimpleNamespace
import unittest
from shopunow.evidence import EvidenceCandidate,ModelBudget


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.production'),'Producer boundary missing')
        from shopunow import production
        self.p=production
        self.candidate=EvidenceCandidate('e1','knowledge','SU-SD-001','v1','Tracking facts',
            'shipping_delivery',clearance='clean',relevance_basis='typed_current_request')
        self.context=production.ProducerInput(request_id='request-1',user_id='owner',conversation_id='chat',
            question='What is tracking?',part='shipping_delivery',evidence=(self.candidate,))
        self.reply={'part':'shipping_delivery','kind':'supported_answer','text':'Tracking facts',
            'cited_ids':['e1'],'support_assertion':True,'target':None,'reason':None,
            'unresolved_parts':[],'saved_action_claim_ids':[]}
        self.calls=[]
        self.models=SimpleNamespace(produce=self.produce)
        self.budget=ModelBudget()

    def produce(self,payload,*,timeout):
        self.calls.append((payload,timeout))
        return self.reply

    def run_producer(self):
        return self.p.run_producer(self.context,models=self.models,budget=self.budget,timeout=lambda:17.)

    def test_supported_contribution_uses_only_admitted_ids_and_original_budget(self):
        result=self.run_producer()
        self.assertEqual((result.kind,result.text,result.cited_ids),('supported_answer','Tracking facts',('e1',)))
        self.assertEqual(self.budget.attempts,1)
        self.assertEqual(self.calls[0][1],17.)
        self.assertEqual(self.calls[0][0]['part'],'shipping_delivery')
        self.assertEqual(self.calls[0][0]['evidence'][0]['excerpt_id'],'e1')
        self.reply['cited_ids'].append('later-mutation')
        self.assertEqual(result.cited_ids,('e1',))

    def test_invalid_or_oversized_answers_never_become_supported_contributions(self):
        for change in ({'cited_ids':['foreign']},{'cited_ids':['e1','e1']},{'support_assertion':False},
                {'part':'hr'},{'text':'\u00e9'*4097},{'text':'\ud800'},
                {'saved_action_claim_ids':['not-saved']},{'unresolved_parts':['invented-destination']}):
            with self.subTest(change=tuple(change)):
                self.reply={**self.reply,**change}
                result=self.run_producer()
                self.assertEqual(result.kind,'technical_failure')
                self.assertIsNone(result.text)
                self.setUp()

    def test_unadmitted_or_foreign_context_never_starts_a_model_call(self):
        from dataclasses import replace
        for candidate in (replace(self.candidate,clearance='uncertain'),
                replace(self.candidate,relevance_basis='semantic_resolution'),
                replace(self.candidate,relevance_basis='pending'),
                replace(self.candidate,destination='hr'),
                replace(self.candidate,source_kind='owned_history',owner_user_id='foreign',destination='history')):
            with self.subTest(candidate=candidate.source_kind+candidate.clearance):
                self.context=replace(self.context,evidence=(candidate,))
                self.assertEqual(self.run_producer().kind,'technical_failure')
                self.assertEqual(self.calls,[])
                self.assertEqual(self.budget.attempts,0)

    def test_targeted_context_request_is_not_an_answer_or_new_department(self):
        self.reply.update(kind='context_needed',text=None,cited_ids=[],support_assertion=False,
                          target='The earlier parcel correction',reason=None)
        result=self.run_producer()
        self.assertEqual(result.kind,'context_needed')
        self.assertEqual(result.target,'The earlier parcel correction')
        self.assertIsNone(result.text)
        self.reply['target']=' '
        self.assertEqual(self.run_producer().kind,'technical_failure')


if __name__=='__main__':
    unittest.main()
