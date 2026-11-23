# Python 3.12 / Pydantic 2.14.1; private producer contributions, never an HTTP answer or saved terminal.
from dataclasses import dataclass  # Frozen internal values detach model output from later mutation.
from typing import Literal  # Native structured output uses the approved finite result vocabulary.
from typing import Annotated  # Attach an explicit schema to the per-call label type.
from pydantic import BaseModel,ConfigDict,Field,WithJsonSchema,create_model  # Reuse the pinned native SDK/Pydantic parsing boundary.
from shopunow.evidence import EvidenceCandidate,PartDisposition  # Reuse code-owned evidence/outcome contracts.


@dataclass(frozen=True)
class ProducerInput:
    request_id: str
    user_id: str
    conversation_id: str
    question: str
    part: str
    evidence: tuple[EvidenceCandidate,...]
    response_style: str | None = None
    unresolved_parts: tuple[str,...] = ()
    contributions: tuple['Contribution',...] = ()  # Only frozen permitted specialist findings enter synthesis.
    dependency: str = 'unclear'  # Code-owned: whether this part may stand without the unresolved parts.
    part_outcomes: tuple[PartDisposition,...] = ()  # Content-free outcomes of the other requested parts.
    missing_context: str | None = None  # Set only when a resumed producer's search found nothing.
    other_parts: tuple[str,...] = ()  # Other requested parts, answered by other producers, never by this one.
    own_notes: tuple[str,...] = ()  # Refusal/handoff topics inside this answered part (R4); never their content.
    correction: tuple[str,...] = ()  # Slice 5: final-review defects for this producer; empty outside correction.
    history_owner: bool = False  # Owner 2026-11-23: only this part may request an earlier conversation.


class ProducerReply(BaseModel):
    # This native response schema is not an evidence-admission or permission schema.
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True)
    part: str
    kind: Literal['supported_answer','context_needed','clarification_needed','handoff_needed',
                  'insufficient_evidence','technical_failure']
    text: str | None
    cited_ids: list[str]
    support_assertion: bool
    target: str | None = Field(description='context_needed: the specific missing earlier case or fact, never a '
        'part label. handoff_needed: exactly your own part. Every other result: null.')
    reason: str | None = Field(description='Non-answer results: why. supported_answer: null when complete; when your own '
        'part is listed in unresolved_parts, optionally what is missing. Never shown to the user.')
    unresolved_parts: list[str] = Field(description='Only part labels from the allowed list. Keep supplied '
        'unresolved parts listed; add your own part only when your own answer is incomplete. Put explanations '
        'in reason, never here.')
    saved_action_claim_ids: list[str]


def allowed_unresolved(part,unresolved_parts,missing_context):
    # The exact labels run_producer accepts; one source for the schema and the check.
    return tuple(dict.fromkeys((part,*unresolved_parts,*(('history',) if missing_context else ()))))


def producer_reply_type(labels):
    # A per-call schema makes labels the code would reject impossible to express.
    return create_model('ProducerReply',__base__=ProducerReply,
        # Always an enum, even for one label, so the provider sees the same schema shape every time.
        unresolved_parts=(list[Annotated[Literal[tuple(labels)],WithJsonSchema({'type':'string','enum':list(labels)})]],
                          ProducerReply.model_fields['unresolved_parts']))


@dataclass(frozen=True)
class Contribution:
    part: str
    kind: str
    text: str | None = None
    cited_ids: tuple[str,...] = ()
    support_assertion: bool = False
    target: str | None = None
    reason: str | None = None
    unresolved_parts: tuple[str,...] = ()


def run_producer(context, *, models, budget, timeout):
    """One bounded call; validation failures become private technical outcomes without raw error text."""
    failure=Contribution(context.part,'technical_failure')
    try:
        ids={row.excerpt_id for row in context.evidence}
        if len(ids)!=len(context.evidence) or len(ids)>20:
            raise ValueError('Invalid admitted evidence IDs.')
        for row in context.evidence:
            if (row.clearance!='clean' or row.relevance_basis not in ('typed_current_request','semantic_adjudication')
                    or row.source_kind not in ('knowledge','owned_history','preference')
                    or row.destination not in (context.part,'history','preference') and context.part!='central_support'
                    or row.source_kind in ('owned_history','preference') and row.owner_user_id!=context.user_id):
                raise ValueError('Producer context is not admitted for this part.')
        payload={'request_id':context.request_id,'part':context.part,'question':context.question,
            'response_style':context.response_style,'unresolved_parts':list(context.unresolved_parts),
            'dependency':context.dependency,'missing_context':context.missing_context,
            'history_owner':context.history_owner,
            'other_parts':list(context.other_parts),'own_notes':list(context.own_notes),
            'part_outcomes':[{'part':row.part,'outcome':row.kind} for row in context.part_outcomes],
            'evidence':[{'excerpt_id':row.excerpt_id,'source_kind':row.source_kind,
                'source_record_id':row.source_record_id,'source_version':row.source_version,
                'text':row.text,'destination':row.destination} for row in context.evidence],
            'contributions':[{'part':row.part,'kind':row.kind,'text':row.text,
                'cited_ids':list(row.cited_ids),'unresolved_parts':list(row.unresolved_parts)}
                for row in context.contributions]}
        if context.correction:
            payload['correction']=list(context.correction)  # Only correction calls carry this field.
        raw=budget.call(lambda remaining:models.produce(payload,timeout=remaining),timeout=timeout)
        checked=ProducerReply.model_validate(raw.model_dump() if hasattr(raw,'model_dump') else raw)
        if (checked.part!=context.part or checked.saved_action_claim_ids
                or len(checked.cited_ids)!=len(set(checked.cited_ids))
                or not set(checked.cited_ids)<=ids
                or not set(checked.unresolved_parts)<=set(allowed_unresolved(context.part,context.unresolved_parts,
                    context.missing_context))|({context.missing_context} if context.missing_context else set())):
            raise ValueError('Unbound producer result or unsaved action claim.')
        if checked.kind=='supported_answer':
            if (not checked.text or not checked.text.strip() or len(checked.text.encode('utf-8'))>8192
                    or not checked.support_assertion or not checked.cited_ids
                    or checked.target is not None
                    # R4: only a partial answer (own part unresolved) may explain its gap; reason stays private.
                    or (checked.reason is not None and context.part not in checked.unresolved_parts)
                    or not any(row.source_kind!='preference' and row.excerpt_id in checked.cited_ids
                               for row in context.evidence)):
                raise ValueError('Unsupported or oversized producer text.')
        else:
            if checked.text is not None or checked.cited_ids or checked.support_assertion:
                raise ValueError('Non-answer result contains an answer.')
            if checked.kind=='context_needed' and (
                    not checked.target or not checked.target.strip() or len(checked.target)>2000):
                raise ValueError('A supplementary request needs a specific missing target.')
            if checked.kind=='handoff_needed' and (
                    checked.target!=context.part or not checked.reason or not checked.reason.strip()):
                raise ValueError('A producer cannot add a handoff destination.')
        # A resumed producer may name its missing item; its own unfinished part keeps its real label.
        unresolved=tuple(dict.fromkeys('history' if part==context.missing_context and part!=context.part else part
                                       for part in checked.unresolved_parts))
        return Contribution(checked.part,checked.kind,checked.text,tuple(checked.cited_ids),
            checked.support_assertion,checked.target,checked.reason,unresolved)
    except Exception:
        return failure  # No draft, raw source content, exception or success claim escapes this failure.


# Flow: admitted part/context -> original shared budget -> native structured result -> detached contribution.


@dataclass(frozen=True)
class DraftSnapshot:
    draft_id: str
    full_text: str
    cited_ids: tuple[str,...]
    contributor_ids: tuple[str,...]
    unresolved_parts: tuple[str,...]
    saved_action_claim_ids: tuple[str,...] = ()
    round: int = 0  # Slice5 owns revisions and both reviews of the same immutable snapshot.


@dataclass(frozen=True)
class ReviewVerdict:
    reviewer: str  # 'security' or 'policy'.
    draft_id: str
    round: int
    verdict: str  # approve | revise | fallback | failure (code-owned: invalid, refused or failed call).
    defect: str | None = None  # Private reviewer explanation; never delivered.
    responsible_producers: tuple[str,...] = ()


class FinalReviewReply(BaseModel):
    # Native reply shape for both final reviewers; code still checks every field afterwards.
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True)
    draft_id: str
    round: int
    verdict: Literal['approve','revise','fallback']
    defect: str | None
    responsible_producers: list[str]


def _enum(values):
    # Always an explicit enum, even for one value, so the provider sees one stable schema shape.
    return Annotated[Literal[tuple(values)],WithJsonSchema({'enum':list(values)})]


def final_review_reply_type(snapshot):
    # Per-call schema: only this draft's ID/round and its actual contributors can be expressed.
    return create_model('FinalReviewReply',__base__=FinalReviewReply,
        draft_id=(_enum((snapshot.draft_id,)),...),round=(_enum((snapshot.round,)),...),
        responsible_producers=(list[_enum(snapshot.contributor_ids)],...))


def run_final_review(snapshot, reviewer, *, role, home_department, models, budget, timeout):
    """One bounded final-review call on one immutable snapshot (Slice 5)."""
    payload={'draft_id':snapshot.draft_id,'round':snapshot.round,'full_text':snapshot.full_text,
        'cited_ids':list(snapshot.cited_ids),'contributor_ids':list(snapshot.contributor_ids),
        'unresolved_parts':list(snapshot.unresolved_parts),
        'saved_action_claim_ids':list(snapshot.saved_action_claim_ids),
        'user_role':role,'home_department':home_department}
    failure=ReviewVerdict(reviewer,snapshot.draft_id,snapshot.round,'failure')
    try:
        if reviewer not in ('security','policy'):
            raise ValueError('Only the two approved final reviewers exist.')
        reply_type=final_review_reply_type(snapshot)
        raw=budget.call(lambda remaining:models.review_final(reviewer,payload,reply_type,timeout=remaining),
                        timeout=timeout)
        checked=reply_type.model_validate(raw.model_dump() if hasattr(raw,'model_dump') else raw)
        producers=tuple(checked.responsible_producers)
        defect=checked.defect.strip() if checked.defect else None
        # Code re-checks binding and verdict shape; the schema alone is not trusted.
        if (checked.draft_id!=snapshot.draft_id or checked.round!=snapshot.round
                or len(producers)!=len(set(producers)) or not set(producers)<=set(snapshot.contributor_ids)
                or (checked.verdict=='approve' and (defect or producers))
                or (checked.verdict=='revise' and (not defect or not producers or len(defect)>2000))):
            raise ValueError('Unbound or malformed final-review verdict.')
        return ReviewVerdict(reviewer,checked.draft_id,checked.round,checked.verdict,defect,producers)
    except Exception:
        return failure  # No raw error text, no retry; delivery stops on this verdict.


@dataclass(frozen=True)
class ProductionResult:
    kind: str
    draft: DraftSnapshot | None = None
    frozen_contributors: tuple[str,...] = ()
    unresolved_parts: tuple[str,...] = ()
    contributions: tuple[Contribution,...] = ()  # Frozen findings, including CentralSupport when invoked.
    contexts: tuple[ProducerInput,...] = ()  # Exact admitted inputs; cited IDs map to these excerpts.
    dispositions: tuple[PartDisposition,...] = ()  # Code-owned alternatives remain distinct from missing labels.


def run_production(*, evidence,service,models,observe_draft=None,observe_freeze=None,observe_result=None):
    """LangGraph 1.2.20: bounded child work, coordinator-owned freeze/assembly, then owned drain.

    The enclosing admitted worker retains its lease until this function returns. No HTTP response,
    terminal persistence, model retry, new destination or independent resource release occurs here.
    """
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
    from dataclasses import replace
    from typing import TypedDict
    from uuid import uuid4
    from langgraph.graph import StateGraph,START,END
    from shopunow.knowledge import knowledge_filter

    attempt,budget,tracking=service.attempt,service.budget,service.tracking
    admission=attempt.admission
    if (admission is None or service.pending is None or evidence.pending_message_id!=service.pending.message_id
            or tuple(evidence.destinations)!=tuple(service.destinations)
            or evidence.kind not in ('ready','clarification_needed','insufficient_evidence')):
        return ProductionResult('technical_failure')
    destinations=tuple(evidence.destinations)
    if len(destinations)!=len(set(destinations)) or len(set(destinations)-{'central_support'})>5:
        return ProductionResult('technical_failure')
    try:
        for part in destinations:
            knowledge_filter(role=service.profile.role,destination=part)
    except ValueError:
        return ProductionResult('technical_failure')
    parts=tuple(part for part in destinations if part!='central_support')
    # Owner 2026-11-23: one department -> its specialist owns history; otherwise central support does.
    owner=parts[0] if len(parts)==1 and 'central_support' not in destinations else 'central_support'
    request,profile,request_id=service.request,service.profile,service.request_id
    base_dispositions=tuple(dict.fromkeys((*evidence.dispositions,
        *(PartDisposition(part,'refusal') for part in evidence.refused_parts),
        *(PartDisposition(part,'handoff_needed') for part in evidence.handoff_parts),
        *(PartDisposition(part,'security_fallback') for part in evidence.security_fallback_parts),
        *((PartDisposition('action','handoff_needed'),) if evidence.requires_action_handoff else ()))))
    base_unresolved=tuple(dict.fromkeys((*evidence.unresolved_parts,
        *(row.part for row in base_dispositions if row.kind!='supported_answer'))))

    def timeout(specialist=False):
        remaining=attempt.normal_work_timeout()
        if remaining is None:
            raise TimeoutError('Production stopped.')
        # Both budgets share admission's original monotonic clock;90 minus60 is30, not a fresh timer.
        remaining-=30. if specialist else 0.
        if remaining<=0:
            raise TimeoutError('Production phase ended.')
        return remaining

    # R4: in an independent request, admitted answerable evidence answers its part; a matched restricted or
    # non-general topic in the same part becomes a note instead of blocking the whole part.
    noted=frozenset(part for part in (*evidence.refused_parts,*evidence.handoff_parts)
                    if evidence.dependency=='independent' and part not in evidence.security_fallback_parts
                    and any(row.source_kind=='knowledge' and row.destination==part for row in evidence.cleared))

    def notes(part):
        return tuple(dict.fromkeys(row.kind for row in base_dispositions if part in noted and row.part==part
                                   and row.kind in ('refusal','handoff_needed')))

    def producer_unresolved(part,unresolved):
        # A noted part is still answered by its own agent; assembly and the draft keep it unresolved.
        return tuple(other for other in unresolved if not (other==part and part in noted))

    def outcomes(part):
        return tuple(row for row in base_dispositions if row.part!=part and row.kind!='supported_answer')

    def context_for(part,rows):
        return ProducerInput(request_id,profile.user_id,request.conversation_id,request.question,part,
            tuple(row for row in rows if row.destination in (part,'history','preference')),
            evidence.response_style,producer_unresolved(part,base_unresolved),dependency=evidence.dependency,
            part_outcomes=outcomes(part),
            other_parts=tuple(other for other in destinations if other!=part),own_notes=notes(part),
            history_owner=part==owner)

    def context_timeout(context,specialist=False):
        remaining=timeout(specialist)
        recalled=tuple(row.conversation_id for row in context.evidence if row.source_kind=='owned_history')
        if recalled and tracking.excludes(recalled):
            raise TimeoutError('Recalled context became unresolved.')
        return remaining

    blocked=(set(evidence.refused_parts)|set(evidence.handoff_parts))-noted|set(evidence.security_fallback_parts)
    # The history owner runs even without evidence, but may then only request history (see factual()).
    owner_may_ask=owner not in blocked and evidence.kind!='clarification_needed'
    eligible=tuple(part for part in destinations if part not in blocked and (part==owner and owner_may_ask or any(
        row.source_kind in ('knowledge','owned_history') and row.destination in (part,'history')
        for row in evidence.cleared)))
    base_dispositions=tuple(dict.fromkeys((*base_dispositions,*(PartDisposition(part,
        'clarification_needed' if evidence.kind=='clarification_needed' else 'insufficient_evidence')
        for part in destinations if part not in eligible and part not in blocked))))
    base_unresolved=tuple(dict.fromkeys((*base_unresolved,
        *(row.part for row in base_dispositions if row.kind!='supported_answer'))))
    contexts={part:context_for(part,evidence.cleared) for part in parts}
    consumed=False  # One supplementary opportunity across all specialists and CentralSupport.
    supplementary_unresolved={}
    supplementary_dispositions=[]  # Only coordinator-received supplementary results enter the boundary.
    history_answered=set()  # Owner parts whose missing facts the history search supplied (no longer unresolved).
    security_failure=False
    executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='shopunow-production')

    class State(TypedDict,total=False):
        frozen: tuple[Contribution,...]
        missing: tuple[str,...]
        result: ProductionResult

    def supplementary_kind(kind):
        # A failed search is not semantic insufficiency; continuation is authorized only by ready.
        if kind in ('clarification_needed','technical_failure','refusal','handoff_needed'):
            return kind
        return 'security_fallback' if kind in ('security_fallback','security_uncertain') else 'insufficient_evidence'

    def factual(context):
        return any(row.source_kind in ('knowledge','owned_history') for row in context.evidence)

    def resumable(part):
        # Only independent parts with their own admitted facts may answer without the missing context.
        return evidence.dependency=='independent' and any(row.source_kind=='knowledge'
            for row in contexts[part].evidence)

    def dispatch(state):
        nonlocal consumed,security_failure
        queued=[part for part in parts if part in eligible]
        active={}
        terminal={}
        requests={}
        supplementary=None
        supplementary_part=None
        supplementary_target=None
        continuation=None
        while True:
            if security_failure:
                break  # An invalid/uncertain required review never rejoins normal production.
            try:
                remaining=timeout(True)
            except TimeoutError:
                break
            while len(active)<2 and (continuation is not None or queued):
                part=continuation if continuation is not None else queued.pop(0)
                continuation=None
                context=contexts[part]  # Each child keeps an immutable evidence snapshot.
                active[executor.submit(run_producer,context,models=models,budget=budget,
                                       timeout=lambda context=context:context_timeout(context,True))]=part
            if requests and not consumed:
                chosen=min(requests,key=parts.index)
                # Wait only for earlier initial decisions, ensuring deterministic one-request arbitration.
                earlier=tuple(part for part in parts[:parts.index(chosen)] if part in eligible)
                if all(part in terminal or part in requests for part in earlier):
                    consumed=True
                    supplementary_part=chosen
                    supplementary_target=requests.pop(chosen).target
                    supplementary=executor.submit(service.supplementary,target=supplementary_target,
                                                   work_timeout=lambda:timeout(True))
            if consumed:
                for part in tuple(requests):
                    terminal[part]=Contribution(part,'insufficient_evidence',target=requests[part].target)
                    del requests[part]
            futures=set(active)|({supplementary} if supplementary is not None else set())
            if not futures:
                break
            done,_=wait(futures,timeout=min(.05,remaining),return_when=FIRST_COMPLETED)
            # Receipt is after structural validation in run_producer and before this guarded acceptance.
            for future in sorted(done,key=lambda value:parts.index(active.get(value,supplementary_part))):
                try:
                    timeout(True)
                except TimeoutError:
                    break
                if future is supplementary:
                    try:
                        extra=future.result()
                        part=supplementary_part
                        supplementary_unresolved[part]=tuple(dict.fromkeys((*extra.unresolved_parts,
                            *extra.refused_parts,*extra.handoff_parts,
                            *(row.part for row in extra.dispositions if row.kind!='supported_answer'),
                            *(('action',) if extra.requires_action_handoff else ()))))
                        supplementary_dispositions.extend(extra.dispositions)
                        if extra.kind in ('security_fallback','security_uncertain'):
                            security_failure=True
                        if extra.kind=='ready' and part in extra.destinations:
                            history_answered.add(part)
                            contexts[part]=replace(context_for(part,extra.cleared),unresolved_parts=tuple(
                                other for other in dict.fromkeys((*base_unresolved,*supplementary_unresolved[part]))
                                if other!=part))
                            continuation=part
                        elif extra.kind=='insufficient_evidence' and resumable(part):
                            # The empty search describes the missing history, not this answerable part.
                            supplementary_unresolved[part]=tuple(dict.fromkeys((*(row for row in
                                supplementary_unresolved[part] if row!=part),'history')))
                            supplementary_dispositions[:]=[row for row in supplementary_dispositions if row.part!=part]
                            contexts[part]=replace(contexts[part],missing_context=supplementary_target)
                            continuation=part  # The single resume answers only what is already supported.
                        else:
                            terminal[part]=Contribution(part,supplementary_kind(extra.kind),target=supplementary_target)
                    except Exception:
                        terminal[supplementary_part]=Contribution(supplementary_part,'technical_failure')
                    supplementary=None
                else:
                    part=active.pop(future)
                    try:
                        outcome=future.result()
                        context_timeout(contexts[part],True)
                    except Exception:
                        outcome=Contribution(part,'technical_failure')
                    if outcome.kind!='context_needed' and not factual(contexts[part]):
                        outcome=Contribution(part,'insufficient_evidence')  # Without evidence: only ask for history.
                    if outcome.kind=='context_needed':
                        if consumed or part!=owner:
                            terminal[part]=Contribution(part,'insufficient_evidence',target=outcome.target)
                        else:
                            requests[part]=outcome
                    else:
                        terminal[part]=outcome
        frozen=tuple(terminal[part] for part in parts if part in terminal)
        if observe_freeze is not None:
            observe_freeze(tuple(row.part for row in frozen))
        return {'frozen':frozen,'missing':tuple(part for part in parts if part in eligible and part not in terminal)}

    def assemble(state):
        nonlocal consumed
        frozen=state['frozen']
        membership=tuple(row.part for row in frozen)
        central=None
        answer=None

        def result(kind,draft=None):
            # One detached boundary serves future correction/review before the owned child drain.
            findings=(*frozen,*((answer,) if central is not None and answer is not None else ()))
            retained=(*(contexts[part] for part in membership),*((central,) if central is not None else ()))
            dispositions=tuple(dict.fromkeys((*base_dispositions,*supplementary_dispositions,
                *(PartDisposition(row.part,'insufficient_evidence' if row.kind=='context_needed' else row.kind)
                  for row in findings),
                *(PartDisposition(part,'incomplete_at_cutoff') for part in state['missing']))))
            known={row.part for row in dispositions if row.kind!='supported_answer'}
            dispositions+=tuple(PartDisposition(part,'insufficient_evidence') for part in unresolved if part not in known)
            return {'result':ProductionResult(kind,draft,membership,
                draft.unresolved_parts if draft is not None else unresolved,findings,retained,dispositions)}

        unresolved=tuple(dict.fromkeys((*(part for part in base_unresolved if part not in history_answered),
            *state['missing'],
            *(row.part for row in frozen if row.kind!='supported_answer'),
            *(part for row in frozen for part in row.unresolved_parts),
            *(part for values in supplementary_unresolved.values() for part in values))))
        if security_failure:
            return result('security_fallback')
        central_owner=owner=='central_support' and owner_may_ask
        # A central owner may still supply its own part from history; the check repeats after its answer.
        if (any(part!='central_support' or not central_owner for part in unresolved)
                and evidence.dependency!='independent'):
            # A refused necessary premise (e.g. refused history) stays a refusal (challenge T2).
            refused=evidence.dependency=='dependent' and any(row.kind=='refusal'
                                                             for row in (*frozen,*supplementary_dispositions))
            return result('refusal' if refused else
                          'clarification_needed' if evidence.dependency=='unclear' else 'insufficient_evidence')
        supported=tuple(row for row in frozen if row.kind=='supported_answer')

        def fallback():
            kinds={row.kind for row in (*base_dispositions,*supplementary_dispositions,*frozen)}
            kind=next((value for value in ('security_fallback','technical_failure','clarification_needed',
                'refusal','handoff_needed') if value in kinds),
                'technical_failure' if state['missing'] else 'insufficient_evidence')
            return result(kind)
        try:
            timeout()
            # Another unfinished requested part needs synthesis, so the draft cannot look complete.
            other_gap=any(part in unresolved for part in destinations
                          if not supported or part!=supported[0].part)
            if len(supported)==1 and 'central_support' not in destinations and not other_gap:
                answer=supported[0]  # Exact pass-through; no extra author or synthesis call.
                context_timeout(contexts[answer.part])
                authors=(answer.part,)
            elif supported or central_owner:
                allowed_ids={identity for row in supported for identity in row.cited_ids}
                pool={row.excerpt_id:row for part in membership for row in contexts[part].evidence
                      if row.excerpt_id in allowed_ids}
                pool.update((row.excerpt_id,row) for row in evidence.cleared
                            if row.destination=='preference' or 'central_support' in destinations and
                            row.destination in ('central_support','history'))
                central=ProducerInput(request_id,profile.user_id,request.conversation_id,request.question,
                    'central_support',tuple(pool.values()),evidence.response_style,
                    producer_unresolved('central_support',unresolved),frozen,
                    dependency=evidence.dependency,part_outcomes=outcomes('central_support'),
                    own_notes=notes('central_support'))
                central=replace(central,history_owner=owner=='central_support')
                answer=run_producer(central,models=models,budget=budget,timeout=lambda:context_timeout(central))
                authors=(*(row.part for row in supported),'central_support')
                if answer.kind!='context_needed' and not factual(central):
                    answer=None
                    return fallback()  # Without evidence central support may only ask for history.
                # Only the history owner opens a search (gap-only synthesis for one department never does).
                if answer.kind=='context_needed' and not consumed and owner=='central_support':
                    consumed=True
                    earlier_unresolved=set(unresolved)  # Status known before this search must survive its resume.
                    extra=service.supplementary(target=answer.target,work_timeout=timeout)
                    unresolved=tuple(dict.fromkeys((*unresolved,*extra.unresolved_parts,*extra.refused_parts,
                        *extra.handoff_parts,*(row.part for row in extra.dispositions if row.kind!='supported_answer'),
                        *(('action',) if extra.requires_action_handoff else ()))))
                    supplementary_dispositions.extend(extra.dispositions)
                    if extra.kind in ('security_fallback','security_uncertain'):
                        answer=Contribution('central_support','security_fallback')
                        return result('security_fallback')
                    if extra.kind=='ready':
                        unresolved=tuple(part for part in unresolved if part!='central_support')  # History supplied it.
                        # The same frozen contributors remain; new history adds context, never late membership.
                        pool.update((row.excerpt_id,row) for row in extra.cleared
                                    if row.destination in ('central_support','history','preference'))
                        central=replace(central,evidence=tuple(pool.values()),
                                        unresolved_parts=producer_unresolved('central_support',unresolved))
                        answer=run_producer(central,models=models,budget=budget,
                                            timeout=lambda:context_timeout(central))
                    elif (extra.kind=='insufficient_evidence' and evidence.dependency=='independent'
                          and any(row.source_kind=='knowledge' for row in central.evidence)):
                        # The single resume answers only what the admitted evidence already supports.
                        unresolved=tuple(dict.fromkeys((*(part for part in unresolved if part!='central_support'
                                                          or part in earlier_unresolved),'history')))
                        supplementary_dispositions[:]=[row for row in supplementary_dispositions
                                                       if row.part!='central_support']
                        central=replace(central,unresolved_parts=producer_unresolved('central_support',unresolved),
                                        missing_context=answer.target)
                        answer=run_producer(central,models=models,budget=budget,
                                            timeout=lambda:context_timeout(central))
                    else:
                        answer=Contribution('central_support',supplementary_kind(extra.kind),target=answer.target)
                context_timeout(central)
            else:
                return fallback()
            unresolved=tuple(dict.fromkeys((*unresolved,*answer.unresolved_parts,
                *((answer.part,) if answer.kind!='supported_answer' else ()))))
            if answer.kind!='supported_answer':
                return result('insufficient_evidence' if answer.kind=='context_needed' else answer.kind)
            if unresolved and evidence.dependency!='independent':
                return result('clarification_needed' if evidence.dependency=='unclear' else 'insufficient_evidence')
            timeout()
            draft=DraftSnapshot(str(uuid4()),answer.text,answer.cited_ids,authors,unresolved)
            if observe_draft is not None:
                observe_draft(draft)  # Private in-process observation; no response/history/terminal write.
            return result('draft_ready',draft)
        except Exception:
            return result('technical_failure')

    graph=StateGraph(State)
    graph.add_node('dispatch',dispatch)
    graph.add_node('assemble',assemble)
    graph.add_edge(START,'dispatch')
    graph.add_edge('dispatch','assemble')
    graph.add_edge('assemble',END)
    try:
        result=graph.compile().invoke({})['result']
        if observe_result is not None:
            observe_result(result)  # Private full boundary, available even for a no-draft disposition.
        return result
    finally:
        # Assembly occurs before this drain. The admitted parent cannot release reservations early.
        executor.shutdown(wait=True,cancel_futures=True)
