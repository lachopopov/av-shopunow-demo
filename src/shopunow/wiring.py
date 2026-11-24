# Python 3.12 / OpenAI 3.46.0 / TypeSafe SDK 0.7.5; Slice 6 wiring for the served answer route.
# One factory turns two provider clients into the admitted reviewed workflow (CD-094, CD-099).
from shopunow.model_clients import AnswerModels, EvidenceModels  # Existing thin typed adapters.
from shopunow.workflow import run_reviewed_workflow  # Slice 5 composer: review, save, read back, deliver.

# Evaluated configuration (CD-099): fixed in code so a host setting cannot silently weaken a check.
SECURITY_TRIGGER = .7  # Attack-risk score that calls the incoming security reviewer.
TOPIC_TRIGGER = .5  # Score at which a department topic counts as present.
SEMANTIC_CONFIDENCE_TRIGGER = .7  # Below this a semantic choice is uncertain and adjudicated.
TOPIC_CONFIDENCE_TRIGGER = .1  # Topic settled when |2p - 1| >= this: p >= 0.55 or <= 0.45 (owner 2026-11-24, plan 4f).
JEV_MODEL = 'jev-1.14.0'
ANSWER_MODEL, ANSWER_REASONING = 'gpt-6.1-sol', 'medium'  # Specialists and CentralSupport.
REVIEW_MODEL, REVIEW_REASONING = 'gpt-6-luna', 'high'  # Both final reviewers (CD-095).
ROUTING_MODEL, ROUTING_REASONING = 'gpt-6.1-sol', 'low'  # Routing tie-breaker (owner 2026-11-23; low = Sol's lowest).
# Excerpt keep/drop call (CandidateResolution), owner 2026-11-24 after the model replay: 1.6 s vs 4.1 s median.
CANDIDATE_MODEL, CANDIDATE_REASONING = 'gpt-6.2-sol', 'low'
# Incoming review (gpt-6-luna medium, one gpt-6.1-terra low backup), history identity (gpt-6-luna),
# embeddings (text-embedding-3-small) and the 5 process-wide model slots are the adapters' own defaults.


def answering(*, openai_client, jev_client, progress=None):
    """Return the storage app's factory: (storage, tracking) -> admitted-question continuation.

    The caller creates the clients (hosted.build_app, or a test); they live for the whole process.
    Nothing here reads credentials or the environment.
    """
    # Pass 4 (owner 2026-11-24): no packaged FAQ copy is loaded; Chroma is the only FAQ store.
    evidence_models = EvidenceModels(jev=jev_client, reviewer=openai_client, embedding=openai_client,
                                     jev_model=JEV_MODEL)
    answer_models = AnswerModels(client=openai_client, model=ANSWER_MODEL, reasoning=ANSWER_REASONING)
    review_models = AnswerModels(client=openai_client, model=REVIEW_MODEL, reasoning=REVIEW_REASONING)
    routing_models = AnswerModels(client=openai_client, model=ROUTING_MODEL, reasoning=ROUTING_REASONING)
    candidate_models = AnswerModels(client=openai_client, model=CANDIDATE_MODEL, reasoning=CANDIDATE_REASONING)

    def factory(*, storage, tracking):
        def admitted(*, request_id, **state):
            # state: the admitted request, stored profile, verified transcript and attempt (admission.py).
            # progress (owner 2026-11-23): step numbers for the UI, keyed by the asking conversation.
            key = (state['request'].user_id, state['request'].conversation_id)
            report = None if progress is None else (lambda step: progress.report(*key, step))
            if report is not None:
                report(1)  # Progress step 1: checking the question.
            try:
                return run_reviewed_workflow(
                    **state, storage=storage, tracking=tracking, request_id=request_id,
                    evidence_models=evidence_models, answer_models=answer_models, review_models=review_models,
                    routing_models=routing_models, candidate_models=candidate_models,
                    jev_client=jev_client, reviewer_client=openai_client, fallback_reviewer_client=openai_client,
                    jev_model=JEV_MODEL, security_trigger=SECURITY_TRIGGER, topic_trigger=TOPIC_TRIGGER,
                    semantic_confidence_trigger=SEMANTIC_CONFIDENCE_TRIGGER,
                    topic_confidence_trigger=TOPIC_CONFIDENCE_TRIGGER,
                    history_identity_resolver=evidence_models.resolve_history_identity, on_progress=report)
            finally:
                if progress is not None:
                    progress.clear(*key)  # However the question ends, its step disappears.
        return admitted
    return factory


# Flow: provider clients -> fixed evaluated models/thresholds -> per-app (storage, tracking)
# -> per-request admitted continuation -> reviewed, saved, read-back outcome or None.
