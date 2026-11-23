# Pydantic v2.14.1 / Python 3.12
# Verify supplied exact-read results only; this module performs no storage I/O.
from dataclasses import dataclass  # Return a small immutable status and optional record.
from datetime import datetime  # Parse the stored ISO timestamp using the standard library.
from typing import Annotated, Literal, Self  # Express bounded identifiers and fixed labels.
from uuid import UUID  # Normalize accepted UUID spellings to one conversation identity.

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError,
    field_validator, model_validator,
)  # Use the pinned native parser, field checks, and cross-field validation.


# Reuse the existing request field definitions, including their syntax constraints.
from shopunow.schemas import QuestionRequest

UserId = Annotated[str, *QuestionRequest.model_fields["user_id"].metadata]
ConversationId = Annotated[str, *QuestionRequest.model_fields["conversation_id"].metadata]
_USER_ID = TypeAdapter(UserId)  # The same syntax boundary applies to lookup selectors.
_CONVERSATION_ID = TypeAdapter(ConversationId)
MessageSequence = Annotated[int, Field(ge=0)]
_MESSAGE_SEQUENCE = TypeAdapter(MessageSequence)  # Origin/order belongs to the caller's prefix.
_EVENT_CAP = TypeAdapter(Annotated[int, Field(ge=1)])  # Configuration must be a positive integer.
Department = Literal[
    "hr", "it_support", "facilities_admin", "billing_payments", "shipping_delivery",
]  # Canonical department labels from the approved synthetic profile source.

# Completed clarification/refusal exchanges may contain relevant owned user context.
# Their text still needs relevance and excerpt clearance; none is authoritative company policy.
HISTORY_EVIDENCE_KINDS = ('completed_answer', 'completed_clarification', 'completed_refusal')


def _bounded_conversation_text(text: str) -> str:
    """Apply the shared stored-text contract without modifying accepted content."""
    if not text.strip() or len(text.encode("utf-8")) > 8192:
        raise ValueError("The stored conversation text is invalid.")
    return text  # Native Pydantic runs this only after strict string/minimum-length checks.


ConversationText = Annotated[str, Field(min_length=1), AfterValidator(_bounded_conversation_text)]


class _CompleteRecord(BaseModel):
    """Shared strict completeness checks, independent of each record's timestamp name."""

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True,
    )  # Do not coerce identity data, accept unexpected fields, or mutate bindings.
    complete: bool  # Strict bool rejects 1 and "true", which Literal[True] can coerce.

    @model_validator(mode="after")
    def require_complete(self) -> Self:
        if not self.complete:  # An incomplete record never becomes a verified value.
            raise ValueError("The record is incomplete.")
        return self


class ProfileRecord(_CompleteRecord):
    """An immutable stored synthetic binding, independent of client role claims."""

    record_type: Literal["profile"]
    created_at: AwareDatetime  # Creation remains required for the immutable profile.
    user_id: UserId
    role: Literal["customer", "employee"]
    home_department: Department | None  # Required field even for a customer's null.
    version: Annotated[int, Field(ge=1)]  # Local record revision, not dataset schema_version.

    @model_validator(mode="after")
    def require_role_department_binding(self) -> Self:
        if (self.role == "customer") != (self.home_department is None):
            raise ValueError("The profile binding is invalid.")
        return self  # Employees require a department; customers must not have one.


class ConversationHeader(_CompleteRecord):
    """Only the owned header; this says nothing about the conversation's events."""

    record_type: Literal["conversation_header"]
    created_at: AwareDatetime  # A header records creation, not preference-update time.
    conversation_id: UUID  # Pydantic's strict JSON mode accepts a UUID string.
    owner_user_id: UserId


class PreferenceRecord(_CompleteRecord):
    """One explicitly saved user-wide style; this does not authorize model use."""

    record_type: Literal["preference"]
    owner_user_id: UserId
    key: Literal["response_style"]  # No arbitrary preference catalog or contact fields.
    value: Literal["brief", "detailed"]  # Do not infer or persist a default on absence.
    version: Annotated[int, Field(ge=1)]  # Validate a saved revision, without claiming freshness.
    updated_at: AwareDatetime  # Require a timezone-aware update time, not created_at.


class _ConversationIdentity(_CompleteRecord):
    """Share strict owner/request identity and native timestamp validation."""

    request_id: ConversationId  # Reuse the existing accepted UUID string syntax.
    owner_user_id: UserId
    conversation_id: ConversationId
    created_at: AwareDatetime

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_metadata_timestamp(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("A stored timestamp string is required.")
        return datetime.fromisoformat(value)  # Native AwareDatetime then requires a timezone.


OfferLabel = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
_OFFER_LABEL = TypeAdapter(OfferLabel)  # Bounded local encoding, not route authorization.


class HandoffOffer(_ConversationIdentity):
    """Contact-free offer structure, without terminal linkage or submission proof."""

    record_type: Literal["offer"]
    offer_id: ConversationId
    reason_code: OfferLabel
    target_department_or_fallback: OfferLabel
    offer_code: Literal["handoff_offer"]  # Local fixed native-document encoding.


class _ConversationEvent(_ConversationIdentity):
    """Normal messages additionally require a nonnegative transcript position."""

    sequence: MessageSequence  # Markers deliberately do not inherit this field.


class PreclearanceTerminal(_ConversationIdentity):
    """A content-free marker structure, without proof of a saved usable offer."""

    record_type: Literal["preclearance_terminal"]
    marker_id: str
    verified_offer_id: ConversationId  # Required reference, compared with caller expectation.
    reason_code: Literal["security_uncertain", "dependency_failure"]
    marker_code: Literal["preclearance_terminal"]  # Local fixed native-document encoding.


class PendingMessage(_ConversationEvent):
    """A structurally valid pending event; never a completed exchange or clearance proof."""

    record_type: Literal["pending_message"]
    message_id: str  # Must also equal the native ID derived from the expected request.
    speaker: Literal["user"]
    text: ConversationText  # Supplied exclusively by the native document.
    status: Literal["pending"]


class TerminalOutcome(_ConversationEvent):
    """Terminal record structure, without delivery, review, evidence or persistence proof."""

    record_type: Literal["terminal_outcome"]
    outcome_id: str
    pending_message_id: str  # Must bind to the same expected request's pending event.
    kind: Literal[
        "completed_answer", "completed_clarification", "completed_refusal",
        "completed_offer", "failed_technical",
    ]
    approved_text_or_code: ConversationText  # The native document, never metadata text.
    cleared_citation_ids: tuple[str, ...] = ()  # Optional labels; not proof of actual clearance.
    verified_offer_id: ConversationId | None = None  # Shape only; no offer read is performed.

    @field_validator("cleared_citation_ids", mode="before")
    @classmethod
    def detach_citation_list(cls, value: object) -> tuple:
        if not isinstance(value, list):
            raise ValueError("A stored citation list is required.")
        return tuple(value)  # Native strict tuple/item validation follows; no mutable alias remains.

    @field_validator("cleared_citation_ids")
    @classmethod
    def require_bounded_citation_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value.encode("utf-8")) > 128 for value in values):
            raise ValueError("A stored citation label is invalid.")
        return values  # Bounded opaque labels, with membership/review checked elsewhere.

    @model_validator(mode="after")
    def require_kind_specific_structure(self) -> Self:
        if self.kind == "completed_offer" and self.verified_offer_id is None:
            raise ValueError("An offer reference is required.")
        if self.kind == "failed_technical" and self.approved_text_or_code != "technical_error":
            raise ValueError("A fixed technical-failure code is required.")
        return self  # The code is never model text; later reopening maps it to a fixed notice.


class _ExactGetResult(BaseModel):
    """Validate native flat arrays without wrapping or translating SDK results."""

    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    ids: list[str]
    metadatas: list[dict[str, object] | None]
    documents: list[str | None]  # Missing include fields and null arrays are unresolved.


@dataclass(frozen=True)
class RecordVerification[T]:
    """Internal status only; non-verified outcomes never expose returned content."""

    status: Literal["verified", "absent", "conflict", "unresolved"]
    record: T | None = None


def _exact_result(result: object, *, record_id: str) -> RecordVerification[_ExactGetResult]:
    """Share native shape/cardinality/ID checks for JSON records and plain-text events."""
    try:
        returned = _ExactGetResult.model_validate(result)
    except ValidationError:
        return RecordVerification("unresolved")
    lengths = (len(returned.ids), len(returned.metadatas), len(returned.documents))
    if lengths == (0, 0, 0):
        return RecordVerification("absent")  # Snapshot absence is not no-late-write proof.
    if lengths != (1, 1, 1):
        return RecordVerification("unresolved")  # No truncation, zipping or first-hit choice.
    if returned.ids[0] != record_id:
        return RecordVerification("conflict")
    return RecordVerification("verified", returned)  # Private intermediate, not a verified record.


def _verify_exact[T: _CompleteRecord](
    result: object, *, record_id: str, metadata: dict[str, str], model: type[T],
) -> RecordVerification[T]:
    """Validate identity metadata and the complete JSON document of a logical record."""
    exact = _exact_result(result, record_id=record_id)
    if exact.record is None:
        return RecordVerification(exact.status)
    returned = exact.record
    try:
        returned_metadata = returned.metadatas[0]
        if returned_metadata is None or returned_metadata.keys() != metadata.keys():
            return RecordVerification("unresolved")  # Required identity fields cannot be missing.
        if returned_metadata != metadata:
            return RecordVerification("conflict")  # Never trust a filter to prove ownership.
        document = returned.documents[0]
        if document is None:
            return RecordVerification("unresolved")
        document.encode("utf-8")  # Reject an actual lone surrogate before parsing JSON.
        record = model.model_validate_json(document)  # Check every logical field natively.
    except (ValidationError, UnicodeError):
        return RecordVerification("unresolved")  # Do not return/log parser details or inputs.
    return RecordVerification("verified", record)


def verify_profile(result: object, *, user_id: str) -> RecordVerification[ProfileRecord]:
    """Verify an exact profile response for a syntactically valid selected user."""

    selected_user = _USER_ID.validate_python(user_id, strict=True)
    verified = _verify_exact(
        result, record_id=f"profile:{selected_user}",
        metadata={"record_type": "profile", "user_id": selected_user}, model=ProfileRecord,
    )  # Prefix plus a bounded ASCII ID is unambiguous and at most 72 UTF-8 bytes.
    if verified.record is not None and verified.record.user_id != selected_user:
        return RecordVerification("conflict")  # Document and metadata must bind the same user.
    return verified


def verify_conversation_header(
    result: object, *, user_id: str, conversation_id: str,
) -> RecordVerification[ConversationHeader]:
    """Verify the header's native ID, metadata and complete document owner binding."""

    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    verified = _verify_exact(
        result, record_id=f"conversation_header:{selected_conversation.hex}",
        metadata={
            "record_type": "conversation_header", "owner_user_id": selected_user,
            "conversation_id": str(selected_conversation),
        }, model=ConversationHeader,
    )  # One canonical UUID creates one 52-byte typed key across accepted input spellings.
    if verified.record is not None and (
        verified.record.owner_user_id != selected_user
        or verified.record.conversation_id != selected_conversation
    ):
        return RecordVerification("conflict")
    return verified


def verify_created_conversation_header(
    result: object, *, expected: ConversationHeader,
) -> RecordVerification[ConversationHeader]:
    """Verify supplied read-back against an independently captured creation intent."""
    intended = ConversationHeader.model_validate(expected.model_dump(warnings="error"))
    checked = verify_conversation_header(
        result, user_id=intended.owner_user_id, conversation_id=str(intended.conversation_id),
    )  # Keep the established native ID, metadata, JSON and completeness validation.
    if checked.record is not None and checked.record != intended:
        return RecordVerification("conflict")  # A different creation time is not this write.
    return checked  # Native timestamps compare instants, not JSON formatting.


def verify_preference(result: object, *, user_id: str) -> RecordVerification[PreferenceRecord]:
    """Verify an exact response for the selected user's sole initial preference key."""

    selected_user = _USER_ID.validate_python(user_id, strict=True)
    verified = _verify_exact(
        result, record_id=f"preference:{selected_user}:response_style",
        metadata={
            "record_type": "preference", "owner_user_id": selected_user, "key": "response_style",
        }, model=PreferenceRecord,
    )  # Colon-free validated IDs make the typed key unambiguous and at most 90 UTF-8 bytes.
    if verified.record is not None and verified.record.owner_user_id != selected_user:
        return RecordVerification("conflict")  # The document cannot replace the metadata owner.
    return verified  # A valid stored prior revision is readable; write read-back is separate.


def verify_pending_message(
    result: object, *, user_id: str, conversation_id: str, request_id: str, sequence: int,
) -> RecordVerification[PendingMessage]:
    """Verify one pending event against caller-owned identity and sequence expectations."""
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    selected_request = UUID(_CONVERSATION_ID.validate_python(request_id, strict=True))
    expected_sequence = _MESSAGE_SEQUENCE.validate_python(sequence, strict=True)
    message_id = f"pending_message:{selected_request.hex}"  # Stable 48-byte typed request key.
    exact = _exact_result(result, record_id=message_id)
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata = exact.record.metadatas[0]
    if metadata is None or "text" in metadata:
        return RecordVerification("unresolved")  # Metadata may never override or supply text.
    try:
        record = PendingMessage.model_validate({**metadata, "text": exact.record.documents[0]})
    except ValidationError:
        return RecordVerification("unresolved")  # Withhold invalid text and validation details.
    if (
        record.message_id != message_id or record.owner_user_id != selected_user
        or record.conversation_id != str(selected_conversation)
        or record.request_id != str(selected_request) or record.sequence != expected_sequence
    ):
        return RecordVerification("conflict")
    return RecordVerification("verified", record)  # Still pending; not normal history/Jev input.


def verify_terminal_outcome(
    result: object, *, user_id: str, conversation_id: str, request_id: str, sequence: int,
) -> RecordVerification[TerminalOutcome]:
    """Verify one terminal record's structure and expected links, without approving delivery."""
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    selected_request = UUID(_CONVERSATION_ID.validate_python(request_id, strict=True))
    expected_sequence = _MESSAGE_SEQUENCE.validate_python(sequence, strict=True)
    outcome_id = f"terminal_outcome:{selected_request.hex}"  # Stable 49-byte typed request key.
    exact = _exact_result(result, record_id=outcome_id)
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata = exact.record.metadatas[0]
    if metadata is None or "approved_text_or_code" in metadata:
        return RecordVerification("unresolved")  # Text/code comes only from the native document.
    try:
        record = TerminalOutcome.model_validate({
            **metadata, "approved_text_or_code": exact.record.documents[0],
        })
    except ValidationError:
        return RecordVerification("unresolved")  # Never expose rejected content or parser detail.
    if (
        record.outcome_id != outcome_id or record.owner_user_id != selected_user
        or record.conversation_id != str(selected_conversation)
        or record.request_id != str(selected_request) or record.sequence != expected_sequence
        or record.pending_message_id != f"pending_message:{selected_request.hex}"
    ):
        return RecordVerification("conflict")
    return RecordVerification("verified", record)  # Pairing, reviews and linked offer remain unproved.


# Overall flow: validate selectors -> exact arrays/ID -> native record/document validation
# -> document identity comparison -> immutable record, or a content-free failure status.


@dataclass(frozen=True)
class PendingTerminalPair:
    """A structurally consistent exchange, without approval or transcript-prefix proof."""

    pending: PendingMessage  # Existing frozen models retain their validated document text.
    terminal: TerminalOutcome


def verify_pending_terminal_pair(
    pending_result: object, terminal_result: object, *, user_id: str,
    conversation_id: str, request_id: str, sequence: int,
) -> RecordVerification[PendingTerminalPair]:
    """Verify both exact responses against one identity and consecutive expected positions."""
    expected_sequence = _MESSAGE_SEQUENCE.validate_python(sequence, strict=True)
    pending = verify_pending_message(
        pending_result, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, sequence=expected_sequence,
    )  # Reuse all existing identity, completeness, native shape and text checks.
    if pending.record is None:
        return RecordVerification("conflict" if pending.status == "conflict" else "unresolved")
    terminal = verify_terminal_outcome(
        terminal_result, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, sequence=expected_sequence + 1,
    )  # Its request-derived link must equal this same request's pending ID.
    if terminal.record is None:
        return RecordVerification("conflict" if terminal.status == "conflict" else "unresolved")
    return RecordVerification("verified", PendingTerminalPair(pending.record, terminal.record))
    # Return both only after success; a missing half is unresolved, never a partial exchange.


def verify_preclearance_terminal(
    result: object, *, user_id: str, conversation_id: str, request_id: str, offer_id: str,
) -> RecordVerification[PreclearanceTerminal]:
    """Verify a supplied content-free marker and its expected offer reference only."""
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    selected_request = UUID(_CONVERSATION_ID.validate_python(request_id, strict=True))
    selected_offer = UUID(_CONVERSATION_ID.validate_python(offer_id, strict=True))
    marker_id = f"preclearance_terminal:{selected_request.hex}"
    exact = _exact_result(result, record_id=marker_id)  # Reuse native shape/identity checks.
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata = exact.record.metadatas[0]
    if metadata is None or "marker_code" in metadata:
        return RecordVerification("unresolved")  # Metadata cannot supply the fixed document.
    try:
        record = PreclearanceTerminal.model_validate({
            **metadata, "marker_code": exact.record.documents[0],
        })  # Native extra=forbid rejects question/contact/model text and normal-message fields.
    except ValidationError:
        return RecordVerification("unresolved")
    if (
        record.marker_id != marker_id or record.owner_user_id != selected_user
        or record.conversation_id != str(selected_conversation)
        or record.request_id != str(selected_request)
        or record.verified_offer_id != str(selected_offer)
    ):
        return RecordVerification("conflict")
    return RecordVerification("verified", record)  # No offer read, persistence or display permission.


# Marker flow: expected selectors -> one exact response -> strict fixed-code model -> links.


def verify_handoff_offer(
    result: object, *, user_id: str, conversation_id: str, request_id: str,
    offer_id: str, reason_code: str, target_department_or_fallback: str,
) -> RecordVerification[HandoffOffer]:
    """Compare a supplied exact offer with caller-owned identity and route expectations."""
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    selected_request = UUID(_CONVERSATION_ID.validate_python(request_id, strict=True))
    selected_offer = UUID(_CONVERSATION_ID.validate_python(offer_id, strict=True))
    expected_reason = _OFFER_LABEL.validate_python(reason_code, strict=True)
    expected_target = _OFFER_LABEL.validate_python(target_department_or_fallback, strict=True)
    verified = _verify_offer_identity(
        result, user_id=selected_user, conversation_id=selected_conversation,
        request_id=selected_request, offer_id=selected_offer,
    )  # Share stored shape/identity parsing without treating historical labels as expectations.
    if verified.record is not None and (
        verified.record.reason_code != expected_reason
        or verified.record.target_department_or_fallback != expected_target
    ):
        return RecordVerification("conflict")
    return verified  # Public exact expected-label checks remain unchanged.


def _verify_offer_identity(
    result: object, *, user_id: str, conversation_id: UUID, request_id: UUID, offer_id: UUID,
) -> RecordVerification[HandoffOffer]:
    """Parse stored labels as data; verify identity without asserting routing permission."""
    exact = _exact_result(result, record_id=f"offer:{offer_id.hex}")
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata = exact.record.metadatas[0]
    if metadata is None or "offer_code" in metadata:
        return RecordVerification("unresolved")  # Metadata cannot replace the fixed document.
    try:
        record = HandoffOffer.model_validate({
            **metadata, "offer_code": exact.record.documents[0],
        })  # Inherited native strict/extra-forbid checks reject contact and submission data.
    except ValidationError:
        return RecordVerification("unresolved")
    if (
        record.offer_id != str(offer_id) or record.owner_user_id != user_id
        or record.conversation_id != str(conversation_id)
        or record.request_id != str(request_id)
    ):
        return RecordVerification("conflict")
    return RecordVerification("verified", record)  # No route authority, terminal or durable save proof.


# Offer flow: expected identity/labels -> exact response -> strict fixed-code model -> comparisons.


@dataclass(frozen=True)
class PreclearanceOfferPair:
    """Consistent supplied fallback records, without persistence or display authority."""

    marker: PreclearanceTerminal  # Existing frozen models contain fixed codes only.
    offer: HandoffOffer


def verify_preclearance_offer_pair(
    marker_result: object, offer_result: object, *, user_id: str, conversation_id: str,
    request_id: str, offer_id: str, reason_code: str, target_department_or_fallback: str,
) -> RecordVerification[PreclearanceOfferPair]:
    """Require a complete marker and offer bound to one expected identity/reason/target."""
    expected_reason = _OFFER_LABEL.validate_python(reason_code, strict=True)
    expected_target = _OFFER_LABEL.validate_python(target_department_or_fallback, strict=True)
    marker = verify_preclearance_terminal(
        marker_result, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, offer_id=offer_id,
    )  # Marker owns the same request and points to the expected offer.
    if marker.record is None:
        return RecordVerification("conflict" if marker.status == "conflict" else "unresolved")
    if marker.record.reason_code != expected_reason:
        return RecordVerification("conflict")  # Do not trust matching IDs with differing reasons.
    offer = verify_handoff_offer(
        offer_result, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, offer_id=offer_id, reason_code=expected_reason,
        target_department_or_fallback=expected_target,
    )  # The offer must bind back to the same request/owner/conversation and expected reason.
    if offer.record is None:
        return RecordVerification("conflict" if offer.status == "conflict" else "unresolved")
    return RecordVerification("verified", PreclearanceOfferPair(marker.record, offer.record))


# Pair flow: expected labels -> exact marker and reason -> exact offer -> both or no content.


@dataclass(frozen=True)
class ClearedExchangeOffer:
    """Consistent supplied exchange/offer structure; no actual clearance or review proof."""

    exchange: PendingTerminalPair  # Preserve the existing immutable validated records.
    offer: HandoffOffer


def verify_cleared_exchange_offer(
    pending_result: object, terminal_result: object, offer_result: object, *,
    user_id: str, conversation_id: str, request_id: str, sequence: int,
    offer_id: str, reason_code: str, target_department_or_fallback: str,
) -> RecordVerification[ClearedExchangeOffer]:
    """Verify a complete consecutive exchange and its expected completed-outcome offer link."""
    selected_offer = UUID(_CONVERSATION_ID.validate_python(offer_id, strict=True))
    expected_reason = _OFFER_LABEL.validate_python(reason_code, strict=True)
    expected_target = _OFFER_LABEL.validate_python(target_department_or_fallback, strict=True)
    exchange = verify_pending_terminal_pair(
        pending_result, terminal_result, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, sequence=sequence,
    )  # Reuse all identity, document, completeness, pending-link and sequence checks.
    if exchange.record is None:
        return RecordVerification(exchange.status)
    terminal = exchange.record.terminal
    if terminal.kind == "failed_technical" or terminal.verified_offer_id != str(selected_offer):
        return RecordVerification("conflict")  # A technical notice or absent/different link is ineligible.
    offer = verify_handoff_offer(
        offer_result, user_id=user_id, conversation_id=conversation_id, request_id=request_id,
        offer_id=str(selected_offer), reason_code=expected_reason,
        target_department_or_fallback=expected_target,
    )  # Offer identity and caller-owned routing expectations must agree with this exchange.
    if offer.record is None:
        return RecordVerification("conflict" if offer.status == "conflict" else "unresolved")
    return RecordVerification("verified", ClearedExchangeOffer(exchange.record, offer.record))


# Linked-exchange flow: expected offer/labels -> complete pair -> terminal link -> exact offer.


@dataclass(frozen=True)
class TranscriptExchange:
    """One completed supplied exchange, with its optional structurally linked offer."""

    exchange: PendingTerminalPair  # Technical failures never enter this collection.
    offer: HandoffOffer | None = None


@dataclass(frozen=True)
class TranscriptNotice:
    """Fixed-code notice data; contains no pending text, model text or citations."""

    request_id: str
    created_at: datetime  # Used only for deterministic notice presentation.
    reason_code: Literal["security_uncertain", "dependency_failure", "technical_error"]
    offer: HandoffOffer | None = None  # Only pre-clearance notices expose a linked offer.


@dataclass(frozen=True)
class CompleteTranscript:
    """Immutable supplied-prefix result; it cannot prove an exhaustive storage scan."""

    exchanges: tuple[TranscriptExchange, ...]
    notices: tuple[TranscriptNotice, ...]
    event_count: int  # Header and linked offers are outside the event collection.
    has_two_event_room: bool  # Admission must still check this under its write reservation.


@dataclass(frozen=True)
class TranscriptEventSelectors:
    """Content-free event structure result, without header, offer or scan proof."""

    offer_ids: tuple[str, ...]  # Canonical sorted unique UUID selectors, not verified offers.
    event_count: int  # Counts all supplied events, including suppressed technical pairs.
    has_two_event_room: bool  # A structural capacity check, not a reservation or admission.


@dataclass(frozen=True)
class _TranscriptEvents:
    """Private validated groups for full verification; never returned by the public stage."""

    groups: tuple[PendingTerminalPair | PreclearanceTerminal, ...]
    selectors: TranscriptEventSelectors  # Public projection deliberately contains no event text.


def _verify_transcript_header(
    result: object, *, user_id: str, conversation_id: UUID,
) -> RecordVerification[ConversationHeader]:
    """Separate valid foreign ownership from corrupted or ambiguous header data."""
    exact = _exact_result(result, record_id=f"conversation_header:{conversation_id.hex}")
    if exact.record is None:
        return RecordVerification("conflict" if exact.status == "absent" else "unresolved")
    metadata = exact.record.metadatas[0]
    if metadata is None:
        return RecordVerification("unresolved")
    try:
        owner = _USER_ID.validate_python(metadata.get("owner_user_id"), strict=True)
    except ValidationError:
        return RecordVerification("unresolved")
    header = verify_conversation_header(
        result, user_id=owner, conversation_id=str(conversation_id),
    )  # Require a complete internally consistent header before classifying it as foreign.
    if header.record is None:
        return RecordVerification("unresolved")
    if owner != user_id:
        return RecordVerification("conflict")  # Identical content-free result to an absent header.
    return header


def verify_owned_conversation_header(
    result: object, *, user_id: str, conversation_id: str,
) -> RecordVerification[ConversationHeader]:
    """Verify standalone ownership before reservation; this performs no storage I/O.

    Absent and valid foreign headers share a content-free conflict. Corrupt or
    ambiguous results are unresolved. Invalid caller selectors raise natively.
    """
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    return _verify_transcript_header(
        result, user_id=selected_user, conversation_id=selected_conversation,
    )  # Reuse complete header/owner classification without manufacturing a transcript.


def verify_transcript_events(
    event_result: object, *, user_id: str, conversation_id: str, cap: int = 50,
) -> RecordVerification[TranscriptEventSelectors]:
    """Select offers only after every supplied owned event and request group passes.

    This performs no I/O and checks no header or offer. Success proves only supplied
    bounded event structure, never completeness of a scan, durability or clearance.
    Invalid stored rows return unresolved with no selectors; invalid callers raise.
    """
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    event_cap = _EVENT_CAP.validate_python(cap, strict=True)
    checked = _verify_transcript_events(
        event_result, selected_user=selected_user,
        selected_conversation=selected_conversation, event_cap=event_cap,
    )
    if checked.record is None:
        return RecordVerification(checked.status)
    return RecordVerification("verified", checked.record.selectors)  # Do not expose private text.


def _verify_transcript_events(
    event_result: object, *, selected_user: str, selected_conversation: UUID, event_cap: int,
) -> RecordVerification[_TranscriptEvents]:
    """Share strict native row/group checks before deriving any linked-offer selectors."""
    try:
        returned = _ExactGetResult.model_validate(event_result)
    except ValidationError:
        return RecordVerification("unresolved")
    count = len(returned.ids)
    if (
        count > event_cap or len(returned.metadatas) != count
        or len(returned.documents) != count or len(set(returned.ids)) != count
    ):
        return RecordVerification("unresolved")  # Never zip-truncate, deduplicate or drop overflow.

    grouped: dict[str, dict[str, PendingMessage | TerminalOutcome | PreclearanceTerminal]] = {}
    positions: list[int] = []  # Normal positions only; markers do not consume sequence numbers.
    for record_id, metadata, document in zip(
        returned.ids, returned.metadatas, returned.documents, strict=True,
    ):
        if metadata is None:
            return RecordVerification("unresolved")
        row = {"ids": [record_id], "metadatas": [metadata], "documents": [document]}
        kind = metadata.get("record_type")
        try:
            request = UUID(_CONVERSATION_ID.validate_python(metadata.get("request_id"), strict=True))
            selectors = {
                "user_id": selected_user, "conversation_id": str(selected_conversation),
                "request_id": str(request),
            }  # Stored selectors cross a protected strict boundary before public verifier calls.
            if kind in ("pending_message", "terminal_outcome"):
                sequence = _MESSAGE_SEQUENCE.validate_python(metadata.get("sequence"), strict=True)
                verifier = verify_pending_message if kind == "pending_message" else verify_terminal_outcome
                checked = verifier(row, **selectors, sequence=sequence)
                positions.append(sequence)
            elif kind == "preclearance_terminal":
                offer_id = _CONVERSATION_ID.validate_python(metadata.get("verified_offer_id"), strict=True)
                checked = verify_preclearance_terminal(row, **selectors, offer_id=offer_id)
            else:
                return RecordVerification("unresolved")  # Includes misplaced headers/offers/submissions.
        except (ValidationError, ValueError):
            return RecordVerification("unresolved")  # Invalid stored IDs/positions never escape as selectors.
        if checked.record is None:
            return RecordVerification("unresolved")  # Foreign/corrupt events are not filtered away.
        request_events = grouped.setdefault(str(request), {})
        if kind in request_events:
            return RecordVerification("unresolved")  # No repeated terminal or marker for one request.
        request_events[kind] = checked.record

    if sorted(positions) != list(range(1, len(positions) + 1)):
        return RecordVerification("unresolved")  # Origin 1, with neither gaps nor duplicate positions.
    groups: list[PendingTerminalPair | PreclearanceTerminal] = []
    for events in grouped.values():
        marker = events.get("preclearance_terminal")
        if marker is not None:
            if len(events) != 1:
                return RecordVerification("unresolved")  # Marker and normal pair are mutually exclusive.
            groups.append(marker)
        else:
            if events.keys() != {"pending_message", "terminal_outcome"}:
                return RecordVerification("unresolved")  # Orphan halves cannot be normal history.
            pending = events["pending_message"]
            terminal = events["terminal_outcome"]
            if terminal.sequence != pending.sequence + 1:
                return RecordVerification("unresolved")  # Global consecutiveness alone permits interleaving.
            groups.append(PendingTerminalPair(pending, terminal))

    # Only complete valid groups may supply selectors, including optional technical references.
    offer_ids = set()
    for group in groups:
        source = group if isinstance(group, PreclearanceTerminal) else group.terminal
        if source.verified_offer_id is not None:
            offer_ids.add(str(UUID(source.verified_offer_id)))
    selectors = TranscriptEventSelectors(tuple(sorted(offer_ids)), count, count + 2 <= event_cap)
    return RecordVerification("verified", _TranscriptEvents(tuple(groups), selectors))


def verify_complete_transcript(
    header_result: object, event_result: object, *, user_id: str, conversation_id: str,
    offer_results: dict[str, object], cap: int = 50,
) -> RecordVerification[CompleteTranscript]:
    """Check all supplied events or return no content; no I/O, review or scan proof.

    Offer results are exact native reads keyed by canonical offer UUID strings. All
    event groups must pass before any offer result is consulted. Conflict means an
    absent/valid foreign header; invalid owned streams return unresolved. Invalid
    caller selectors raise. Every referenced offer still needs exact verification.
    """
    selected_user = _USER_ID.validate_python(user_id, strict=True)
    selected_conversation = UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True))
    event_cap = _EVENT_CAP.validate_python(cap, strict=True)
    header = _verify_transcript_header(
        header_result, user_id=selected_user, conversation_id=selected_conversation,
    )
    if header.record is None:
        return RecordVerification(header.status)  # Preserve header-first ownership/error precedence.
    checked_events = _verify_transcript_events(
        event_result, selected_user=selected_user,
        selected_conversation=selected_conversation, event_cap=event_cap,
    )
    if checked_events.record is None:
        return RecordVerification("unresolved")  # No offer is consulted for any invalid event set.

    exchanges: list[TranscriptExchange] = []
    notices: list[TranscriptNotice] = []
    for group in checked_events.record.groups:
        marker = group if isinstance(group, PreclearanceTerminal) else None
        if marker is not None:
            source = marker
        else:
            pending, terminal = group.pending, group.terminal
            source = terminal
        request_id = source.request_id  # Keep each request binding even when offer selectors deduplicate.

        offer: HandoffOffer | None = None
        if source.verified_offer_id is not None:
            # Validate even a technical reference, but never expose it in a technical notice.
            offer_id = UUID(source.verified_offer_id)  # Existing strict event model validated the UUID syntax.
            checked_offer = _verify_offer_identity(
                offer_results.get(str(offer_id)), user_id=selected_user,
                conversation_id=selected_conversation, request_id=UUID(request_id), offer_id=offer_id,
            )  # Parse historical labels as stored data, never as independent route expectations.
            if checked_offer.record is None:
                return RecordVerification("unresolved")
            offer = checked_offer.record
        if marker is not None:
            if offer is None or marker.reason_code != offer.reason_code:
                return RecordVerification("unresolved")
            notices.append(TranscriptNotice(request_id, marker.created_at, marker.reason_code, offer))
        elif terminal.kind == "failed_technical":
            notices.append(TranscriptNotice(request_id, terminal.created_at, "technical_error"))
            # Do not retain the failed pending text, terminal citations or optional offer in output.
        else:
            exchanges.append(TranscriptExchange(PendingTerminalPair(pending, terminal), offer))

    exchanges.sort(key=lambda item: item.exchange.pending.sequence)
    notices.sort(key=lambda item: (item.created_at, item.request_id))
    return RecordVerification("verified", CompleteTranscript(
        tuple(exchanges), tuple(notices), checked_events.record.selectors.event_count,
        checked_events.record.selectors.has_two_event_room,
    ))  # Tuple collections contain only immutable validated values, detached from native inputs.


# Event-stage flow: strict caller selectors -> all native rows -> complete groups/positions
# -> immutable content-free offer selectors, or unresolved with no selectors.
# Full flow: owned header -> shared event stage -> every linked offer per request
# -> suppress technical content -> ordered immutable result or no content.


def verify_conversation_headers(result, *, user_id):
    """Validate every enumerated header before any unresolved-ID exclusion is applied."""
    selected = _USER_ID.validate_python(user_id, strict=True)
    try:
        native = _ExactGetResult.model_validate(result)
        if not (len(native.ids) == len(native.documents) == len(native.metadatas)):
            return RecordVerification('unresolved')
        if len(set(native.ids)) != len(native.ids):
            return RecordVerification('unresolved')
        headers = []
        for index, document in enumerate(native.documents):
            if document is None:
                return RecordVerification('unresolved')
            document.encode('utf-8')
            candidate = ConversationHeader.model_validate_json(document)
            # Reuse the existing exact native shape and metadata/ID/owner verifier.
            checked = verify_conversation_header(
                {'ids': [native.ids[index]], 'metadatas': [native.metadatas[index]],
                 'documents': [document]}, user_id=selected,
                conversation_id=str(candidate.conversation_id))
            if checked.record is None:
                return RecordVerification('unresolved')
            headers.append(checked.record)
        return RecordVerification('verified', tuple(headers))
    except (ValidationError, UnicodeError):
        return RecordVerification('unresolved')


def verify_updated_preference(result, *, expected):
    """Read-back must equal the independent complete intended update, including version/time."""
    intended = PreferenceRecord.model_validate(expected.model_dump(warnings='error'))
    checked = verify_preference(result, user_id=intended.owner_user_id)
    if checked.record is not None and checked.record != intended:
        return RecordVerification('conflict')
    return checked


# Slice 6 (LLD §4 Escalations / submission): simulated handoff form record and its exact checks.

class HandoffSubmission(_CompleteRecord):
    """One immutable simulated handoff request; never proof of email, staff or assignment."""

    record_type: Literal["submission"]
    submission_id: ConversationId  # Stable: derived from the offer ID, so one offer has one record.
    receipt_id: ConversationId
    owner_user_id: UserId
    conversation_id: ConversationId
    offer_id: ConversationId
    request_reference: ConversationId  # The offer's own request; untrusted form text is not stored.
    reason_code: OfferLabel  # Derived from the saved offer, never from the client.
    target: OfferLabel
    name: Annotated[str, Field(min_length=1, max_length=200)]  # Synthetic form data, document only.
    phone: Annotated[str, Field(min_length=1, max_length=40)] | None
    email: Annotated[str, Field(min_length=1, max_length=254)] | None
    simulated_review: Literal[True]
    created_at: AwareDatetime


def verify_submittable_offer(
    result: object, *, user_id: str, conversation_id: str, offer_id: str,
) -> RecordVerification[HandoffOffer]:
    """Exact offer owned by this user and conversation; its request and labels are read as data."""
    selected_offer = UUID(_CONVERSATION_ID.validate_python(offer_id, strict=True))
    exact = _exact_result(result, record_id=f"offer:{selected_offer.hex}")
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata = exact.record.metadatas[0]
    try:
        request_id = UUID(_CONVERSATION_ID.validate_python((metadata or {}).get("request_id"), strict=True))
    except (ValidationError, ValueError):
        return RecordVerification("unresolved")
    return _verify_offer_identity(  # Reuse the one stored-offer parser and identity comparison.
        result, user_id=_USER_ID.validate_python(user_id, strict=True),
        conversation_id=UUID(_CONVERSATION_ID.validate_python(conversation_id, strict=True)),
        request_id=request_id, offer_id=selected_offer)


def verify_offer_link(terminal_result: object, marker_result: object, *, offer: HandoffOffer) -> bool:
    """True only when the offer's request saved a completed_offer terminal or fallback marker for it."""
    terminal = _exact_result(terminal_result, record_id="terminal_outcome:" + UUID(offer.request_id).hex)
    if terminal.record is not None:
        metadata = terminal.record.metadatas[0] or {}
        checked = verify_terminal_outcome(  # Reuse the full terminal parser with its stored position.
            terminal_result, user_id=offer.owner_user_id, conversation_id=offer.conversation_id,
            request_id=offer.request_id, sequence=metadata.get("sequence"),
        ) if isinstance(metadata.get("sequence"), int) else RecordVerification("unresolved")
        return (checked.record is not None and checked.record.kind == "completed_offer"
                and checked.record.verified_offer_id == offer.offer_id)
    if terminal.status != "absent":
        return False
    marker = verify_preclearance_terminal(
        marker_result, user_id=offer.owner_user_id, conversation_id=offer.conversation_id,
        request_id=offer.request_id, offer_id=offer.offer_id)
    return marker.record is not None


SUBMISSION_NAMESPACE = UUID("6f1f3c0e-5b1a-4f0e-9d3c-2a7b8e4d1c60")  # Fixed: one submission ID per offer.


def submission_id_for(offer_id: str) -> str:
    from uuid import uuid5
    return str(uuid5(SUBMISSION_NAMESPACE, str(UUID(offer_id))))


def verify_submission(result: object, *, offer: HandoffOffer) -> RecordVerification[HandoffSubmission]:
    """Exact saved submission for this offer; the document holds every field, metadata only IDs."""
    exact = _exact_result(result, record_id="submission:" + UUID(offer.offer_id).hex)
    if exact.record is None:
        return RecordVerification(exact.status)
    metadata, document = exact.record.metadatas[0] or {}, exact.record.documents[0]
    try:
        record = HandoffSubmission.model_validate_json(document or "")
    except ValidationError:
        return RecordVerification("unresolved")
    if (record.offer_id != offer.offer_id or record.owner_user_id != offer.owner_user_id
            or record.conversation_id != offer.conversation_id or record.request_reference != offer.request_id
            or record.submission_id != submission_id_for(offer.offer_id)
            or record.reason_code != offer.reason_code or record.target != offer.target_department_or_fallback
            or metadata.get("record_type") != "submission" or metadata.get("offer_id") != offer.offer_id
            or metadata.get("owner_user_id") != offer.owner_user_id
            or metadata.get("conversation_id") != offer.conversation_id):
        return RecordVerification("conflict")
    return RecordVerification("verified", record)  # Exact read only; no delivery or staff claim.


# Submission flow: owned exact offer -> verified link -> exact submission (absent, same, or conflict).
