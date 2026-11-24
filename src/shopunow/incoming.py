# Python 3.12 / Pydantic 2.14.1; incoming context contains lower-trust data only.
from dataclasses import dataclass  # Freeze the detached model-input snapshot.
from uuid import UUID  # Compare accepted UUID spellings by identity.
from math import isfinite, isclose  # Reject unresolved and invalid numeric judgments.
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer  # Native typed answers.

from shopunow.records import CompleteTranscript, ProfileRecord
from shopunow.schemas import QuestionRequest


@dataclass(frozen=True)
class ActiveExchange:
    user_text: str  # Preserve the complete user message without truncation.
    assistant_text: str  # Preserve its complete approved terminal text.


@dataclass(frozen=True)
class IncomingContext:
    question: str  # The current question counts toward the character budget.
    role: str  # Stored binding, never an identity claim extracted from text.
    home_department: str | None
    exchanges: tuple[ActiveExchange, ...]
    omitted: bool  # Explicitly tell judgments that earlier context is unavailable.


def build_incoming_context(transcript: CompleteTranscript, *, profile: ProfileRecord,
                           request: QuestionRequest) -> IncomingContext:
    """Select a suffix of an already verified owned transcript; no clearance is granted."""
    if profile.user_id != request.user_id:
        raise ValueError('Stored profile does not match the admitted request.')
    conversation = UUID(request.conversation_id)
    # Recheck every supplied exchange before selection so omitted foreign text cannot hide.
    for item in transcript.exchanges:
        for record in (item.exchange.pending, item.exchange.terminal):
            if record.owner_user_id != profile.user_id or UUID(record.conversation_id) != conversation:
                raise ValueError('Active context does not match the admitted request.')
        if item.exchange.terminal.kind == 'failed_technical':
            raise ValueError('Technical outcomes are not eligible active exchanges.')
    remaining = 8000 - len(request.question)  # Unicode characters, not encoded bytes.
    selected = []
    for item in reversed(transcript.exchanges):
        pair = item.exchange
        length = len(pair.pending.text) + len(pair.terminal.approved_text_or_code)
        if len(selected) == 6 or length > remaining:
            break  # Keep a contiguous recent suffix; never split or substitute exchanges.
        selected.append(ActiveExchange(pair.pending.text, pair.terminal.approved_text_or_code))
        remaining -= length
    return IncomingContext(request.question, profile.role, profile.home_department,
                           tuple(reversed(selected)), len(selected) < len(transcript.exchanges))


# Flow: verified admission inputs -> binding checks -> bounded complete suffix -> frozen data.


CHOICE_LABELS = {
    'intent': ('information', 'individual_case_or_action', 'mixed', 'unclear_or_other'),
    'sentiment': ('positive', 'neutral', 'negative'),
    'department_dependency': ('independent', 'dependent', 'unclear'),
}
NOUL_FIELDS = (
    'company_wide_topic', 'topic_hr', 'topic_it_support', 'topic_facilities_admin',
    'topic_billing_payments', 'topic_shipping_delivery',
    'prompt_injection', 'sql_injection', 'profile_override_attempt', 'access_concern',
)
# Reuse the existing topic/dependency contract after clean history resolves an unknown subject.
HISTORY_ROUTE_FIELDS = tuple(key for key in NOUL_FIELDS
                            if key=='company_wide_topic' or key.startswith('topic_')) + ('department_dependency',)


TOPIC_SETTLED = .1  # = 2 x (0.55 - 0.5); was the shared 0.7 (= p 0.85). Jev replay 2026-11-24, plan §4f.


def _number(value, maximum=1.0):
    # Python bool is an int subclass; a boolean is not a provider probability.
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= maximum:
        raise ValueError('Invalid judgment number.')


def semantic_confident(answer, threshold):
    """Use native uncertainty for semantics; this never authorizes access or security."""
    if threshold is None:
        return True  # Unconfigured callers retain the frozen policy, not a new serving default.
    _number(threshold)
    if isinstance(answer,NoulAnswer):  # Topics: settled at p >= 0.55 or <= 0.45 (owner 2026-11-24, plan §4f).
        return abs(2*answer.noul-1)>=TOPIC_SETTLED
    return answer.confidence>=threshold  # Choices (intent, dependency, relevance) keep the configured bar.


def validate_judgments(answers):
    """Validate all obligatory native answers before consuming any individual signal."""
    return _validate_fields(answers,set(CHOICE_LABELS) | set(NOUL_FIELDS) | {'severity'})


def validate_history_route(answers):
    """The post-clearance routing pass over recalled history."""
    return _validate_fields(answers,set(HISTORY_ROUTE_FIELDS))


def _validate_fields(answers,required):
    # Both fixed contracts retain identical native type/distribution/numeric validation.
    if not isinstance(answers, dict) or set(answers) != required:
        raise ValueError('Incomplete or unexpected obligatory judgments.')
    checked = {}
    choices = CHOICE_LABELS
    for key, raw in answers.items():
        # Revalidate native SDK objects too; constructed instances are not validation proof.
        if isinstance(raw, (ChoiceAnswer, NoulAnswer, ScoreAnswer)):
            raw = raw.model_dump()
        if not isinstance(raw, dict):
            raise ValueError('Invalid judgment answer.')
        model = ChoiceAnswer if key in choices else ScoreAnswer if key == 'severity' else NoulAnswer
        if set(raw) != set(model.model_fields):
            raise ValueError('Missing or unexpected native answer fields.')
        if model is NoulAnswer:
            _number(raw.get('noul'))
        else:
            _number(raw.get('confidence'))
            probabilities = raw.get('probabilities')
            labels = set(choices[key]) if model is ChoiceAnswer else {0, 1, 2}
            if not isinstance(probabilities, dict) or set(probabilities) != labels:
                raise ValueError('Invalid judgment distribution labels.')
            for value in probabilities.values():
                _number(value)
            if not isclose(sum(probabilities.values()), 1.0, abs_tol=0.01):
                raise ValueError('Invalid judgment probability mass.')
            if model is ChoiceAnswer:
                choice = raw.get('choice')
                if choice not in labels or probabilities[choice] < max(probabilities.values()):
                    raise ValueError('Invalid selected judgment label.')
            else:
                _number(raw.get('score'), maximum=2.0)
                if not isinstance(raw.get('legend'), dict) or set(raw['legend']) != labels:
                    raise ValueError('Invalid severity rubric.')
                expected = sum(level * probability for level, probability in probabilities.items())
                # Normalizing a mass within .01 of 1 on a 0..2 rubric can change
                # the expectation by at most .02. This is not a general provider
                # rounding bound or a routing threshold; live precision is unverified.
                if not isclose(raw['score'], expected, abs_tol=0.02):
                    raise ValueError('Severity contradicts its probability distribution.')
        checked[key] = model.model_validate(raw, strict=True)
    return checked  # Typed values remain advisory; this function grants no clearance.


AttackType = Literal['prompt_injection', 'sql_injection', 'profile_override_attempt']


class IncomingReview(BaseModel):
    """Native structured reviewer output; access policy remains code-owned."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True, hide_input_in_errors=True)
    verdict: Literal['clear', 'confirmed_attack', 'uncertain']
    attack_types: list[AttackType]  # Required empty array for clear/uncertain.

    @model_validator(mode='after')
    def require_consistent_types(self):
        if bool(self.attack_types) != (self.verdict == 'confirmed_attack'):
            raise ValueError('Attack confirmation requires attack types exclusively.')
        if len(set(self.attack_types)) != len(self.attack_types):
            raise ValueError('Duplicate attack types.')
        return self


def reviewer_disposition(raw):
    """Malformed or missing reviewer output always withholds normal processing."""
    try:
        return IncomingReview.model_validate(raw).verdict
    except (ValidationError, ValueError, TypeError):
        return 'uncertain'  # Callers must use the verified fallback, never claim a saved offer.


# Flow: complete SDK answer set -> structural/numeric checks -> advisory typed judgments;
# reviewer output -> native strict contract -> clear/confirmed/uncertain disposition.
