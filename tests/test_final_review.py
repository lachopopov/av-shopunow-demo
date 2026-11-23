# OpenAI 3.46.0 / Pydantic 2.14.1; Slice 5 final-review reply contract through the real SDK parser.
# Local HTTP responses only (httpx2.MockTransport); never a live provider call.
import json  # Inspect the native request and author the controlled transport reply.
import unittest
import httpx2  # The SDK's own HTTP layer; the mock replaces only the network boundary.
from openai import OpenAI
from shopunow.evidence import ModelBudget  # The one shared per-request call counter.
from shopunow.model_clients import AnswerModels  # Reviewers reuse the existing structured-call adapter.
from shopunow.production import DraftSnapshot,run_final_review


class FinalReviewContractTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]  # Every native request body and its timeout, in order.
        self.budget=ModelBudget()
        # A frozen two-contributor draft; IDs are literals, not derived from the code under test.
        self.snapshot=DraftSnapshot('draft-1','Parcels ship in 3-5 days. Refunds take 7 days.',('e1','e2'),
                                    ('shipping_delivery','billing_payments'),(),(),round=0)
        self.reply={'draft_id':'draft-1','round':0,'verdict':'approve','defect':None,'responsible_producers':[]}

    def review(self,reviewer='security',*,status=200):
        def respond(request):
            self.requests.append((json.loads(request.content),request.extensions['timeout']))
            if status!=200:
                return httpx2.Response(status,json={'error':{'message':'Controlled failure','type':'server_error'}})
            payload={'id':'resp_review','object':'response','created_at':0,'status':'completed',
                'model':'actual-controlled-reviewer','usage':{'input_tokens':9,'output_tokens':4,'total_tokens':13},
                'output':[{'id':'msg_review','type':'message','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':json.dumps(self.reply),'annotations':[]}]}]}
            return httpx2.Response(200,json=payload)
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
            models=AnswerModels(client=client,model='requested-reviewer',reasoning='high')
            return run_final_review(self.snapshot,reviewer,role='customer',home_department=None,
                                    models=models,budget=self.budget,timeout=lambda:6.)

    def test_approval_reviews_the_exact_snapshot_with_profile_context_in_one_call(self):
        # Break caught: reviewer sees a different draft, lacks the role, or the call is retried/uncounted.
        verdict=self.review()
        self.assertEqual((verdict.reviewer,verdict.draft_id,verdict.round,verdict.verdict),
                         ('security','draft-1',0,'approve'))
        body,_=self.requests[0]
        self.assertEqual(body['model'],'requested-reviewer')
        self.assertEqual(body['reasoning'],{'effort':'high'})
        self.assertEqual(json.loads(body['input']),{'draft_id':'draft-1','round':0,
            'full_text':'Parcels ship in 3-5 days. Refunds take 7 days.','cited_ids':['e1','e2'],
            'contributor_ids':['shipping_delivery','billing_payments'],'unresolved_parts':[],
            'saved_action_claim_ids':[],'user_role':'customer','home_department':None})
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.budget.attempts,1)

    def test_reply_schema_allows_only_this_draft_round_and_its_contributors(self):
        # Break caught: a generic schema lets the model echo a wrong ID or blame a non-contributor (CD-086 lesson).
        self.review('policy')
        body,_=self.requests[0]
        props=body['text']['format']['schema']['properties']
        self.assertEqual(props['draft_id']['enum'],['draft-1'])
        self.assertEqual(props['round']['enum'],[0])
        self.assertEqual(props['responsible_producers']['items']['enum'],['shipping_delivery','billing_payments'])

    def test_valid_revise_names_contributors_and_a_private_defect(self):
        # Break caught: a valid correction request is discarded, so no producer can ever be corrected.
        self.reply.update(verdict='revise',defect='Mentions another customer order.',
                          responsible_producers=['billing_payments'])
        verdict=self.review()
        self.assertEqual((verdict.verdict,verdict.defect,verdict.responsible_producers),
                         ('revise','Mentions another customer order.',('billing_payments',)))

    def test_unknown_reviewer_role_is_failure_without_a_model_call(self):
        # Break caught: a typo or new role silently runs an unchecked review with no approved checklist.
        verdict=self.review('quality')
        self.assertEqual(verdict.verdict,'failure')
        self.assertEqual(self.requests,[])

    def test_invalid_or_failed_review_becomes_failure_without_retry(self):
        # Break caught: a malformed or failed verdict is treated as approval/revise, raises, or is retried.
        cases={'server_error':({},500),
               'wrong_draft':({'draft_id':'draft-0'},200),
               'wrong_round':({'round':1},200),
               'unknown_producer':({'verdict':'revise','defect':'x','responsible_producers':['hr']},200),
               'revise_without_producer':({'verdict':'revise','defect':'x','responsible_producers':[]},200),
               'revise_without_defect':({'verdict':'revise','defect':None,'responsible_producers':['billing_payments']},200),
               'blank_defect':({'verdict':'revise','defect':'  ','responsible_producers':['billing_payments']},200),
               'approve_with_defect':({'defect':'x'},200),
               'approve_with_producer':({'responsible_producers':['billing_payments']},200),
               'duplicate_producer':({'verdict':'revise','defect':'x',
                                      'responsible_producers':['billing_payments','billing_payments']},200)}
        for name,(change,status) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.reply.update(change)
                verdict=self.review(status=status)
                self.assertEqual((verdict.reviewer,verdict.draft_id,verdict.round,verdict.verdict,
                                  verdict.responsible_producers),('security','draft-1',0,'failure',()))
                self.assertEqual(len(self.requests),1)
                self.assertEqual(self.budget.attempts,1)


if __name__=='__main__':
    unittest.main()
