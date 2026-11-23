# OpenAI 3.46.0 / TypeSafe SDK 0.7.5; native clients with local HTTP transports only.
import json
from types import SimpleNamespace
import unittest
import httpx2
from openai import OpenAI
from typesafe_sdk import TypeSafeClient, RetryPolicy
from shopunow import model_clients


class EvidenceClientTests(unittest.TestCase):
    def test_semantic_resolution_uses_explicit_shared_model_and_native_typed_output(self):
        # Wrong model configuration, omitted target binding or unobserved usage would break U1/U2.
        self.assertTrue(hasattr(model_clients,'AnswerModels'),'Shared answer/semantic adapter is missing')
        parsed={'question':'What is tracking?','status':'resolved','intent':'information',
            'dependency':'independent','destinations':['shipping_delivery'],
            'source_matches':[{'source_id':'SU-SD-001','match':'relevant'}]}
        payload={'id':'resp_resolution','object':'response','created_at':0,'status':'completed',
            'model':'controlled-answer','usage':{'input_tokens':11,'output_tokens':7,'total_tokens':18},
            'output':[{'id':'msg_resolution','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':json.dumps(parsed),'annotations':[]}]}]}
        observed=[]
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as client:
            models=model_clients.AnswerModels(client=client,model='controlled-answer',reasoning='low',
                observe_response=lambda stage,response:observed.append((stage,response.model,response.usage.total_tokens)))
            result=models.resolve_routing({'question':'What is tracking?','history_status':'unavailable'},timeout=2)
        self.assertEqual(result.model_dump(),parsed)
        body=self.calls[0]
        self.assertEqual(body['model'],'controlled-answer')
        self.assertEqual(body['reasoning'],{'effort':'low'})
        self.assertIs(body['store'],False)
        self.assertEqual(json.loads(body['input']),{'question':'What is tracking?','history_status':'unavailable'})
        self.assertEqual(observed,[('semantic_routing','controlled-answer',18)])

    def test_semantic_candidate_resolution_rejects_extra_output_and_refusal(self):
        # An answer-like extra field or provider refusal must never become admission authority.
        self.assertTrue(hasattr(model_clients,'AnswerModels'),'Shared answer/semantic adapter is missing')
        for content in (
                {'type':'output_text','text':json.dumps({'question':'Q','outcome':{'status':'resolved',
                    'relevant_excerpt_ids':['e1']},'answer':'Unrequested generated answer'}),'annotations':[]},
                {'type':'refusal','refusal':'Unavailable'}):
            with self.subTest(content=content['type']):
                payload={'id':'resp_resolution','object':'response','created_at':0,'status':'completed',
                    'model':'controlled-answer','output':[{'id':'msg_resolution','type':'message',
                        'role':'assistant','status':'completed','content':[content]}]}
                with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as client:
                    models=model_clients.AnswerModels(client=client,model='controlled-answer',reasoning='low')
                    with self.assertRaises(ValueError):
                        models.resolve_candidates({'question':'Q'},timeout=2)

    def native_request(self,call,reply):
        payload={'id':'resp_contract','object':'response','created_at':0,'status':'completed',
            'model':'controlled-answer','output':[{'id':'msg_contract','type':'message','role':'assistant',
                'status':'completed','content':[{'type':'output_text','text':json.dumps(reply),'annotations':[]}]}]}
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as client:
            return call(model_clients.AnswerModels(client=client,model='controlled-answer',reasoning='low')),self.calls[-1]

    def test_candidate_contract_cannot_express_relevant_ids_with_an_unresolved_status(self):
        parsed,body=self.native_request(lambda models:models.resolve_candidates({'question':'Q'},timeout=2),
            {'question':'Q','outcome':{'status':'resolved','relevant_excerpt_ids':['e1']}})
        self.assertEqual(parsed.outcome.relevant_excerpt_ids,['e1'])
        self.assertIn('answers only part',body['instructions'])
        with self.assertRaises(ValueError):
            self.native_request(lambda models:models.resolve_candidates({'question':'Q'},timeout=2),
                {'question':'Q','outcome':{'status':'insufficient','relevant_excerpt_ids':['e1']}})

    def test_routing_contract_separates_missing_facts_from_unresolved_routing(self):
        reply={'question':'Q','status':'resolved','intent':'information','dependency':'independent',
               'destinations':['hr'],'source_matches':[]}
        _,body=self.native_request(lambda models:models.resolve_routing({'question':'Q'},timeout=2),reply)
        self.assertIn('Missing facts are not a routing failure',body['instructions'])

    def test_native_history_identity_has_full_pool_no_judgment_hints_and_observed_usage(self):
        parsed={'status':'clear','selected_excerpt_ids':['e2']}
        payload={'id':'resp_identity','object':'response','created_at':0,'status':'completed',
            'model':'gpt-6-luna','usage':{'input_tokens':20,'output_tokens':8,'total_tokens':28},
            'output':[{'id':'msg_identity','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':json.dumps(parsed),'annotations':[]}]}]}
        candidates=tuple(SimpleNamespace(excerpt_id='e'+str(n),source_kind='owned_history',
            text=text,destination='history',conversation_id='owned',returned_metadata={
                'sequence':n*2,'kind':'completed_clarification'}) for n,text in (
                    (1,'An earlier account.'),(2,'A correction identifying a different case.')))
        observed=[]
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as client:
            models=model_clients.EvidenceModels(jev=None,reviewer=client,embedding=None,jev_model='offline',
                observe_response=lambda stage,response:observed.append((stage,response.model,response.usage.total_tokens)))
            self.assertTrue(hasattr(models,'resolve_history_identity'),'Native identity classifier missing')
            result=models.resolve_history_identity('What did the correction establish?',candidates,timeout=2)
        self.assertEqual(result.model_dump(),parsed)
        body=self.calls[0]
        self.assertIs(body['store'],False)
        self.assertEqual(body['reasoning'],{'effort':'medium'})
        self.assertIn('identified even when it lacks some requested facts',body['instructions'])
        self.assertEqual(json.loads(body['input']),{'question':'What did the correction establish?','excerpts':[
            {'excerpt_id':'e1','source_kind':'owned_history','destination':'history','text':'An earlier account.',
             'conversation_id':'owned','sequence':2,'completion_kind':'completed_clarification'},
            {'excerpt_id':'e2','source_kind':'owned_history','destination':'history','text':'A correction identifying a different case.',
             'conversation_id':'owned','sequence':4,'completion_kind':'completed_clarification'}]})
        self.assertEqual(observed,[('history_identity','gpt-6-luna',28)])

    def test_relevance_preserves_native_distribution_for_later_uncertainty_policy(self):
        # A weak negative must remain distinguishable from a settled rejection.
        from typesafe_sdk import ChoiceAnswer
        answer={'type':'choice','choice':'irrelevant','confidence':.385,
                'probabilities':{'relevant':.40,'irrelevant':.59,'uncertain':.01}}
        payload={'model':'offline-jev','usage':{},'answers':{'e1':answer}}
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                            transport=self.transport(payload)) as client:
            models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
            result=models.relevance('What does tracking mean?',self.candidates,timeout=2)
        self.assertIsInstance(result,dict)
        self.assertIsInstance(result['e1'],ChoiceAnswer)
        self.assertEqual(result['e1'].model_dump(),answer)

    def setUp(self):
        self.assertTrue(hasattr(model_clients,'EvidenceModels'), 'Native evidence-model boundary is missing')
        self.calls=[]
        self.candidates=(SimpleNamespace(excerpt_id='e1',source_kind='knowledge',
            text='Tracking explains delivery stages.',destination='shipping_delivery'),)

    def transport(self, payload):
        def respond(request):
            self.calls.append(json.loads(request.content))
            return httpx2.Response(200,json=payload)
        return httpx2.MockTransport(respond)

    def test_native_reviewer_has_distinct_per_id_contract_and_no_storage(self):
        parsed={'verdicts':[{'excerpt_id':'e1','verdict':'clean'}]}
        payload={'id':'resp_offline','object':'response','created_at':0,'status':'completed',
            'model':'gpt-6-luna','output':[{'id':'msg_offline','type':'message','role':'assistant',
                'status':'completed','content':[{'type':'output_text','text':json.dumps(parsed),'annotations':[]}]}]}
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as reviewer:
            models=model_clients.EvidenceModels(jev=None,reviewer=reviewer,embedding=reviewer,jev_model='offline-jev')
            self.assertEqual(models.review(self.candidates,timeout=2),parsed['verdicts'])
        self.assertIs(self.calls[0]['store'],False)
        self.assertIn('every',self.calls[0]['instructions'])
        self.assertIn('verdicts',self.calls[0]['text']['format']['schema']['properties'])
        self.assertEqual(json.loads(self.calls[0]['input'])[0]['excerpt_id'],'e1')

    def test_native_embedding_identity_dimensions_and_input_order(self):
        payload={'object':'list','model':'text-embedding-3-small',
            'data':[{'object':'embedding','index':0,'embedding':[1.0]+[0.0]*1535}],
            'usage':{'prompt_tokens':3,'total_tokens':3}}
        with OpenAI(api_key='offline-test',http_client=httpx2.Client(transport=self.transport(payload))) as client:
            models=model_clients.EvidenceModels(jev=None,reviewer=client,embedding=client,jev_model='offline-jev')
            vectors=models.embed(['Tracking?'],timeout=2)
        self.assertEqual(len(vectors[0]),1536)
        self.assertEqual(self.calls[0]['model'],'text-embedding-3-small')
        self.assertEqual(self.calls[0]['dimensions'],1536)
        self.assertEqual(self.calls[0]['input'],['Tracking?'])

    def test_native_identity_and_nullable_usage_reach_observer_before_judgments_are_discarded(self):
        from shopunow.incoming import IncomingContext
        from tests.test_incoming import judgment_answers
        observed=[]
        payload={'model':'actual-jev','usage':{'input_tokens':19,'output_tokens':None},
                 'answers':judgment_answers()}
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                            transport=self.transport(payload)) as client:
            answers=model_clients.judge_incoming(client,IncomingContext('Question','customer',None,(),False),
                model='requested-jev',timeout=2,observe_response=lambda stage,response:
                    observed.append((stage,response.model,response.usage.model_dump())))
        self.assertIn('intent',answers)
        self.assertEqual(observed,[('incoming_judgment','actual-jev',{'input_tokens':19,'output_tokens':None})])

    def test_native_relevance_selects_ids_and_global_case_ambiguity(self):
        def choice(label,labels):
            return {'type':'choice','choice':label,'confidence':1.0,
                    'probabilities':{x:float(x==label) for x in labels}}
        payload={'model':'offline-jev','usage':{},'answers':{
            'e1':choice('relevant',['relevant','irrelevant','uncertain']),
            'case_resolution':choice('ambiguous',['clear','ambiguous','insufficient'])}}
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                            transport=self.transport(payload)) as client:
            models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
            answers=models.relevance('Which replacement?',(SimpleNamespace(
                excerpt_id='e1',source_kind='owned_history',text='A replacement case',destination='history',
                conversation_id='owned',returned_metadata={'sequence':2,'kind':'completed_clarification'}),),timeout=2)
        self.assertEqual({key:value.choice for key,value in answers.items()},
                         {'case_resolution':'ambiguous','e1':'relevant'})
        self.assertEqual(set(self.calls[0]['questions']),{'e1','case_resolution'})

    def test_descriptor_questions_bind_each_target_and_keep_uncertainty_separate(self):
        # Different source meanings must remain attached to their own judgment after reordering.
        candidates=(SimpleNamespace(excerpt_id='source_badge',source_kind='source_descriptor',
            text='How do I report a damaged badge?',destination='facilities_admin'),
            SimpleNamespace(excerpt_id='source_chair',source_kind='source_descriptor',
            text='How do I request a chair repair?',destination='facilities_admin'))
        for ordered in (candidates,candidates[::-1]):
            with self.subTest(first=ordered[0].excerpt_id):
                captured=[]
                def respond(request):
                    body=json.loads(request.content)
                    captured.append(body)
                    labels={'source_badge':'relevant','source_chair':'uncertain','case_resolution':'clear'}
                    answers={key:{'type':'choice','choice':labels[key],'confidence':1.0,
                        'probabilities':{name:float(name==labels[key]) for name in value['criteria']}}
                        for key,value in body['questions'].items()}
                    return httpx2.Response(200,json={'model':'offline-jev','usage':{},'answers':answers})
                with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                                    transport=httpx2.MockTransport(respond)) as client:
                    models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
                    result=models.relevance('How should I report my damaged badge?',ordered,timeout=2)
                body=captured[0]
                self.assertEqual(set(body['questions']),{'source_badge','source_chair'})
                for index,row in enumerate(ordered):
                    instruction=body['questions'][row.excerpt_id]['instructions']
                    self.assertIn(f'excerpts[{index}]',instruction)
                    self.assertIn('`question`',instruction)
                    self.assertIn('source descriptor',instruction)
                    self.assertEqual(body['state']['excerpts'][index]['excerpt_id'],row.excerpt_id)
                self.assertEqual({key:value.choice for key,value in result.items()},
                                 {'source_badge':'relevant','source_chair':'uncertain'})

    def test_history_relevance_supplies_verified_relationships_to_global_case_question(self):
        candidates=(SimpleNamespace(excerpt_id='e1',source_kind='owned_history',text='My badge broke.',
            destination='history',conversation_id='owned-conversation',
            returned_metadata={'sequence':2,'kind':'completed_clarification'}),)
        payload={'model':'offline-jev','usage':{},'answers':{
            'e1':{'type':'choice','choice':'relevant','confidence':1.0,
                  'probabilities':{'relevant':1.0,'irrelevant':0.0,'uncertain':0.0}},
            'case_resolution':{'type':'choice','choice':'clear','confidence':1.0,
                  'probabilities':{'clear':1.0,'ambiguous':0.0,'insufficient':0.0}}}}
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                            transport=self.transport(payload)) as client:
            models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
            models.relevance('What did we discuss about the badge?',candidates,timeout=2)
        state=self.calls[0]['state']['excerpts'][0]
        self.assertEqual(state.get('conversation_id'),'owned-conversation')
        self.assertEqual(state.get('sequence'),2)
        self.assertEqual(state.get('completion_kind'),'completed_clarification')
        self.assertIn('excerpts[0]',self.calls[0]['questions']['e1']['instructions'])

    def test_post_clearance_history_routing_reuses_native_topics_with_separate_clean_state(self):
        from tests.test_incoming import judgment_answers
        from shopunow.incoming import HISTORY_ROUTE_FIELDS
        answers=judgment_answers()
        answers['topic_shipping_delivery']['noul']=.9
        payload={'model':'offline-jev','usage':{},
                 'answers':{key:answers[key] for key in HISTORY_ROUTE_FIELDS}}
        candidate=SimpleNamespace(excerpt_id='e1',source_kind='owned_history',clearance='clean',
                                  conversation_id='owned',text='A completed parcel discussion')
        with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                            transport=self.transport(payload)) as client:
            models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
            result=models.history_route('What did the earlier case establish?',(candidate,),
                                       role='customer',home_department=None,timeout=2)
            self.assertEqual(result['topic_shipping_delivery'].noul,.9)
            candidate.clearance='uncertain'
            with self.assertRaises(ValueError):
                models.history_route('Earlier case?',(candidate,),role='customer',home_department=None,timeout=2)
        self.assertEqual(len(self.calls),1)  # Unreviewed context cannot start another provider request.
        body=self.calls[0]
        self.assertEqual(set(body['questions']),set(HISTORY_ROUTE_FIELDS))
        self.assertEqual(body['state'],{'question':'What did the earlier case establish?',
            'role':'customer','home_department':None,'cleared_history':[{
                'excerpt_id':'e1','text':'A completed parcel discussion','conversation_id':'owned'}]})
        self.assertTrue(all('`cleared_history`' in row['instructions'] for row in body['questions'].values()))
        candidate.clearance='clean'
        for malformed in (
                {key:value for key,value in payload['answers'].items() if key!='topic_hr'},
                {**payload['answers'],'unexpected':{'type':'noul','noul':0.0}}):
            with self.subTest(fields=tuple(malformed)):
                invalid={**payload,'answers':malformed}
                with TypeSafeClient(api_key='offline-test',retry=RetryPolicy(max_retries=0),
                                    transport=self.transport(invalid)) as client:
                    models=model_clients.EvidenceModels(jev=client,reviewer=None,embedding=None,jev_model='offline-jev')
                    with self.assertRaises(ValueError):
                        models.history_route('Earlier case?',(candidate,),role='customer',home_department=None,timeout=2)


class EvidenceSDKTests(unittest.TestCase):
    def test_pinned_native_chroma_query_normalizes_array_filters_and_embeddings(self):
        import asyncio
        from uuid import UUID
        from chromadb.api.models.AsyncCollection import AsyncCollection
        from chromadb.types import Collection
        from shopunow.sdk_writes import SDKEvidenceRecords
        calls=[]
        class NativeServer:
            async def _query(self,**payload):
                calls.append(payload)
                return {'ids':[[]],'documents':[[]],'metadatas':[[]],'distances':None,
                        'embeddings':None,'uris':None,'data':None,'included':['documents','metadatas']}
        model=Collection(id=UUID(int=1),name='prepared-knowledge',configuration_json={},
            serialized_schema=None,metadata={'embedding_model':'text-embedding-3-small','embedding_dimensions':1536},
            dimension=1536,tenant='offline-tenant',database='offline-database',version=0,log_position=0)
        collection=AsyncCollection(client=NativeServer(),model=model,embedding_function=None)
        class Worker:
            def call(self,operation,*,timeout):
                return asyncio.run(operation())
        storage=SDKEvidenceRecords(Worker(),profiles=None,conversations=None,escalations=None,knowledge=collection)
        where={'$and':[{'eligible_audience':{'$contains':'customer'}},{'classification':'general'}]}
        result=storage.knowledge([1.0]+[0.0]*1535,where=where,limit=4,timeout=2)
        self.assertEqual(result['ids'],[[]])
        self.assertEqual(calls[0]['where'],where)
        self.assertEqual(calls[0]['n_results'],4)
        self.assertEqual(len(calls[0]['query_embeddings'][0]),1536)

    def test_wrong_collection_embedding_configuration_stops_query_before_native_call(self):
        from shopunow.sdk_writes import SDKEvidenceRecords
        calls=[]
        class Worker:
            def call(self,*args,**kwargs):
                calls.append('dispatch')
        collection=SimpleNamespace(metadata={'embedding_model':'wrong','embedding_dimensions':3},query=lambda **_:None)
        storage=SDKEvidenceRecords(Worker(),profiles=None,conversations=None,
                                    escalations=None,knowledge=collection)
        with self.assertRaises(ValueError):
            storage.knowledge([1.0]*1536,where={'subject':'shipping_delivery'},limit=4,timeout=2)
        self.assertEqual(calls,[])

    def test_native_sdk_mappings_filter_before_retrieval_and_never_embed_conversations(self):
        from shopunow import sdk_writes
        self.assertTrue(hasattr(sdk_writes,'SDKEvidenceRecords'),'Native evidence storage mappings are missing')
        import asyncio
        calls=[]
        class Collection:
            metadata={'embedding_model':'text-embedding-3-small','embedding_dimensions':1536}
            async def get(self,**kwargs):
                calls.append(('get',kwargs))
                return {'ids':[],'documents':[],'metadatas':[]}
            async def query(self,**kwargs):
                calls.append(('query',kwargs))
                return {'ids':[[]],'documents':[[]],'metadatas':[[]]}
        class Worker:
            def call(self,operation,*,timeout):
                return asyncio.run(operation())
        collection=Collection()
        storage=sdk_writes.SDKEvidenceRecords(Worker(),profiles=collection,conversations=collection,
                                             escalations=collection,knowledge=collection)
        storage.history_page('syn-cust-01',offset=100,timeout=2)
        self.assertEqual(calls[0][1]['where'],{'$and':[{'owner_user_id':'syn-cust-01'},
            {'record_type':'terminal_outcome'},{'kind':{'$in':[
                'completed_answer','completed_clarification','completed_refusal']}},{'complete':True}]})
        self.assertEqual(calls[0][1]['offset'],100)
        self.assertEqual(calls[0][1]['limit'],100)
        storage.knowledge([1.0]+[0.0]*1535,where={'subject':'shipping_delivery'},limit=4,timeout=2)
        self.assertEqual(calls[1][0],'query')
        self.assertEqual(calls[1][1]['where'],{'subject':'shipping_delivery'})


class EvidenceStorageWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_fourth_collection_reuses_lifespan_client_without_initialization(self):
        from shopunow.storage_app import create_storage_app
        from shopunow.sdk_writes import SDKEvidenceRecords
        calls=[]
        class Client:
            async def get_collection(self,name,*,embedding_function):
                calls.append((name,embedding_function))
                return SimpleNamespace(metadata={'embedding_model':'text-embedding-3-small',
                                                  'embedding_dimensions':1536})
        client=Client()
        async def connect():
            calls.append('connect')
            return client
        app=create_storage_app(connect=connect,api_token='offline-test',collection_names=('profiles','chats','offers'),
                               knowledge_collection_name='knowledge')
        async with app.application.router.lifespan_context(app.application):
            self.assertIsInstance(app.storage,SDKEvidenceRecords)
            self.assertIs(app.worker.client,client)
            self.assertFalse(any(getattr(route,'path',None)=='/interactions' for route in app.application.routes))
        self.assertEqual(calls,['connect',('profiles',None),('chats',None),('offers',None),('knowledge',None)])
