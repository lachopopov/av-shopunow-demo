# Chroma 1.5.9 / Python 3.12; thin immutable operations, existing reader/client ownership.
from functools import partial
from time import monotonic
from uuid import UUID  # Stable existing exact-read IDs, never a new record format.
from shopunow.records import (ProfileRecord, HandoffOffer, PreclearanceTerminal, verify_profile,
                             HISTORY_EVIDENCE_KINDS)
from shopunow.sdk_reads import SDKReads


class SDKRecords(SDKReads):
    def write_offer(self, offer, *, timeout):
        # Revalidate before crossing the native immutable add boundary; no upsert/retry.
        intended = HandoffOffer.model_validate_json(offer.model_dump_json(warnings='error'))
        return self._worker.call(partial(self._escalations.add,
            ids=['offer:' + UUID(intended.offer_id).hex], documents=['handoff_offer'],
            metadatas=[intended.model_dump(mode='json', exclude={'offer_code'})],
            embeddings=[[1.0, 0.0, 0.0]]), timeout=timeout)

    def write_preclearance_marker(self, marker, *, timeout):
        # The native schema forbids question/model text and any normal message sequence.
        intended = PreclearanceTerminal.model_validate_json(marker.model_dump_json(warnings='error'))
        return self._worker.call(partial(self._conversations.add,
            ids=[intended.marker_id], documents=['preclearance_terminal'],
            metadatas=[intended.model_dump(mode='json', exclude={'marker_code'})],
            embeddings=[[1.0, 0.0, 0.0]]), timeout=timeout)

    def write_submission(self, submission, *, timeout):
        # Slice 6: synthetic contact data stays in the document; metadata carries only IDs/labels.
        from shopunow.records import HandoffSubmission
        intended = HandoffSubmission.model_validate_json(submission.model_dump_json(warnings='error'))
        return self._worker.call(partial(self._escalations.add,
            ids=['submission:' + UUID(intended.offer_id).hex], documents=[intended.model_dump_json()],
            metadatas=[{'record_type': 'submission', 'offer_id': intended.offer_id,
                        'owner_user_id': intended.owner_user_id, 'conversation_id': intended.conversation_id}],
            embeddings=[[1.0, 0.0, 0.0]]), timeout=timeout)

    def exact_terminal(self, request_id, *, timeout):
        # Conversations read only; the handoff route checks an offer's link without Knowledge (Slice 6).
        return self._get(self._conversations, ids=['terminal_outcome:' + UUID(request_id).hex], timeout=timeout)

    def exact_submission(self, offer_id, *, timeout):
        return self._get(self._escalations, ids=['submission:' + UUID(offer_id).hex], timeout=timeout)

    def exact_preclearance_marker(self, request_id, *, timeout):
        return self._get(self._conversations,
            ids=['preclearance_terminal:' + UUID(request_id).hex], timeout=timeout)

    def write_header(self, header, *, timeout):
        # Explicit operational vectors prevent implicit embedding generation; all handles use dimension 3.
        return self._worker.call(partial(self._conversations.add,
            ids=['conversation_header:'+header.conversation_id.hex],
            documents=[header.model_dump_json(warnings='error')],
            metadatas=[{'record_type':'conversation_header','owner_user_id':header.owner_user_id,
                        'conversation_id':str(header.conversation_id)}],
            embeddings=[[1.0,0.0,0.0]]),timeout=timeout)

    def initialize_profile(self, profile, *, timeout):
        """Explicit provisioning only: never called by application startup or a route."""
        intended = ProfileRecord.model_validate_json(profile.model_dump_json(warnings='error'))
        key = 'profile:'+intended.user_id
        deadline = monotonic()+min(8.0,timeout)

        def remaining():
            budget=deadline-monotonic()
            if budget<=0:
                raise TimeoutError('Profile initialization deadline expired.')
            return budget

        async def initialize():
            current=verify_profile(await self._profiles.get(ids=[key],
                include=['metadatas','documents']),user_id=intended.user_id)
            remaining()
            if current.record is not None:
                if (current.record.role,current.record.home_department)!=(intended.role,intended.home_department):
                    raise ValueError('Stored profile binding conflicts.')
                return 'existing'  # Existing immutable identity wins; no replacement timestamp/version.
            if current.status!='absent':
                raise ValueError('Profile initialization is unresolved.')
            await self._profiles.add(ids=[key],documents=[intended.model_dump_json(warnings='error')],
                metadatas=[{'record_type':'profile','user_id':intended.user_id}],
                embeddings=[[1.0,0.0,0.0]])
            remaining()
            checked=verify_profile(await self._profiles.get(ids=[key],
                include=['metadatas','documents']),user_id=intended.user_id)
            remaining()
            if checked.record != intended:
                raise ValueError('Intended profile save is unverified.')
            return 'created'

        return self._worker.call(initialize,timeout=remaining())  # One original read/write/read budget.


# Flow: coordinator retains intent -> one native add -> coordinator exact read-back;
# explicit profile provisioner preserves existing bindings and stops on uncertain results.


class SDKEvidenceRecords(SDKRecords):
    """Opt-in fourth collection on the SAME existing worker; no startup initialization."""
    def __init__(self,worker,*,profiles,conversations,escalations,knowledge):
        super().__init__(worker,profiles=profiles,conversations=conversations,escalations=escalations)
        self._knowledge_collection=knowledge

    def knowledge(self,vector,*,where,limit,timeout):
        self._check_knowledge_configuration()
        operation=partial(self._knowledge_collection.query,query_embeddings=[vector],
            where=where,n_results=limit,include=['metadatas','documents'])
        return self._worker.call(operation,timeout=timeout)

    def history_page(self,user_id,*,offset,timeout):
        return self._get(self._conversations,timeout=timeout,offset=offset,limit=100,
            where={'$and':[{'owner_user_id':user_id},{'record_type':'terminal_outcome'},
                          {'kind':{'$in':list(HISTORY_EVIDENCE_KINDS)}},{'complete':True}]})

    def write_pending(self,pending,*,timeout):
        from shopunow.records import PendingMessage
        intended=PendingMessage.model_validate_json(pending.model_dump_json(warnings='error'))
        return self._worker.call(partial(self._conversations.add,ids=[intended.message_id],
            documents=[intended.text],metadatas=[intended.model_dump(mode='json',exclude={'text'})],
            embeddings=[[1.0,0.0,0.0]]),timeout=timeout)

    def exact_pending(self,request_id,*,timeout):
        return self._get(self._conversations,ids=['pending_message:'+UUID(request_id).hex],timeout=timeout)

    def write_terminal(self,terminal,*,timeout):
        # Slice 5: same mapping as the pending event; text only in the native document, never metadata.
        from shopunow.records import TerminalOutcome
        intended=TerminalOutcome.model_validate_json(terminal.model_dump_json(warnings='error'))
        return self._worker.call(partial(self._conversations.add,ids=[intended.outcome_id],
            documents=[intended.approved_text_or_code],
            # Matches verify_terminal_outcome: citations as a native list (omitted when empty), offer ID optional.
            metadatas=[intended.model_dump(mode='json',exclude={'approved_text_or_code','cleared_citation_ids',
                                                                 'verified_offer_id'})
                       |({'cleared_citation_ids':list(intended.cleared_citation_ids)}
                         if intended.cleared_citation_ids else {})
                       |({'verified_offer_id':intended.verified_offer_id} if intended.verified_offer_id else {})],
            embeddings=[[1.0,0.0,0.0]]),timeout=timeout)

    def exact_knowledge(self,source_id,*,timeout):
        self._check_knowledge_configuration()
        return self._get(self._knowledge_collection,ids=[source_id],timeout=timeout)

    def write_knowledge(self,record,vector,*,timeout):
        self._check_knowledge_configuration()
        from shopunow.knowledge import knowledge_payload
        document,metadata=knowledge_payload(record)
        return self._worker.call(partial(self._knowledge_collection.add,ids=[record.source_id],
            documents=[document],metadatas=[metadata],embeddings=[vector]),timeout=timeout)

    def _check_knowledge_configuration(self):
        metadata=self._knowledge_collection.metadata or {}
        if (metadata.get('embedding_model')!='text-embedding-3-small' or
                type(metadata.get('embedding_dimensions')) is not int or
                metadata['embedding_dimensions']!=1536):
            raise ValueError('Knowledge collection embedding identity is not verified.')


# Flow: admitted request -> existing worker -> explicit native query/get/add -> independent verifiers.
