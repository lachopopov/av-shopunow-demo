# LangGraph 1.2.20 / Python 3.12; incoming decisions plus admitted attack/fallback assembly.
from dataclasses import dataclass
from typing import TypedDict
from types import MappingProxyType  # Detach the validated model judgments from graph state.
from langgraph.graph import StateGraph, START, END
from shopunow.incoming import IncomingContext, validate_judgments, reviewer_disposition, _number, semantic_confident


@dataclass(frozen=True)
class IncomingDecision:
    kind: str  # An internal tag, never a claim of an answer or saved offer.
    destinations: tuple[str, ...] = ()
    refused_departments: tuple[str, ...] = ()
    dependency: str = 'independent'
    intent: str = 'unclear_or_other'
    requires_action_handoff: bool = False  # Mixed requests retain the unresolved action part.
    proposed_destinations: tuple[str, ...] = ()  # Search hints, never dispatch or permission.
    judgments: object = None  # Native typed answers survive the internal decision boundary.


class IncomingState(TypedDict, total=False):
    context: IncomingContext  # Current lower-trust input is isolated per invocation.
    answers: dict
    decision: IncomingDecision


class IncomingInput(TypedDict):
    context: IncomingContext  # Public graph input cannot seed answers, clearance or decisions.


def permitted_routes(answers,*,role,topic_trigger,semantic_confidence_trigger=None):
    """One code-owned access rule for both initial and history-resolved topic judgments."""
    _number(topic_trigger)
    departments=tuple(department for department in
        ('hr','it_support','facilities_admin','billing_payments','shipping_delivery')
        if answers['topic_'+department].noul>=topic_trigger
        and semantic_confident(answers['topic_'+department],semantic_confidence_trigger))
    refused=tuple(department for department in departments
                  if role=='customer' and department in ('hr','it_support','facilities_admin'))
    permitted=tuple(department for department in departments if department not in refused)
    destinations=(('central_support',) if answers['company_wide_topic'].noul>=topic_trigger
                  and semantic_confident(answers['company_wide_topic'],semantic_confidence_trigger) else ())+permitted
    return destinations,refused,answers['department_dependency'].choice


def build_incoming_graph(*, judge, reviewer, authorize, security_trigger=None, topic_trigger=None,
                         semantic_confidence_trigger=None,allow_semantic_resolution=False):
    """Caller-supplied evaluated triggers; no default threshold or normal served route.

    Controlled tests may inject provisional numbers. Production enablement still requires
    the separately reviewed evaluation evidence; supplying a number is not that evidence.
    """
    for trigger in (security_trigger, topic_trigger, semantic_confidence_trigger):
        if trigger is not None:
            _number(trigger)

    def judge_node(state):
        if not authorize():
            return {'decision': IncomingDecision('stopped')}
        if security_trigger is None or topic_trigger is None:
            return {'decision': IncomingDecision('disabled')}
        try:
            answers = validate_judgments(judge(state['context']))
        except Exception:
            return {'decision': IncomingDecision('dependency_failure')}
        return {'answers': answers}

    def safety_node(state):
        if not authorize():
            return {'decision': IncomingDecision('stopped')}
        risks = {key: state['answers'][key].noul for key in
                 ('prompt_injection', 'sql_injection', 'profile_override_attempt')}
        if max(risks.values()) < security_trigger:
            return {}  # Only the incoming gate passed; access remains mandatory.
        try:
            raw = reviewer(state['context'], risks)
            raw = raw.model_dump() if hasattr(raw, 'model_dump') else raw
            disposition = reviewer_disposition(raw)
        except Exception:
            return {'decision': IncomingDecision('dependency_failure')}
        if not authorize():
            return {'decision': IncomingDecision('stopped')}
        if disposition != 'clear':
            return {'decision': IncomingDecision(
                'confirmed_attack' if disposition == 'confirmed_attack' else 'security_uncertain')}
        return {}

    def access_node(state):
        if not authorize():
            return {'decision': IncomingDecision('stopped')}
        answers, context = state['answers'], state['context']
        proposed=tuple(destination for key,destination in (
            ('company_wide_topic','central_support'),('topic_hr','hr'),('topic_it_support','it_support'),
            ('topic_facilities_admin','facilities_admin'),('topic_billing_payments','billing_payments'),
            ('topic_shipping_delivery','shipping_delivery')) if answers[key].noul>=topic_trigger or
                not semantic_confident(answers[key],semantic_confidence_trigger))
        # Downstream retrieval must still enforce audience/general/home-department scope.
        destinations,refused,dependency=permitted_routes(answers,role=context.role,topic_trigger=topic_trigger,
            semantic_confidence_trigger=semantic_confidence_trigger)
        kind = 'continue'
        if answers['sentiment'].choice == 'negative':
            kind = 'handoff'  # Whole-request escalation follows security and access checks.
        elif refused and (not destinations or dependency != 'independent'):
            kind = 'refusal'
        elif answers['intent'].choice == 'unclear_or_other':
            kind = 'clarification'
        elif answers['intent'].choice == 'individual_case_or_action':
            kind = 'handoff'
        elif not destinations and not (semantic_confidence_trigger is not None and proposed):
            # Owner 2026-11-23: information naming no department goes to central support, the history owner.
            kind = 'continue' if answers['intent'].choice == 'information' else 'handoff'
            destinations = ('central_support',) if kind == 'continue' else ()
        # Only a low-confidence action label with a possible route may be checked; a confident one hands off.
        action_label=answers['intent'].choice=='individual_case_or_action'
        weak_action=(action_label and bool(proposed)
                     and not semantic_confident(answers['intent'],semantic_confidence_trigger))
        if (allow_semantic_resolution and semantic_confidence_trigger is not None
                and answers['sentiment'].choice!='negative'
                and (not action_label or weak_action)
                and ((kind=='clarification' and not semantic_confident(answers['intent'],semantic_confidence_trigger))
                     or (kind=='handoff' and (weak_action or (answers['intent'].choice=='information' and proposed))))):
            kind='semantic_resolution'  # Safety already passed; no producer dispatch is authorized yet.
        if kind != 'continue':
            destinations = ()
        intent = answers['intent'].choice
        return {'decision': IncomingDecision(kind, destinations, refused, dependency, intent,
            intent in ('mixed', 'individual_case_or_action'),proposed,MappingProxyType(answers))}

    # Conditional edges forbid access routing after any non-clear safety disposition.
    graph = StateGraph(IncomingState, input_schema=IncomingInput)
    graph.add_node('judge', judge_node)
    graph.add_node('safety', safety_node)
    graph.add_node('access', access_node)
    graph.add_edge(START, 'judge')
    graph.add_conditional_edges('judge', lambda state: 'end' if 'decision' in state else 'safety',
                                {'end': END, 'safety': 'safety'})
    graph.add_conditional_edges('safety', lambda state: 'end' if 'decision' in state else 'access',
                                {'end': END, 'access': 'access'})
    graph.add_edge('access', END)
    return graph.compile()  # No checkpoints, durable messages, caching or automatic retries.


# Flow: authorization/calibration -> complete judgments -> incoming safety -> access/route.


def run_incoming(*, request, profile, transcript, attempt, tracking, storage, request_id,
                 jev_client, reviewer_client, jev_model, security_trigger=None,
                 topic_trigger=None, uuid_factory=None, utc_now=None, fallback_reviewer_client=None,
                 model_budget=None, on_cleared=None,observe_response=None,semantic_confidence_trigger=None,
                 allow_semantic_resolution=False,observe_decision=None):
    """Admission continuation; no normal retrieval/dispatch or later-slice terminal save.

    Clients and thresholds must be explicitly supplied. Normal production routing has
    no configured trigger until the separate labeled evaluation is reviewed.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    from shopunow.incoming import build_incoming_context
    from shopunow.model_clients import judge_incoming, review_with_fallback
    from shopunow.preclearance import record_preclearance_fallback
    from shopunow.schemas import InteractionResponse, ConfirmedAttackOutcome
    from shopunow.api import question_event  # Reuse the established sanitized event allowlist.

    if tracking is None or tracking is not attempt._hold_tracking:
        raise ValueError('Incoming workflow must use the admission-owned tracker.')
    context = build_incoming_context(transcript, profile=profile, request=request)

    def timeout():
        remaining = attempt.normal_work_timeout()
        if remaining is None:
            raise TimeoutError('Incoming workflow is stopped.')
        return remaining

    def judge(context):
        try:
            def operation(remaining):
                return judge_incoming(jev_client,context,model=jev_model,timeout=remaining,
                                      observe_response=observe_response)
            result = (operation(timeout()) if model_budget is None else
                      model_budget.call(operation,timeout=timeout))
        except Exception:
            question_event(request_id, 'dependency_failure', stage='incoming_judgment')
            raise
        question_event(request_id, 'validated', stage='incoming_judgment')
        return result

    graph = build_incoming_graph(
        judge=judge,
        reviewer=lambda context, risks: review_with_fallback(reviewer_client, context,
            risks=risks, timeout=timeout, fallback_client=fallback_reviewer_client,
            call=None if model_budget is None else model_budget.call,observe_response=observe_response),
        authorize=attempt.authorize_normal_work, security_trigger=security_trigger,
        topic_trigger=topic_trigger,semantic_confidence_trigger=semantic_confidence_trigger,
        allow_semantic_resolution=allow_semantic_resolution)
    decision = graph.invoke({'context':context})['decision']
    question_event(request_id, decision.kind, stage='incoming_gate')
    if not attempt.authorize_normal_work():
        return None
    if observe_decision is not None:
        observe_decision(decision)  # Private composition state, never a saved/delivered-result assertion.
    if decision.kind in ('continue','semantic_resolution') and on_cleared is not None:
        if model_budget is None:
            raise ValueError('Evidence continuation requires the original model budget.')
        on_cleared(decision)  # Internal observation only; later terminal persistence still gates delivery.
        return None
    if decision.kind == 'confirmed_attack':
        outcome = ConfirmedAttackOutcome(kind='confirmed_attack_block',
                                         text='The request cannot be processed.')
    elif decision.kind in ('security_uncertain', 'dependency_failure'):
        now = utc_now or (lambda:datetime.now(timezone.utc))
        outcome = record_preclearance_fallback(storage, attempt=attempt, tracking=tracking,
            user_id=request.user_id, conversation_id=request.conversation_id,
            request_id=request_id, offer_id=str((uuid_factory or uuid4)()),
            reason=decision.kind, created_at=now().isoformat())
        question_event(request_id, 'verified' if outcome.offer_id is not None else 'unverified',
                       stage='preclearance_write')
    else:
        return None  # Remaining terminal kinds need later-slice persistence before display.
    return InteractionResponse(user_id=request.user_id, conversation_id=request.conversation_id,
                               request_id=request_id, outcome=outcome)


def run_private_workflow(*,request,profile,transcript,attempt,tracking,storage,request_id,
                         evidence_models,answer_models,jev_client,reviewer_client,jev_model,
                         security_trigger=None,topic_trigger=None,semantic_confidence_trigger=None,
                         history_identity_resolver=None,fallback_reviewer_client=None,model_budget=None,
                         observe_evidence=None,observe_result=None,observe_response=None,utc_now=None,
                         uuid_factory=None,routing_models=None,on_progress=None):
    """Compose Slice4 inside the admitted worker; public return remains the incoming safe result.

    All clients/configuration are explicitly injected. Private observations are request-local;
    this function neither installs an endpoint nor reviews, persists or delivers a generated draft.
    """
    from datetime import datetime,timezone
    from shopunow.evidence import EvidenceService,ModelBudget,PartDisposition
    from shopunow.production import ProductionResult,run_production

    budget=model_budget if model_budget is not None else ModelBudget()  # One counter for the entire journey.
    decisions=[]
    progressed=False

    def emit(result):
        if observe_result is not None:
            observe_result(result)  # No private result is returned to the HTTP admission boundary.

    def nonproducing(kind,dispositions=(),unresolved=()):
        aliases={'clarification':'clarification_needed','handoff':'handoff_needed',
                 'security_uncertain':'security_fallback','dependency_failure':'technical_failure'}
        kind=aliases.get(kind,kind)
        # Control-only exits carry their actual tag; they do not assert an unrelated technical failure.
        if kind in ('clarification_needed','handoff_needed','security_fallback','technical_failure',
                    'refusal','insufficient_evidence') and (not dispositions or kind in (
                    'security_fallback','technical_failure')):
            dispositions=tuple(dict.fromkeys((*dispositions,PartDisposition('request',kind))))
        unresolved=tuple(dict.fromkeys((*unresolved,*(row.part for row in dispositions))))
        emit(ProductionResult(kind,unresolved_parts=unresolved,dispositions=dispositions))

    def cleared(decision):
        nonlocal progressed
        progressed=True
        if on_progress is not None:
            on_progress(2)  # Progress step 2: finding the right department.
        now=utc_now or (lambda:datetime.now(timezone.utc))
        options={} if uuid_factory is None else {'uuid_factory':uuid_factory}
        service=EvidenceService(request=request,profile=profile,transcript=transcript,attempt=attempt,
            tracking=tracking,storage=storage,request_id=request_id,created_at=now().isoformat(),
            models=evidence_models,answer_models=answer_models,routing_models=routing_models,on_progress=on_progress,budget=budget,
            topic_trigger=topic_trigger,semantic_confidence_trigger=semantic_confidence_trigger,
            history_identity_resolver=history_identity_resolver,**options)
        evidence=service.prepare(decision)  # Exact pending verification precedes retrieval and generation.
        if observe_evidence is not None:
            observe_evidence(evidence)
        if evidence.kind in ('ready','clarification_needed','insufficient_evidence') and evidence.destinations:
            if on_progress is not None:
                on_progress(4)  # Progress step 4: writing the answer.
            run_production(evidence=evidence,service=service,models=answer_models,observe_result=emit)
        else:
            dispositions=tuple(dict.fromkeys((*evidence.dispositions,
                *(PartDisposition(part,'refusal') for part in evidence.refused_parts),
                *(PartDisposition(part,'handoff_needed') for part in evidence.handoff_parts),
                *((PartDisposition('action','handoff_needed'),) if evidence.requires_action_handoff else ()))))
            nonproducing(evidence.kind,dispositions,evidence.unresolved_parts)

    public=run_incoming(request=request,profile=profile,transcript=transcript,attempt=attempt,
        tracking=tracking,storage=storage,request_id=request_id,jev_client=jev_client,
        reviewer_client=reviewer_client,jev_model=jev_model,security_trigger=security_trigger,
        topic_trigger=topic_trigger,semantic_confidence_trigger=semantic_confidence_trigger,
        allow_semantic_resolution=answer_models is not None,model_budget=budget,on_cleared=cleared,
        fallback_reviewer_client=fallback_reviewer_client,observe_response=observe_response,
        utc_now=utc_now,uuid_factory=uuid_factory,observe_decision=decisions.append)
    if not progressed:
        # Observe after the existing fallback save/verification completes; decisions alone prove no save.
        decision=decisions[-1] if decisions else IncomingDecision('stopped')
        dispositions=tuple(PartDisposition(part,'refusal') for part in decision.refused_departments)
        if decision.requires_action_handoff:
            dispositions+= (PartDisposition('action','handoff_needed'),)
        nonproducing(decision.kind,dispositions)
    return public  # Only existing attack/fallback envelopes or None; generated answering remains private.


# Flow: admitted identity/lease -> original incoming gates -> verified evidence -> bounded private production
# -> private immutable draft/disposition; existing public safe envelopes and resource ownership stay intact.


# Owner-approved fixed code-owned texts (Slice 5 plan §9).
STORAGE_CALL_SECONDS=8.0  # MLD §11: provisional cap per Chroma call.
ROUND_FLOOR_SECONDS=28.  # Owner 2026-11-20: one reviewer call (20 s cap) plus one final write (8 s).
MAX_REVISION_ROUNDS=3  # LLD: three shared revised rounds after the first review (rounds 0-3).
CLARIFICATION_TEXT='Could you add more detail so I can help with this question?'
REFUSAL_TEXT="I can't help with that request."
SUPPORT_OFFER_TEXT='Our support team can help with this. You can request a support handoff.'  # Owner 2026-11-20.
TECHNICAL_TEXT='Something went wrong while answering. You can request a support handoff.'
# Owner 2026-11-21 (CD-098): added by code before final review when a draft lists unresolved parts.
PARTIAL_SENTENCE=('For anything not answered here, you can ask in this chat, or in a new conversation later, '
                  'to be put in touch with our support team.')


def with_partial_sentence(snapshot):
    """Return the snapshot reviewers must judge: a partial draft ends with the fixed support sentence."""
    from dataclasses import replace
    if not snapshot.unresolved_parts:
        return snapshot
    return replace(snapshot, full_text=snapshot.full_text + '\n\n' + PARTIAL_SENTENCE)


def run_reviewed_workflow(*, review_models, **options):
    """Slice 5 composer around the unchanged private workflow (plan 2026-11-20 §3).

    Reviews a draft with both final reviewers on the same immutable snapshot, saves the terminal
    once, reads it back exactly, and only then returns a deliverable outcome. Rows of the outcome
    table not yet implemented return None (no delivery, no success claim).
    """
    from shopunow.evidence import ModelBudget
    if options.get('model_budget') is None:
        options['model_budget'] = ModelBudget()  # One counter shared by production and final review.
    results = []
    public = run_private_workflow(observe_result=results.append, **options)
    if public is not None or not results:
        return public  # Existing attack/fallback envelopes stay unchanged.
    return complete_reviewed(results[-1], review_models=review_models, **options)


def complete_reviewed(result, *, review_models, **options):
    """Apply review, correction, saving and the outcome table to one private production result.

    Separate seam so the Slice 5 evaluation can start from frozen production results (plan §7b).
    """
    from concurrent.futures import ThreadPoolExecutor  # Native parallelism for the two reviewers.
    from uuid import UUID
    from dataclasses import replace
    from shopunow.production import DraftSnapshot, run_final_review, run_producer
    from uuid import uuid4
    from shopunow.records import HandoffOffer, TerminalOutcome, verify_handoff_offer, verify_terminal_outcome
    from datetime import datetime, timezone
    from shopunow.schemas import (AnswerOutcome, ClarificationOutcome, FallbackOutcome, HandoffOfferOutcome,
                                  InteractionResponse,
                                  RefusalOutcome)

    request, profile, attempt = options['request'], options['profile'], options['attempt']
    storage, tracking, request_id = options['storage'], options['tracking'], options['request_id']
    budget = options['model_budget']

    def remaining():
        value = attempt.normal_work_timeout()
        if value is None:
            raise TimeoutError('Request stopped.')
        return value

    draft = result.draft
    now = (options.get('utc_now') or (lambda: datetime.now(timezone.utc)))().isoformat()
    pending = tracking.pending_intent(request_id)
    if pending is None and result.kind in ('refusal', 'clarification_needed', 'handoff_needed',
                                           'insufficient_evidence'):
        # Gate outcome: the question was never saved, so save and exact-verify it first (write if missing).
        from shopunow.evidence import EvidenceService
        pending = EvidenceService(storage=storage, models=options['evidence_models'],
            attempt=attempt, tracking=tracking, request=request, profile=profile,
            transcript=options['transcript'], request_id=request_id, created_at=now, budget=budget).save_pending()
    if pending is None or tracking.excludes((request.conversation_id,)):
        return None  # No verified saved question, so no final record and no success claim.
    try:
        # A retained intent is not proof of a stored question: read it back exactly before any final write.
        from shopunow.records import verify_pending_message
        left = attempt.normal_work_timeout()  # None after a stop: Q1 cleanup still needs this verified read.
        stored = verify_pending_message(storage.exact_pending(
            request_id, timeout=STORAGE_CALL_SECONDS if left is None else min(STORAGE_CALL_SECONDS, left)),
            user_id=pending.owner_user_id, conversation_id=pending.conversation_id, request_id=request_id,
            sequence=pending.sequence)
        if stored.record != pending:
            raise ValueError('Saved question differs from its intent.')
    except Exception:
        return None  # No verified pending/terminal prefix: no final record (LLD) and no success claim.

    write_started = False  # Once a final write starts, no cleanup may write a second final record.

    def save_terminal(kind, text, *, citations=(), offer_id=None, time_left=remaining):
        # One write, then exact read-back; anything unverified excludes the conversation.
        nonlocal write_started
        terminal = TerminalOutcome.model_validate(dict(
            record_type='terminal_outcome', outcome_id='terminal_outcome:' + UUID(request_id).hex,
            request_id=pending.request_id, owner_user_id=pending.owner_user_id,
            conversation_id=pending.conversation_id, sequence=pending.sequence + 1,
            pending_message_id=pending.message_id, kind=kind, approved_text_or_code=text,
            cleared_citation_ids=list(citations), verified_offer_id=offer_id, complete=True, created_at=now))
        first = min(STORAGE_CALL_SECONDS, time_left())  # A stop before this point is not an unknown write.
        try:
            write_started = True
            storage.write_terminal(terminal, timeout=first)
            checked = verify_terminal_outcome(storage.exact_terminal(request_id, timeout=min(STORAGE_CALL_SECONDS, time_left())),
                user_id=pending.owner_user_id, conversation_id=pending.conversation_id,
                request_id=request_id, sequence=pending.sequence + 1)
            if checked.record != terminal:
                raise ValueError('Terminal save is unverified.')
        except Exception:
            tracking.exclude_conversation(request.conversation_id)
            raise
        return terminal

    def deliver(outcome):
        return InteractionResponse(user_id=request.user_id, conversation_id=request.conversation_id,
                                   request_id=request_id, outcome=outcome)

    def offer(reason, text, target='human_support'):
        # Contact-free offer: write, exact read-back, then a terminal linked to it (LLD §4).
        offer_id = str(uuid4())
        intended = HandoffOffer.model_validate(dict(record_type='offer', offer_id=offer_id,
            request_id=pending.request_id, owner_user_id=pending.owner_user_id,
            conversation_id=pending.conversation_id, reason_code=reason,
            target_department_or_fallback=target, offer_code='handoff_offer', created_at=now, complete=True))
        first = min(STORAGE_CALL_SECONDS, remaining())  # A stop before this point is not an unknown write.
        try:
            storage.write_offer(intended, timeout=first)
            checked = verify_handoff_offer(storage.exact_offer(offer_id, timeout=min(STORAGE_CALL_SECONDS, remaining())),
                user_id=pending.owner_user_id, conversation_id=pending.conversation_id, request_id=request_id,
                offer_id=offer_id, reason_code=reason, target_department_or_fallback=target)
            if checked.record != intended:
                raise ValueError('Offer save is unverified.')
        except Exception:
            # Plan table: an unconfirmed offer is held (never shown or linked); the request ends in the
            # fixed technical record. If that save is unknown too, save_terminal excludes the conversation.
            save_terminal('failed_technical', 'technical_error')
            return deliver(FallbackOutcome(kind='fallback_error', text='The request could not be completed safely.',
                                           offer_id=None))
        save_terminal('completed_offer', text, offer_id=offer_id)
        return deliver(HandoffOfferOutcome(kind='handoff_offer', offer_id=offer_id, text=text))

    def decide():
        # The outcome table, applied only after the question is saved and verified.
        nonlocal draft
        if result.kind == 'clarification_needed':
            save_terminal('completed_clarification', CLARIFICATION_TEXT)
            return deliver(ClarificationOutcome(kind='clarification', text=CLARIFICATION_TEXT))
        if result.kind == 'refusal':
            save_terminal('completed_refusal', REFUSAL_TEXT)
            return deliver(RefusalOutcome(kind='refusal', text=REFUSAL_TEXT))
        if result.kind == 'technical_failure':
            return offer('technical_failure', TECHNICAL_TEXT)
        support_reasons = {'handoff_needed': 'handoff_needed', 'insufficient_evidence': 'insufficient_evidence',
                           'security_fallback': 'security_uncertain'}
        if result.kind in support_reasons:
            return offer(support_reasons[result.kind], SUPPORT_OFFER_TEXT)
        if result.kind != 'draft_ready' or draft is None:
            raise TimeoutError('Request stopped.')  # Stop rows after the saved question go to Q1 cleanup.
        findings = {row.part: row for row in result.contributions}  # Latest finding per producer.
        contexts = {row.part: row for row in result.contexts}  # Exact admitted inputs, reused for corrections.

        def review(snapshot):
            # Both reviewers judge the same immutable snapshot; the pool waits for both before returning.
            if options.get('on_progress') is not None:
                options['on_progress'](5)  # Progress step 5: checking the answer.
            with ThreadPoolExecutor(2) as pool:
                return list(pool.map(lambda reviewer: run_final_review(
                    snapshot, reviewer, role=profile.role, home_department=profile.home_department,
                    models=review_models, budget=budget, timeout=remaining), ('security', 'policy')))

        def correct(part, defects, open_parts=()):
            # Correction mode: the full original deadline (90 s), not the 60 s specialist cutoff.
            context = replace(contexts[part], correction=defects,
                contributions=tuple(findings.get(row.part, row) for row in contexts[part].contributions))
            if part == 'central_support':
                # Assembly rule on current findings: synthesis may cite what the specialists now cite.
                cited = {identity for row in context.contributions if row.kind == 'supported_answer'
                         for identity in row.cited_ids}
                # Keep CentralSupport's own rows (preference/central/history); drop specialist sources
                # that no current finding cites.
                before = {identity for row in contexts[part].contributions if row.kind == 'supported_answer'
                          for identity in row.cited_ids}
                pool = {row.excerpt_id: row for row in context.evidence if row.excerpt_id in cited
                        or row.excerpt_id not in before or row.destination in ('preference', 'central_support')}
                pool.update((row.excerpt_id, row) for other in context.contributions if other.part in contexts
                            for row in contexts[other.part].evidence if row.excerpt_id in cited)
                context = replace(context, evidence=tuple(pool.values()), unresolved_parts=tuple(open_parts))
            recalled = tuple(row.conversation_id for row in context.evidence if row.source_kind == 'owned_history')

            def context_remaining():
                # Same rule as production: recalled history excluded meanwhile makes the result unusable.
                if recalled and tracking.excludes(recalled):
                    raise TimeoutError('Recalled context became unresolved.')
                return remaining()
            findings[part] = run_producer(context, models=options['answer_models'], budget=budget,
                                          timeout=context_remaining)
            return findings[part]

        def revise(snapshot, verdicts):
            # Only named producers correct; other findings are preserved; CentralSupport rebuilds synthesis.
            named = {part for row in verdicts for part in row.responsible_producers}
            order = [part for part in snapshot.contributor_ids if part in named and part != 'central_support']
            if 'central_support' in snapshot.contributor_ids:
                order.append('central_support')  # Rebuilt after specialists, corrected only if named.
            open_parts = dict.fromkeys(snapshot.unresolved_parts)  # Ordered set of unresolved parts.
            dependent = contexts[snapshot.contributor_ids[0]].dependency != 'independent'
            for part in order:
                corrected = correct(part, tuple(row.defect for row in verdicts if part in row.responsible_producers),
                                    open_parts)
                open_parts.update(dict.fromkeys(corrected.unresolved_parts))
                # Assembly rules: a non-answer goes to the offer; a non-independent request never gets a
                # conclusion built on an unresolved part.
                if corrected.kind != 'supported_answer':
                    return corrected.kind  # The table row for this non-answer decides the offer.
                if dependent and open_parts:
                    return 'insufficient_evidence'
            if not order:
                return 'technical_failure'  # A revise naming no current contributor cannot be corrected.
            answer = findings[order[-1]]
            return DraftSnapshot(str(uuid4()), answer.text, answer.cited_ids, snapshot.contributor_ids,
                tuple(open_parts), round=snapshot.round + 1)

        def fits(calls):
            # A round starts only when its model calls fit the 40-attempt budget and enough time is left.
            return budget.attempts + calls <= 40 and remaining() >= ROUND_FLOOR_SECONDS

        def oversized(snapshot):
            # The added sentence can push a complete text over the limit: never truncate or review it.
            return len(snapshot.full_text.encode('utf-8')) > 8192

        draft = with_partial_sentence(draft)
        if oversized(draft):
            return offer('technical_failure', TECHNICAL_TEXT)
        while True:
            if not fits(2):
                return offer('technical_failure', TECHNICAL_TEXT)
            verdicts = review(draft)
            kinds = {row.verdict for row in verdicts}  # Precedence: failure > revise > approve.
            if kinds == {'approve'}:
                break
            if not kinds <= {'approve', 'revise'}:
                return offer('technical_failure', TECHNICAL_TEXT)
            if draft.round >= MAX_REVISION_ROUNDS:
                return offer('revision_rounds_exhausted', SUPPORT_OFFER_TEXT)  # Owner 2026-11-20.
            named = {part for row in verdicts for part in row.responsible_producers} - {'central_support'}
            if not fits(len(named) + ('central_support' in draft.contributor_ids) + 2):
                return offer('technical_failure', TECHNICAL_TEXT)  # Corrections plus both re-reviews must fit.
            draft = revise(draft, verdicts)
            if not isinstance(draft, str):
                draft = with_partial_sentence(draft)
                if oversized(draft):
                    return offer('technical_failure', TECHNICAL_TEXT)
            if isinstance(draft, str):  # A correction ended in a non-answer: its own table row.
                if draft in support_reasons:
                    return offer(support_reasons[draft], SUPPORT_OFFER_TEXT)
                if draft in ('context_needed', 'clarification_needed'):
                    # Pass 8: the corrector found the frozen evidence insufficient; not a technical failure.
                    return offer('insufficient_evidence', SUPPORT_OFFER_TEXT)
                return offer('technical_failure', TECHNICAL_TEXT)
        recalled = tuple(row.conversation_id for part in draft.contributor_ids for row in contexts[part].evidence
                         if row.source_kind == 'owned_history')
        if recalled and tracking.excludes(recalled):
            return offer('technical_failure', TECHNICAL_TEXT)  # History excluded during review is unusable.
        terminal = save_terminal('completed_answer', draft.full_text, citations=draft.cited_ids)
        return deliver(AnswerOutcome(kind='answer', text=terminal.approved_text_or_code,
                                     cited_ids=terminal.cleared_citation_ids))

    try:
        return decide()
    except Exception:
        if write_started or tracking.excludes((request.conversation_id,)) or attempt.normal_work_timeout() is not None:
            raise  # Unknown final write (conversation excluded) or a genuine error while still running.
    # Q1 (LLD clarification 2026-11-20): after a stop, one fixed technical record keeps the history readable.
    save_terminal('failed_technical', 'technical_error', time_left=lambda: STORAGE_CALL_SECONDS)
    return None  # Admission's own stop/deadline response applies; no success claim.
