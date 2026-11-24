# OpenAI 3.46.0 / TypeSafe SDK 0.7.5; native adapters with controlled local external responses.
import json
from datetime import datetime,timezone
from uuid import UUID
import httpx2
from shopunow import knowledge,workflow
from shopunow.model_clients import AnswerModels,EvidenceModels
from shopunow.sdk_writes import SDKEvidenceRecords
from tests.incoming_fixture import IncomingFixture
from tests.test_evidence import EvidenceStorage
from tests.test_preclearance_writes import ImmediateBridge
from tests.test_records import transcript_pair,event_result,header_result


class PrivateWorkflowFixture(IncomingFixture):
    """Reuse real native SDK parsing, stored add/get mapping and the existing private HTTP host."""
    def __init__(self,tracking,*,source_ids=('SU-SD-001',)):
        super().__init__(tracking)
        full=knowledge.load_knowledge()
        self.catalog={key:full[key] for key in source_ids}
        self.answers['topic_shipping_delivery']['noul']=1.
        self.destinations=('shipping_delivery',)
        self.dependency='independent'
        self.routing_status='resolved'
        self.security_trigger=.7
        self.history_kind='clear'
        self.current_match='relevant'
        self.admit_candidates=True
        self.history_verdict=self.current_verdict='clean'
        self.history_topics=('shipping_delivery',)
        self.history_topic_score=1.  # Controlled strength of a routed historical topic.
        self.history_company_score=None  # Optional separate company-wide score on the history route.
        self.history_topic_scores={}  # Optional per-topic scores that replace the shared routed strength.
        self.routing_intent='information'  # Controlled adjudicated intent; None leaves intent unresolved.
        self.semantic_confidence_trigger=.7  # Explicit workflow configuration; None means not configured.
        self.history_match='relevant'  # Controlled relevance of owned-history excerpts.
        self.current_match_for=lambda text:self.current_match  # Controlled per-excerpt Knowledge relevance.
        self.candidate_reply=None  # Optional controlled candidate adjudication failure.
        self.boundaries,self.evidence_results,self.producer_calls,self.usage=[],[],[],[]
        self.draft_text='Private supported draft text'
        self.producer_hook=lambda payload,reply:reply
        self.observer_hook=lambda result:None
        self.budget=None
        self.native_storage=SDKEvidenceRecords(ImmediateBridge(),profiles=None,
            conversations=self.conversations,escalations=self.offers,knowledge=None)
        self.storage=EvidenceStorage(next(iter(self.catalog.values())),knowledge)
        # Actual pending/fallback add/get mappings share the same controlled record collections.
        for name in ('write_offer','exact_offer','write_preclearance_marker','exact_preclearance_marker'):
            setattr(self.storage,name,getattr(self.native_storage,name))
        self.storage.write_pending=self.write_pending
        self.storage.exact_pending=self.exact_pending
        self.storage.knowledge=self.query_knowledge
        self.evidence_models=EvidenceModels(jev=self.jev,reviewer=self.reviewer,embedding=self.reviewer,
            jev_model='offline-jev',observe_response=self.observe_response)
        self.answer_models=AnswerModels(client=self.reviewer,model='controlled-answer',reasoning='low',
            observe_response=self.observe_response)

    def write_pending(self,pending,*,timeout):
        self.storage.calls.append('pending_write')
        self.storage.pending=pending
        return self.native_storage.write_pending(pending,timeout=timeout)

    def exact_pending(self,request_id,*,timeout):
        self.storage.calls.append('pending_read')
        raw=self.native_storage.exact_pending(request_id,timeout=timeout)
        return {'ids':[],'documents':[],'metadatas':[]} if self.storage.fail_pending else raw

    def query_knowledge(self,vector,*,where,limit,timeout):
        self.storage.calls.append(('knowledge',where))
        # Honour the code-owned department filter like the store does (owner 2026-11-23, challenge M7).
        def matches(labels):
            # Each clause is an equality, or {'$contains': role} on the audience list.
            return all(value['$contains'] in labels.get(key,()) if isinstance(value,dict) else labels.get(key)==value
                       for clause in where['$and'] for key,value in clause.items())
        rows=[(key,*payload) for key,payload in ((key,knowledge.knowledge_payload(record))
              for key,record in self.catalog.items()) if matches(payload[1])][:limit]
        return {'ids':[[row[0] for row in rows]],'documents':[[row[1] for row in rows]],
                'metadatas':[[row[2] for row in rows]]}

    @staticmethod
    def choice(label,labels):
        return {'type':'choice','choice':label,'confidence':1.,
                'probabilities':{name:float(name==label) for name in labels}}

    def judge(self,request):
        body=json.loads(request.content)
        state=body['state']
        if 'excerpts' in state:
            stage='relevance'
            answers={row['excerpt_id']:self.choice(self.current_match_for(row['text']) if row['source_kind']=='knowledge'
                else self.history_match,('relevant','irrelevant','uncertain'))
                for row in state['excerpts']}
            if 'case_resolution' in body['questions']:
                answers['case_resolution']=self.choice(self.history_kind,('clear','ambiguous','insufficient'))
        elif 'cleared_history' in state:
            stage='history_route'
            answers={key:json.loads(json.dumps(self.answers[key])) for key in body['questions']}
            for key in answers:
                if key.startswith('topic_'):
                    answers[key]['noul']=(self.history_topic_score if key.removeprefix('topic_') in self.history_topics else 0.)
            for topic,score in self.history_topic_scores.items():
                answers['topic_'+topic]['noul']=score
            if self.history_company_score is not None:
                answers['company_wide_topic']['noul']=self.history_company_score
        else:
            stage='incoming'
            answers=self.answers
        self.calls.append((stage,body))
        self.model_hook(stage)
        return httpx2.Response(200,json={'model':'offline-jev','usage':{'input_tokens':13,'output_tokens':2},
                                        'answers':answers})

    def review(self,request):
        body=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            self.calls.append(('embedding',body))
            self.model_hook('embedding')
            return httpx2.Response(200,json={'object':'list','model':'text-embedding-3-small',
                'usage':{'prompt_tokens':3,'total_tokens':3},'data':[
                    {'object':'embedding','index':index,'embedding':[1.]+[0.]*1535}
                    for index,_ in enumerate(body['input'])]})
        schema=body['text']['format']['name']
        payload=json.loads(body['input'])
        self.calls.append((schema,body))
        self.model_hook(schema)
        if schema=='IncomingReview':
            reply={'verdict':self.verdict,'attack_types':['prompt_injection'] if self.verdict=='confirmed_attack' else []}
        elif schema=='ExcerptReview':
            reply={'verdicts':[{'excerpt_id':row['excerpt_id'],'verdict':self.history_verdict
                if row['source_kind']=='owned_history' else self.current_verdict} for row in payload]}
        elif schema=='RouterDecision':
            reply={'question':payload['question'],'status':self.routing_status,'intent':self.routing_intent,
                'dependency':self.dependency,'departments':list(self.destinations)}
        elif schema=='CandidateResolution':
            reply=self.candidate_reply(payload) if self.candidate_reply is not None else {'question':payload['question'],
                'outcome':{'status':'resolved','relevant_excerpt_ids':[row['excerpt_id'] for row in payload['candidates']]}
                if self.admit_candidates else {'status':'insufficient'}}
        elif schema=='ProducerReply':
            self.producer_calls.append(payload)
            reply={'part':payload['part'],'kind':'supported_answer','text':self.draft_text,
                'cited_ids':[row['excerpt_id'] for row in payload['evidence'] if row['source_kind']!='preference'],
                'support_assertion':True,'target':None,'reason':None,'unresolved_parts':[],
                'saved_action_claim_ids':[]}
            reply=self.producer_hook(payload,reply)
        else:
            raise AssertionError('Unexpected native operation: '+schema)
        return httpx2.Response(200,json={'id':'resp_private','object':'response','created_at':0,
            'status':'completed','model':body['model'],'usage':{'input_tokens':13,'output_tokens':5,'total_tokens':18},
            'output':[{'id':'msg_private','type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':json.dumps(reply),'annotations':[]}]}]})

    def observe_response(self,stage,response):
        self.usage.append((stage,response.model,response.usage))

    def history(self,*,foreign=False,question=None,refused=False):
        # Use existing exact transcript fixtures in a distinct recalled conversation.
        pending,terminal=transcript_pair(kind='completed_refusal' if refused else 'completed_answer')
        if question is not None:
            pending['documents']=[question]  # Pass 5: the user's own earlier question.
        if refused:
            terminal['documents']=[workflow.REFUSAL_TEXT]  # The fixed refusal text the app saves.
        conversation=str(UUID(int=900))
        for row in (pending,terminal):
            row['metadatas'][0]['conversation_id']=conversation
        if foreign:
            terminal['metadatas'][0]['owner_user_id']='syn-cust-02'
        header=header_result()
        header['ids']=['conversation_header:'+UUID(conversation).hex]
        header['metadatas'][0]['conversation_id']=conversation
        document=json.loads(header['documents'][0])
        document['conversation_id']=conversation
        header['documents']=[json.dumps(document)]
        self.storage.history_rows=terminal
        self.storage.header=lambda *args,**kwargs:header
        self.storage.events=lambda *args,**kwargs:event_result(pending,terminal)
        return conversation

    def continue_admitted(self,*,request_id,**state):
        from shopunow.evidence import ModelBudget
        self.budget=ModelBudget()
        self.attempt=state['attempt']
        def observe(result):
            self.boundaries.append(result)
            self.observer_hook(result)
        try:
            self.result=workflow.run_private_workflow(**state,storage=self.storage,tracking=self.tracking,
                request_id=request_id,evidence_models=self.evidence_models,
                answer_models=self.answer_models,jev_client=self.jev,reviewer_client=self.reviewer,
                jev_model='offline-jev',security_trigger=self.security_trigger,topic_trigger=.5,semantic_confidence_trigger=self.semantic_confidence_trigger,
                model_budget=self.budget,observe_response=self.observe_response,
                observe_evidence=self.evidence_results.append,observe_result=observe,
                utc_now=lambda:datetime(2026,11,16,tzinfo=timezone.utc))
            return self.result  # Only the existing incoming public result/None may escape to admission.
        except Exception as error:
            self.error=error
            raise


# Flow: real admission -> native models/real composition -> controlled external records -> private observations.
