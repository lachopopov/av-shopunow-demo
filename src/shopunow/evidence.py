# Python 3.12 / Pydantic 2.14.1; internal post-clearance evidence, never a delivered answer.
from dataclasses import dataclass, replace
from threading import BoundedSemaphore, Lock
from uuid import UUID, uuid4
from types import MappingProxyType
from typing import Literal, Mapping
from typesafe_sdk import ChoiceAnswer  # Preserve provider-native judgments, not a parallel probability schema.
from shopunow.incoming import semantic_confident
from shopunow.knowledge import knowledge_filter, verify_knowledge
from shopunow.records import (PendingMessage, verify_pending_message, verify_terminal_outcome,
    verify_complete_transcript, verify_transcript_events)
from shopunow.records import verify_owned_conversation_header, HISTORY_EVIDENCE_KINDS


_MODEL_SLOTS = BoundedSemaphore(5)  # One process-wide bound, shared by all participating requests.


class ModelBudget:
    """Count actual attempts, including failed attempts, without resetting the request deadline."""
    def __init__(self, *, attempts=0):
        if type(attempts) is not int or not 0 <= attempts <= 40:
            raise ValueError('Invalid inherited model-attempt count.')
        self.attempts, self._lock = attempts, Lock()

    def call(self, operation, *, timeout):
        # timeout is a guarded function, so waits never reuse old authorization.
        timeout()
        if not _MODEL_SLOTS.acquire(blocking=False):
            raise TimeoutError('Model capacity unavailable.')
        try:
            with self._lock:
                if self.attempts >= 40:
                    raise TimeoutError('Model-attempt budget exhausted.')
                remaining = timeout()
                self.attempts += 1
            result = operation(min(20.0,remaining))  # Approved per-call cap inside the original deadline.
            timeout()  # Late results cannot become usable evidence.
            return result
        finally:
            _MODEL_SLOTS.release()


@dataclass(frozen=True)
class EvidenceCandidate:
    excerpt_id: str
    source_kind: str
    source_record_id: str
    source_version: str | None
    text: str
    destination: str
    owner_user_id: str | None = None
    conversation_id: str | None = None
    clearance: str = 'pending'
    audience: tuple[str, ...] = ()
    department_or_company_wide: str | None = None
    classification: str | None = None
    approval_current: bool | None = None
    returned_metadata: Mapping | None = None
    retrieval_scope: Mapping | None = None
    relevance_basis: str = 'pending'


@dataclass(frozen=True)
class PartDisposition:
    # Code-owned outcomes survive synthesis; a model's empty unresolved list cannot erase them.
    part: str
    kind: Literal['supported_answer','clarification_needed','handoff_needed','insufficient_evidence',
                  'technical_failure','refusal','security_fallback','unavailable','incomplete_at_cutoff']


@dataclass(frozen=True)
class EvidenceResult:
    kind: str
    cleared: tuple[EvidenceCandidate, ...] = ()
    unresolved_parts: tuple[str, ...] = ()
    pending_message_id: str | None = None
    response_style: str | None = None
    security_fallback_parts: tuple[str, ...] = ()
    refused_parts: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()  # Validated producer destinations, finalized after case discovery if needed.
    resolution_candidates: tuple[EvidenceCandidate, ...] = ()  # Clean and permitted, not admitted answer facts.
    judgments: Mapping | None = None  # Native answers keyed by their stage and target.
    unresolved_judgments: tuple[str, ...] = ()
    dependency: str = 'unclear'  # Downstream partial-answer policy must not reconstruct this from labels.
    requires_action_handoff: bool = False  # Preserve a mixed request's unsupported action for assembly.
    dispositions: tuple[PartDisposition, ...] = ()  # Content-free historical/access outcomes, not answer facts.


def _rows(raw, *, query=False):
    # Preserve native IDs/documents/metadata, rejecting malformed/unequal batches.
    if not isinstance(raw, dict):
        raise ValueError('Invalid retrieval response.')
    values = [raw.get(key) for key in ('ids','documents','metadatas')]
    if query:
        if any(type(value) is not list or len(value) != 1 for value in values):
            raise ValueError('Invalid query batch.')
        values = [value[0] for value in values]
    if any(type(value) is not list for value in values) or len({len(v) for v in values}) != 1:
        raise ValueError('Incomplete retrieval rows.')
    if any(type(key) is not str for key in values[0]) or len(set(values[0])) != len(values[0]):
        raise ValueError('Invalid retrieval IDs.')
    return list(zip(*values, strict=True))


class EvidenceService:
    """Caller owns admitted lease/clients; only an actual clear routing continuation invokes prepare.

    The result is an internal producer-input boundary. No HTTP success, terminal write,
    producer generation, or live enablement is supplied by this component.
    """
    def __init__(self, *, storage, models, attempt, tracking, request, profile,
                 transcript, request_id, created_at, budget, uuid_factory=uuid4,topic_trigger=None,
                 semantic_confidence_trigger=None,topic_confidence_trigger=None,history_identity_resolver=None,
                 answer_models=None,
                 routing_models=None,candidate_models=None,on_progress=None):
        if (tracking is not attempt._hold_tracking or request.user_id != profile.user_id
                or str(UUID(request.conversation_id)) != attempt._hold_id):
            raise ValueError('Evidence must retain admitted identity and tracking.')
        self.storage, self.models = storage, models
        self.attempt, self.tracking, self.request, self.profile = attempt, tracking, request, profile
        self.transcript, self.request_id, self.created_at = transcript, str(UUID(request_id)), created_at
        self.budget, self.uuid_factory = budget, uuid_factory
        self.topic_trigger=topic_trigger  # Explicit incoming configuration; no new default threshold.
        self.semantic_confidence_trigger=semantic_confidence_trigger
        self.topic_confidence_trigger=topic_confidence_trigger  # Topic bar (plan 4f), from wiring.py.
        self.history_identity_resolver=history_identity_resolver  # Explicit opt-in; normal serving stays unchanged.
        self._identity_attempted=False  # Shared across initial and supplementary searches in this request.
        self._identity_selected_ids=set()  # New classifier selections still need exact historical access.
        self.searches, self.pending, self._ids = 0, None, set()
        self._supplemented = False
        self._selected, self._reviewed = [], 0
        self._relevance_unresolved=set()  # Matching uncertainty never becomes access permission or factual support.
        self._judgments,self._unresolved_judgments={},[]
        self._resolution_candidates=[]
        self._pending_destinations=set()
        self._history_dispositions=[]  # Keep initial and supplementary outcomes; a later lookup cannot erase one.
        self._history_access_ids=set()  # Permission applies to exact reviewed records, not a later history search.
        self._malicious_history_ids=set()  # Only successful per-ID review can establish this exclusion.
        self.answer_models=answer_models  # Explicit opt-in; no normal runtime model is enabled.
        self.routing_models=routing_models  # Routing tie-breaker model (owner 2026-11-23); None: answer models.
        self.candidate_models=candidate_models  # Excerpt keep/drop model (owner 2026-11-24); None: answer models.
        self.on_progress=on_progress  # UI progress step callback (owner 2026-11-23); None: not reported.
        self._routing_attempted=self._candidate_attempted=False
        self._work_timeout=None  # The serialized continuation may impose the earlier specialist cutoff.

    def _adjudicate_route(self,decision):
        """Resolve semantics once; code retains access, source disposition and dispatch authority."""
        from shopunow.model_clients import RouterDecision
        if self.answer_models is None or self._routing_attempted:
            return decision
        self._routing_attempted=True  # Invalid/failed responses consume this opportunity too.
        # D5 (owner 2026-11-23): the question, role and home department only; never entries, history or scores.
        payload={'question':self.request.question,'role':self.profile.role,
                 'home_department':self.profile.home_department}
        raw=self._model((self.routing_models or self.answer_models).resolve_routing,payload)
        resolved=RouterDecision.model_validate(raw.model_dump() if hasattr(raw,'model_dump') else raw)
        if (resolved.question!=self.request.question
                or len(resolved.departments)!=len(set(resolved.departments))):
            raise ValueError('Unbound routing adjudication.')
        self._judgments['semantic.routing']=resolved
        if resolved.status!='resolved':
            return replace(decision,kind='clarification_needed')  # The question could mean different things.
        # Nullable values never replace an unresolved judgment with a convenient default.
        intent_unresolved=('incoming.intent' in self._unresolved_judgments or decision.kind=='semantic_resolution')
        dependency_unresolved=any(key.endswith('.department_dependency') for key in self._unresolved_judgments)
        intent=((resolved.intent or 'unclear_or_other') if intent_unresolved else decision.intent)
        dependency=((resolved.dependency or 'unclear') if dependency_unresolved else decision.dependency)
        self.dependency=dependency  # Partial adjudication must not expose an unresolved winning label.
        if intent_unresolved and resolved.intent is not None:
            self._unresolved_judgments=[key for key in self._unresolved_judgments if key!='incoming.intent']
        if dependency_unresolved and resolved.dependency is not None:
            self._unresolved_judgments=[key for key in self._unresolved_judgments
                                       if not key.endswith('.department_dependency')]
        proposed=[]
        for part in resolved.departments:
            key='incoming.'+('company_wide_topic' if part=='central_support' else 'topic_'+part)
            answer=self._judgments.get(key)
            if (answer is None or key in self._unresolved_judgments or
                    (self.topic_trigger is not None and answer.noul>=self.topic_trigger)):
                proposed.append(part)
        destinations=tuple(dict.fromkeys((*decision.destinations,*proposed)))
        refused=tuple(dict.fromkeys((*decision.refused_departments,*(part for part in destinations
            if self.profile.role=='customer' and part in ('hr','it_support','facilities_admin')))))
        self.refused_parts=refused  # Early action/ambiguity exits must retain code-owned access dispositions.
        permitted=tuple(part for part in destinations if part not in refused)
        for part in destinations:
            suffix='company_wide_topic' if part=='central_support' else 'topic_'+part
            self._unresolved_judgments=[key for key in self._unresolved_judgments
                                       if not key.endswith('.'+suffix)]
        if not any(key.startswith('incoming.') and not (key.startswith('incoming.topic_') or
                key=='incoming.company_wide_topic') for key in self._unresolved_judgments):
            self._pending_destinations.difference_update(permitted)
        # Only an explicit information result with a usable route clears a low-confidence action label.
        weak_action=decision.intent=='individual_case_or_action' and intent_unresolved
        cleared_action=weak_action and intent=='information' and bool(permitted)
        self._action_required=((decision.requires_action_handoff and not cleared_action)
                               or intent in ('mixed','individual_case_or_action'))
        kind=('refusal' if refused and (not permitted or dependency!='independent') else
              'handoff_needed' if intent=='individual_case_or_action' or (weak_action and not cleared_action
                                                                         and intent=='information') else
              'clarification_needed' if intent=='unclear_or_other' else
              'continue' if permitted else 'insufficient_evidence')
        if kind=='insufficient_evidence' and not refused and intent=='information':
            kind,permitted='continue',('central_support',)  # Owner 2026-11-23: central support owns it.
        return replace(decision,kind=kind,destinations=permitted,refused_departments=refused,
            intent=intent,dependency=dependency,proposed_destinations=permitted,
            requires_action_handoff=self._action_required)

    def _adjudicate_candidates(self,result):
        """Promote only exact, security-cleared, access-verified relevant candidate IDs."""
        from shopunow.model_clients import CandidateResolution
        if (self.answer_models is None or self._candidate_attempted or not result.resolution_candidates
                or result.kind not in ('ready','insufficient_evidence','clarification_needed')):
            return result
        candidates=tuple(row for row in result.resolution_candidates if row.clearance=='clean'
            and (row.destination in self.destinations or row.source_kind=='owned_history')
            and (row.source_kind!='owned_history' or row.source_record_id in self._history_access_ids)
            and row.destination not in self._pending_destinations)
        if not candidates or 'incoming.intent' in self._unresolved_judgments:
            return result
        self._candidate_attempted=True
        payload={'question':self.request.question,'destinations':list(self.destinations),
            'candidates':[{'excerpt_id':row.excerpt_id,'text':row.text,'source_kind':row.source_kind,
                'source_record_id':row.source_record_id,'source_version':row.source_version,
                'destination':row.destination,'owner_user_id':row.owner_user_id,
                'conversation_id':row.conversation_id,'audience':list(row.audience),
                'department_or_company_wide':row.department_or_company_wide,
                'classification':row.classification,'approval_current':row.approval_current,
                'clearance':row.clearance,'relevance_basis':row.relevance_basis,
                # Detach existing verified mappings into JSON-compatible native input, without new authority.
                'returned_metadata':dict(row.returned_metadata or {}),
                'retrieval_scope':dict(row.retrieval_scope or {})} for row in candidates]}
        raw=self._model((self.candidate_models or self.answer_models).resolve_candidates,payload)
        resolved=CandidateResolution.model_validate(raw.model_dump() if hasattr(raw,'model_dump') else raw)
        status=resolved.outcome.status
        ids=resolved.outcome.relevant_excerpt_ids if status=='resolved' else []
        if (resolved.question!=self.request.question or len(ids)!=len(set(ids))
                or not set(ids)<={row.excerpt_id for row in candidates}):
            raise ValueError('Unbound candidate adjudication.')
        self._judgments['semantic.candidates']=resolved
        if status!='resolved':
            return replace(result,kind='clarification_needed' if status=='ambiguous' else result.kind)
        decided={row.excerpt_id for row in candidates}
        promoted=[replace(row,relevance_basis='semantic_adjudication') for row in candidates if row.excerpt_id in ids]
        self._selected.extend(promoted)
        self._resolution_candidates=[row for row in self._resolution_candidates if row.excerpt_id not in decided]
        self._unresolved_judgments=[key for key in self._unresolved_judgments
            if key not in {'knowledge.'+identity for identity in decided}]
        settled={row.destination for row in candidates} - {
            row.destination for row in self._resolution_candidates}
        self._relevance_unresolved.difference_update(settled)
        factual=any(row.source_kind in ('knowledge','owned_history') for row in self._selected)
        return replace(result,kind='ready' if factual and result.kind!='clarification_needed' else result.kind,
            cleared=tuple(self._selected),resolution_candidates=tuple(self._resolution_candidates),
            unresolved_parts=tuple(part for part in result.unresolved_parts if part not in settled))

    def timeout(self):
        remaining = self.attempt.normal_work_timeout()
        if remaining is None:
            raise TimeoutError('Evidence request stopped.')
        if self._work_timeout is not None:
            remaining=min(remaining,self._work_timeout())
            if remaining<=0:
                raise TimeoutError('Evidence continuation cutoff reached.')
        return remaining

    def _read(self, operation, *args, **kwargs):
        result = operation(*args, timeout=min(8.0,self.timeout()), **kwargs)
        self.timeout()
        return result

    def _event(self,outcome,stage):
        from shopunow.api import question_event
        question_event(self.request_id,outcome,stage=stage)  # Fixed code plus opaque ID only.

    def _model(self, operation, *args,**kwargs):
        return self.budget.call(lambda remaining:operation(*args,timeout=remaining,**kwargs), timeout=self.timeout)

    def _matching(self,target,candidates,stage):
        """Apply commitment policy while retaining the native answers for a later consumer."""
        answers=self._model(self.models.relevance,target,tuple(candidates))
        expected={row.excerpt_id for row in candidates}
        history=any(row.source_kind=='owned_history' for row in candidates)
        if set(answers)!=expected|({'case_resolution'} if history else set()):
            raise ValueError('Incomplete relevance judgments.')
        selected,uncertain=[],[]
        kind=None
        for key,answer in answers.items():
            if not isinstance(answer,ChoiceAnswer):
                raise ValueError('Relevance requires native validated Choice answers.')
            identity=stage+'.'+key
            self._judgments[identity]=answer
            confident=semantic_confident(answer,self.semantic_confidence_trigger)
            if not confident or answer.choice=='uncertain':
                self._unresolved_judgments.append(identity)
            if key=='case_resolution':
                kind=answer.choice if confident else 'uncertain'
                if kind!='clear' and identity not in self._unresolved_judgments:
                    self._unresolved_judgments.append(identity)
            elif confident and answer.choice=='relevant':
                selected.append(key)
            elif not confident or answer.choice=='uncertain':
                # Keep the whole distribution; tied third options cannot silently disappear.
                ranked=sorted(answer.probabilities.values(),reverse=True)
                if answer.choice=='uncertain' or answer.probabilities['relevant']>=ranked[1]:
                    uncertain.append(key)
        return kind or ('clear' if selected else 'insufficient'),tuple(selected),tuple(uncertain)

    def _candidate(self, *, kind, key, text, destination, version=None, conversation=None,
                   metadata=None,scope=None):
        # IDs are issued by code and never taken from retrieved text/model output.
        identity = 'e' + str(len(self._ids)+1)
        self._ids.add(identity)
        labels={} if metadata is None else metadata
        # Freeze detached native labels; array-valued audience metadata cannot mutate clearance.
        detached={key:tuple(value) if isinstance(value,list) else value for key,value in labels.items()}
        return EvidenceCandidate(identity,kind,key,version,text,destination,
            self.profile.user_id if kind != 'knowledge' else None,conversation,
            audience=tuple(labels.get('eligible_audience',())),
            department_or_company_wide=labels.get('subject'),classification=labels.get('classification'),
            approval_current=True if kind=='knowledge' else None,
            returned_metadata=MappingProxyType(detached),
            retrieval_scope=MappingProxyType({} if scope is None else dict(scope)))

    def save_pending(self):
        """Slice 5: save and exact-verify the question for a gate outcome that ran no evidence stage."""
        self._save_pending()
        return self.pending

    def _save_pending(self):
        if self.pending is not None:
            raise ValueError('Pending intent cannot be reused.')
        if not self.transcript.has_two_event_room:
            raise ValueError('No reserved event room.')
        # Markers have no normal sequence: only prior complete normal pairs consume positions.
        markers=sum(notice.reason_code!='technical_error' for notice in self.transcript.notices)
        sequence = self.transcript.event_count-markers+1  # Failed technical pairs still occupy two positions.
        pending = PendingMessage.model_validate(dict(record_type='pending_message',
            message_id='pending_message:'+UUID(self.request_id).hex, request_id=self.request_id,
            owner_user_id=self.profile.user_id, conversation_id=str(UUID(self.request.conversation_id)),
            sequence=sequence,speaker='user',text=self.request.question,status='pending',
            complete=True,created_at=self.created_at))
        self.timeout()
        self.tracking.retain_pending(pending)  # Retain exact immutable intent before writer entry.
        self.timeout()
        started = False
        try:
            started = True
            self._read(self.storage.write_pending, pending)
            raw = self._read(self.storage.exact_pending,self.request_id)
            checked = verify_pending_message(raw,user_id=self.profile.user_id,
                conversation_id=self.request.conversation_id,request_id=self.request_id,sequence=sequence)
            if checked.record != pending:
                raise ValueError('Intended pending save is unverified.')
            self.timeout()
            self.pending = pending
            self._event('verified','pending_write')
        finally:
            if started and self.pending is None:
                self.tracking.exclude_conversation(self.request.conversation_id)
                self._event('unverified','pending_write')

    def _knowledge(self, destinations):
        if self.on_progress is not None:
            self.on_progress(3)  # Progress step 3: looking up information.
        candidates = []
        if not destinations:
            return candidates
        vector = self._model(self.models.embed,[self.request.question])[0]
        for destination in destinations:  # Owner 2026-11-23: the whole department, filtered by code.
            where = knowledge_filter(role=self.profile.role,destination=destination)
            raw = self._read(self.storage.knowledge,vector,where=where,limit=4)
            rows = _rows(raw,query=True)
            if len(rows)>4:
                raise ValueError('Knowledge response exceeds requested bound.')
            for key,text,metadata in rows:
                record = verify_knowledge(key,text,metadata,role=self.profile.role,
                    department=self.profile.home_department,destination=destination)
                if record is not None:
                    candidates.append(self._candidate(kind='knowledge',key=key,text=text,
                        destination=destination,version=record.version,metadata=metadata,
                        scope={'role':self.profile.role,'destination':destination}))
        return candidates

    def _history_transcript(self, conversation_id):
        if self.tracking.excludes((conversation_id,)):
            raise ValueError('Recalled conversation is unresolved.')
        header = self._read(self.storage.header,conversation_id)
        owned=verify_owned_conversation_header(header,user_id=self.profile.user_id,
                                               conversation_id=conversation_id)
        if owned.record is None:
            raise ValueError('History conversation ownership is unverified.')
        events = self._read(self.storage.events,conversation_id,limit=51)
        if len(_rows(events))>50:
            raise ValueError('History event cap exceeded.')
        if conversation_id == str(UUID(self.request.conversation_id)):
            # This request's exact-verified pending is not eligible history. Remove only that exact row.
            rows = _rows(events)
            for index,(key,text,metadata) in enumerate(rows):
                if key == self.pending.message_id:
                    checked=verify_pending_message({'ids':[key],'documents':[text],'metadatas':[metadata]},
                        user_id=self.profile.user_id,conversation_id=conversation_id,
                        request_id=self.request_id,sequence=self.pending.sequence)
                    if checked.record != self.pending:
                        raise ValueError('Active pending changed.')
                    events={name:[value for i,value in enumerate(events[name]) if i!=index]
                            for name in ('ids','documents','metadatas')}
                    break
        checked=verify_transcript_events(events,user_id=self.profile.user_id,conversation_id=conversation_id)
        if checked.record is None:
            raise ValueError('Incomplete history prefix.')
        offers={key:self._read(self.storage.exact_offer,key) for key in checked.record.offer_ids}
        verified=verify_complete_transcript(header,events,user_id=self.profile.user_id,
            conversation_id=conversation_id,offer_results=offers)
        if verified.record is None:
            raise ValueError('Owned history cannot be reconstructed.')
        if self.tracking.excludes((conversation_id,)):
            raise ValueError('Recalled conversation became unresolved.')
        return verified.record

    def _history(self, target):
        # Record at the owning lookup boundary, including ordinary and supplementary entry points.
        try:
            kind,candidates=self._history_candidates(target)
        except Exception:
            self._history_dispositions.append(PartDisposition('history','technical_failure'))
            raise
        if kind!='clear' or not candidates:
            outcome='clarification_needed' if kind=='ambiguous' else (
                'unavailable' if kind=='clear' else 'insufficient_evidence')
            self._history_dispositions.append(PartDisposition('history',outcome))
        return kind,candidates

    def _history_candidates(self, target):
        if self.searches >= 1:
            raise ValueError('History-search budget exhausted.')  # One search per request (owner 2026-11-23).
        self.searches += 1
        offset, seen, transcripts, candidates = 0,set(),{},[]
        while True:
            rows = _rows(self._read(self.storage.history_page,self.profile.user_id,offset=offset))
            if not rows:
                break  # Only an exhausted scan supports a complete candidate pool.
            if len(rows)>100:
                raise ValueError('History page exceeds native requested bound.')
            for key,text,metadata in rows:
                if key in seen or type(metadata) is not dict:
                    raise ValueError('Invalid history candidate.')
                seen.add(key)
                conversation=str(UUID(metadata['conversation_id']))
                checked=verify_terminal_outcome({'ids':[key],'documents':[text],'metadatas':[metadata]},
                    user_id=self.profile.user_id,conversation_id=conversation,
                    request_id=metadata['request_id'],sequence=metadata['sequence'])
                if checked.record is None or checked.record.kind not in HISTORY_EVIDENCE_KINDS:
                    raise ValueError('Ineligible or foreign completed-history candidate.')
                if conversation not in transcripts:
                    transcripts[conversation]=self._history_transcript(conversation)
                exchange=next((item.exchange for item in transcripts[conversation].exchanges
                    if item.exchange.terminal==checked.record),None)
                if exchange is None:
                    raise ValueError('History candidate has no verified complete exchange.')
                candidates.append(self._candidate(kind='owned_history',key=key,
                    text='User: '+exchange.pending.text+'\nAssistant: '+exchange.terminal.approved_text_or_code,
                    destination='history',conversation=conversation,metadata=metadata,
                    scope={'owner_user_id':self.profile.user_id,'kind':checked.record.kind}))
            offset += len(rows)  # A short page is not proof that no older case exists.
        if not candidates:
            return 'clear',[]
        kind,selected,uncertain = self._matching(target,candidates,f'history_{self.searches}')
        if (kind=='uncertain' and self.history_identity_resolver is not None and not self._identity_attempted):
            from shopunow.model_clients import HistoryIdentityResolution
            self._identity_attempted=True  # Failed/invalid calls cannot create a second adjudication.
            raw=self._model(self.history_identity_resolver,target,tuple(candidates))
            resolved=HistoryIdentityResolution.model_validate(
                raw.model_dump() if isinstance(raw,HistoryIdentityResolution) else raw,strict=True)
            ids=resolved.selected_excerpt_ids
            if (len(ids)!=len(set(ids)) or not set(ids)<={row.excerpt_id for row in candidates}
                    or (resolved.status=='clear')!=bool(ids)):
                raise ValueError('Invalid contributing historical exchange selection.')
            stage=f'history_{self.searches}'
            self._judgments[stage+'.identity_resolution']=resolved
            kind,selected,uncertain=resolved.status,tuple(ids),()
            if kind=='clear':
                self._identity_selected_ids.update(ids)
                replaced={stage+'.case_resolution',*(stage+'.'+identity for identity in ids)}
                self._unresolved_judgments=[key for key in self._unresolved_judgments if key not in replaced]
        chosen=self._select(candidates,selected,uncertain)
        if kind not in ('clear','ambiguous','insufficient','uncertain'):
            raise ValueError('Invalid history matching result.')
        if uncertain and kind=='clear':
            if self.semantic_confidence_trigger is None:
                return 'uncertain',[]  # Preserve the frozen explicit-uncertainty policy when unconfigured.
            # Identity is settled, but candidate contribution still needs semantic resolution.
            chosen.extend(replace(row,relevance_basis='semantic_resolution') for row in candidates
                          if row.excerpt_id in uncertain)
        return kind,chosen if kind=='clear' else []

    @staticmethod
    def _select(candidates, selected, uncertain=()):
        if (type(selected) not in (tuple,list) or len(selected)!=len(set(selected))
                or type(uncertain) not in (tuple,list) or len(uncertain)!=len(set(uncertain))
                or set(selected)&set(uncertain)
                or not (set(selected)|set(uncertain))<= {row.excerpt_id for row in candidates}):
            raise ValueError('Invalid relevance selection.')
        chosen=set(selected)
        return [replace(row,relevance_basis='typed_current_request') for row in candidates if row.excerpt_id in chosen]

    def _clear(self, candidates, destinations):
        retained=self._selected+self._resolution_candidates
        recalled=tuple(row.conversation_id for row in candidates+retained
                       if row.source_kind=='owned_history')
        if recalled and self.tracking.excludes(recalled):
            self._history_dispositions.append(PartDisposition('history','technical_failure'))
            raise ValueError('Recalled evidence is held.')
        if len(candidates)+len(retained)>20:
            return EvidenceResult('insufficient_evidence',unresolved_parts=tuple(destinations))
        for part in destinations:
            if sum(row.destination==part and row.source_kind=='knowledge' for row in retained+candidates)>4:
                return EvidenceResult('insufficient_evidence',unresolved_parts=tuple(destinations))
        clean, uncertain = [],set()
        # Across initial and supplementary work, retain a total of at most two ten-ID review batches.
        for start in range(0,len(candidates),10):
            if self._reviewed>=2:
                if any(row.source_kind=='owned_history' for row in candidates[start:]):
                    self._history_dispositions.append(PartDisposition('history','security_fallback'))
                return EvidenceResult('security_fallback',unresolved_parts=tuple(destinations))
            batch=candidates[start:start+10]
            self._reviewed+=1
            try:
                verdicts=self._model(self.models.review,tuple(batch))
                expected={row.excerpt_id for row in batch}
                if (type(verdicts) is not list or len(verdicts)!=len(expected) or
                    any(type(v) is not dict or set(v)!={'excerpt_id','verdict'} or
                        v['verdict'] not in ('clean','malicious','uncertain') for v in verdicts) or
                    {v['excerpt_id'] for v in verdicts}!=expected):
                    raise ValueError('Invalid per-ID verdict mapping.')
                mapping={v['excerpt_id']:v['verdict'] for v in verdicts}
            except Exception:
                if any(row.source_kind=='owned_history' for row in batch):
                    self._history_dispositions.append(PartDisposition('history','security_fallback'))
                return EvidenceResult('security_fallback',unresolved_parts=tuple(destinations))
            clean.extend(replace(row,clearance='clean') for row in batch if mapping[row.excerpt_id]=='clean')
            self._malicious_history_ids.update(row.excerpt_id for row in batch
                if row.source_kind=='owned_history' and mapping[row.excerpt_id]=='malicious')
            if any(row.source_kind=='owned_history' and mapping[row.excerpt_id]=='uncertain' for row in batch):
                self._history_dispositions.append(PartDisposition('history','security_fallback'))
            uncertain.update(row.destination for row in batch if mapping[row.excerpt_id]=='uncertain')
        historical=[row for row in candidates if row.source_kind=='owned_history']
        if historical and all(row.excerpt_id in self._malicious_history_ids for row in historical):
            self._history_dispositions.append(PartDisposition('history','unavailable'))
        affected_parts=tuple(part for part in destinations if part in uncertain or
                             'history' in uncertain or 'preference' in uncertain)
        # Keep supported independent parts available; uncertain parts cannot receive answer context.
        affected=set(affected_parts)
        shared_uncertain='history' in uncertain or 'preference' in uncertain
        clean=[row for row in retained+clean if row.destination not in affected
               and not (shared_uncertain and row.destination in ('history','preference'))]
        if 'incoming.intent' in self._unresolved_judgments:
            # Identifying a case/topic cannot establish an informational rather than action request.
            clean=[replace(row,relevance_basis='semantic_resolution') if row.source_kind in (
                'knowledge','owned_history') else row for row in clean]
        self._resolution_candidates=[row for row in clean if row.relevance_basis=='semantic_resolution']
        self._selected=[row for row in clean if row.relevance_basis!='semantic_resolution']
        unresolved=tuple(part for part in destinations if not any(
            row.destination in (part,'history') for row in self._selected))
        # This is availability, not semantic sufficiency: only later producers can assert full support.
        unresolved=tuple(dict.fromkeys((*unresolved,*(part for part in destinations
            if part in uncertain or 'history' in uncertain or 'preference' in uncertain))))
        factual=any(row.source_kind in ('knowledge','owned_history') for row in self._selected)
        blocked=bool(affected_parts) and (not factual or self.dependency!='independent')
        kind='security_fallback' if blocked else 'ready' if factual else 'insufficient_evidence'
        self.timeout()
        recalled=tuple(row.conversation_id for row in clean if row.source_kind=='owned_history')
        if recalled and self.tracking.excludes(recalled):
            self._history_dispositions.append(PartDisposition('history','technical_failure'))
            raise ValueError('Recalled evidence became unresolved during review.')
        return EvidenceResult(kind,tuple(self._selected),unresolved,self.pending.message_id,
                              self.request.response_style,affected_parts,
                              getattr(self,'refused_parts',()))

    def _with_dispositions(self, result, destinations):
        # Every exit retains established part outcomes and the already verified pending link.
        refused=tuple(dict.fromkeys((*getattr(self,'refused_parts',()),*result.refused_parts)))
        unresolved=tuple(dict.fromkeys((*result.unresolved_parts,*sorted(self._relevance_unresolved))))
        kind=result.kind
        factual=any(row.source_kind in ('knowledge','owned_history') for row in result.cleared)
        if (kind=='insufficient_evidence' and not factual and destinations
                and not self._relevance_unresolved and set(destinations)<=set(refused)):
            # Do not hide an unsupported independent destination behind another part's refusal.
            kind='refusal'
        pending=result.pending_message_id or (None if self.pending is None else self.pending.message_id)
        candidates=tuple(row for row in self._resolution_candidates
                         if row.source_kind!='owned_history' or row.source_record_id in self._history_access_ids) if kind in (
            'ready','insufficient_evidence','clarification_needed') else ()
        return replace(result,kind=kind,refused_parts=refused,
                       unresolved_parts=unresolved,pending_message_id=pending,
                       resolution_candidates=candidates,judgments=MappingProxyType(dict(self._judgments)),
                       unresolved_judgments=tuple(dict.fromkeys(self._unresolved_judgments)),
                       dispositions=tuple(dict.fromkeys((*result.dispositions,*self._history_dispositions,
                           *(PartDisposition(part,'refusal') for part in refused),
                           *(PartDisposition(part,'security_fallback') for part in result.security_fallback_parts),
                           *((PartDisposition('action','handoff_needed'),) if
                             getattr(self,'_action_required',False) else ())))))

    def prepare(self, decision):
        # Routing dispositions already exist even if pending persistence or classification fails.
        self.refused_parts=decision.refused_departments
        self._action_required=decision.requires_action_handoff
        result=self._prepare(decision)
        destinations=getattr(self,'destinations',decision.destinations)
        result=replace(self._with_dispositions(result,destinations),destinations=destinations,
                       dependency=getattr(self,'dependency',decision.dependency),
                       requires_action_handoff=self._action_required)
        try:
            result=self._adjudicate_candidates(result)
            result=replace(self._with_dispositions(result,destinations),destinations=destinations)
        except Exception:
            # Failure withholds only unconfirmed candidates; evidence already admitted remains usable.
            factual=any(row.source_kind in ('knowledge','owned_history') for row in result.cleared)
            withheld=result.resolution_candidates
            history_failed=any(row.source_kind=='owned_history' for row in withheld)
            result=replace(result,kind=result.kind if factual else 'technical_failure',
                cleared=result.cleared if factual else (),resolution_candidates=(),
                unresolved_parts=tuple(dict.fromkeys((*result.unresolved_parts,*(row.destination for row in withheld)))),
                dispositions=tuple(dict.fromkeys((*result.dispositions,*((PartDisposition('history','technical_failure'),)
                    if history_failed else ())))))
        allowed={'ready','refusal','handoff','handoff_needed','clarification','clarification_needed',
                 'insufficient_evidence','security_fallback','technical_failure','confirmed_attack',
                 'security_uncertain','dependency_failure','disabled','stopped'}
        self._event(result.kind if result.kind in allowed else 'technical_failure','evidence_preparation')
        return result

    def _prepare(self, decision):
        if decision.kind not in ('continue','semantic_resolution'):
            return EvidenceResult(decision.kind)
        if not self.attempt.authorize_normal_work():
            return EvidenceResult('stopped')
        try:
            if decision.judgments is not None:
                self._judgments.update(('incoming.'+key,value) for key,value in decision.judgments.items())
                if self.semantic_confidence_trigger is not None:
                    for key,value in decision.judgments.items():
                        if key in ('severity','prompt_injection','sql_injection','profile_override_attempt','access_concern'):
                            continue  # Security and operating precedence keep their own approved rules.
                        if key=='sentiment':
                            continue  # Only a negative label escalates (upstream); its confidence never holds back evidence.
                        if not semantic_confident(value,self.semantic_confidence_trigger,self.topic_confidence_trigger):
                            self._unresolved_judgments.append('incoming.'+key)
                            if key=='company_wide_topic' or key.startswith('topic_'):
                                self._pending_destinations.add('central_support' if key=='company_wide_topic'
                                                               else key.removeprefix('topic_'))
                            else:
                                self._pending_destinations.update(decision.proposed_destinations)
            if decision.kind=='semantic_resolution':
                self._save_pending()
                decision=self._adjudicate_route(decision)
                if decision.kind!='continue':
                    return EvidenceResult(decision.kind if decision.kind!='semantic_resolution'
                                          else 'insufficient_evidence')
            # Validate every scope before any write/model/lookup can begin.
            for destination in decision.destinations:
                knowledge_filter(role=self.profile.role,destination=destination)
            if self.pending is None:
                self._save_pending()
            self.dependency=decision.dependency
            self.destinations=decision.destinations
            # D4 (owner 2026-11-23): the LLM router runs only when Jev is unsure; it never sees entries.
            if self.answer_models is not None and self._unresolved_judgments:
                decision=self._adjudicate_route(decision)
                if decision.kind!='continue':
                    return EvidenceResult(decision.kind)
                self.dependency=decision.dependency
                self.destinations=decision.destinations
            lookups=self.destinations  # Only settled departments are searched (no weak-topic hints).
            if len(set(lookups))!=len(lookups):
                raise ValueError('Invalid validated route.')
            if not lookups:
                self._relevance_unresolved.update(decision.proposed_destinations)
                return EvidenceResult('insufficient_evidence')  # An unsettled hint grants no facts.
            for destination in lookups:
                knowledge_filter(role=self.profile.role,destination=destination)
            if 'incoming.intent' in self._unresolved_judgments:
                self.destinations=()  # A weak intent keeps rows as resolution context only.
            self.refused_parts=decision.refused_departments
            if self.dependency!='independent' and self.refused_parts:
                return EvidenceResult('refusal',refused_parts=self.refused_parts)
            candidates=self._knowledge(lookups)
            if candidates:
                kind,selected,uncertain=self._matching(self.request.question,candidates,'knowledge')
                if kind not in ('clear','insufficient'):
                    raise ValueError('Invalid Knowledge relevance result.')
                self._relevance_unresolved.update(row.destination for row in candidates if row.excerpt_id in uncertain)
                chosen=self._select(candidates,selected,uncertain)
                if self.semantic_confidence_trigger is not None:
                    chosen.extend(replace(row,relevance_basis='semantic_resolution') for row in candidates
                                  if row.excerpt_id in uncertain)
                    chosen=[replace(row,relevance_basis='semantic_resolution')
                            if row.destination in self._pending_destinations else row for row in chosen]
                    self._relevance_unresolved.update(row.destination for row in chosen
                                                     if row.relevance_basis=='semantic_resolution')
                candidates=chosen
            if self.request.response_style is not None:
                candidates.append(self._candidate(kind='preference',key='response_style',
                    text=self.request.response_style,destination='preference'))
            result=self._clear(candidates,lookups)
            result=self._authorize_identity_history(result,self.request.question)
            routing=self._judgments.get('semantic.routing')
            if getattr(routing,'status',None)=='resolved':
                # A search hint the adjudicator did not route was never a requested part.
                hints=set(lookups)-set(self.destinations)
                self._relevance_unresolved.difference_update(hints)
                result=replace(result,unresolved_parts=tuple(part for part in result.unresolved_parts
                                                             if part not in hints))
            return replace(result,unresolved_parts=tuple(dict.fromkeys(
                (*result.unresolved_parts,*self.refused_parts))))
        except Exception:
            return EvidenceResult('technical_failure')  # No raw exceptions or source text in operational output.

    def _authorize_identity_history(self,result,target):
        # D1 has its own pre-dispatch access phase. These other entry points must enforce it too.
        history=tuple(row for row in self._selected+self._resolution_candidates
                      if row.source_kind=='owned_history'
                      and row.source_record_id not in self._history_access_ids)
        if not history or result.kind not in ('ready','insufficient_evidence'):
            return result
        from shopunow.workflow import permitted_routes
        answers=self._historical_access(target,history)
        recalled=tuple(row.conversation_id for row in self._selected+self._resolution_candidates
                       if row.source_kind=='owned_history')
        if self.tracking.excludes(recalled):
            self._history_dispositions.append(PartDisposition('history','technical_failure'))
            raise ValueError('Recalled evidence became unresolved during historical access.')
        stage=f'history_access_{self.searches}.'
        self._judgments.update((stage+key,value) for key,value in answers.items())
        unsettled=[stage+key for key,value in answers.items()
                   if not semantic_confident(value,self.semantic_confidence_trigger,self.topic_confidence_trigger)]
        destinations,refused,dependency=permitted_routes(answers,role=self.profile.role,
            topic_trigger=self.topic_trigger,semantic_confidence_trigger=self.semantic_confidence_trigger,
            topic_confidence_trigger=self.topic_confidence_trigger)
        self._unresolved_judgments.extend(unsettled)
        weak_intent='incoming.intent' in self._unresolved_judgments
        # Customers: only a weak restricted or currently answered topic can change this access decision.
        guarded={stage+'topic_'+part for part in ('hr','it_support','facilities_admin')}|{
            stage+('company_wide_topic' if part=='central_support' else 'topic_'+part) for part in self.destinations}
        # Employees: saved history holds no internal fact (owner 2026-11-24, Pass 5), so it is released;
        # customers keep a confident route for what matters (H5, unchanged).
        released=self.profile.role=='employee' or (not guarded.intersection(unsettled) and bool(destinations)
            and (weak_intent or set(destinations)<=set(self.destinations)))
        if released and not refused:
            self._history_access_ids.update(row.source_record_id for row in history)
            return result
        # No partition of shared history by subject exists. Withhold the whole newly selected set.
        excluded={row.excerpt_id for row in history}
        self._selected=[row for row in self._selected if row.excerpt_id not in excluded]
        self._resolution_candidates=[row for row in self._resolution_candidates if row.excerpt_id not in excluded]
        self.refused_parts=tuple(dict.fromkeys((*self.refused_parts,*refused)))
        self._history_dispositions.append(PartDisposition('history','refusal' if refused else 'insufficient_evidence'))
        self._relevance_unresolved.add('history')
        factual=any(row.source_kind in ('knowledge','owned_history') for row in self._selected)
        kind='refusal' if refused and (not factual or dependency!='independent') else (
            'ready' if factual else 'insufficient_evidence')
        if kind=='refusal':
            self._selected=[]
            self._resolution_candidates=[]
        uncovered=tuple(part for part in self.destinations if not any(
            row.destination in (part,'history') for row in self._selected))
        return replace(result,kind=kind,cleared=tuple(self._selected),
            unresolved_parts=tuple(dict.fromkeys((*result.unresolved_parts,*uncovered))))

    def _historical_access(self,target,history):
        # Routing this exact history is also an authoritative failure boundary, separate from matching.
        try:
            answers=dict(self._model(self.models.history_route,target,history,
                role=self.profile.role,home_department=self.profile.home_department))
            recalled=tuple(row.conversation_id for row in self._selected+self._resolution_candidates
                           if row.source_kind=='owned_history')
            if self.tracking.excludes(recalled):
                raise ValueError('Recalled evidence became unresolved during historical routing.')
            return answers
        except Exception:
            self._history_dispositions.append(PartDisposition('history','technical_failure'))
            raise

    def supplementary(self, *, target,work_timeout=None):
        # Supplementary insufficiency must not erase independently established refused parts.
        destinations=getattr(self,'destinations',())
        previous=self._work_timeout
        self._work_timeout=work_timeout
        try:
            return replace(self._with_dispositions(self._supplementary(target=target),destinations),
                           destinations=destinations,dependency=getattr(self,'dependency','unclear'),
                           requires_action_handoff=getattr(self,'_action_required',False))
        finally:
            self._work_timeout=previous  # Only the owned supplementary task mutates this service at this point.

    def _supplementary(self, *, target):
        # Only a later producer's code-validated context-needed continuation calls this seam.
        if (self.pending is None or self._supplemented or type(target) is not str
                or not target.strip() or len(target)>2000):
            return EvidenceResult('technical_failure')
        self._supplemented=True  # One producer continuation, even when no initial history search ran.
        try:
            kind,candidates=self._history(target)
            if kind!='clear':
                # Only the searched history is unresolved; other answered parts keep their own outcomes.
                self._relevance_unresolved.add('history')
                return EvidenceResult('clarification_needed' if kind=='ambiguous' else 'insufficient_evidence')
            existing={(row.source_kind,row.source_record_id,row.source_version) for row in
                      self._selected+self._resolution_candidates}
            candidates=[row for row in candidates if
                        (row.source_kind,row.source_record_id,row.source_version) not in existing]
            if candidates and self._reviewed>=2:
                # Owner decision: unreviewed history stays unused; reaching the limit is not a security finding.
                self._history_dispositions.append(PartDisposition('history','insufficient_evidence'))
                candidates=[]
            if not candidates:
                return EvidenceResult('insufficient_evidence',tuple(self._selected),
                                      ('history',),self.pending.message_id)
            result=self._clear(candidates,self.destinations)
            result=self._authorize_identity_history(result,target)
            new_ids={row.excerpt_id for row in candidates}
            if result.kind=='ready' and not any(row.source_kind=='owned_history' and
                    row.excerpt_id in new_ids for row in result.cleared):
                return replace(result,kind='insufficient_evidence',unresolved_parts=('history',))
            return result
        except Exception:
            return EvidenceResult('technical_failure')


# Flow: actual routing clearance -> retain/add/exact-read pending -> scoped candidates
# -> independent source/owner/prefix verification -> relevance -> exact per-ID review -> clean producer input.
