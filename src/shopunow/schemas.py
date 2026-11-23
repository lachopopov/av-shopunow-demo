# Pydantic v2.14.1
# These checks establish syntax only; ownership is a later Slice 1 task.
from typing import Annotated, Literal  # Native constraints and fixed public labels.
from uuid import UUID  # Normalize conversation selectors for correlation.

from pydantic import BaseModel, ConfigDict, Field, field_validator  # Native validation.


from email.errors import HeaderParseError  # Normalize native parser failures into validation.
from email.headerregistry import Address  # Standard-library mailbox syntax, no email SDK/dependency.
from pydantic import model_validator, ValidationInfo  # Native tags and trusted validation context.

class QuestionRequest(BaseModel):
    """A syntactically valid question; it is not yet authorized or admitted."""

    model_config = ConfigDict(strict=True, extra="forbid")  # No coercion/client claims.
    user_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    conversation_id: Annotated[
        str,
        Field(pattern=r"^(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})$"),
    ]  # Accept UUID hex/standard hyphenated syntax; neither proves ownership.
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    response_style: Literal['brief', 'detailed'] | None = None  # Session-only presentation control.

    @field_validator("question")
    @classmethod
    def valid_text(cls, question: str) -> str:
        if not question.strip():  # Trim only to detect blankness.
            raise ValueError("A nonblank question is required.")
        question.encode("utf-8")  # Reject lone surrogates from escaped JSON too.
        return question  # Preserve the submitted text exactly.


# Overall flow: strict shape -> bounded syntax -> nonblank UTF-8 text.


# History models reuse request syntax without importing the internal records module.
UserId = Annotated[str, *QuestionRequest.model_fields["user_id"].metadata]
ConversationId = Annotated[str, *QuestionRequest.model_fields["conversation_id"].metadata]


class HistoryQuery(BaseModel):
    """Exactly two syntactic selectors; neither establishes stored ownership."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    user_id: UserId  # Reuse the existing synthetic identifier contract.
    conversation_id: ConversationId  # Preserve accepted spelling until selector capture.


class HistorySelectors(BaseModel):
    """Captured operation selectors; syntax and immutability confer no ownership."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    request_id: ConversationId  # Reference for this read, not a historical exchange.
    user_id: UserId
    conversation_id: ConversationId

    @field_validator("conversation_id")
    @classmethod
    def canonical_conversation(cls, value: str) -> str:
        return str(UUID(value))  # Match the existing verified header's UUID identity.


class HistoryUserMessage(BaseModel):
    """Public stored user message, with no assistant-only or internal fields."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    message_id: str
    request_id: ConversationId  # Historical request, independent of the read reference.
    sequence: Annotated[int, Field(ge=1)]
    speaker: Literal["user"] = "user"
    text: str  # Already bounded and verified by the transcript stage; never truncate.


class HistoryAssistantMessage(BaseModel):
    """Public completed assistant outcome; technical failures are notices instead."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    message_id: str  # Stored terminal outcome ID occupies the public message ID field.
    request_id: ConversationId
    sequence: Annotated[int, Field(ge=1)]
    speaker: Literal["assistant"] = "assistant"
    text: str
    outcome_kind: Literal[
        "completed_answer", "completed_clarification", "completed_refusal", "completed_offer",
    ]


class HistoryNotice(BaseModel):
    """Content-free historical notice; an offer link does not prove submission."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    request_id: ConversationId
    reason_code: str
    offer_id: ConversationId | None  # Required null survives ordinary native serialization.
    code_owned_text: str


class HistoryResponse(HistorySelectors):
    """Public format only: no ownership, clearance, scan or freshness certificate."""

    messages: tuple[HistoryUserMessage | HistoryAssistantMessage, ...]
    notices: tuple[HistoryNotice, ...]  # Native JSON serialization emits arrays for tuples.


# History flow: captured immutable selectors + explicitly projected fields -> native JSON.


# Pydantic v2.14.1; public identity-read syntax and content allowlists.
DepartmentName = Literal['hr', 'it_support', 'facilities_admin', 'billing_payments', 'shipping_delivery']


class ProfileResolveRequest(BaseModel):
    """Optional client selections never replace a stored profile binding."""

    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    user_id: UserId
    role: Literal['customer', 'employee'] | None = None
    home_department: DepartmentName | None = None


class PreferenceReadQuery(BaseModel):
    """Exactly one synthetic user selector, without write parameters."""

    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    user_id: UserId


class IdentityReadSelectors(PreferenceReadQuery):
    """Capture correlation independently of optional client profile selections."""

    request_id: ConversationId


class ProfileBinding(PreferenceReadQuery):
    """Only public stored-binding fields; no record metadata or revision."""

    role: Literal['customer', 'employee']
    home_department: DepartmentName | None


class ProfileResolveResponse(IdentityReadSelectors):
    profile: ProfileBinding
    created: Literal[False]  # Initial predefined-profile phase performs no creation.


class PreferenceReadResponse(IdentityReadSelectors):
    response_style: Literal['brief', 'detailed'] | None  # Absence is explicit, never inferred.


# Flow: strict request -> verified stored values -> explicit public response fields.


# Pydantic v2.14.1; owned-record wire fields, independent of storage metadata.
class ConversationCreateRequest(PreferenceReadQuery):
    pass  # Exactly one user selector; no client-generated conversation ID.


class ConversationCreateResponse(IdentityReadSelectors):
    conversation_id: ConversationId


class ConversationReference(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    conversation_id: ConversationId
    created_at: str  # Project the verified aware timestamp as ISO text.


class ConversationListResponse(IdentityReadSelectors):
    conversations: tuple[ConversationReference, ...]


class PreferenceUpdateRequest(PreferenceReadQuery):
    key: Literal['response_style']
    value: Literal['brief', 'detailed']


class PreferenceUpdateSelectors(PreferenceUpdateRequest):
    request_id: ConversationId


class PreferenceUpdateResponse(IdentityReadSelectors):
    key: Literal['response_style']
    value: Literal['brief', 'detailed']


# Pydantic v2.14.1; future wire shapes only, never proof of review or persistence.


class TextOutcome(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    text: str

    @field_validator('text')
    @classmethod
    def bounded_output(cls, value):
        if not value.strip() or len(value.encode('utf-8')) > 8192:
            raise ValueError('Invalid complete output text.')
        return value  # Never truncate a future reviewed result to fit the wire contract.


class AnswerOutcome(TextOutcome):
    kind: Literal['answer']
    cited_ids: tuple[Annotated[str, Field(min_length=1)], ...]


class ClarificationOutcome(TextOutcome):
    kind: Literal['clarification']


class RefusalOutcome(TextOutcome):
    kind: Literal['refusal']


class HandoffOfferOutcome(TextOutcome):
    kind: Literal['handoff_offer']
    offer_id: ConversationId  # Later recorder must verify the saved offer and its terminal link.
    # text: the saved code-owned terminal text, so the live and reopened offer match (LLD 2026-11-21).


class ConfirmedAttackOutcome(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    kind: Literal['confirmed_attack_block']
    text: Literal['The request cannot be processed.']


class FallbackOutcome(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    kind: Literal['fallback_error']
    text: Literal['The request could not be completed safely.']
    offer_id: ConversationId | None  # Shape only; null cannot be omitted accidentally.


class InteractionResponse(HistorySelectors):
    outcome: Annotated[AnswerOutcome | ClarificationOutcome | RefusalOutcome |
                       HandoffOfferOutcome | ConfirmedAttackOutcome | FallbackOutcome,
                       Field(discriminator='kind')]


class HandoffRequest(HistoryQuery):
    offer_id: ConversationId
    name: Annotated[str, Field(max_length=200)]  # Bounded synthetic form data (one Escalations document).
    phone: Annotated[str, Field(max_length=40)] | None = None
    email: Annotated[str, Field(max_length=254)] | None = None

    @field_validator('name','phone','email')
    @classmethod
    def nonblank_contact(cls, value):
        if value is not None:
            if not value.strip():
                raise ValueError("A supplied contact field cannot be blank.")
            value.encode('utf-8')
        return value  # Preserve input; it remains untrusted, synthetic form data.

    @field_validator('email')
    @classmethod
    def mailbox_syntax(cls, value):
        if value is not None:
            try:
                address=Address(addr_spec=value)
                if not address.username or not address.domain:
                    raise ValueError('Missing mailbox parts.')
            except (ValueError,HeaderParseError,IndexError):
                # Python 3.12's parser can raise IndexError for an incomplete domain.
                raise ValueError('A valid mailbox with a domain is required.') from None
        return value

    @model_validator(mode='after')
    def contact_rule(self, info:ValidationInfo):
        if self.phone is None and self.email is None:
            raise ValueError('At least one contact method is required.')
        # Only backend-derived follow-up wording may supply this validation context.
        if info.context and info.context.get('email_followup_required') is True and self.email is None:
            raise ValueError('Email is required for the promised follow-up.')
        return self


class HandoffSelectors(HandoffRequest):
    request_id: ConversationId  # This submission's reference, captured before any storage work.


class HandoffResponse(HistorySelectors):
    submission_id: ConversationId
    receipt_id: ConversationId
    contact_method: Literal['phone','email']  # The recorder selects an actually supplied method.
    simulated: bool

    @field_validator('simulated')
    @classmethod
    def simulated_only(cls, value):
        if value is not True:
            raise ValueError("Only simulated handoffs are supported.")
        return value


class HealthResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    status: Literal['ok'] = 'ok'  # Process liveness only, never provider/storage readiness.
