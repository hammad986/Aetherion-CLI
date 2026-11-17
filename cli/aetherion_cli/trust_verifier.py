"""Phase 1 signed-catalog trust model and verifier.

This module is deliberately independent of plugin installation and lifecycle
code. It validates a bounded, versioned catalog envelope and verifies an
Ed25519 signature with a caller-supplied trusted key provider. A key appearing
inside the envelope is never used as a trust anchor.

The catalog payload is constrained to JSON values that have unambiguous RFC
8785 JSON Canonicalization Scheme (JCS) serialization for this schema: objects,
arrays, strings, booleans, and bounded integers. Floating point numbers are
rejected rather than serialized with a non-JCS approximation. The
canonicalizer sorts object property names by UTF-16 code units, emits UTF-8,
and rejects duplicate JSON names during raw parsing.

No trust root is configured here. Production code must supply an explicitly
bootstrapped ``CatalogKeyProvider``; an empty provider therefore produces an
honest ``catalog_key_unknown`` result rather than approval.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Union
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_CATALOG_ENTRIES = 256
MAX_STRING_BYTES = 4096
MAX_PLUGIN_ID_BYTES = 64
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_SAFE_INTEGER = 2**53 - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_GITHUB_HOSTS = {"github.com", "www.github.com"}


class IntegrityStatus(str, Enum):
    UNKNOWN = "unknown"
    CHECKED = "checked"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    CORRUPT = "corrupt"


class CatalogAuthStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    UNSIGNED = "unsigned"
    UNKNOWN_KEY = "unknown_key"
    INVALID = "invalid"
    EXPIRED = "expired"
    REPLAYED = "replayed"
    VERIFIED = "verified"
    REVOKED = "revoked"


class PublisherAuthStatus(str, Enum):
    NOT_CHECKED = "not_checked"
    UNSIGNED = "unsigned"
    UNKNOWN_IDENTITY = "unknown_identity"
    INVALID = "invalid"
    VERIFIED = "verified"
    REVOKED = "revoked"
    MISMATCHED_IDENTITY = "mismatched_identity"


class CompatibilityStatus(str, Enum):
    UNKNOWN = "unknown"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    SOURCE_ONLY = "source_only"


class PolicyStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    USER_APPROVED = "user_approved"
    CATALOG_APPROVED = "catalog_approved"
    DENIED = "denied"
    STALE = "stale"
    REVOKED = "revoked"


class ExecutionStatus(str, Enum):
    DISABLED = "disabled"


class CatalogReason(str, Enum):
    SIGNATURE_MISSING = "catalog_signature_missing"
    SIGNATURE_INVALID = "catalog_signature_invalid"
    KEY_UNKNOWN = "catalog_key_unknown"
    KEY_REVOKED = "catalog_key_revoked"
    EXPIRED = "catalog_expired"
    NOT_YET_VALID = "catalog_not_yet_valid"
    SEQUENCE_ROLLBACK = "catalog_sequence_rollback"
    ENTRY_MISSING = "catalog_entry_missing"
    SUBJECT_MISMATCH = "catalog_subject_mismatch"
    PAYLOAD_DIGEST_MISMATCH = "catalog_payload_digest_mismatch"
    SCHEMA_INVALID = "catalog_schema_invalid"
    KEY_SCOPE_MISMATCH = "catalog_key_scope_mismatch"
    TRUST_STORE_CORRUPT = "trust_store_corrupt"
    ARCHIVE_DIGEST_MISMATCH = "archive_digest_mismatch"
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    INSTALLED_FILE_DIGEST_MISMATCH = "installed_file_digest_mismatch"
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    SOURCE_ONLY_EXECUTION_DISABLED = "source_only_execution_disabled"
    TRUST_ROOT_UNCONFIGURED = "catalog_trust_root_unconfigured"
    ENDPOINT_UNCONFIGURED = "catalog_endpoint_unconfigured"
    CACHE_CORRUPT = "catalog_cache_corrupt"
    TRANSPORT_FAILURE = "catalog_transport_failure"
    CONTENT_TYPE_INVALID = "catalog_content_type_invalid"
    REDIRECT_REJECTED = "catalog_redirect_rejected"
    RESPONSE_OVERSIZED = "catalog_response_oversized"
    OFFLINE_STALE = "catalog_offline_stale"
    KEYSET_SIGNATURE_INVALID = "keyset_signature_invalid"
    KEYSET_SEQUENCE_ROLLBACK = "keyset_sequence_rollback"
    KEYSET_EXPIRED = "keyset_expired"
    KEYSET_NOT_YET_VALID = "keyset_not_yet_valid"
    KEY_ROTATION_REJECTED = "key_rotation_rejected"
    KEY_RECOVERY_REQUIRES_OPERATOR = "key_recovery_requires_operator_material"
    TRUST_ROOT_REPLACEMENT_REQUIRES_OPERATOR = "trust_root_replacement_requires_operator"
    KEYSET_UNCONFIGURED = "keyset_unconfigured"
    CATALOG_KEY_EXPIRED = "catalog_key_expired"
    REVOCATION_STATUS_STALE = "catalog_revocation_status_stale"
    KEY_REVOCATION_ROLLBACK = "key_revocation_rollback"
    TRUST_LIFECYCLE_STATE_MISSING = "trust_lifecycle_state_missing"
    TRUST_LIFECYCLE_STATE_UPGRADE_REQUIRED = "trust_lifecycle_state_upgrade_required"
    TRUST_LIFECYCLE_TRANSACTION_CORRUPT = "trust_lifecycle_transaction_corrupt"
    TRUST_LIFECYCLE_IO_FAILURE = "trust_lifecycle_io_failure"
    ROOT_TRANSITION_UNAUTHORIZED = "root_transition_unauthorized"
    ROOT_TRANSITION_SIGNATURE_INVALID = "root_transition_signature_invalid"
    ROOT_FINGERPRINT_MISMATCH = "root_fingerprint_mismatch"


@dataclass(frozen=True)
class TrustState:
    """Independent trust dimensions with conservative legacy defaults."""

    integrity_status: IntegrityStatus = IntegrityStatus.UNKNOWN
    catalog_auth_status: CatalogAuthStatus = CatalogAuthStatus.UNSIGNED
    publisher_auth_status: PublisherAuthStatus = PublisherAuthStatus.NOT_CHECKED
    compatibility_status: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    policy_status: PolicyStatus = PolicyStatus.UNREVIEWED
    execution_status: ExecutionStatus = ExecutionStatus.DISABLED
    reason_code: Optional[str] = None
    authority_key_id: Optional[str] = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TrustState":
        """Read trust fields without upgrading legacy records."""

        def enum_value(enum_type: type[Enum], key: str, default: Enum) -> Enum:
            value = record.get(key)
            try:
                return enum_type(value) if value is not None else default
            except (TypeError, ValueError):
                return default

        return cls(
            integrity_status=enum_value(IntegrityStatus, "integrity_status", IntegrityStatus.UNKNOWN),  # type: ignore[arg-type]
            catalog_auth_status=enum_value(CatalogAuthStatus, "catalog_auth_status", CatalogAuthStatus.UNSIGNED),  # type: ignore[arg-type]
            publisher_auth_status=enum_value(PublisherAuthStatus, "publisher_auth_status", PublisherAuthStatus.NOT_CHECKED),  # type: ignore[arg-type]
            compatibility_status=enum_value(CompatibilityStatus, "compatibility_status", CompatibilityStatus.UNKNOWN),  # type: ignore[arg-type]
            policy_status=enum_value(PolicyStatus, "policy_status", PolicyStatus.UNREVIEWED),  # type: ignore[arg-type]
            execution_status=ExecutionStatus.DISABLED,
            reason_code=record.get("trust_reason_code") if isinstance(record.get("trust_reason_code"), str) else None,
            authority_key_id=record.get("catalog_key_id") if isinstance(record.get("catalog_key_id"), str) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "integrity_status": self.integrity_status.value,
            "catalog_auth_status": self.catalog_auth_status.value,
            "publisher_auth_status": self.publisher_auth_status.value,
            "compatibility_status": self.compatibility_status.value,
            "policy_status": self.policy_status.value,
            "execution_status": ExecutionStatus.DISABLED.value,
            "trust_reason_code": self.reason_code,
            "catalog_key_id": self.authority_key_id,
        }


@dataclass(frozen=True)
class CatalogKey:
    """A public key obtained from an independently configured trust source."""

    key_id: str
    public_key: bytes
    algorithm: str = "Ed25519"
    scopes: frozenset[str] = frozenset({"catalog"})
    revoked: bool = False
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    keyset_id: Optional[str] = None
    keyset_sequence: Optional[int] = None
    keyset_expires_at: Optional[str] = None


class TrustStoreCorruptError(ValueError):
    """Raised by a key provider when its independent keyset is invalid."""

    def __init__(self, message: str, reason_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class CatalogKeyProvider(Protocol):
    """Resolve a key from a trust store, never from the catalog envelope."""

    def get_key(self, key_id: str) -> Optional[CatalogKey]:
        ...


@dataclass(frozen=True)
class MappingCatalogKeyProvider:
    """Explicit keyset adapter for tests and controlled callers.

    The adapter does not imply production bootstrapping; callers must decide
    how the mapping was independently authenticated before passing it in.
    """

    keys: Mapping[str, CatalogKey]

    def get_key(self, key_id: str) -> Optional[CatalogKey]:
        return self.keys.get(key_id)


@dataclass(frozen=True)
class CatalogSubject:
    """Exact source object a signed entry is expected to approve."""

    plugin_id: str
    repository_url: str
    commit_sha: str
    archive_sha256: str
    archive_size_bytes: int
    manifest_path: str
    manifest_sha256: str
    manifest_schema_version: str
    plugin_api_version: str
    compatibility: str
    execution_enabled: bool = False


@dataclass(frozen=True)
class CatalogVerificationResult:
    """Qualified result identifying object, authority, subject, and reason."""

    catalog_auth_status: CatalogAuthStatus
    policy_status: PolicyStatus
    reason_code: Optional[CatalogReason]
    detail: str
    verified_object: str = "catalog_payload"
    authority_key_id: Optional[str] = None
    sequence: Optional[int] = None
    subject_plugin_id: Optional[str] = None
    canonical_payload_sha256: Optional[str] = None
    execution_status: ExecutionStatus = ExecutionStatus.DISABLED
    entry: Optional[Mapping[str, Any]] = None

    @property
    def qualified_status(self) -> str:
        if self.catalog_auth_status == CatalogAuthStatus.VERIFIED:
            return "catalog_signature_verified"
        return f"catalog_signature_{self.catalog_auth_status.value}"

    @property
    def verified(self) -> bool:
        return self.catalog_auth_status == CatalogAuthStatus.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified_status": self.qualified_status,
            "catalog_auth_status": self.catalog_auth_status.value,
            "policy_status": self.policy_status.value,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "detail": self.detail,
            "verified_object": self.verified_object,
            "authority_key_id": self.authority_key_id,
            "sequence": self.sequence,
            "subject_plugin_id": self.subject_plugin_id,
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "execution_status": ExecutionStatus.DISABLED.value,
        }


EnvelopeInput = Union[bytes, str, Mapping[str, Any]]

_ENVELOPE_KEYS = frozenset(
    {
        "format",
        "key_id",
        "algorithm",
        "canonicalization",
        "payload_sha256",
        "payload",
        "signature_base64",
    }
)
_PAYLOAD_KEYS = frozenset({"format", "catalog_id", "sequence", "issued_at", "expires_at", "entries"})
_ENTRY_KEYS = frozenset({"plugin_id", "repository", "selection", "archive", "manifest", "policy"})
_REPOSITORY_KEYS = frozenset({"canonical_url", "owner", "name"})
_SELECTION_KEYS = frozenset({"commit_sha", "ref_policy"})
_ARCHIVE_KEYS = frozenset({"format", "sha256", "size_bytes"})
_MANIFEST_KEYS = frozenset({"path", "sha256", "schema_version", "plugin_api_version"})
_POLICY_KEYS = frozenset({"compatibility", "execution_enabled", "publisher_evidence", "review_status"})


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def parse_catalog_json(raw: Union[bytes, str]) -> Mapping[str, Any]:
    """Parse a bounded catalog envelope and reject duplicate/NaN JSON values."""

    if isinstance(raw, bytes):
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("catalog envelope exceeds the 2 MiB limit")
        text = raw.decode("utf-8", errors="strict")
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError("catalog envelope exceeds the 2 MiB limit")
        text = raw
    else:
        raise TypeError("catalog envelope must be UTF-8 bytes or text")
    value = json.loads(
        text,
        object_pairs_hook=_duplicate_key_rejector,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("catalog envelope must be a JSON object")
    return value


def _jcs_string(value: str) -> str:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("JCS strings cannot contain lone UTF-16 surrogates")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utf16_sort_key(value: str) -> bytes:
    return value.encode("utf-16be", errors="strict")


def canonicalize_jcs(value: Any) -> bytes:
    """Serialize the supported catalog value subset according to RFC 8785 JCS.

    Catalog schema numbers are deliberately limited to safe integers.
    Rejecting floats avoids accidentally using Python's different number
    rendering rules while preserving exact JCS semantics for this data model.
    """

    def encode(node: Any) -> str:
        if node is None:
            return "null"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if isinstance(node, int) and not isinstance(node, bool):
            if not -MAX_SAFE_INTEGER <= node <= MAX_SAFE_INTEGER:
                raise ValueError("JCS integer is outside the safe JSON number range")
            return str(node)
        if isinstance(node, float):
            raise ValueError("floating point values are not supported in catalog JCS payloads")
        if isinstance(node, str):
            return _jcs_string(node)
        if isinstance(node, list):
            return "[" + ",".join(encode(item) for item in node) + "]"
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise ValueError("JCS object keys must be strings")
            ordered = sorted(node.items(), key=lambda item: _utf16_sort_key(item[0]))
            return "{" + ",".join(_jcs_string(key) + ":" + encode(item) for key, item in ordered) + "}"
        raise ValueError(f"unsupported JCS value type: {type(node).__name__}")

    return encode(value).encode("utf-8")


def _expect_object(value: Any, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{name} has an unsupported or incomplete schema")
    return value


def _expect_string(value: Any, name: str, *, maximum: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} must be a non-empty bounded string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _expect_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be lowercase hexadecimal SHA-256")
    return value


def _expect_commit(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 40-character commit SHA")
    return value


def _expect_nonnegative_int(value: Any, name: str, *, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def canonicalize_github_repository(value: Any) -> str:
    """Validate and canonicalize the repository identity used by signed entries."""

    source = _expect_string(value, "repository.canonical_url", maximum=512)
    parsed = urlsplit(source)
    if parsed.scheme != "https" or parsed.hostname is None or parsed.hostname.lower() not in _GITHUB_HOSTS:
        raise ValueError("repository.canonical_url must be an HTTPS GitHub URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("repository.canonical_url contains an invalid port") from exc
    if parsed.username or parsed.password or port is not None or parsed.query or parsed.fragment:
        raise ValueError("repository.canonical_url cannot contain credentials, ports, queries, or fragments")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("repository.canonical_url must identify a repository root")
    owner, repository = parts
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner):
        raise ValueError("repository owner is invalid")
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository):
        raise ValueError("repository name is invalid")
    return f"https://github.com/{owner}/{repository}"


def _parse_timestamp(value: Any, name: str) -> datetime:
    text = _expect_string(value, name, maximum=32)
    if not _TIMESTAMP_RE.fullmatch(text):
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _schema_validate_envelope(envelope: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = _expect_object(envelope, "envelope", _ENVELOPE_KEYS)
    if raw["format"] != "aetherion.catalog.envelope.v1":
        raise ValueError("unsupported envelope format")
    if raw["algorithm"] != "Ed25519":
        raise ValueError("unsupported signature algorithm")
    if raw["canonicalization"] != "RFC8785-JCS":
        raise ValueError("unsupported canonicalization")
    key_id = _expect_string(raw["key_id"], "envelope.key_id", maximum=128)
    if not _KEY_ID_RE.fullmatch(key_id):
        raise ValueError("envelope.key_id has an invalid format")
    _expect_sha256(raw["payload_sha256"], "envelope.payload_sha256")
    signature = raw["signature_base64"]
    if not isinstance(signature, str) or not signature or len(signature) > 128:
        raise ValueError("envelope.signature_base64 is malformed")
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("envelope.signature_base64 is not strict base64") from exc
    if len(decoded) != 64:
        raise ValueError("envelope.signature_base64 must decode to 64 bytes")
    payload = _expect_object(raw["payload"], "payload", _PAYLOAD_KEYS)
    return raw, payload


def _schema_validate_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], datetime, datetime]:
    raw = _expect_object(payload, "payload", _PAYLOAD_KEYS)
    if raw["format"] != "aetherion.catalog.payload.v1":
        raise ValueError("unsupported payload format")
    _expect_string(raw["catalog_id"], "payload.catalog_id", maximum=128)
    sequence = _expect_nonnegative_int(raw["sequence"], "payload.sequence")
    if sequence == 0:
        raise ValueError("payload.sequence must be greater than zero")
    issued_at = _parse_timestamp(raw["issued_at"], "payload.issued_at")
    expires_at = _parse_timestamp(raw["expires_at"], "payload.expires_at")
    if expires_at <= issued_at:
        raise ValueError("payload.expires_at must be after issued_at")
    entries = raw["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_CATALOG_ENTRIES:
        raise ValueError("payload.entries must contain 1-256 entries")
    validated_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_subjects: set[tuple[str, str]] = set()
    for index, item in enumerate(entries):
        entry = _expect_object(item, f"payload.entries[{index}]", _ENTRY_KEYS)
        plugin_id = _expect_string(entry["plugin_id"], f"entries[{index}].plugin_id", maximum=MAX_PLUGIN_ID_BYTES)
        if not _PLUGIN_ID_RE.fullmatch(plugin_id) or plugin_id in seen_ids:
            raise ValueError("duplicate or invalid plugin ID")
        seen_ids.add(plugin_id)
        repository = _expect_object(entry["repository"], f"entries[{index}].repository", _REPOSITORY_KEYS)
        canonical_url = canonicalize_github_repository(repository["canonical_url"])
        if repository["canonical_url"] != canonical_url:
            raise ValueError("repository.canonical_url is not in canonical form")
        owner = _expect_string(repository["owner"], f"entries[{index}].repository.owner", maximum=100)
        name = _expect_string(repository["name"], f"entries[{index}].repository.name", maximum=100)
        parsed_parts = canonical_url.rstrip("/").split("/")
        if owner != parsed_parts[-2] or name != parsed_parts[-1]:
            raise ValueError("repository owner/name do not match canonical URL")
        selection = _expect_object(entry["selection"], f"entries[{index}].selection", _SELECTION_KEYS)
        commit_sha = _expect_commit(selection["commit_sha"], f"entries[{index}].selection.commit_sha")
        ref_policy = _expect_string(selection["ref_policy"], f"entries[{index}].selection.ref_policy", maximum=64)
        if ref_policy != "curated_commit_only":
            raise ValueError("unsupported selection ref policy")
        subject_key = (canonical_url, commit_sha)
        if subject_key in seen_subjects:
            raise ValueError("duplicate repository/commit entry")
        seen_subjects.add(subject_key)
        archive = _expect_object(entry["archive"], f"entries[{index}].archive", _ARCHIVE_KEYS)
        if archive["format"] != "github-codeload-zip":
            raise ValueError("unsupported archive format")
        _expect_sha256(archive["sha256"], f"entries[{index}].archive.sha256")
        _expect_nonnegative_int(
            archive["size_bytes"],
            f"entries[{index}].archive.size_bytes",
            maximum=MAX_ENVELOPE_BYTES * 32,
        )
        manifest = _expect_object(entry["manifest"], f"entries[{index}].manifest", _MANIFEST_KEYS)
        manifest_path = _expect_string(manifest["path"], f"entries[{index}].manifest.path", maximum=512)
        components = manifest_path.split("/")
        if "\\" in manifest_path or manifest_path.startswith("/") or any(
            component in {"", ".", ".."} for component in components
        ):
            raise ValueError("manifest.path must be a relative POSIX path")
        _expect_sha256(manifest["sha256"], f"entries[{index}].manifest.sha256")
        if not isinstance(manifest["schema_version"], str) or len(manifest["schema_version"].encode("utf-8")) > 64:
            raise ValueError("manifest.schema_version must be a bounded string")
        if not isinstance(manifest["plugin_api_version"], str) or len(manifest["plugin_api_version"].encode("utf-8")) > 64:
            raise ValueError("manifest.plugin_api_version must be a bounded string")
        policy = _expect_object(entry["policy"], f"entries[{index}].policy", _POLICY_KEYS)
        compatibility = _expect_string(policy["compatibility"], f"entries[{index}].policy.compatibility", maximum=32)
        if compatibility not in {status.value for status in CompatibilityStatus}:
            raise ValueError("unsupported compatibility status")
        if policy["execution_enabled"] is not False:
            raise ValueError("policy.execution_enabled must be false")
        publisher_evidence = _expect_string(policy["publisher_evidence"], f"entries[{index}].policy.publisher_evidence", maximum=32)
        if publisher_evidence != "optional":
            raise ValueError("unsupported publisher evidence policy")
        review_status = _expect_string(policy["review_status"], f"entries[{index}].policy.review_status", maximum=32)
        if review_status != "approved":
            raise ValueError("unsupported review status")
        validated_entries.append(entry)
    if [item["plugin_id"] for item in validated_entries] != sorted(
        (item["plugin_id"] for item in validated_entries), key=_utf16_sort_key
    ):
        raise ValueError("entries must be deterministically ordered by plugin_id")
    raw["sequence"] = sequence
    return raw, validated_entries, issued_at, expires_at


def _result(
    status: CatalogAuthStatus,
    reason: Optional[CatalogReason],
    detail: str,
    *,
    key_id: Optional[str] = None,
    sequence: Optional[int] = None,
    plugin_id: Optional[str] = None,
    payload_digest: Optional[str] = None,
    policy: PolicyStatus = PolicyStatus.UNREVIEWED,
    entry: Optional[Mapping[str, Any]] = None,
) -> CatalogVerificationResult:
    return CatalogVerificationResult(
        catalog_auth_status=status,
        policy_status=policy,
        reason_code=reason,
        detail=detail,
        authority_key_id=key_id,
        sequence=sequence,
        subject_plugin_id=plugin_id,
        canonical_payload_sha256=payload_digest,
        entry=entry,
    )


def verify_catalog(
    envelope: EnvelopeInput,
    trusted_keys: CatalogKeyProvider,
    *,
    now: Optional[datetime] = None,
    highest_accepted_sequence: Optional[int] = None,
    expected_subject: Optional[CatalogSubject] = None,
    clock_skew: timedelta = MAX_CLOCK_SKEW,
) -> CatalogVerificationResult:
    """Verify a signed catalog without mutating application state."""

    try:
        raw_envelope = parse_catalog_json(envelope) if isinstance(envelope, (bytes, str)) else envelope
        if not isinstance(raw_envelope, Mapping):
            raise ValueError("catalog envelope must be a mapping")
        if "signature_base64" not in raw_envelope and _ENVELOPE_KEYS - {"signature_base64"} <= set(raw_envelope):
            return _result(
                CatalogAuthStatus.UNSIGNED,
                CatalogReason.SIGNATURE_MISSING,
                "catalog envelope does not contain a signature",
                key_id=raw_envelope.get("key_id") if isinstance(raw_envelope.get("key_id"), str) else None,
            )
        raw, payload = _schema_validate_envelope(raw_envelope)
        validated_payload, entries, issued_at, expires_at = _schema_validate_payload(payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        return _result(CatalogAuthStatus.INVALID, CatalogReason.SCHEMA_INVALID, str(exc)[:240])

    key_id = raw["key_id"]
    sequence = validated_payload["sequence"]
    try:
        payload_bytes = canonicalize_jcs(payload)
    except ValueError as exc:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.SCHEMA_INVALID,
            str(exc)[:240],
            key_id=key_id,
            sequence=sequence,
        )
    payload_digest = hashlib.sha256(payload_bytes).hexdigest()
    if payload_digest != raw["payload_sha256"]:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.PAYLOAD_DIGEST_MISMATCH,
            "canonical payload digest does not match the envelope",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )

    try:
        key = trusted_keys.get_key(key_id)
    except TrustStoreCorruptError as exc:
        try:
            provider_reason = CatalogReason(exc.reason_code) if exc.reason_code else CatalogReason.TRUST_STORE_CORRUPT
        except ValueError:
            provider_reason = CatalogReason.TRUST_STORE_CORRUPT
        return _result(
            CatalogAuthStatus.INVALID,
            provider_reason,
            str(exc)[:240],
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    except Exception:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.TRUST_STORE_CORRUPT,
            "trusted key provider failed",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if key is None:
        return _result(
            CatalogAuthStatus.UNKNOWN_KEY,
            CatalogReason.KEY_UNKNOWN,
            "catalog key is not present in the explicitly supplied trust store",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if key.key_id != key_id or key.algorithm != "Ed25519" or "catalog" not in key.scopes:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.KEY_SCOPE_MISMATCH,
            "trusted key identity, algorithm, or catalog scope does not match",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if key.revoked:
        return _result(
            CatalogAuthStatus.REVOKED,
            CatalogReason.KEY_REVOKED,
            "catalog signing key is revoked",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if not isinstance(key.public_key, bytes) or len(key.public_key) != 32:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.TRUST_STORE_CORRUPT,
            "trusted Ed25519 public key must be 32 bytes",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    try:
        signature = base64.b64decode(raw["signature_base64"], validate=True)
        Ed25519PublicKey.from_public_bytes(key.public_key).verify(signature, payload_bytes)
    except (InvalidSignature, ValueError, binascii.Error):
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.SIGNATURE_INVALID,
            "catalog signature does not verify with the trusted key",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )

    if highest_accepted_sequence is not None:
        if isinstance(highest_accepted_sequence, bool) or not isinstance(highest_accepted_sequence, int):
            return _result(
                CatalogAuthStatus.INVALID,
                CatalogReason.SCHEMA_INVALID,
                "highest_accepted_sequence must be an integer",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
            )
        if sequence <= highest_accepted_sequence:
            return _result(
                CatalogAuthStatus.REPLAYED,
                CatalogReason.SEQUENCE_ROLLBACK,
                "catalog sequence is not newer than the accepted sequence",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
            )

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.SCHEMA_INVALID,
            "verification time must be timezone-aware",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    current = current.astimezone(timezone.utc)
    if not isinstance(clock_skew, timedelta) or clock_skew < timedelta(0) or clock_skew > MAX_CLOCK_SKEW:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.SCHEMA_INVALID,
            "clock skew must be between zero and five minutes",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    try:
        if key.valid_from:
            valid_from = _parse_timestamp(key.valid_from, "trusted key.valid_from")
            if valid_from > current + clock_skew:
                return _result(
                    CatalogAuthStatus.INVALID,
                    CatalogReason.NOT_YET_VALID,
                    "catalog signing key is not yet valid",
                    key_id=key_id,
                    sequence=sequence,
                    payload_digest=payload_digest,
                )
        if key.valid_until and _parse_timestamp(key.valid_until, "trusted key.valid_until") <= current:
            return _result(
                CatalogAuthStatus.EXPIRED,
                CatalogReason.CATALOG_KEY_EXPIRED,
                "catalog signing key is expired",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
            )
        if key.keyset_expires_at and _parse_timestamp(key.keyset_expires_at, "trusted keyset.expires_at") <= current:
            return _result(
                CatalogAuthStatus.EXPIRED,
                CatalogReason.KEYSET_EXPIRED,
                "delegated catalog keyset is expired",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
            )
    except ValueError as exc:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.TRUST_STORE_CORRUPT,
            str(exc)[:240],
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if issued_at > current + clock_skew:
        return _result(
            CatalogAuthStatus.INVALID,
            CatalogReason.NOT_YET_VALID,
            "catalog issued_at is beyond the permitted future clock skew",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )
    if expires_at <= current:
        return _result(
            CatalogAuthStatus.EXPIRED,
            CatalogReason.EXPIRED,
            "catalog expires_at is in the past",
            key_id=key_id,
            sequence=sequence,
            payload_digest=payload_digest,
        )

    selected_entry: Optional[Mapping[str, Any]] = None
    if expected_subject is not None:
        if expected_subject.execution_enabled is not False:
            return _result(
                CatalogAuthStatus.INVALID,
                CatalogReason.SCHEMA_INVALID,
                "expected subject cannot request execution in Option A",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
                plugin_id=expected_subject.plugin_id,
            )
        try:
            expected_url = canonicalize_github_repository(expected_subject.repository_url)
        except ValueError:
            return _result(
                CatalogAuthStatus.INVALID,
                CatalogReason.SUBJECT_MISMATCH,
                "expected repository subject is invalid",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
                plugin_id=expected_subject.plugin_id,
            )
        for candidate in entries:
            if candidate["plugin_id"] == expected_subject.plugin_id:
                selected_entry = candidate
                break
        if selected_entry is None:
            return _result(
                CatalogAuthStatus.INVALID,
                CatalogReason.ENTRY_MISSING,
                "catalog contains no entry for the requested plugin ID",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
                plugin_id=expected_subject.plugin_id,
            )
        repository = selected_entry["repository"]
        selection = selected_entry["selection"]
        archive = selected_entry["archive"]
        manifest = selected_entry["manifest"]
        policy = selected_entry["policy"]
        actual = (
            repository["canonical_url"],
            selection["commit_sha"],
            archive["sha256"],
            archive["size_bytes"],
            manifest["path"],
            manifest["sha256"],
            manifest["schema_version"],
            manifest["plugin_api_version"],
            policy["compatibility"],
            policy["execution_enabled"],
        )
        expected = (
            expected_url,
            expected_subject.commit_sha,
            expected_subject.archive_sha256,
            expected_subject.archive_size_bytes,
            expected_subject.manifest_path,
            expected_subject.manifest_sha256,
            expected_subject.manifest_schema_version,
            expected_subject.plugin_api_version,
            expected_subject.compatibility,
            expected_subject.execution_enabled,
        )
        if actual != expected:
            reason = CatalogReason.SUBJECT_MISMATCH
            if actual[2] != expected[2] or actual[3] != expected[3]:
                reason = CatalogReason.ARCHIVE_DIGEST_MISMATCH
            elif actual[5] != expected[5]:
                reason = CatalogReason.MANIFEST_DIGEST_MISMATCH
            return _result(
                CatalogAuthStatus.INVALID,
                reason,
                "catalog entry does not bind the requested repository, commit, archive, manifest, or policy",
                key_id=key_id,
                sequence=sequence,
                payload_digest=payload_digest,
                plugin_id=expected_subject.plugin_id,
                entry=selected_entry,
            )

    return _result(
        CatalogAuthStatus.VERIFIED,
        None,
        "catalog payload signature verified against the explicitly supplied trusted authority",
        key_id=key_id,
        sequence=sequence,
        payload_digest=payload_digest,
        plugin_id=expected_subject.plugin_id if expected_subject else None,
        policy=PolicyStatus.CATALOG_APPROVED if expected_subject is not None else PolicyStatus.UNREVIEWED,
        entry=selected_entry,
    )


def trust_fields_for_record(
    record: Mapping[str, Any],
    verification: Optional[CatalogVerificationResult] = None,
) -> dict[str, Any]:
    """Return conservative fields for manager/CLI/TUI integration."""

    state = TrustState.from_record(record)
    fields = state.to_dict()
    if not fields["trust_reason_code"]:
        if state.compatibility_status is CompatibilityStatus.SOURCE_ONLY:
            fields["trust_reason_code"] = CatalogReason.SOURCE_ONLY_EXECUTION_DISABLED.value
        elif state.compatibility_status is CompatibilityStatus.UNKNOWN:
            fields["trust_reason_code"] = CatalogReason.COMPATIBILITY_UNKNOWN.value
    if verification is not None:
        projected_policy = verification.policy_status.value
        if verification.catalog_auth_status is CatalogAuthStatus.REVOKED:
            projected_policy = PolicyStatus.REVOKED.value
        elif verification.catalog_auth_status is CatalogAuthStatus.EXPIRED or verification.reason_code in {
            CatalogReason.KEYSET_EXPIRED,
            CatalogReason.CATALOG_KEY_EXPIRED,
            CatalogReason.REVOCATION_STATUS_STALE,
        }:
            projected_policy = PolicyStatus.STALE.value
        fields.update(
            {
                "catalog_auth_status": verification.catalog_auth_status.value,
                "policy_status": projected_policy,
                "trust_reason_code": verification.reason_code.value if verification.reason_code else None,
                "catalog_key_id": verification.authority_key_id,
                "catalog_sequence": verification.sequence,
            }
        )
    fields["execution_enabled"] = False
    return fields
