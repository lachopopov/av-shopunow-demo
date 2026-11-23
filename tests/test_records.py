# Python 3.12 / Pydantic v2.14.1
# Controlled native-get-shaped inputs exercise real application verification, without I/O.
import json  # Produce ordinary JSON fixtures, independently of production models.
import unittest  # Use the existing offline test runner.
from copy import deepcopy  # Isolate each corrupt-record example.
from datetime import datetime, timezone  # Hand-check the verified creation time.
from uuid import UUID  # Hand-check the verified conversation identity.

from shopunow.records import (
    verify_conversation_header, verify_pending_message, verify_preference, verify_profile,
    verify_terminal_outcome,
)


USER_ID = "syn-cust-01"  # Synthetic selectors, never account credentials.
CONVERSATION_ID = "01234567-89ab-4cde-8fab-0123456789ab"
PROFILE = {
    "record_type": "profile", "user_id": USER_ID, "role": "customer",
    "home_department": None, "created_at": "2026-11-02T10:00:00Z",
    "complete": True, "version": 1,
}
HEADER = {
    "record_type": "conversation_header", "conversation_id": CONVERSATION_ID,
    "owner_user_id": USER_ID, "created_at": "2026-11-02T10:00:00Z", "complete": True,
}


def profile_result(document=None):
    # Match the native flat get shape; unrelated native fields remain present.
    return {
        "ids": ["profile:syn-cust-01"],
        "metadatas": [{"record_type": "profile", "user_id": USER_ID}],
        "documents": [json.dumps(PROFILE if document is None else document)],
        "embeddings": None, "uris": None, "data": None,
        "included": ["metadatas", "documents"],
    }


def header_result(document=None):
    # Literal expected IDs do not reuse any production encoding helper.
    return {
        "ids": ["conversation_header:0123456789ab4cde8fab0123456789ab"],
        "metadatas": [{
            "record_type": "conversation_header", "owner_user_id": USER_ID,
            "conversation_id": CONVERSATION_ID,
        }],
        "documents": [json.dumps(HEADER if document is None else document)],
        "embeddings": None, "uris": None, "data": None,
        "included": ["metadatas", "documents"],
    }


class ExactRecordTests(unittest.TestCase):
    """Catch missing identity, completeness, shape and stored-binding checks."""

    def check_profile(self, result):
        return verify_profile(result, user_id=USER_ID)

    def check_header(self, result):
        return verify_conversation_header(
            result, user_id=USER_ID, conversation_id=CONVERSATION_ID,
        )

    def assert_rejected(self, result, status):
        # A rejection must withhold the record, not just attach a warning to it.
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)

    def test_complete_profile_returns_stored_binding(self):
        # Removing role/department validation or replacing the stored binding breaks this.
        for role, department in [
            ("customer", None), ("employee", "hr"), ("employee", "it_support"),
            ("employee", "facilities_admin"), ("employee", "billing_payments"),
            ("employee", "shipping_delivery"),
        ]:
            with self.subTest(role=role, department=department):
                result = self.check_profile(profile_result({
                    **PROFILE, "role": role, "home_department": department,
                }))
                self.assertEqual(result.status, "verified")
                self.assertEqual(result.record.user_id, "syn-cust-01")
                self.assertEqual(result.record.role, role)
                self.assertEqual(result.record.home_department, department)
                self.assertEqual(result.record.version, 1)
                self.assertEqual(result.record.created_at, datetime(2026, 11, 2, 10, tzinfo=timezone.utc))

    def test_complete_header_accepts_equivalent_uuid_selectors(self):
        # UUID normalization must preserve one storage key across accepted spellings.
        for selector in [CONVERSATION_ID, CONVERSATION_ID.replace("-", "").upper()]:
            with self.subTest(selector=selector):
                result = verify_conversation_header(
                    header_result(), user_id=USER_ID, conversation_id=selector,
                )
                self.assertEqual(result.status, "verified")
                self.assertEqual(result.record.owner_user_id, USER_ID)
                self.assertEqual(result.record.conversation_id, UUID(CONVERSATION_ID))

    def test_empty_complete_get_result_is_absent_for_both_types(self):
        empty = {"ids": [], "documents": [], "metadatas": [], "embeddings": None}
        for check in [self.check_profile, self.check_header]:
            with self.subTest(check=check.__name__):
                self.assert_rejected(check(empty), "absent")

    def test_wrong_native_id_type_or_owner_never_returns_a_record(self):
        for make, check, owner_key in [
            (profile_result, self.check_profile, "user_id"),
            (header_result, self.check_header, "owner_user_id"),
        ]:
            for field, value in [("id", "unrelated"), ("record_type", "preference"), (owner_key, "syn-cust-02")]:
                with self.subTest(kind=make.__name__, field=field):
                    raw = make()
                    if field == "id":
                        raw["ids"][0] = value
                    else:
                        raw["metadatas"][0][field] = value
                    self.assert_rejected(check(raw), "conflict")

    def test_header_metadata_conversation_link_must_match(self):
        raw = header_result()
        raw["metadatas"][0]["conversation_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assert_rejected(self.check_header(raw), "conflict")

    def test_document_identity_cannot_disagree_with_metadata(self):
        for base, make, check, field, value in [
            (PROFILE, profile_result, self.check_profile, "user_id", "syn-cust-02"),
            (PROFILE, profile_result, self.check_profile, "record_type", "preference"),
            (HEADER, header_result, self.check_header, "owner_user_id", "syn-cust-02"),
            (HEADER, header_result, self.check_header, "record_type", "pending_message"),
            (HEADER, header_result, self.check_header, "conversation_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ]:
            with self.subTest(kind=make.__name__, field=field):
                # Wrong type fails schema validation; a valid foreign identity conflicts.
                status = "unresolved" if field == "record_type" else "conflict"
                self.assert_rejected(check(make({**base, field: value})), status)

    def test_every_required_document_field_including_null_department_is_required(self):
        for base, make, check in [
            (PROFILE, profile_result, self.check_profile),
            (HEADER, header_result, self.check_header),
        ]:
            for field in base:
                with self.subTest(kind=make.__name__, field=field):
                    partial = {key: value for key, value in base.items() if key != field}
                    self.assert_rejected(check(make(partial)), "unresolved")

    def test_complete_requires_the_boolean_true(self):
        for base, make, check in [
            (PROFILE, profile_result, self.check_profile),
            (HEADER, header_result, self.check_header),
        ]:
            for value in [False, "true", 1, None]:
                with self.subTest(kind=make.__name__, complete=value):
                    self.assert_rejected(check(make({**base, "complete": value})), "unresolved")

    def test_invalid_profile_binding_is_unresolved(self):
        for changes in [
            {"role": "admin"}, {"role": "Employee"}, {"role": 1},
            {"home_department": "hr"}, {"role": "employee", "home_department": None},
            {"role": "employee", "home_department": "unknown"},
        ]:
            with self.subTest(changes=changes):
                self.assert_rejected(self.check_profile(profile_result({**PROFILE, **changes})), "unresolved")

    def test_bad_creation_time_or_profile_version_is_unresolved(self):
        for created_at in ["", "yesterday", "2026-11-02", "2026-11-02T10:00:00", 123, None]:
            for base, make, check in [
                (PROFILE, profile_result, self.check_profile),
                (HEADER, header_result, self.check_header),
            ]:
                with self.subTest(kind=make.__name__, created_at=created_at):
                    self.assert_rejected(check(make({**base, "created_at": created_at})), "unresolved")
        for version in [0, -1, "1", True, None]:
            with self.subTest(version=version):
                self.assert_rejected(self.check_profile(profile_result({**PROFILE, "version": version})), "unresolved")

    def test_missing_invalid_or_unexpected_document_is_unresolved(self):
        for make, check in [(profile_result, self.check_profile), (header_result, self.check_header)]:
            for document in [None, "", "{", "null", "[]", "{}", "\ud800", b"{}"]:
                with self.subTest(kind=make.__name__, document=repr(document)):
                    raw = make()
                    raw["documents"] = [document]
                    self.assert_rejected(check(raw), "unresolved")
            raw = make()
            payload = json.loads(raw["documents"][0])
            raw["documents"][0] = json.dumps({**payload, "text": "private fixture text"})
            rejected = check(raw)
            self.assert_rejected(rejected, "unresolved")
            self.assertNotIn("private fixture text", repr(rejected))

    def test_malformed_get_envelope_is_not_absence(self):
        for make, check in [(profile_result, self.check_profile), (header_result, self.check_header)]:
            invalid = [None, {}, [], {"ids": []}, {"ids": [], "documents": None, "metadatas": None}]
            for field in ["ids", "documents", "metadatas"]:
                raw = make()
                raw[field] = []
                invalid.append(raw)  # Misaligned arrays must never be silently zipped.
                raw = make()
                raw[field] = [raw[field][0], deepcopy(raw[field][0])]
                invalid.append(raw)
            raw = make()
            for field in ["ids", "documents", "metadatas"]:
                raw[field] *= 2
            invalid.append(raw)  # Even duplicated exact hits cannot be picked arbitrarily.
            for raw in invalid:
                with self.subTest(kind=make.__name__, raw=repr(raw)[:80]):
                    self.assert_rejected(check(raw), "unresolved")

    def test_missing_or_extra_metadata_is_unresolved(self):
        for make, check in [(profile_result, self.check_profile), (header_result, self.check_header)]:
            valid = make()["metadatas"][0]
            invalid = [None, {}, {**valid, "unexpected": "private"}]
            invalid.extend({key: value for key, value in valid.items() if key != field} for field in valid)
            for metadata in invalid:
                with self.subTest(kind=make.__name__, metadata=metadata):
                    raw = make()
                    raw["metadatas"][0] = metadata
                    self.assert_rejected(check(raw), "unresolved")

    def test_invalid_lookup_selectors_do_not_produce_authoritative_results(self):
        for user_id in ["", "../foreign", "x" * 65, 1]:
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                verify_profile(profile_result(), user_id=user_id)
        for conversation_id in ["", "not-a-uuid", "{01234567-89ab-4cde-8fab-0123456789ab}"]:
            with self.subTest(conversation_id=conversation_id), self.assertRaises(ValueError):
                verify_conversation_header(header_result(), user_id=USER_ID, conversation_id=conversation_id)


class OwnedConversationHeaderTests(unittest.TestCase):
    """Catch unsafe ownership classification before a caller reserves a conversation."""

    def verify(self, result, *, user_id=USER_ID, conversation_id=CONVERSATION_ID):
        # A missing public API must fail an assertion, rather than test-module import.
        from shopunow import records
        verifier = getattr(records, "verify_owned_conversation_header", None)
        self.assertTrue(callable(verifier), "Standalone owned-header verifier is missing")
        return verifier(result, user_id=user_id, conversation_id=conversation_id)

    def assert_unresolved(self, raw):
        # Returning a record with an error status would still disclose its content.
        result = self.verify(raw)
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.record)

    def test_owned_header_returns_complete_binding_for_equivalent_uuid_selectors(self):
        # Catch a wrong selected owner/key or a failure to normalize accepted UUIDs.
        for selector in [CONVERSATION_ID, "0123456789AB4CDE8FAB0123456789AB"]:
            with self.subTest(selector=selector):
                result = self.verify(header_result(), conversation_id=selector)
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.owner_user_id, "syn-cust-01")
                self.assertEqual(result.record.conversation_id, UUID(CONVERSATION_ID))
                self.assertEqual(result.record.created_at, datetime(2026, 11, 2, 10, tzinfo=timezone.utc))
                self.assertTrue(result.record.complete)

    def test_absence_and_valid_foreign_header_return_identical_content_free_conflict(self):
        # Catch disclosure of valid foreign ownership or accidental absent success.
        absent = self.verify({"ids": [], "metadatas": [], "documents": []})
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        self.assertEqual(absent.status, "conflict")
        self.assertIsNone(absent.record)
        self.assertEqual(self.verify(foreign), absent)

    def test_malformed_or_ambiguous_native_results_are_unresolved(self):
        # Catch treating malformed/duplicate results as absence or choosing a first row.
        invalid = [None, {}, {"ids": [], "metadatas": None, "documents": []}]
        wrong_id = header_result()
        wrong_id["ids"] = ["conversation_header:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"]
        invalid.append(wrong_id)
        for field in ("ids", "metadatas", "documents"):
            missing, short, duplicate = header_result(), header_result(), header_result()
            del missing[field]
            short[field] = []
            duplicate[field] *= 2
            invalid.extend((missing, short, duplicate))
        duplicate = header_result()
        for field in ("ids", "metadatas", "documents"):
            duplicate[field] *= 2
        invalid.append(duplicate)
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_unresolved(raw)

    def test_invalid_metadata_is_unresolved_even_when_it_claims_foreign_ownership(self):
        # Catch classifying corrupt identity metadata as a valid foreign header.
        metadata = header_result()["metadatas"][0]
        invalid = [None, {}, {**metadata, "unexpected": "PRIVATE"}]
        invalid.extend({key: value for key, value in metadata.items() if key != field}
                       for field in metadata)
        invalid.extend({**metadata, field: value} for field, value in [
            ("owner_user_id", "../foreign"), ("owner_user_id", None),
            ("record_type", "profile"),
            ("conversation_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ])
        for returned_metadata in invalid:
            with self.subTest(metadata=returned_metadata):
                raw = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
                raw["metadatas"] = [returned_metadata]
                self.assert_unresolved(raw)

    def test_corrupt_documents_and_disagreeing_bindings_are_unresolved(self):
        # Catch trusting metadata before proving complete document/identity agreement.
        for owner in ("syn-cust-01", "syn-cust-02"):
            document = {**HEADER, "owner_user_id": owner}
            invalid_documents = [None, "{", "null", "\ud800"]
            invalid_documents.extend(json.dumps({**document, field: value}) for field, value in [
                ("complete", False), ("complete", "true"), ("created_at", "invalid"),
                ("record_type", "profile"), ("owner_user_id", "syn-cust-03"),
                ("conversation_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                ("unexpected", "PRIVATE"),
            ])
            invalid_documents.extend(json.dumps({key: value for key, value in document.items()
                                                 if key != field}) for field in document)
            for returned_document in invalid_documents:
                with self.subTest(owner=owner, document=repr(returned_document)):
                    raw = header_result()
                    raw["metadatas"][0]["owner_user_id"] = owner
                    raw["documents"] = [returned_document]
                    self.assert_unresolved(raw)

    def test_invalid_caller_selectors_raise_native_validation_before_classification(self):
        # Catch omitted selector checks, including when storage would indicate absence.
        from pydantic import ValidationError
        absent = {"ids": [], "metadatas": [], "documents": []}
        for user_id in ("", "../foreign", "x" * 65, 1, None):
            with self.subTest(user_id=user_id), self.assertRaises(ValidationError):
                self.verify(absent, user_id=user_id)
        for conversation_id in ("", "not-a-uuid", "{01234567-89ab-4cde-8fab-0123456789ab}", 1, None):
            with self.subTest(conversation_id=conversation_id), self.assertRaises(ValidationError):
                self.verify(absent, conversation_id=conversation_id)


PREFERENCE = {
    "record_type": "preference", "owner_user_id": USER_ID,
    "key": "response_style", "value": "brief", "version": 1,
    "updated_at": "2026-11-02T10:00:00Z", "complete": True,
}  # Independently specified from LLD §4, without profile/header-only fields.


def preference_result(document=None):
    # Match the same native response boundary; do not use a production ID builder.
    return {
        "ids": ["preference:syn-cust-01:response_style"],
        "metadatas": [{
            "record_type": "preference", "owner_user_id": USER_ID, "key": "response_style",
        }],
        "documents": [json.dumps(PREFERENCE if document is None else document)],
        "embeddings": None, "uris": None, "data": None,
        "included": ["metadatas", "documents"],
    }


class PreferenceRecordTests(unittest.TestCase):
    """Catch unauthorized values, foreign bindings, and incomplete saved records."""

    def assert_rejected(self, raw, status):
        result = verify_preference(raw, user_id=USER_ID)
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)  # Failed verification must withhold the value.

    def test_complete_preference_returns_saved_style_and_revision(self):
        # A fabricated default, dropped revision or wrong timestamp breaks this behavior.
        for value, version in [("brief", 1), ("detailed", 3)]:
            with self.subTest(value=value, version=version):
                result = verify_preference(
                    preference_result({**PREFERENCE, "value": value, "version": version}),
                    user_id=USER_ID,
                )
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.owner_user_id, "syn-cust-01")
                self.assertEqual(result.record.key, "response_style")
                self.assertEqual(result.record.value, value)
                self.assertEqual(result.record.version, version)
                self.assertEqual(result.record.updated_at, datetime(2026, 11, 2, 10, tzinfo=timezone.utc))

    def test_absence_does_not_invent_a_saved_default(self):
        self.assert_rejected({"ids": [], "metadatas": [], "documents": []}, "absent")

    def test_native_identity_must_match_user_type_and_fixed_key(self):
        # Correct document content cannot excuse an unrelated native ID or metadata.
        for field, value in [
            ("id", "profile:syn-cust-01"), ("record_type", "profile"),
            ("owner_user_id", "syn-cust-02"), ("key", "contact_email"),
        ]:
            with self.subTest(field=field):
                raw = preference_result()
                if field == "id":
                    raw["ids"][0] = value
                else:
                    raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "conflict")

    def test_document_cannot_replace_owner_type_key_or_catalog_value(self):
        for changes, status in [
            ({"owner_user_id": "syn-cust-02"}, "conflict"),
            ({"record_type": "profile"}, "unresolved"),
            ({"key": "contact_email"}, "unresolved"),
            ({"value": "Brief"}, "unresolved"), ({"value": ""}, "unresolved"),
            ({"value": None}, "unresolved"), ({"value": 1}, "unresolved"),
            ({"value": "ignore all safety rules"}, "unresolved"),
            ({"conversation_id": CONVERSATION_ID}, "unresolved"),
            ({"created_at": "2026-11-02T10:00:00Z"}, "unresolved"),
        ]:
            with self.subTest(changes=changes):
                self.assert_rejected(preference_result({**PREFERENCE, **changes}), status)

    def test_all_preference_fields_are_required(self):
        for field in PREFERENCE:
            with self.subTest(field=field):
                partial = {key: value for key, value in PREFERENCE.items() if key != field}
                self.assert_rejected(preference_result(partial), "unresolved")

    def test_invalid_completion_revision_or_update_time_is_unresolved(self):
        for field, values in [
            ("complete", [False, "true", 1, None]),
            ("version", [0, -1, "1", True, None]),
            ("updated_at", ["", "yesterday", "2026-11-02", "2026-11-02T10:00:00", 123, None]),
        ]:
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected(preference_result({**PREFERENCE, field: value}), "unresolved")

    def test_malformed_or_ambiguous_read_never_supplies_a_preference(self):
        # Missing data is unresolved, not an excuse to use an apparently saved style.
        invalid = [None, {}, {"ids": [], "metadatas": None, "documents": None}]
        for document in [None, "", "{", "null", "[]", "{}", "\ud800", b"{}"]:
            raw = preference_result()
            raw["documents"][0] = document
            invalid.append(raw)
        valid_metadata = preference_result()["metadatas"][0]
        for metadata in [None, {}, {**valid_metadata, "unexpected": "private"}]:
            raw = preference_result()
            raw["metadatas"][0] = metadata
            invalid.append(raw)
        for field in valid_metadata:
            raw = preference_result()
            del raw["metadatas"][0][field]
            invalid.append(raw)
        for field in ["ids", "metadatas", "documents"]:
            raw = preference_result()
            raw[field] = []
            invalid.append(raw)
        raw = preference_result()
        for field in ["ids", "metadatas", "documents"]:
            raw[field] *= 2
        invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:80]):
                self.assert_rejected(raw, "unresolved")

    def test_valid_maximum_length_user_id_has_its_own_preference(self):
        # Failing to use the full selector, or ambiguous ID assembly, breaks this binding.
        selected_user = "u" * 64
        raw = preference_result({**PREFERENCE, "owner_user_id": selected_user})
        raw["ids"] = ["preference:" + "u" * 64 + ":response_style"]
        raw["metadatas"][0]["owner_user_id"] = selected_user
        result = verify_preference(raw, user_id=selected_user)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.owner_user_id, selected_user)

    def test_invalid_user_selector_cannot_resolve_a_preference(self):
        for user_id in ["", "../foreign", "x" * 65, "u:response_style", 1]:
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                verify_preference(preference_result(), user_id=user_id)


REQUEST_ID = "fedcba98-7654-4321-8abc-fedcba987654"
PENDING_METADATA = {
    "record_type": "pending_message",
    "message_id": "pending_message:fedcba98765443218abcfedcba987654",
    "request_id": REQUEST_ID, "owner_user_id": USER_ID,
    "conversation_id": CONVERSATION_ID, "sequence": 1, "speaker": "user",
    "created_at": "2026-11-02T10:00:00Z", "status": "pending", "complete": True,
}  # Literal logical fields, independently specified from LLD §4.


def pending_result(*, text="  Help with my delivery?  ", metadata=None):
    # Conversation text is the native document itself, never JSON-wrapped or metadata.
    return {
        "ids": ["pending_message:fedcba98765443218abcfedcba987654"],
        "metadatas": [dict(PENDING_METADATA if metadata is None else metadata)],
        "documents": [text], "embeddings": None, "uris": None, "data": None,
        "included": ["metadatas", "documents"],
    }


class PendingMessageTests(unittest.TestCase):
    """Catch wrong request/owner/position bindings and unsafe stored message text."""

    def verify(self, raw, **selectors):
        expected = {
            "user_id": USER_ID, "conversation_id": CONVERSATION_ID,
            "request_id": REQUEST_ID, "sequence": 1,
        }
        return verify_pending_message(raw, **{**expected, **selectors})

    def assert_rejected(self, raw, status):
        result = self.verify(raw)
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)  # No pending content leaks through rejection.

    def test_complete_pending_message_preserves_document_and_binding(self):
        # A first-hit shortcut or accidental trimming/normal-history conversion fails this.
        text = '  Help?\n"quoted" \\ 😀  '
        result = self.verify(pending_result(text=text))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.text, text)
        self.assertEqual(result.record.message_id, "pending_message:fedcba98765443218abcfedcba987654")
        self.assertEqual(result.record.request_id, REQUEST_ID)
        self.assertEqual(result.record.owner_user_id, USER_ID)
        self.assertEqual(result.record.conversation_id, CONVERSATION_ID)
        self.assertEqual(result.record.sequence, 1)
        self.assertEqual(result.record.speaker, "user")
        self.assertEqual(result.record.status, "pending")
        self.assertEqual(result.record.created_at, datetime(2026, 11, 2, 10, tzinfo=timezone.utc))

    def test_equivalent_uuid_selectors_resolve_the_same_pending_record(self):
        # Accepted request/conversation spellings must preserve stable request identity.
        result = self.verify(
            pending_result(), conversation_id=CONVERSATION_ID.replace("-", "").upper(),
            request_id=REQUEST_ID.replace("-", "").upper(),
        )
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.request_id, REQUEST_ID)

    def test_absent_pending_record_returns_no_content(self):
        self.assert_rejected({"ids": [], "metadatas": [], "documents": []}, "absent")

    def test_native_id_and_metadata_identity_must_agree_with_expectations(self):
        # Valid foreign records cannot be rebound by a matching storage filter.
        for field, value in [
            ("native_id", "pending_message:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("message_id", "pending_message:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("owner_user_id", "syn-cust-02"),
            ("conversation_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            ("request_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            ("sequence", 2), ("sequence", 0),
        ]:
            with self.subTest(field=field, value=value):
                raw = pending_result()
                if field == "native_id":
                    raw["ids"][0] = value
                else:
                    raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "conflict")

    def test_every_pending_metadata_field_is_required(self):
        for field in PENDING_METADATA:
            with self.subTest(field=field):
                partial = {key: value for key, value in PENDING_METADATA.items() if key != field}
                self.assert_rejected(pending_result(metadata=partial), "unresolved")

    def test_invalid_type_status_speaker_completeness_sequence_and_time_fail_safely(self):
        for field, values in [
            ("record_type", ["terminal_outcome", "conversation_header", None]),
            ("speaker", ["assistant", None]), ("status", ["completed", "failed", None]),
            ("complete", [False, 1, "true", None]),
            ("sequence", [-1, "1", True, 1.5, None]),
            ("request_id", ["bad-uuid", None]), ("conversation_id", ["bad-uuid", None]),
            ("owner_user_id", ["../foreign", None]),
            ("created_at", ["", "yesterday", "2026-11-02T10:00:00", 123, None]),
        ]:
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected(
                        pending_result(metadata={**PENDING_METADATA, field: value}), "unresolved",
                    )

    def test_document_text_accepts_exact_utf8_byte_boundary_without_truncation(self):
        # Character counting alone would admit 2,049 four-byte characters incorrectly.
        for text in ["x" * 8192, "😀" * 2048, "😀" * 2047 + "abcd"]:
            with self.subTest(byte_length=len(text.encode("utf-8"))):
                result = self.verify(pending_result(text=text))
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.text, text)
        for text in ["x" * 8193, "😀" * 2048 + "x", "😀" * 2049]:
            with self.subTest(byte_length=len(text.encode("utf-8"))):
                self.assert_rejected(pending_result(text=text), "unresolved")

    def test_missing_blank_nonstring_or_non_utf8_document_is_unresolved(self):
        for text in [None, "", " \t\n", "\ud800", b"text", 123]:
            with self.subTest(text=repr(text)):
                self.assert_rejected(pending_result(text=text), "unresolved")

    def test_metadata_cannot_supply_or_override_document_text(self):
        for document in [None, "actual document"]:
            with self.subTest(document=document):
                self.assert_rejected(pending_result(
                    text=document, metadata={**PENDING_METADATA, "text": "private metadata text"},
                ), "unresolved")
        self.assert_rejected(pending_result(metadata={**PENDING_METADATA, "role": "employee"}), "unresolved")

    def test_malformed_or_ambiguous_native_response_is_not_a_pending_message(self):
        invalid = [None, {}, {"ids": [], "metadatas": None, "documents": None}]
        for field in ["ids", "metadatas", "documents"]:
            raw = pending_result()
            raw[field] = []
            invalid.append(raw)
        raw = pending_result()
        for field in ["ids", "metadatas", "documents"]:
            raw[field] *= 2
        invalid.append(raw)
        raw = pending_result()
        raw["metadatas"][0] = None
        invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:80]):
                self.assert_rejected(raw, "unresolved")

    def test_sequence_origin_is_supplied_by_the_caller(self):
        # This single-record check cannot decide where a complete transcript starts.
        result = self.verify(pending_result(metadata={**PENDING_METADATA, "sequence": 0}), sequence=0)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.sequence, 0)

    def test_invalid_expected_selectors_are_rejected(self):
        for selectors in [
            {"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
            {"request_id": "bad-uuid"}, {"request_id": None},
            {"sequence": -1}, {"sequence": True}, {"sequence": "1"},
        ]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(pending_result(), **selectors)


OFFER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TERMINAL_METADATA = {
    "record_type": "terminal_outcome",
    "outcome_id": "terminal_outcome:fedcba98765443218abcfedcba987654",
    "request_id": REQUEST_ID, "owner_user_id": USER_ID,
    "conversation_id": CONVERSATION_ID,
    "pending_message_id": "pending_message:fedcba98765443218abcfedcba987654",
    "sequence": 2, "kind": "completed_answer",
    "created_at": "2026-11-02T10:00:01Z", "complete": True,
}  # Required terminal fields from LLD §4, separate from any real review/delivery proof.


def terminal_result(*, document="  The stored response.  ", metadata=None):
    # Native document contains only approved_text_or_code; no JSON wrapper or metadata text.
    return {
        "ids": ["terminal_outcome:fedcba98765443218abcfedcba987654"],
        "metadatas": [dict(TERMINAL_METADATA if metadata is None else metadata)],
        "documents": [document], "embeddings": None, "uris": None, "data": None,
        "included": ["metadatas", "documents"],
    }


class TerminalOutcomeTests(unittest.TestCase):
    """Catch corrupt terminal identity, payload and kind-specific structure."""

    def verify(self, raw, **selectors):
        expected = {
            "user_id": USER_ID, "conversation_id": CONVERSATION_ID,
            "request_id": REQUEST_ID, "sequence": 2,
        }
        return verify_terminal_outcome(raw, **{**expected, **selectors})

    def assert_rejected(self, raw, status):
        result = self.verify(raw)
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)  # Failed verification must withhold all terminal content.

    def test_complete_normal_terminal_kinds_preserve_document_and_binding(self):
        # Dropping a required identity comparison or modifying stored text breaks this.
        for kind in ["completed_answer", "completed_clarification", "completed_refusal", "completed_offer"]:
            with self.subTest(kind=kind):
                metadata = {**TERMINAL_METADATA, "kind": kind}
                if kind == "completed_offer":
                    metadata["verified_offer_id"] = OFFER_ID
                document = '  A stored response.\n"quoted" 😀  '
                result = self.verify(terminal_result(document=document, metadata=metadata))
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.outcome_id, "terminal_outcome:fedcba98765443218abcfedcba987654")
                self.assertEqual(result.record.pending_message_id, "pending_message:fedcba98765443218abcfedcba987654")
                self.assertEqual(result.record.owner_user_id, USER_ID)
                self.assertEqual(result.record.conversation_id, CONVERSATION_ID)
                self.assertEqual(result.record.request_id, REQUEST_ID)
                self.assertEqual(result.record.sequence, 2)
                self.assertEqual(result.record.kind, kind)
                self.assertEqual(result.record.approved_text_or_code, document)
                self.assertEqual(result.record.created_at, datetime(2026, 11, 2, 10, 0, 1, tzinfo=timezone.utc))

    def test_failed_technical_requires_a_fixed_code_not_arbitrary_error_text(self):
        metadata = {**TERMINAL_METADATA, "kind": "failed_technical"}
        result = self.verify(terminal_result(document="technical_error", metadata=metadata))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.kind, "failed_technical")
        self.assertEqual(result.record.approved_text_or_code, "technical_error")
        for document in ["private provider traceback", "A model-written answer", " technical_error "]:
            with self.subTest(document=document):
                self.assert_rejected(terminal_result(document=document, metadata=metadata), "unresolved")

    def test_equivalent_uuid_selectors_resolve_the_same_terminal(self):
        result = self.verify(
            terminal_result(), request_id=REQUEST_ID.replace("-", "").upper(),
            conversation_id=CONVERSATION_ID.replace("-", "").upper(),
        )
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.request_id, REQUEST_ID)

    def test_exact_absence_returns_no_terminal_content(self):
        self.assert_rejected({"ids": [], "metadatas": [], "documents": []}, "absent")

    def test_wrong_native_identity_links_or_expected_sequence_conflict(self):
        for field, value in [
            ("native_id", "terminal_outcome:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("outcome_id", "terminal_outcome:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("pending_message_id", "pending_message:aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("owner_user_id", "syn-cust-02"), ("conversation_id", OFFER_ID),
            ("request_id", OFFER_ID), ("sequence", 1),
        ]:
            with self.subTest(field=field):
                raw = terminal_result()
                if field == "native_id":
                    raw["ids"][0] = value
                else:
                    raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "conflict")

    def test_all_terminal_metadata_fields_are_required(self):
        for field in TERMINAL_METADATA:
            with self.subTest(field=field):
                partial = {key: value for key, value in TERMINAL_METADATA.items() if key != field}
                self.assert_rejected(terminal_result(metadata=partial), "unresolved")

    def test_invalid_type_kind_completion_timestamp_and_sequence_are_unresolved(self):
        for field, values in [
            ("record_type", ["pending_message", "preclearance_terminal", None]),
            ("kind", ["answer", "confirmed_attack_block", "preclearance_terminal", None]),
            ("complete", [False, 1, "true", None]),
            ("created_at", ["", "2026-11-02T10:00:00", "123", 123, None]),
            ("sequence", [-1, "2", True, 1.5, None]),
            ("owner_user_id", ["../foreign", None]),
            ("conversation_id", ["bad-uuid", None]), ("request_id", ["bad-uuid", None]),
        ]:
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected(terminal_result(metadata={**TERMINAL_METADATA, field: value}), "unresolved")

    def test_terminal_document_has_exact_utf8_byte_bounds_without_truncation(self):
        for document in ["x" * 8192, "😀" * 2048]:
            with self.subTest(bytes=len(document.encode("utf-8"))):
                result = self.verify(terminal_result(document=document))
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.approved_text_or_code, document)
        for document in ["x" * 8193, "😀" * 2048 + "x", "😀" * 2049, "", " \t", "\ud800", None, b"text"]:
            with self.subTest(document=repr(document)[:40]):
                self.assert_rejected(terminal_result(document=document), "unresolved")

    def test_metadata_cannot_replace_document_or_assert_review_approval(self):
        for extra in [
            {"approved_text_or_code": "private metadata text"}, {"text": "private metadata text"},
            {"security_approved": True}, {"speaker": "assistant"},
        ]:
            with self.subTest(extra=extra):
                self.assert_rejected(terminal_result(metadata={**TERMINAL_METADATA, **extra}), "unresolved")

    def test_optional_citation_ids_are_validated_and_detached_from_mutable_input(self):
        # A frozen record must not retain a mutable citation list owned by the response.
        citations = ["evidence-001", "evidence-002"]
        raw = terminal_result(metadata={**TERMINAL_METADATA, "cleared_citation_ids": citations})
        result = self.verify(raw)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        citations.append("unverified-later-change")
        self.assertEqual(result.record.cleared_citation_ids, ("evidence-001", "evidence-002"))
        for value in ["evidence-001", [1], [""], [" "], ["\ud800"], ["x" * 129], None]:
            with self.subTest(value=repr(value)[:50]):
                self.assert_rejected(terminal_result(metadata={**TERMINAL_METADATA, "cleared_citation_ids": value}), "unresolved")

    def test_omitted_optional_references_do_not_invent_citations_or_an_offer(self):
        result = self.verify(terminal_result())
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.cleared_citation_ids, ())
        self.assertIsNone(result.record.verified_offer_id)

    def test_citation_label_local_limit_counts_utf8_bytes(self):
        # Characterize the implemented byte guard without breaking correct code for RED.
        for label, status in [("x" * 128, "verified"), ("😀" * 32, "verified"), ("😀" * 33, "unresolved")]:
            with self.subTest(bytes=len(label.encode("utf-8"))):
                raw = terminal_result(metadata={**TERMINAL_METADATA, "cleared_citation_ids": [label]})
                result = self.verify(raw)
                self.assertEqual(result.status, status)
                if status == "verified":
                    self.assertEqual(result.record.cleared_citation_ids, (label,))
                else:
                    self.assertIsNone(result.record)

    def test_completed_offer_requires_a_well_formed_offer_reference(self):
        metadata = {**TERMINAL_METADATA, "kind": "completed_offer"}
        self.assert_rejected(terminal_result(metadata=metadata), "unresolved")
        for value in ["", "bad-uuid", 1, None]:
            with self.subTest(value=value):
                self.assert_rejected(terminal_result(metadata={**metadata, "verified_offer_id": value}), "unresolved")
        # Other completed kinds may carry a partial-answer offer link; no extra kind ban.
        result = self.verify(terminal_result(metadata={**TERMINAL_METADATA, "verified_offer_id": OFFER_ID}))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.verified_offer_id, OFFER_ID)

    def test_malformed_or_ambiguous_native_results_are_not_terminal_records(self):
        invalid = [None, {}, {"ids": [], "metadatas": None, "documents": None}]
        for field in ["ids", "metadatas", "documents"]:
            raw = terminal_result()
            raw[field] = []
            invalid.append(raw)
        raw = terminal_result()
        for field in ["ids", "metadatas", "documents"]:
            raw[field] *= 2
        invalid.append(raw)
        raw = terminal_result()
        raw["metadatas"][0] = None
        invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:60]):
                self.assert_rejected(raw, "unresolved")

    def test_invalid_expected_selectors_do_not_yield_terminal_authority(self):
        for selectors in [
            {"user_id": "../foreign"}, {"request_id": "bad-uuid"},
            {"conversation_id": "bad-uuid"}, {"sequence": -1},
            {"sequence": True}, {"sequence": "2"},
        ]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(terminal_result(), **selectors)


# Overall flow: hand-built native-shaped fixture -> real verifier -> binding or safe status.


class PendingTerminalPairTests(unittest.TestCase):
    """LLD §4: never expose half an exchange or accept mismatched consecutive events."""

    def verify(self, pending=None, terminal=None, **selectors):
        from shopunow.records import verify_pending_terminal_pair
        return verify_pending_terminal_pair(
            pending_result() if pending is None else pending,
            terminal_result() if terminal is None else terminal,
            **{"user_id": USER_ID, "conversation_id": CONVERSATION_ID,
               "request_id": REQUEST_ID, "sequence": 1, **selectors},
        )  # Supply independent native-shaped boundary fixtures, not prevalidated models.

    def test_complete_pair_returns_both_unmodified_records(self):
        result = self.verify()
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.pending.text, "  Help with my delivery?  ")
        self.assertEqual(result.record.terminal.approved_text_or_code, "  The stored response.  ")
        self.assertEqual((result.record.pending.sequence, result.record.terminal.sequence), (1, 2))

    def test_nonconsecutive_terminal_positions_conflict_without_content(self):
        for position in [0, 1, 3, 50]:
            with self.subTest(position=position):
                result = self.verify(terminal=terminal_result(metadata={**TERMINAL_METADATA, "sequence": position}))
                self.assertEqual(result.status, "conflict")
                self.assertIsNone(result.record)

    def test_identity_or_link_mismatch_on_either_half_withholds_entire_pair(self):
        for side, fields in [
            ("pending", {"owner_user_id": "foreign", "conversation_id": OFFER_ID,
                         "request_id": OFFER_ID, "message_id": "pending_message:wrong", "sequence": 0}),
            ("terminal", {"owner_user_id": "foreign", "conversation_id": OFFER_ID,
                          "request_id": OFFER_ID, "pending_message_id": "pending_message:wrong",
                          "outcome_id": "terminal_outcome:wrong"}),
        ]:
            for field, value in fields.items():
                with self.subTest(side=side, field=field):
                    raw = (pending_result(metadata={**PENDING_METADATA, field: value}) if side == "pending"
                           else terminal_result(metadata={**TERMINAL_METADATA, field: value}))
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "conflict")
                    self.assertIsNone(result.record)

    def test_missing_malformed_incomplete_or_duplicate_half_is_unresolved(self):
        for side, fixture in [("pending", pending_result), ("terminal", terminal_result)]:
            incomplete = fixture()
            incomplete["metadatas"][0]["complete"] = False
            duplicate = fixture()
            for key in ["ids", "metadatas", "documents"]:
                duplicate[key] *= 2
            for raw in [{"ids": [], "metadatas": [], "documents": []}, {}, incomplete, duplicate]:
                with self.subTest(side=side, raw=raw):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "unresolved")
                    self.assertIsNone(result.record)

    def test_zero_origin_and_equivalent_selector_spellings_are_accepted(self):
        result = self.verify(
            pending=pending_result(metadata={**PENDING_METADATA, "sequence": 0}),
            terminal=terminal_result(metadata={**TERMINAL_METADATA, "sequence": 1}),
            sequence=0, request_id=REQUEST_ID.replace("-", "").upper(),
            conversation_id=CONVERSATION_ID.replace("-", "").upper(),
        )
        self.assertEqual(result.status, "verified")
        self.assertEqual((result.record.pending.sequence, result.record.terminal.sequence), (0, 1))

    def test_all_terminal_kinds_keep_their_structural_meaning(self):
        for kind in ["completed_answer", "completed_clarification", "completed_refusal", "completed_offer", "failed_technical"]:
            metadata = {**TERMINAL_METADATA, "kind": kind}
            if kind == "completed_offer":
                metadata["verified_offer_id"] = OFFER_ID
            raw = terminal_result(metadata=metadata, document="technical_error" if kind == "failed_technical" else "Reviewed text")
            with self.subTest(kind=kind):
                result = self.verify(terminal=raw)
                self.assertEqual(result.status, "verified")
                self.assertEqual(result.record.terminal.kind, kind)

    def test_invalid_expected_position_is_not_coerced(self):
        from pydantic import ValidationError
        for sequence in [-1, True, "1", 1.5]:
            with self.subTest(sequence=sequence), self.assertRaises(ValidationError):
                self.verify(sequence=sequence)


MARKER_METADATA = {
    "record_type": "preclearance_terminal",
    "marker_id": "preclearance_terminal:fedcba98765443218abcfedcba987654",
    "request_id": REQUEST_ID, "owner_user_id": USER_ID,
    "conversation_id": CONVERSATION_ID, "verified_offer_id": OFFER_ID,
    "reason_code": "security_uncertain", "created_at": "2026-11-02T10:00:00Z", "complete": True,
}  # Literal LLD §4 fields: no normal-message text, position or pending link.


def marker_result():
    return {
        "ids": ["preclearance_terminal:fedcba98765443218abcfedcba987654"],
        "metadatas": [dict(MARKER_METADATA)], "documents": ["preclearance_terminal"],
        "embeddings": None, "included": ["metadatas", "documents"],
    }  # A supplied native boundary fixture, without a client or actual stored offer.


class PreclearanceMarkerTests(unittest.TestCase):
    def verify(self, raw, **selectors):
        from shopunow.records import verify_preclearance_terminal
        return verify_preclearance_terminal(raw, **{
            "user_id": USER_ID, "conversation_id": CONVERSATION_ID,
            "request_id": REQUEST_ID, "offer_id": OFFER_ID, **selectors,
        })

    def assert_rejected(self, raw, status):
        result = self.verify(raw)
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)  # Never expose even a partly valid marker.

    def test_both_allowed_reasons_return_content_free_marker(self):
        for reason in ["security_uncertain", "dependency_failure"]:
            raw = marker_result()
            raw["metadatas"][0]["reason_code"] = reason
            result = self.verify(raw)
            with self.subTest(reason=reason):
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.reason_code, reason)
                self.assertEqual(result.record.verified_offer_id, OFFER_ID)
                self.assertEqual(result.record.marker_code, "preclearance_terminal")
                self.assertFalse(hasattr(result.record, "text"))

    def test_identity_and_expected_offer_mismatches_conflict(self):
        for field, value in [
            ("marker_id", "preclearance_terminal:wrong"), ("owner_user_id", "foreign"),
            ("conversation_id", OFFER_ID), ("request_id", OFFER_ID),
            ("verified_offer_id", CONVERSATION_ID),
        ]:
            with self.subTest(field=field):
                raw = marker_result()
                raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "conflict")
        raw = marker_result()
        raw["ids"][0] = "terminal_outcome:fedcba98765443218abcfedcba987654"
        self.assert_rejected(raw, "conflict")

    def test_every_required_field_is_required(self):
        for field in MARKER_METADATA:
            with self.subTest(field=field):
                raw = marker_result()
                del raw["metadatas"][0][field]
                self.assert_rejected(raw, "unresolved")

    def test_question_model_contact_and_normal_message_fields_are_rejected(self):
        for field, value in [
            ("text", "private question"), ("question", "private question"),
            ("approved_text_or_code", "model answer"), ("email", "synthetic@example.invalid"),
            ("pending_message_id", "pending_message:wrong"), ("sequence", 1),
            ("marker_code", "preclearance_terminal"), ("speaker", "user"),
        ]:
            with self.subTest(field=field):
                raw = marker_result()
                raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "unresolved")

    def test_document_must_be_exact_fixed_code(self):
        for document in [None, "", "private question", "preclearance_terminal ",
                         "PREClearance_terminal", "\ud800", b"preclearance_terminal"]:
            with self.subTest(document=repr(document)):
                raw = marker_result()
                raw["documents"] = [document]
                self.assert_rejected(raw, "unresolved")

    def test_invalid_reason_completion_timestamp_and_identifiers_are_unresolved(self):
        for field, values in [
            ("reason_code", ["confirmed_attack", "completed_answer", "", None]),
            ("complete", [False, 1, "true", None]),
            ("created_at", ["2026-11-02T10:00:00", 123, "bad-date", None]),
            ("verified_offer_id", ["bad-uuid", None]),
            ("record_type", ["terminal_outcome", None]),
        ]:
            for value in values:
                with self.subTest(field=field, value=value):
                    raw = marker_result()
                    raw["metadatas"][0][field] = value
                    self.assert_rejected(raw, "unresolved")

    def test_absence_malformed_and_ambiguous_native_results_withhold_marker(self):
        self.assert_rejected({"ids": [], "metadatas": [], "documents": []}, "absent")
        for raw in [None, {}, {**marker_result(), "metadatas": [None]},
                    {**marker_result(), "documents": []},
                    {key: value * 2 for key, value in marker_result().items() if key in ["ids", "metadatas", "documents"]}]:
            with self.subTest(raw=raw):
                self.assert_rejected(raw, "unresolved")

    def test_uuid_selectors_normalize_without_changing_record_identity(self):
        result = self.verify(marker_result(), **{
            key: value.replace("-", "").upper() for key, value in [
                ("conversation_id", CONVERSATION_ID), ("request_id", REQUEST_ID), ("offer_id", OFFER_ID),
            ]
        })
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.request_id, REQUEST_ID)

    def test_invalid_selectors_cannot_yield_marker_authority(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"request_id": "bad-uuid"}, {"offer_id": "bad-uuid"}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(marker_result(), **selectors)


OFFER_METADATA = {
    "record_type": "offer", "offer_id": OFFER_ID, "owner_user_id": USER_ID,
    "conversation_id": CONVERSATION_ID, "request_id": REQUEST_ID,
    "reason_code": "individual_case", "target_department_or_fallback": "shipping_delivery",
    "created_at": "2026-11-02T10:00:00Z", "complete": True,
}  # Expected routing labels are supplied by the caller, not a new route catalog.


def offer_result():
    return {
        "ids": ["offer:" + OFFER_ID.replace("-", "")],
        "metadatas": [dict(OFFER_METADATA)], "documents": ["handoff_offer"],
        "embeddings": None, "included": ["metadatas", "documents"],
    }  # Contact-free native-shaped input; no actual saved offer or terminal proof.


class HandoffOfferTests(unittest.TestCase):
    def verify(self, raw, **selectors):
        from shopunow.records import verify_handoff_offer
        return verify_handoff_offer(raw, **{
            "user_id": USER_ID, "conversation_id": CONVERSATION_ID,
            "request_id": REQUEST_ID, "offer_id": OFFER_ID, "reason_code": "individual_case",
            "target_department_or_fallback": "shipping_delivery", **selectors,
        })

    def assert_rejected(self, raw, status):
        result = self.verify(raw)
        self.assertEqual(result.status, status)
        self.assertIsNone(result.record)

    def test_valid_department_and_fallback_offers_preserve_expected_values(self):
        for reason, target in [("individual_case", "shipping_delivery"),
                               ("security_uncertain", "human_support"),
                               ("dependency_failure", "human_support")]:
            raw = offer_result()
            raw["metadatas"][0].update(reason_code=reason, target_department_or_fallback=target)
            result = self.verify(raw, reason_code=reason, target_department_or_fallback=target)
            with self.subTest(reason=reason, target=target):
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.offer_id, OFFER_ID)
                self.assertEqual(result.record.reason_code, reason)
                self.assertEqual(result.record.target_department_or_fallback, target)
                self.assertEqual(result.record.offer_code, "handoff_offer")

    def test_native_and_logical_identity_route_or_reason_conflicts_withhold_offer(self):
        for field, value in [
            ("offer_id", CONVERSATION_ID), ("owner_user_id", "foreign"),
            ("conversation_id", OFFER_ID), ("request_id", OFFER_ID),
            ("reason_code", "different_reason"), ("target_department_or_fallback", "hr"),
        ]:
            with self.subTest(field=field):
                raw = offer_result()
                raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "conflict")
        raw = offer_result()
        raw["ids"][0] = "offer:wrong"
        self.assert_rejected(raw, "conflict")

    def test_every_required_field_is_required(self):
        for field in OFFER_METADATA:
            with self.subTest(field=field):
                raw = offer_result()
                del raw["metadatas"][0][field]
                self.assert_rejected(raw, "unresolved")

    def test_contact_request_text_and_submission_claims_are_not_offer_fields(self):
        for field, value in [("name", "Synthetic Name"), ("email", "test@example.invalid"),
                             ("phone", "000"), ("text", "untrusted request"),
                             ("question", "untrusted request"), ("receipt_id", REQUEST_ID),
                             ("submission_id", REQUEST_ID), ("offer_code", "handoff_offer"),
                             ("sequence", 1)]:
            with self.subTest(field=field):
                raw = offer_result()
                raw["metadatas"][0][field] = value
                self.assert_rejected(raw, "unresolved")

    def test_document_is_fixed_code_never_contact_or_question_text(self):
        for value in [None, "", "handoff_offer ", "private contact", "\ud800", b"handoff_offer"]:
            with self.subTest(value=repr(value)):
                raw = offer_result()
                raw["documents"] = [value]
                self.assert_rejected(raw, "unresolved")

    def test_malformed_completeness_timestamp_and_codes_are_unresolved(self):
        for field, values in [
            ("complete", [False, 1, "true", None]),
            ("created_at", ["2026-11-02T10:00:00", "bad-date", 123, None]),
            ("offer_id", ["bad-uuid", None]), ("record_type", ["submission", None]),
            ("reason_code", ["", "free text", "x" * 65, "é", None]),
            ("target_department_or_fallback", ["", "free text", "x" * 65, "é", None]),
        ]:
            for value in values:
                with self.subTest(field=field, value=value):
                    raw = offer_result()
                    raw["metadatas"][0][field] = value
                    self.assert_rejected(raw, "unresolved")

    def test_absence_malformed_and_duplicate_responses_expose_no_offer(self):
        self.assert_rejected({"ids": [], "metadatas": [], "documents": []}, "absent")
        for raw in [None, {}, {**offer_result(), "metadatas": [None]},
                    {**offer_result(), "documents": []},
                    {key: value * 2 for key, value in offer_result().items() if key in ["ids", "metadatas", "documents"]}]:
            with self.subTest(raw=raw):
                self.assert_rejected(raw, "unresolved")

    def test_uuid_selectors_normalize_and_boundary_labels_are_accepted(self):
        raw = offer_result()
        raw["metadatas"][0]["reason_code"] = "a" * 64
        result = self.verify(raw, reason_code="a" * 64, **{
            key: value.replace("-", "").upper() for key, value in [
                ("conversation_id", CONVERSATION_ID), ("request_id", REQUEST_ID), ("offer_id", OFFER_ID),
            ]
        })
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.reason_code, "a" * 64)

    def test_invalid_expected_selectors_raise_before_returning_offer(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"request_id": "bad-uuid"}, {"offer_id": "bad-uuid"},
                          {"reason_code": "free text"}, {"reason_code": "x" * 65},
                          {"target_department_or_fallback": ""}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(offer_result(), **selectors)


class PreclearanceOfferPairTests(unittest.TestCase):
    """LLD §4: two valid-looking halves must still form one consistent fallback pair."""

    def offer(self):
        raw = offer_result()
        raw["metadatas"][0].update(reason_code="security_uncertain", target_department_or_fallback="human_support")
        return raw  # Hand-specified pairing with the existing security-uncertain marker.

    def verify(self, marker=None, offer=None, **selectors):
        from shopunow.records import verify_preclearance_offer_pair
        return verify_preclearance_offer_pair(
            marker_result() if marker is None else marker,
            self.offer() if offer is None else offer,
            **{"user_id": USER_ID, "conversation_id": CONVERSATION_ID,
               "request_id": REQUEST_ID, "offer_id": OFFER_ID,
               "reason_code": "security_uncertain", "target_department_or_fallback": "human_support",
               **selectors},
        )

    def test_matching_pair_for_each_preclearance_reason_returns_both_records(self):
        for reason in ["security_uncertain", "dependency_failure"]:
            marker, offer = marker_result(), self.offer()
            marker["metadatas"][0]["reason_code"] = reason
            offer["metadatas"][0]["reason_code"] = reason
            result = self.verify(marker, offer, reason_code=reason)
            with self.subTest(reason=reason):
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.marker.reason_code, reason)
                self.assertEqual(result.record.offer.reason_code, reason)
                self.assertEqual(result.record.marker.verified_offer_id, OFFER_ID)
                self.assertEqual(result.record.offer.offer_id, OFFER_ID)

    def test_different_valid_reasons_on_either_half_conflict_without_partial_content(self):
        for side in ["marker", "offer"]:
            raw = marker_result() if side == "marker" else self.offer()
            raw["metadatas"][0]["reason_code"] = "dependency_failure"
            with self.subTest(side=side):
                result = self.verify(**{side: raw})
                self.assertEqual(result.status, "conflict")
                self.assertIsNone(result.record)

    def test_identity_and_link_mismatch_on_either_half_withholds_pair(self):
        for side in ["marker", "offer"]:
            for field, value in [("owner_user_id", "foreign"), ("conversation_id", OFFER_ID),
                                 ("request_id", OFFER_ID),
                                 ("verified_offer_id" if side == "marker" else "offer_id", CONVERSATION_ID)]:
                raw = marker_result() if side == "marker" else self.offer()
                raw["metadatas"][0][field] = value
                with self.subTest(side=side, field=field):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "conflict")
                    self.assertIsNone(result.record)

    def test_missing_incomplete_malformed_or_duplicate_half_is_unresolved(self):
        for side in ["marker", "offer"]:
            fixture = marker_result if side == "marker" else self.offer
            incomplete = fixture()
            incomplete["metadatas"][0]["complete"] = False
            duplicate = fixture()
            for key in ["ids", "metadatas", "documents"]:
                duplicate[key] *= 2
            for raw in [{"ids": [], "metadatas": [], "documents": []}, {}, incomplete, duplicate]:
                with self.subTest(side=side, raw=raw):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "unresolved")
                    self.assertIsNone(result.record)
        result = self.verify({}, {})
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.record)

    def test_target_mismatch_conflicts_even_when_links_and_reason_match(self):
        raw = self.offer()
        raw["metadatas"][0]["target_department_or_fallback"] = "shipping_delivery"
        result = self.verify(offer=raw)
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.record)

    def test_content_or_normal_message_fields_cannot_enter_either_half(self):
        for side in ["marker", "offer"]:
            for field, value in [("text", "untrusted question"), ("email", "test@example.invalid"), ("sequence", 1)]:
                raw = marker_result() if side == "marker" else self.offer()
                raw["metadatas"][0][field] = value
                with self.subTest(side=side, field=field):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "unresolved")
                    self.assertIsNone(result.record)
            raw = marker_result() if side == "marker" else self.offer()
            raw["documents"][0] = "untrusted question"
            result = self.verify(**{side: raw})
            self.assertEqual(result.status, "unresolved")
            self.assertIsNone(result.record)

    def test_equivalent_uuid_selectors_are_accepted(self):
        result = self.verify(**{key: value.replace("-", "").upper() for key, value in [
            ("conversation_id", CONVERSATION_ID), ("request_id", REQUEST_ID), ("offer_id", OFFER_ID),
        ]})
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.offer.target_department_or_fallback, "human_support")

    def test_invalid_expected_selectors_raise_without_yielding_a_pair(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"request_id": "bad-uuid"}, {"offer_id": "bad-uuid"},
                          {"reason_code": "free text"}, {"target_department_or_fallback": ""}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(**selectors)


class ClearedExchangeOfferTests(unittest.TestCase):
    """LLD §4: a linked offer needs the complete owned consecutive exchange."""

    def terminal(self):
        return terminal_result(metadata={**TERMINAL_METADATA, "kind": "completed_offer", "verified_offer_id": OFFER_ID})

    def verify(self, pending=None, terminal=None, offer=None, **selectors):
        from shopunow.records import verify_cleared_exchange_offer
        return verify_cleared_exchange_offer(
            pending_result() if pending is None else pending,
            self.terminal() if terminal is None else terminal,
            offer_result() if offer is None else offer,
            **{"user_id": USER_ID, "conversation_id": CONVERSATION_ID,
               "request_id": REQUEST_ID, "sequence": 1, "offer_id": OFFER_ID,
               "reason_code": "individual_case", "target_department_or_fallback": "shipping_delivery",
               **selectors},
        )

    def test_completed_kinds_with_link_return_exchange_and_offer_unchanged(self):
        for kind in ["completed_offer", "completed_answer", "completed_clarification", "completed_refusal"]:
            raw = self.terminal()
            raw["metadatas"][0]["kind"] = kind
            with self.subTest(kind=kind):
                result = self.verify(terminal=raw)
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.exchange.pending.text, "  Help with my delivery?  ")
                self.assertEqual(result.record.exchange.terminal.kind, kind)
                self.assertEqual(result.record.exchange.terminal.approved_text_or_code, "  The stored response.  ")
                self.assertEqual(result.record.offer.offer_id, OFFER_ID)

    def test_missing_or_wrong_terminal_offer_link_conflicts_without_partial_content(self):
        for link in [None, CONVERSATION_ID]:
            raw = self.terminal()
            raw["metadatas"][0].update(kind="completed_answer", verified_offer_id=link)
            result = self.verify(terminal=raw)
            self.assertEqual(result.status, "conflict")
            self.assertIsNone(result.record)

    def test_technical_failure_cannot_become_linked_offer_response(self):
        raw = self.terminal()
        raw["metadatas"][0]["kind"] = "failed_technical"
        raw["documents"][0] = "technical_error"
        result = self.verify(terminal=raw)
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.record)

    def test_identity_or_sequence_mismatch_in_any_record_withholds_all_records(self):
        for side in ["pending", "terminal", "offer"]:
            fixture = {"pending": pending_result, "terminal": self.terminal, "offer": offer_result}[side]
            for field, value in [("owner_user_id", "foreign"), ("conversation_id", OFFER_ID), ("request_id", OFFER_ID)]:
                raw = fixture()
                raw["metadatas"][0][field] = value
                with self.subTest(side=side, field=field):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "conflict")
                    self.assertIsNone(result.record)
        raw = self.terminal()
        raw["metadatas"][0]["sequence"] = 3
        result = self.verify(terminal=raw)
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.record)

    def test_invalid_or_absent_component_is_unresolved_never_partial_success(self):
        for side in ["pending", "terminal", "offer"]:
            fixture = {"pending": pending_result, "terminal": self.terminal, "offer": offer_result}[side]
            incomplete = fixture()
            incomplete["metadatas"][0]["complete"] = False
            duplicate = fixture()
            for key in ["ids", "metadatas", "documents"]:
                duplicate[key] *= 2
            for raw in [{}, {"ids": [], "metadatas": [], "documents": []}, incomplete, duplicate]:
                with self.subTest(side=side, raw=raw):
                    result = self.verify(**{side: raw})
                    self.assertEqual(result.status, "unresolved")
                    self.assertIsNone(result.record)

    def test_offer_reason_target_and_identity_must_match_expectations(self):
        for field, value in [("offer_id", CONVERSATION_ID), ("reason_code", "different_reason"),
                             ("target_department_or_fallback", "hr")]:
            raw = offer_result()
            raw["metadatas"][0][field] = value
            with self.subTest(field=field):
                result = self.verify(offer=raw)
                self.assertEqual(result.status, "conflict")
                self.assertIsNone(result.record)

    def test_uuid_selector_normalization_and_zero_pending_position_are_supported(self):
        pending, terminal = pending_result(), self.terminal()
        pending["metadatas"][0]["sequence"] = 0
        terminal["metadatas"][0]["sequence"] = 1
        result = self.verify(pending, terminal, sequence=0, **{
            key: value.replace("-", "").upper() for key, value in [
                ("conversation_id", CONVERSATION_ID), ("request_id", REQUEST_ID), ("offer_id", OFFER_ID),
            ]
        })
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.exchange.terminal.sequence, 1)

    def test_invalid_expected_selectors_do_not_return_a_linked_exchange(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"request_id": "bad-uuid"}, {"offer_id": "bad-uuid"},
                          {"sequence": True}, {"sequence": -1},
                          {"reason_code": "free text"}, {"target_department_or_fallback": ""}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(**selectors)


def event_result(*results):
    # Combine existing native rows, without inventing a second test input format.
    return {
        key: [value for result in results for value in result[key]]
        for key in ("ids", "metadatas", "documents")
    }


def transcript_pair(number=1, sequence=1, kind="completed_answer", *, with_offer=False):
    # Distinct deterministic fixture IDs allow multi-request histories without I/O.
    request = str(UUID(int=number))
    pending = pending_result(metadata={
        **PENDING_METADATA, "request_id": request, "sequence": sequence,
        "message_id": "pending_message:" + UUID(request).hex,
    })
    pending["ids"] = ["pending_message:" + UUID(request).hex]
    terminal = terminal_result(metadata={
        **TERMINAL_METADATA, "request_id": request, "sequence": sequence + 1,
        "outcome_id": "terminal_outcome:" + UUID(request).hex, "kind": kind,
        "pending_message_id": pending["ids"][0],
        **({"verified_offer_id": str(UUID(int=number + 1000))} if with_offer else {}),
    }, document="technical_error" if kind == "failed_technical" else "Stored answer " + str(number))
    terminal["ids"] = ["terminal_outcome:" + UUID(request).hex]
    return pending, terminal


def transcript_marker(number=1, *, created_at="2026-11-02T10:00:00Z"):
    marker = marker_result()  # Reuse the approved content-free marker fixture.
    marker["ids"] = ["preclearance_terminal:" + UUID(int=number).hex]
    marker["metadatas"][0].update({
        "marker_id": marker["ids"][0], "request_id": str(UUID(int=number)),
        "verified_offer_id": str(UUID(int=number + 1000)), "created_at": created_at,
    })
    return marker


def transcript_offer(number=1, *, reason="individual_case", target="shipping_delivery"):
    offer = offer_result()  # Literal field contracts remain independent of production models.
    offer["ids"] = ["offer:" + UUID(int=number + 1000).hex]
    offer["metadatas"][0].update({
        "offer_id": str(UUID(int=number + 1000)), "request_id": str(UUID(int=number)),
        "reason_code": reason, "target_department_or_fallback": target,
    })
    return offer


class TranscriptEventTests(unittest.TestCase):
    """Protect the event-only boundary before a caller can select linked offers."""

    def verify(self, events=None, **selectors):
        from shopunow import records
        verifier = getattr(records, "verify_transcript_events", None)
        self.assertTrue(callable(verifier), "The public event-validation stage is required.")
        return verifier(
            event_result() if events is None else events,
            **{"user_id": USER_ID, "conversation_id": CONVERSATION_ID, **selectors},
        )  # Exercise native supplied rows, without a header or any offer results.

    def assert_unresolved(self, events, **selectors):
        result = self.verify(events, **selectors)
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.record)  # No partial selector set may survive any invalid event.

    def test_complete_pair_selects_offer_without_claiming_offer_or_header_proof(self):
        pending, terminal = transcript_pair(with_offer=True)
        result = self.verify(event_result(terminal, pending))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.offer_ids, ("00000000-0000-0000-0000-0000000003e9",))
        self.assertEqual(result.record.event_count, 2)
        self.assertTrue(result.record.has_two_event_room)
        self.assertFalse(hasattr(result.record, "header"))
        self.assertFalse(hasattr(result.record, "exchanges"))

    def test_empty_unlinked_and_marker_streams_return_only_needed_selectors(self):
        for rows, offer_ids, count in [
            ((), (), 0), (transcript_pair(), (), 2),
            ((transcript_marker(),), ("00000000-0000-0000-0000-0000000003e9",), 1),
        ]:
            with self.subTest(count=count):
                result = self.verify(event_result(*rows))
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.offer_ids, offer_ids)
                self.assertEqual(result.record.event_count, count)

    def test_mixed_selectors_are_canonical_unique_and_order_independent(self):
        pending, terminal = transcript_pair(2, with_offer=True)
        duplicate_reference = transcript_marker(3)
        duplicate_reference["metadatas"][0]["verified_offer_id"] = str(UUID(int=1002))
        rows = [pending, terminal, transcript_marker(), duplicate_reference]
        result = self.verify(event_result(*rows))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.offer_ids, (
            "00000000-0000-0000-0000-0000000003e9", "00000000-0000-0000-0000-0000000003ea",
        ))  # One lookup selector does not certify its binding to either request.
        self.assertEqual(result.record.event_count, 4)
        self.assertEqual(self.verify(event_result(*reversed(rows))), result)
        self.assertEqual(self.verify(event_result(*rows),
                                    conversation_id=CONVERSATION_ID.replace("-", "").upper()), result)

    def test_technical_reference_is_selected_without_exposing_question_or_citations(self):
        pending, terminal = transcript_pair(kind="failed_technical", with_offer=True)
        pending["documents"] = ["PRIVATE failed pending text"]
        terminal["metadatas"][0]["cleared_citation_ids"] = ["PRIVATE-citation"]
        result = self.verify(event_result(terminal, pending))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.offer_ids, ("00000000-0000-0000-0000-0000000003e9",))
        self.assertNotIn("PRIVATE", repr(result))
        self.assertFalse(hasattr(result.record, "groups"))

    def test_foreign_event_with_valid_offer_uuid_returns_no_selectors(self):
        for field, value in [("owner_user_id", "syn-cust-02"), ("conversation_id", str(UUID(int=9)))]:
            pending, terminal = transcript_pair(with_offer=True)
            terminal["metadatas"][0][field] = value
            with self.subTest(field=field):
                self.assert_unresolved(event_result(pending, terminal))

    def test_malformed_last_row_withholds_preceding_valid_reference(self):
        invalid = transcript_marker(2)
        invalid["metadatas"][0]["complete"] = False
        self.assert_unresolved(event_result(transcript_marker(), invalid))
        self.assert_unresolved(event_result(
            *transcript_pair(with_offer=True),
            {"ids": ["unknown"], "metadatas": [None], "documents": [None]},
        ))

    def test_native_shape_cardinality_and_duplicate_ids_are_not_repaired(self):
        for field in ("ids", "metadatas", "documents"):
            for change in ("missing", "null", "short", "tuple"):
                raw = event_result(*transcript_pair(with_offer=True))
                if change == "missing":
                    del raw[field]
                elif change == "null":
                    raw[field] = None
                elif change == "short":
                    raw[field].pop()
                else:
                    raw[field] = tuple(raw[field])
                with self.subTest(field=field, change=change):
                    self.assert_unresolved(raw)
        pending, terminal = transcript_pair(with_offer=True)
        self.assert_unresolved(event_result(pending, terminal, pending))

    def test_bad_native_ids_types_identity_links_and_text_withhold_all_selectors(self):
        for field, value in [
            ("record_type", "offer"), ("request_id", "not-a-uuid"), ("complete", False),
            ("outcome_id", "wrong"), ("pending_message_id", "pending_message:wrong"),
            ("verified_offer_id", "not-a-uuid"), ("sequence", True),
        ]:
            pending, terminal = transcript_pair(with_offer=True)
            terminal["metadatas"][0][field] = value
            with self.subTest(field=field):
                self.assert_unresolved(event_result(pending, terminal))
        for text in (None, "", "\ud800", "😀" * 2049):
            pending, terminal = transcript_pair(with_offer=True)
            terminal["documents"] = [text]
            self.assert_unresolved(event_result(pending, terminal))
        pending, terminal = transcript_pair(with_offer=True)
        terminal["ids"] = ["terminal_outcome:wrong"]
        self.assert_unresolved(event_result(pending, terminal))
        marker = transcript_marker()
        marker["metadatas"][0]["sequence"] = 1
        self.assert_unresolved(event_result(marker))  # Markers cannot consume normal positions.

    def test_orphans_mixed_request_kinds_and_nonadjacent_pairs_return_no_selectors(self):
        pending, terminal = transcript_pair(with_offer=True)
        for rows in [(pending,), (terminal,), (pending, terminal, transcript_marker())]:
            with self.subTest(count=len(rows)):
                self.assert_unresolved(event_result(*rows))
        for sequences in [(0, 1), (2, 3), (1, 3), (1, 1)]:
            pending, terminal = transcript_pair(with_offer=True)
            pending["metadatas"][0]["sequence"], terminal["metadatas"][0]["sequence"] = sequences
            with self.subTest(sequences=sequences):
                self.assert_unresolved(event_result(pending, terminal))
        first_pending, first_terminal = transcript_pair(with_offer=True)
        second_pending, second_terminal = transcript_pair(2, 2, with_offer=True)
        first_terminal["metadatas"][0]["sequence"] = 3
        self.assert_unresolved(event_result(first_pending, second_pending, first_terminal, second_terminal))

    def test_default_cap_accepts_fifty_and_withholds_every_selector_at_fifty_one(self):
        for count, room in [(48, True), (49, False), (50, False), (51, None)]:
            rows = event_result(*(transcript_marker(i) for i in range(1, count + 1)))
            with self.subTest(count=count):
                if room is None:
                    self.assert_unresolved(rows)
                else:
                    result = self.verify(rows)
                    self.assertEqual(result.status, "verified")
                    self.assertIsNotNone(result.record)
                    self.assertEqual(result.record.event_count, count)
                    self.assertEqual(len(result.record.offer_ids), count)
                    self.assertEqual(result.record.has_two_event_room, room)

    def test_custom_cap_preserves_empty_and_exact_capacity_rules(self):
        for cap, room in [(1, False), (2, True), (3, True)]:
            result = self.verify(cap=cap)
            self.assertEqual(result.status, "verified")
            self.assertIsNotNone(result.record)
            self.assertEqual(result.record.has_two_event_room, room)
        self.assert_unresolved(event_result(*transcript_pair()), cap=1)
        result = self.verify(event_result(*transcript_pair()), cap=2)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertFalse(result.record.has_two_event_room)

    def test_selectors_are_frozen_and_detached_from_native_input(self):
        from dataclasses import FrozenInstanceError
        raw = transcript_marker()
        result = self.verify(event_result(raw))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        raw["metadatas"][0]["verified_offer_id"] = str(UUID(int=9))
        self.assertEqual(result.record.offer_ids, ("00000000-0000-0000-0000-0000000003e9",))
        with self.assertRaises(FrozenInstanceError):
            result.record.offer_ids = ()

    def test_invalid_caller_selectors_raise_instead_of_becoming_storage_failures(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"cap": True}, {"cap": 0}, {"cap": "50"}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(**selectors)


class CompleteTranscriptTests(unittest.TestCase):
    """Protect the approved all-or-nothing supplied-prefix contract, not live storage."""

    def verify(self, events=None, *, header=None, offers=None, **selectors):
        from shopunow.records import verify_complete_transcript
        return verify_complete_transcript(
            header_result() if header is None else header,
            event_result() if events is None else events,
            offer_results={} if offers is None else offers,
            **{"user_id": USER_ID, "conversation_id": CONVERSATION_ID, **selectors},
        )

    def assert_unresolved(self, events=None, **kwargs):
        result = self.verify(events, **kwargs)
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.record)  # No valid subset survives an invalid supplied event.

    def test_later_orphan_is_rejected_before_any_supplied_offer_result_is_consulted(self):
        class ObservedOffers(dict):
            def get(self, key, default=None):
                consulted.append(key)
                return super().get(key, default)

        consulted = []  # Observe mapping consultation, not actual storage I/O.
        offers = ObservedOffers({str(UUID(int=1001)): transcript_offer()})
        rows = event_result(*transcript_pair(with_offer=True), transcript_pair(2, 3)[0])
        self.assert_unresolved(rows, offers=offers)
        self.assertEqual(consulted, [])  # A complete valid neighbor cannot authorize an early lookup.

    def test_shared_offer_selector_still_requires_each_request_binding(self):
        first = transcript_pair(with_offer=True)
        second = transcript_pair(2, 3, with_offer=True)
        second[1]["metadatas"][0]["verified_offer_id"] = str(UUID(int=1001))
        self.assert_unresolved(event_result(*first, *second), offers={
            str(UUID(int=1001)): transcript_offer(),
        })  # Selector deduplication must not reuse request 1's verified offer for request 2.

    def test_owned_empty_history_is_readable_with_two_event_room(self):
        result = self.verify()
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.exchanges, ())
        self.assertEqual(result.record.notices, ())
        self.assertEqual(result.record.event_count, 0)
        self.assertTrue(result.record.has_two_event_room)

    def test_shuffled_completed_pairs_sort_by_position_for_all_completed_kinds(self):
        rows = []
        offers = {}
        for number, kind in enumerate([
            "completed_answer", "completed_clarification", "completed_refusal", "completed_offer",
        ], start=1):
            rows.extend(transcript_pair(number, 2 * number - 1, kind, with_offer=True))
            offers[str(UUID(int=number + 1000))] = transcript_offer(number)
        ordered = self.verify(event_result(*rows), offers=offers)
        shuffled = self.verify(event_result(*reversed(rows)), offers=offers)
        self.assertEqual(ordered.status, "verified")
        self.assertIsNotNone(ordered.record)
        self.assertEqual(shuffled, ordered)
        self.assertEqual([item.exchange.pending.sequence for item in ordered.record.exchanges], [1, 3, 5, 7])
        self.assertEqual([item.exchange.terminal.kind for item in ordered.record.exchanges], [
            "completed_answer", "completed_clarification", "completed_refusal", "completed_offer",
        ])
        self.assertEqual([item.offer.reason_code for item in ordered.record.exchanges], ["individual_case"] * 4)
        self.assertEqual(ordered.record.event_count, 8)

    def test_completed_pair_without_offer_preserves_documents_and_citations(self):
        pending, terminal = transcript_pair()
        terminal["metadatas"][0]["cleared_citation_ids"] = ["evidence-1"]
        result = self.verify(event_result(terminal, pending))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        exchange = result.record.exchanges[0]
        self.assertEqual(exchange.exchange.pending.text, "  Help with my delivery?  ")
        self.assertEqual(exchange.exchange.terminal.approved_text_or_code, "Stored answer 1")
        self.assertEqual(exchange.exchange.terminal.cleared_citation_ids, ("evidence-1",))
        self.assertIsNone(exchange.offer)

    def test_marker_only_history_returns_content_free_notice_with_linked_offer(self):
        result = self.verify(event_result(transcript_marker()), offers={
            str(UUID(int=1001)): transcript_offer(reason="security_uncertain", target="human_support"),
        })
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.exchanges, ())
        self.assertEqual(result.record.event_count, 1)
        notice = result.record.notices[0]
        self.assertEqual(notice.reason_code, "security_uncertain")
        self.assertEqual(notice.offer.offer_id, "00000000-0000-0000-0000-0000000003e9")
        self.assertFalse(hasattr(notice, "sequence"))

    def test_mixed_history_and_notices_have_independent_deterministic_order(self):
        pending, terminal = transcript_pair(4)
        rows = [terminal, transcript_marker(3, created_at="2026-11-02T11:00:00+01:00"),
                transcript_marker(2), pending, transcript_marker(1, created_at="2026-11-02T09:00:00Z")]
        offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain") for i in (1, 2, 3)}
        result = self.verify(event_result(*rows), offers=offers)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual([n.request_id for n in result.record.notices], [str(UUID(int=i)) for i in (1, 2, 3)])
        self.assertEqual(result.record.exchanges[0].exchange.pending.request_id, str(UUID(int=4)))
        self.assertEqual(result.record.event_count, 5)
        self.assertEqual(self.verify(event_result(*reversed(rows)), offers=offers), result)

    def test_technical_failure_hides_entire_pair_citations_and_optional_offer(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                pending, terminal = transcript_pair(kind="failed_technical", with_offer=linked)
                pending["documents"] = ["PRIVATE failed pending text"]
                terminal["metadatas"][0]["cleared_citation_ids"] = ["PRIVATE-citation"]
                later = transcript_pair(2, 3)
                offers = {str(UUID(int=1001)): transcript_offer()} if linked else {}
                result = self.verify(event_result(*later, terminal, pending), offers=offers)
                self.assertEqual(result.status, "verified")
                self.assertIsNotNone(result.record)
                self.assertEqual(result.record.event_count, 4)
                self.assertEqual(len(result.record.exchanges), 1)
                self.assertEqual(result.record.exchanges[0].exchange.pending.sequence, 3)
                self.assertEqual(result.record.notices[0].reason_code, "technical_error")
                self.assertIsNone(result.record.notices[0].offer)
                for hidden in ("PRIVATE", "00000000-0000-0000-0000-0000000003e9"):
                    self.assertNotIn(hidden, repr(result.record))

    def test_technical_notices_join_timestamp_request_order(self):
        pending, terminal = transcript_pair(1, kind="failed_technical")
        result = self.verify(event_result(transcript_marker(2), terminal, pending), offers={
            str(UUID(int=1002)): transcript_offer(2, reason="security_uncertain"),
        })
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual([n.reason_code for n in result.record.notices], ["security_uncertain", "technical_error"])

    def test_absent_and_well_formed_foreign_headers_share_content_free_failure(self):
        foreign = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        foreign["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        absent = self.verify(header=event_result())
        self.assertEqual(absent.status, "conflict")  # Only header ownership uses this status here.
        self.assertIsNone(absent.record)
        self.assertEqual(self.verify(header=foreign), absent)

    def test_corrupt_headers_are_unresolved_including_malformed_foreign_binding(self):
        invalid = [header_result({**HEADER, "complete": False}), {"ids": [], "documents": [], "metadatas": None}]
        for field, value in [("owner_user_id", "../foreign"), ("record_type", "profile"),
                             ("conversation_id", str(UUID(int=9)))]:
            raw = header_result()
            raw["metadatas"][0][field] = value
            invalid.append(raw)
        raw = header_result()
        raw["ids"] = ["conversation_header:wrong"]
        invalid.append(raw)
        raw = header_result({**HEADER, "owner_user_id": "syn-cust-02", "complete": False})
        raw["metadatas"][0]["owner_user_id"] = "syn-cust-02"
        invalid.append(raw)
        raw = header_result({**HEADER, "owner_user_id": "syn-cust-02"})
        invalid.append(raw)  # Metadata and document disagree; not a well-formed foreign header.
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_unresolved(header=raw)

    def test_missing_null_unequal_or_non_native_arrays_never_return_partial_history(self):
        for field in ("ids", "metadatas", "documents"):
            for change in ("missing", "null", "short", "tuple"):
                raw = event_result(*transcript_pair())
                if change == "missing":
                    del raw[field]
                elif change == "null":
                    raw[field] = None
                elif change == "short":
                    raw[field].pop()
                else:
                    raw[field] = tuple(raw[field])
                with self.subTest(field=field, change=change):
                    self.assert_unresolved(raw)

    def test_every_foreign_or_corrupt_event_invalidates_other_valid_pairs(self):
        for field, value in [("owner_user_id", "syn-cust-02"), ("conversation_id", str(UUID(int=9))),
                             ("record_type", "offer"), ("complete", False), ("created_at", "invalid")]:
            pending, terminal = transcript_pair(2, 3)
            pending["metadatas"][0][field] = value
            with self.subTest(field=field):
                self.assert_unresolved(event_result(*transcript_pair(), pending, terminal))
        for row in (header_result(), {"ids": ["unknown"], "metadatas": [None], "documents": [None]}):
            self.assert_unresolved(event_result(*transcript_pair(), row))

    def test_duplicate_native_ids_fail_even_when_array_lengths_are_valid(self):
        pending, terminal = transcript_pair()
        self.assert_unresolved(event_result(pending, terminal, pending))

    def test_orphans_and_multiple_terminals_invalidate_complete_neighbors(self):
        pending, terminal = transcript_pair(2, 3)
        for rows in [(pending,), (terminal,), (pending, terminal, terminal)]:
            with self.subTest(rows=len(rows)):
                self.assert_unresolved(event_result(*transcript_pair(), *rows))

    def test_global_origin_gaps_duplicate_positions_and_nonadjacent_pairs_fail(self):
        for sequences in [(0, 1), (2, 3), (1, 3), (1, 1)]:
            pending, terminal = transcript_pair()
            pending["metadatas"][0]["sequence"], terminal["metadatas"][0]["sequence"] = sequences
            with self.subTest(sequences=sequences):
                self.assert_unresolved(event_result(pending, terminal))
        first = transcript_pair()
        for position in (1, 4):
            self.assert_unresolved(event_result(*first, *transcript_pair(2, position)))
        p1, t1 = transcript_pair()
        p2, t2 = transcript_pair(2, 2)
        t1["metadatas"][0]["sequence"] = 3  # Positions 1–4 exist but pairs interleave.
        self.assert_unresolved(event_result(p1, p2, t1, t2))

    def test_marker_and_normal_records_cannot_share_a_request(self):
        self.assert_unresolved(event_result(*transcript_pair(), transcript_marker()), offers={
            str(UUID(int=1001)): transcript_offer(reason="security_uncertain"),
        })

    def test_malformed_stored_selectors_are_unresolved_without_escaping_exceptions(self):
        for make in (lambda: transcript_pair()[0], lambda: transcript_pair()[1], transcript_marker):
            for field, values in [("request_id", [None, "bad-uuid", True]),
                                  ("conversation_id", ["bad-uuid", None]),
                                  ("created_at", [None, 123, "2026-11-02T10:00:00"])]:
                for value in values:
                    raw = make()
                    raw["metadatas"][0][field] = value
                    with self.subTest(field=field, value=value):
                        self.assert_unresolved(event_result(raw))
        for value in (True, "1", -1, 1.5, None):
            pending, terminal = transcript_pair()
            pending["metadatas"][0]["sequence"] = value
            self.assert_unresolved(event_result(pending, terminal))
        for value in (None, "bad-uuid", True):
            marker = transcript_marker()
            marker["metadatas"][0]["verified_offer_id"] = value
            self.assert_unresolved(event_result(marker))

    def test_required_offer_failures_invalidate_marker_completed_and_technical_paths(self):
        for path in ("marker", "completed_answer", "failed_technical"):
            rows = [transcript_marker()] if path == "marker" else transcript_pair(kind=path, with_offer=True)
            good = transcript_offer(reason="security_uncertain" if path == "marker" else "individual_case")
            invalid = [event_result(), {}, {**good, "ids": ["offer:wrong"]},
                       {**good, "documents": ["untrusted free text"]}]
            for field, value in [("owner_user_id", "syn-cust-02"), ("conversation_id", str(UUID(int=9))),
                                 ("request_id", str(UUID(int=9))), ("offer_id", str(UUID(int=9))),
                                 ("record_type", "submission"), ("complete", False),
                                 ("reason_code", "free text"), ("target_department_or_fallback", "bad target")]:
                raw = deepcopy(good)
                raw["metadatas"][0][field] = value
                invalid.append(raw)
            for offer in invalid:
                with self.subTest(path=path, offer=offer):
                    self.assert_unresolved(event_result(*rows), offers={str(UUID(int=1001)): offer})
            self.assert_unresolved(event_result(*rows))  # A reference with no supplied result fails too.

    def test_marker_offer_reason_must_agree_but_historical_labels_are_not_route_permissions(self):
        marker = transcript_marker()
        self.assert_unresolved(event_result(marker), offers={str(UUID(int=1001)): transcript_offer()})
        pending, terminal = transcript_pair(with_offer=True)
        result = self.verify(event_result(pending, terminal), offers={
            str(UUID(int=1001)): transcript_offer(reason="historical_label", target="historical_fallback"),
        })
        self.assertEqual(result.status, "verified")  # Structure accepts bounded labels, not authorized routing.
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.exchanges[0].offer.reason_code, "historical_label")

    def test_bad_native_or_internal_links_and_documents_invalidate_entire_prefix(self):
        for field, value in [("outcome_id", "wrong"), ("pending_message_id", "wrong"),
                             ("verified_offer_id", "bad-uuid"), ("approved_text_or_code", "metadata text")]:
            pending, terminal = transcript_pair()
            terminal["metadatas"][0][field] = value
            self.assert_unresolved(event_result(pending, terminal))
        for text in (None, "", "\ud800", "😀" * 2049):
            for index in (0, 1):
                rows = transcript_pair()
                rows[index]["documents"] = [text]
                self.assert_unresolved(event_result(*rows))
        pending, terminal = transcript_pair()
        pending["ids"] = ["pending_message:wrong"]
        self.assert_unresolved(event_result(pending, terminal))

    def test_cap_boundaries_are_readable_without_silent_truncation(self):
        for count, status, room in [(48, "verified", True), (49, "verified", False),
                                    (50, "verified", False), (51, "unresolved", None)]:
            rows = [transcript_marker(i) for i in range(1, count + 1)]
            offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain") for i in range(1, count + 1)}
            with self.subTest(count=count):
                result = self.verify(event_result(*rows), offers=offers)
                self.assertEqual(result.status, status)
                if room is None:
                    self.assertIsNone(result.record)
                else:
                    self.assertIsNotNone(result.record)
                    self.assertEqual(result.record.event_count, count)
                    self.assertEqual(len(result.record.notices), count)
                    self.assertEqual(result.record.has_two_event_room, room)

    def test_mixed_capacity_counts_events_not_header_or_offers(self):
        rows = [row for i in range(1, 25) for row in transcript_pair(i, 2 * i - 1)]
        result = self.verify(event_result(*rows))
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.event_count, 48)
        self.assertTrue(result.record.has_two_event_room)
        for marker_count, room in [(1, False), (2, False)]:
            markers = [transcript_marker(i) for i in range(25, 25 + marker_count)]
            offers = {str(UUID(int=i + 1000)): transcript_offer(i, reason="security_uncertain") for i in range(25, 25 + marker_count)}
            result = self.verify(event_result(*rows, *markers), offers=offers)
            self.assertEqual(result.status, "verified")
            self.assertIsNotNone(result.record)
            self.assertEqual(result.record.event_count, 48 + marker_count)
            self.assertEqual(result.record.has_two_event_room, room)

    def test_configurable_cap_includes_exact_full_empty_and_small_histories(self):
        for cap, room in [(1, False), (2, True), (3, True)]:
            result = self.verify(cap=cap)
            self.assertEqual(result.status, "verified")
            self.assertIsNotNone(result.record)
            self.assertEqual(result.record.has_two_event_room, room)
        self.assert_unresolved(event_result(*transcript_pair()), cap=1)
        result = self.verify(event_result(*transcript_pair()), cap=2)
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertFalse(result.record.has_two_event_room)

    def test_success_is_detached_and_deeply_immutable_with_no_partial_failure_payload(self):
        from dataclasses import FrozenInstanceError
        pending, terminal = transcript_pair(with_offer=True)
        terminal["metadatas"][0]["cleared_citation_ids"] = ["evidence-1"]
        offer = transcript_offer()
        result = self.verify(event_result(pending, terminal), offers={str(UUID(int=1001)): offer})
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        terminal["metadatas"][0]["cleared_citation_ids"].append("mutated")
        pending["documents"][0] = "mutated"
        offer["metadatas"][0]["reason_code"] = "mutated"
        exchange = result.record.exchanges[0]
        self.assertEqual(exchange.exchange.terminal.cleared_citation_ids, ("evidence-1",))
        self.assertEqual(exchange.exchange.pending.text, "  Help with my delivery?  ")
        self.assertEqual(exchange.offer.reason_code, "individual_case")
        with self.assertRaises(FrozenInstanceError):
            result.record.event_count = 0
        with self.assertRaises(FrozenInstanceError):
            exchange.offer = None
        with self.assertRaises(ValueError):
            exchange.exchange.pending.text = "mutated"

    def test_invalid_caller_selectors_and_cap_are_not_coerced(self):
        for selectors in [{"user_id": "../foreign"}, {"conversation_id": "bad-uuid"},
                          {"cap": 0}, {"cap": -1}, {"cap": True}, {"cap": "50"}, {"cap": 1.5}]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.verify(**selectors)

    def test_equivalent_uuid_caller_selector_uses_same_owned_header(self):
        result = self.verify(conversation_id=CONVERSATION_ID.replace("-", "").upper())
        self.assertEqual(result.status, "verified")
        self.assertIsNotNone(result.record)
        self.assertEqual(result.record.event_count, 0)
