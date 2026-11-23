# OpenAI 3.46.0 / Pydantic 2.14.1; actual SDK serialization with local HTTP responses, never a live call.
import json  # Inspect the native request and supply independently authored transport responses.
from dataclasses import replace  # Keep each producer case detached from other cases.
import unittest
import httpx2
from openai import OpenAI
from shopunow.evidence import EvidenceCandidate,ModelBudget
from shopunow.model_clients import AnswerModels
from shopunow.production import ProducerInput,run_producer


class ProductionClientTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        self.observed=[]
        self.budget=ModelBudget()
        self.context=ProducerInput('request-1','owner','chat','Explain tracking','shipping_delivery',(
            EvidenceCandidate('e1','knowledge','SU-SD-001','v1','Tracking facts','shipping_delivery',
                              clearance='clean',relevance_basis='typed_current_request'),),response_style='brief')
        self.reply={'part':'shipping_delivery','kind':'supported_answer','text':'Tracking facts',
            'cited_ids':['e1'],'support_assertion':True,'target':None,'reason':None,
            'unresolved_parts':[],'saved_action_claim_ids':[]}

    def call(self,*,content=None,status=200):
        def respond(request):
            self.requests.append((json.loads(request.content),request.extensions['timeout']))
            if status!=200:
                return httpx2.Response(status,json={'error':{'message':'Controlled unavailable service','type':'server_error'}})
            payload={'id':'resp_producer','object':'response','created_at':0,'status':'completed',
                'model':'actual-controlled-answer','usage':{'input_tokens':13,'output_tokens':5,'total_tokens':18},
                'output':[{'id':'msg_producer','type':'message','role':'assistant','status':'completed',
                    'content':[content or {'type':'output_text','text':json.dumps(self.reply),'annotations':[]}]}]}
            return httpx2.Response(200,json=payload)
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
            models=AnswerModels(client=client,model='requested-controlled-answer',reasoning='low',
                observe_response=lambda stage,response:self.observed.append(
                    (stage,response.model,response.usage.total_tokens)))
            return run_producer(self.context,models=models,budget=self.budget,timeout=lambda:7.)

    def test_native_production_uses_exact_admitted_input_explicit_model_and_observed_actual_usage(self):
        result=self.call()
        self.assertEqual((result.kind,result.text,result.cited_ids),('supported_answer','Tracking facts',('e1',)))
        body,timeouts=self.requests[0]
        self.assertEqual(body['model'],'requested-controlled-answer')
        self.assertEqual(body['reasoning'],{'effort':'low'})
        self.assertIs(body['store'],False)
        self.assertEqual(json.loads(body['input']),{'request_id':'request-1','part':'shipping_delivery',
            'question':'Explain tracking','response_style':'brief','unresolved_parts':[],
            'dependency':'unclear','missing_context':None,'history_owner':False,'other_parts':[],'part_outcomes':[],  # other_parts: R3
            'evidence':[{'excerpt_id':'e1','source_kind':'knowledge','source_record_id':'SU-SD-001',
                         'source_version':'v1','text':'Tracking facts','destination':'shipping_delivery'}],
            'contributions':[]})
        self.assertTrue(all(value==7. for value in timeouts.values()))
        self.assertEqual(self.budget.attempts,1)
        self.assertEqual(self.observed,[('answer_production','actual-controlled-answer',18)])

    def test_native_nonanswer_alternatives_remain_distinct_without_answer_text(self):
        for kind,target,reason in (('context_needed','Earlier parcel correction',None),
                ('clarification_needed',None,None),('handoff_needed','shipping_delivery','Missing support'),
                ('insufficient_evidence',None,None),('technical_failure',None,None)):
            with self.subTest(kind=kind):
                self.setUp()
                self.reply.update(kind=kind,text=None,cited_ids=[],support_assertion=False,target=target,reason=reason)
                result=self.call()
                self.assertEqual((result.kind,result.text,result.target,result.reason),(kind,None,target,reason))
                self.assertEqual(len(self.requests),1)

    def test_native_refusal_malformed_unknown_citation_and_server_failure_never_produce_draft_or_retry(self):
        cases=(('refusal',{'type':'refusal','refusal':'Unavailable'},200),
               ('malformed',{'type':'output_text','text':'{}','annotations':[]},200),
               ('server',None,500),('unknown_id',None,200),('extra_field',None,200))
        for case,content,status in cases:
            with self.subTest(case=case):
                self.setUp()
                if case=='unknown_id':
                    self.reply['cited_ids']=['not-admitted']
                if case=='extra_field':
                    self.reply['permission_override']=True
                result=self.call(content=content,status=status)
                self.assertEqual(result.kind,'technical_failure')
                self.assertIsNone(result.text)
                self.assertEqual(self.budget.attempts,1)
                self.assertEqual(len(self.requests),1)

    def test_unpromoted_context_is_withheld_before_native_transport(self):
        self.context=replace(self.context,evidence=(replace(self.context.evidence[0],relevance_basis='semantic_resolution'),))
        self.assertEqual(self.call().kind,'technical_failure')
        self.assertEqual(self.requests,[])
        self.assertEqual(self.budget.attempts,0)


# Flow: admitted context -> real native SDK -> controlled HTTP -> validation and usage observations.
if __name__=='__main__':
    unittest.main()
