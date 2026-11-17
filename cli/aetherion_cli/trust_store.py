"""Explicitly provisioned catalog verification-key and lifecycle support.

The root remains outside the signed catalog and must be provisioned by an
operator or release process. Phase 3 adds a root-signed delegated keyset,
sequence watermarks, atomic activation, audit records, and explicit recovery;
it still has no production key defaults and never accepts catalog-embedded
keys as trust anchors.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .trust_verifier import (
    CatalogKey,
    CatalogKeyProvider,
    TrustStoreCorruptError,
    canonicalize_jcs,
    parse_catalog_json,
)

MAX_TRUST_KEYSET_BYTES = 256 * 1024
MAX_LIFECYCLE_TRANSACTION_BYTES = 8 * 1024 * 1024
LIFECYCLE_STATE_VERSION = 2
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STATE_FIELDS = frozenset(
    {
        "version",
        "root_fingerprint",
        "keyset_fingerprint",
        "keyset_id",
        "keyset_sequence",
        "highest_keyset_sequence",
        "keyset_expires_at",
        "root_generation",
        "key_id_fingerprints",
        "revoked_key_ids",
        "revoked_key_fingerprints",
        "previous_root_fingerprint",
        "last_transition",
    }
)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        return path.is_symlink() or os.path.islink(str(path)) or bool(is_junction(str(path)))
    except OSError:
        return True


@dataclass(frozen=True)
class TrustRootStatus:
    """Observable provisioning state, not an assertion of host-level trust."""

    configured: bool
    source: Optional[str] = None
    fingerprint: Optional[str] = None
    active_key_ids: tuple[str, ...] = ()
    reason_code: Optional[str] = None
    provisioning_boundary: str = "none"
    keyset_id: Optional[str] = None
    keyset_sequence: Optional[int] = None
    highest_keyset_sequence: Optional[int] = None
    root_generation: Optional[int] = None
    keyset_expires_at: Optional[str] = None
    revoked_key_ids: tuple[str, ...] = ()
    last_transition: Optional[str] = None


class UnconfiguredCatalogKeyProvider:
    """No-root provider used when no operator trust material is configured."""

    status = TrustRootStatus(
        configured=False,
        reason_code="catalog_trust_root_unconfigured",
        provisioning_boundary="none",
    )

    def get_key(self, _key_id: str) -> Optional[CatalogKey]:
        return None


class CorruptCatalogKeyProvider:
    """Provider that preserves a configured-keyset failure as fail-closed state."""

    def __init__(self, source: str, error: str) -> None:
        self.status = TrustRootStatus(
            configured=False,
            source=source,
            reason_code="trust_store_corrupt",
            provisioning_boundary="explicit_configured_file",
        )
        self._error = error

    def get_key(self, _key_id: str) -> Optional[CatalogKey]:
        raise TrustStoreCorruptError(self._error)


class ConfiguredCatalogKeyProvider:
    """Load public catalog keys from an explicitly provisioned local file.

    The path is an operator/release provisioning boundary, not a cryptographic
    claim by this application. A same-privilege attacker able to replace both
    the application and this file can replace the trust root; that limitation is
    surfaced in ``TrustRootStatus.provisioning_boundary``.
    """

    def __init__(self, source: Path, keys: Mapping[str, CatalogKey], fingerprint: str) -> None:
        self.source = source
        self._keys = dict(keys)
        self.status = TrustRootStatus(
            configured=True,
            source=str(source),
            fingerprint=fingerprint,
            active_key_ids=tuple(sorted(key_id for key_id, key in self._keys.items() if not key.revoked)),
            provisioning_boundary="explicit_configured_file",
        )

    @classmethod
    def from_file(cls, source: Path) -> "ConfiguredCatalogKeyProvider":
        source = source.expanduser()
        try:
            if (source.parent.exists() and _is_link_or_reparse(source.parent)) or _is_link_or_reparse(source) or not source.is_file():
                raise TrustStoreCorruptError("configured catalog trust root is not a regular file")
            if source.stat().st_size > MAX_TRUST_KEYSET_BYTES:
                raise TrustStoreCorruptError("configured catalog trust root exceeds the 256 KiB limit")
            raw = source.read_bytes()
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"configured catalog trust root is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
        except Exception as exc:
            raise TrustStoreCorruptError(f"configured catalog trust root is unreadable: {exc}") from exc
        return cls.from_bytes(source, raw)

    @classmethod
    def from_bytes(cls, source: Path, raw: bytes) -> "ConfiguredCatalogKeyProvider":
        if len(raw) > MAX_TRUST_KEYSET_BYTES:
            raise TrustStoreCorruptError("configured catalog trust root exceeds the 256 KiB limit")
        try:
            document = parse_catalog_json(raw)
        except Exception as exc:
            if isinstance(exc, TrustStoreCorruptError):
                raise
            raise TrustStoreCorruptError(f"configured catalog trust root is unreadable: {exc}") from exc
        if set(document) != {"format", "keyset_id", "keys"}:
            raise TrustStoreCorruptError("configured catalog trust root has an unsupported schema")
        if document["format"] != "aetherion.catalog.trust-root.v1":
            raise TrustStoreCorruptError("configured catalog trust root has an unsupported format")
        if not isinstance(document["keyset_id"], str) or not document["keyset_id"]:
            raise TrustStoreCorruptError("configured catalog trust root has an invalid keyset ID")
        raw_keys = document["keys"]
        if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > 256:
            raise TrustStoreCorruptError("configured catalog trust root must contain 1-256 keys")
        keys: dict[str, CatalogKey] = {}
        for index, raw_key in enumerate(raw_keys):
            if not isinstance(raw_key, dict) or set(raw_key) != {
                "key_id",
                "algorithm",
                "scopes",
                "public_key_base64",
                "revoked",
            }:
                raise TrustStoreCorruptError(f"configured catalog key {index} has an unsupported schema")
            key_id = raw_key["key_id"]
            if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id) or key_id in keys:
                raise TrustStoreCorruptError(f"configured catalog key {index} has a duplicate or invalid key ID")
            if raw_key["algorithm"] != "Ed25519":
                raise TrustStoreCorruptError(f"configured catalog key {key_id} has an unsupported algorithm")
            scopes = raw_key["scopes"]
            if (
                not isinstance(scopes, list)
                or not scopes
                or any(not isinstance(scope, str) or not scope for scope in scopes)
                or len(set(scopes)) != len(scopes)
            ):
                raise TrustStoreCorruptError(f"configured catalog key {key_id} has invalid scopes")
            encoded = raw_key["public_key_base64"]
            if not isinstance(encoded, str):
                raise TrustStoreCorruptError(f"configured catalog key {key_id} has invalid public key encoding")
            try:
                public_key = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise TrustStoreCorruptError(f"configured catalog key {key_id} has invalid public key encoding") from exc
            if len(public_key) != 32:
                raise TrustStoreCorruptError(f"configured catalog key {key_id} must contain a 32-byte Ed25519 key")
            if not isinstance(raw_key["revoked"], bool):
                raise TrustStoreCorruptError(f"configured catalog key {key_id} has an invalid revocation flag")
            keys[key_id] = CatalogKey(
                key_id=key_id,
                public_key=public_key,
                algorithm="Ed25519",
                scopes=frozenset(scopes),
                revoked=raw_key["revoked"],
            )
        fingerprint = hashlib.sha256(canonicalize_jcs(document)).hexdigest()
        return cls(source, keys, fingerprint)

    def get_key(self, key_id: str) -> Optional[CatalogKey]:
        return self._keys.get(key_id)


def load_catalog_key_provider(
    path: Optional[str],
    keyset_path: Optional[str] = None,
    state_path: Optional[str] = None,
    *,
    clock: Optional[Callable[[], datetime]] = None,
) -> CatalogKeyProvider:
    """Load explicit operator material or return an honest unconfigured provider."""

    if not path or not path.strip():
        return UnconfiguredCatalogKeyProvider()
    source = Path(path).expanduser()
    try:
        if keyset_path and keyset_path.strip():
            lifecycle_state = Path(state_path).expanduser() if state_path and state_path.strip() else source.with_name("catalog-trust-state.json")
            return LifecycleCatalogKeyProvider(source, Path(keyset_path).expanduser(), lifecycle_state, clock=clock)
        return ConfiguredCatalogKeyProvider.from_file(source)
    except Exception as exc:
        return CorruptCatalogKeyProvider(str(source), str(exc))


def trust_root_status(provider: CatalogKeyProvider) -> TrustRootStatus:
    status = getattr(provider, "status", None)
    if isinstance(status, TrustRootStatus):
        return status
    return TrustRootStatus(
        configured=True,
        reason_code=None,
        provisioning_boundary="caller_supplied_provider",
    )


MAX_KEYSET_ENVELOPE_BYTES = 2 * 1024 * 1024
_KEYSET_ENVELOPE_KEYS = frozenset(
    {"format", "key_id", "algorithm", "canonicalization", "payload_sha256", "payload", "signature_base64"}
)
_KEYSET_PAYLOAD_KEYS = frozenset(
    {"format", "keyset_id", "sequence", "parent_keyset_id", "issued_at", "expires_at", "keys"}
)
_KEYSET_KEY_KEYS = frozenset(
    {"key_id", "algorithm", "scopes", "public_key_base64", "revoked", "valid_from", "valid_until", "revocation_reason"}
)


@dataclass(frozen=True)
class DelegatedKeyset:
    keyset_id: str
    sequence: int
    parent_keyset_id: str
    issued_at: str
    expires_at: str
    root_key_id: str
    fingerprint: str
    keys: Mapping[str, CatalogKey]
    revoked_key_ids: tuple[str, ...]
    raw: bytes


def _atomic_bytes_write(path: Path, data: bytes) -> None:
    if _is_link_or_reparse(path):
        raise TrustStoreCorruptError("trust lifecycle file cannot be a symlink or reparse point")
    if path.parent.exists() and _is_link_or_reparse(path.parent):
        raise TrustStoreCorruptError("trust lifecycle directory cannot be a symlink or reparse point")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except (OSError, ValueError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise TrustStoreCorruptError(f"trust lifecycle file publication failed: {exc}", "trust_lifecycle_io_failure") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _utc_now(clock: Optional[Callable[[], datetime]]) -> datetime:
    value = clock() if clock else datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        raise TrustStoreCorruptError(f"{field} must be a UTC timestamp", "catalog_schema_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TrustStoreCorruptError(f"{field} is invalid", "catalog_schema_invalid") from exc
    return parsed.astimezone(timezone.utc)


def _expect_exact_object(value: Any, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise TrustStoreCorruptError(f"{name} has an unsupported schema", "catalog_schema_invalid")
    return value


def _expect_nonempty(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise TrustStoreCorruptError(f"{name} is invalid", "catalog_schema_invalid")
    return value


def _parse_transaction_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_LIFECYCLE_TRANSACTION_BYTES:
        raise TrustStoreCorruptError("trust lifecycle transaction is oversized", "trust_lifecycle_transaction_corrupt")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TrustStoreCorruptError("trust lifecycle transaction contains duplicate fields", "trust_lifecycle_transaction_corrupt")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"unsupported JSON constant {constant}")),
        )
    except TrustStoreCorruptError:
        raise
    except Exception as exc:
        raise TrustStoreCorruptError(f"trust lifecycle transaction is unreadable: {exc}", "trust_lifecycle_transaction_corrupt") from exc
    if not isinstance(value, dict):
        raise TrustStoreCorruptError("trust lifecycle transaction must be an object", "trust_lifecycle_transaction_corrupt")
    return value


def _parse_delegated_keyset(
    raw: bytes,
    root_provider: ConfiguredCatalogKeyProvider,
    *,
    current: Optional[DelegatedKeyset],
    historical_key_ids: Optional[set[str]] = None,
    historical_key_fingerprints: Optional[set[str]] = None,
    key_id_fingerprints: Optional[Mapping[str, str]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> DelegatedKeyset:
    historical_key_ids = historical_key_ids or set()
    historical_key_fingerprints = historical_key_fingerprints or set()
    key_id_fingerprints = key_id_fingerprints or {}
    if len(raw) > MAX_KEYSET_ENVELOPE_BYTES:
        raise TrustStoreCorruptError("delegated keyset exceeds the 2 MiB limit", "trust_store_corrupt")
    try:
        envelope = parse_catalog_json(raw)
    except Exception as exc:
        if isinstance(exc, TrustStoreCorruptError):
            raise
        raise TrustStoreCorruptError(f"delegated keyset is unreadable: {exc}", "trust_store_corrupt") from exc
    envelope = _expect_exact_object(envelope, "delegated keyset envelope", _KEYSET_ENVELOPE_KEYS)
    if envelope["format"] != "aetherion.catalog.keyset.envelope.v1":
        raise TrustStoreCorruptError("delegated keyset envelope format is unsupported", "catalog_schema_invalid")
    if envelope["algorithm"] != "Ed25519" or envelope["canonicalization"] != "RFC8785-JCS":
        raise TrustStoreCorruptError("delegated keyset cryptographic profile is unsupported", "catalog_schema_invalid")
    root_key_id = _expect_nonempty(envelope["key_id"], "delegated keyset root key ID", 128)
    if not re.fullmatch(r"[0-9a-f]{64}", str(envelope["payload_sha256"])):
        raise TrustStoreCorruptError("delegated keyset payload digest is invalid", "catalog_schema_invalid")
    signature = envelope["signature_base64"]
    if not isinstance(signature, str):
        raise TrustStoreCorruptError("delegated keyset signature encoding is invalid", "catalog_schema_invalid")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TrustStoreCorruptError("delegated keyset signature encoding is invalid", "catalog_schema_invalid") from exc
    if len(signature_bytes) != 64:
        raise TrustStoreCorruptError("delegated keyset signature length is invalid", "catalog_schema_invalid")
    payload = _expect_exact_object(envelope["payload"], "delegated keyset payload", _KEYSET_PAYLOAD_KEYS)
    if payload["format"] != "aetherion.catalog.keyset.payload.v1":
        raise TrustStoreCorruptError("delegated keyset payload format is unsupported", "catalog_schema_invalid")
    keyset_id = _expect_nonempty(payload["keyset_id"], "delegated keyset ID", 128)
    parent_keyset_id = payload["parent_keyset_id"]
    if not isinstance(parent_keyset_id, str) or len(parent_keyset_id.encode("utf-8")) > 128:
        raise TrustStoreCorruptError("delegated keyset parent ID is invalid", "catalog_schema_invalid")
    sequence = payload["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise TrustStoreCorruptError("delegated keyset sequence is invalid", "catalog_schema_invalid")
    issued_at = _expect_nonempty(payload["issued_at"], "delegated keyset issued_at", 32)
    expires_at = _expect_nonempty(payload["expires_at"], "delegated keyset expires_at", 32)
    issued = _parse_utc_timestamp(issued_at, "delegated keyset issued_at")
    expires = _parse_utc_timestamp(expires_at, "delegated keyset expires_at")
    now = _utc_now(clock)
    if expires <= issued:
        raise TrustStoreCorruptError("delegated keyset validity interval is invalid", "catalog_schema_invalid")
    if issued > now + timedelta(minutes=5):
        raise TrustStoreCorruptError("delegated keyset is not yet valid", "keyset_not_yet_valid")
    if expires <= now:
        raise TrustStoreCorruptError("delegated keyset is expired", "keyset_expired")
    if current is not None:
        if sequence < current.sequence:
            raise TrustStoreCorruptError("delegated keyset sequence rolled back", "keyset_sequence_rollback")
        if sequence > current.sequence and parent_keyset_id != current.keyset_id:
            raise TrustStoreCorruptError("delegated keyset parent does not match the active keyset", "key_rotation_rejected")

    payload_bytes = canonicalize_jcs(payload)
    if hashlib.sha256(payload_bytes).hexdigest() != envelope["payload_sha256"]:
        raise TrustStoreCorruptError("delegated keyset payload digest mismatch", "keyset_signature_invalid")
    root_key = root_provider.get_key(root_key_id)
    if root_key is None:
        raise TrustStoreCorruptError("delegated keyset root key is unknown", "catalog_key_unknown")
    if root_key.revoked or root_key.algorithm != "Ed25519" or "keyset" not in root_key.scopes:
        raise TrustStoreCorruptError("delegated keyset root key lacks active keyset scope", "catalog_key_scope_mismatch")
    if len(root_key.public_key) != 32:
        raise TrustStoreCorruptError("delegated keyset root key is malformed", "trust_store_corrupt")
    try:
        Ed25519PublicKey.from_public_bytes(root_key.public_key).verify(signature_bytes, payload_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise TrustStoreCorruptError("delegated keyset signature is invalid", "keyset_signature_invalid") from exc

    raw_keys = payload["keys"]
    if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > 256:
        raise TrustStoreCorruptError("delegated keyset must contain 1-256 keys", "catalog_schema_invalid")
    keys: dict[str, CatalogKey] = {}
    revoked_ids: list[str] = []
    for index, raw_key in enumerate(raw_keys):
        item = _expect_exact_object(raw_key, f"delegated key {index}", _KEYSET_KEY_KEYS)
        key_id = item["key_id"]
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id) or key_id in keys:
            raise TrustStoreCorruptError("delegated key ID is duplicate or invalid", "catalog_schema_invalid")
        if item["algorithm"] != "Ed25519":
            raise TrustStoreCorruptError("delegated key algorithm is unsupported", "catalog_schema_invalid")
        scopes = item["scopes"]
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(scope, str) or not scope for scope in scopes)
            or len(set(scopes)) != len(scopes)
            or "catalog" not in scopes
        ):
            raise TrustStoreCorruptError("delegated key scope is invalid", "catalog_key_scope_mismatch")
        try:
            public_key = base64.b64decode(item["public_key_base64"], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise TrustStoreCorruptError("delegated key encoding is invalid", "catalog_schema_invalid") from exc
        if len(public_key) != 32:
            raise TrustStoreCorruptError("delegated key must be 32 bytes", "catalog_schema_invalid")
        if not isinstance(item["revoked"], bool):
            raise TrustStoreCorruptError("delegated key revocation state is invalid", "catalog_schema_invalid")
        valid_from = item["valid_from"]
        valid_until = item["valid_until"]
        if valid_from is not None:
            _parse_utc_timestamp(valid_from, f"delegated key {key_id}.valid_from")
        if valid_until is not None:
            _parse_utc_timestamp(valid_until, f"delegated key {key_id}.valid_until")
        if valid_from and valid_until and _parse_utc_timestamp(valid_until, "valid_until") <= _parse_utc_timestamp(valid_from, "valid_from"):
            raise TrustStoreCorruptError("delegated key validity interval is invalid", "catalog_schema_invalid")
        reason = item["revocation_reason"]
        if not isinstance(reason, str) or len(reason.encode("utf-8")) > 256:
            raise TrustStoreCorruptError("delegated key revocation reason is invalid", "catalog_schema_invalid")
        if item["revoked"]:
            revoked_ids.append(key_id)
        public_key_fingerprint = hashlib.sha256(public_key).hexdigest()
        known_fingerprint = key_id_fingerprints.get(key_id)
        if known_fingerprint is not None and known_fingerprint != public_key_fingerprint:
            raise TrustStoreCorruptError("delegated key ID was rebound to different key material", "key_rotation_rejected")
        if not item["revoked"] and (key_id in historical_key_ids or public_key_fingerprint in historical_key_fingerprints):
            raise TrustStoreCorruptError("delegated key revocation is not monotonic", "key_revocation_rollback")
        keys[key_id] = CatalogKey(
            key_id=key_id,
            public_key=public_key,
            algorithm="Ed25519",
            scopes=frozenset(scopes),
            revoked=item["revoked"],
            valid_from=valid_from,
            valid_until=valid_until,
            keyset_id=keyset_id,
            keyset_sequence=sequence,
            keyset_expires_at=expires_at,
        )
    fingerprint = hashlib.sha256(canonicalize_jcs(envelope)).hexdigest()
    if current is not None and sequence == current.sequence:
        if keyset_id != current.keyset_id or fingerprint != current.fingerprint:
            raise TrustStoreCorruptError("delegated keyset sequence rolled back", "keyset_sequence_rollback")
    return DelegatedKeyset(
        keyset_id=keyset_id,
        sequence=sequence,
        parent_keyset_id=parent_keyset_id,
        issued_at=issued_at,
        expires_at=expires_at,
        root_key_id=root_key_id,
        fingerprint=fingerprint,
        keys=keys,
        revoked_key_ids=tuple(sorted(revoked_ids)),
        raw=raw,
    )


class LifecycleCatalogKeyProvider:
    """Root-authorized delegated catalog-key provider with rollback state."""

    def __init__(
        self,
        root_path: Path,
        keyset_path: Optional[Path],
        state_path: Path,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.root_path = Path(root_path).expanduser()
        self.keyset_path = Path(keyset_path).expanduser() if keyset_path else None
        self.state_path = Path(state_path).expanduser()
        self.history_path = self.state_path.with_name(f"{self.state_path.stem}.history.jsonl")
        self.transaction_path = self.state_path.with_name(f".{self.state_path.stem}.transaction.json")
        self.lock_path = self.state_path.with_name(f".{self.state_path.stem}.lock")
        self.clock = clock
        self._keys: dict[str, CatalogKey] = {}
        self._keyset: Optional[DelegatedKeyset] = None
        self._status = TrustRootStatus(
            configured=False,
            source=str(self.root_path),
            reason_code="catalog_trust_root_unconfigured",
            provisioning_boundary="explicit_configured_file",
        )

    @property
    def status(self) -> TrustRootStatus:
        try:
            self._ensure_loaded()
        except TrustStoreCorruptError as exc:
            self._status = TrustRootStatus(
                configured=False,
                source=str(self.root_path),
                fingerprint=self._status.fingerprint,
                active_key_ids=self._status.active_key_ids,
                reason_code=exc.reason_code or "trust_store_corrupt",
                provisioning_boundary="explicit_configured_file",
                keyset_id=self._status.keyset_id,
                keyset_sequence=self._status.keyset_sequence,
                highest_keyset_sequence=self._status.highest_keyset_sequence,
                root_generation=self._status.root_generation,
                keyset_expires_at=self._status.keyset_expires_at,
                revoked_key_ids=self._status.revoked_key_ids,
                last_transition=self._status.last_transition,
            )
        return self._status

    def _root_provider(self) -> ConfiguredCatalogKeyProvider:
        try:
            provider = ConfiguredCatalogKeyProvider.from_file(self.root_path)
        except TrustStoreCorruptError:
            raise
        if not provider._keys or any("keyset" not in key.scopes for key in provider._keys.values()):
            raise TrustStoreCorruptError("configured root must contain keyset-scoped keys", "catalog_key_scope_mismatch")
        return provider

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "version": LIFECYCLE_STATE_VERSION,
            "root_fingerprint": None,
            "keyset_fingerprint": None,
            "keyset_id": None,
            "keyset_sequence": 0,
            "highest_keyset_sequence": 0,
            "keyset_expires_at": None,
            "root_generation": 0,
            "key_id_fingerprints": {},
            "revoked_key_ids": [],
            "revoked_key_fingerprints": [],
            "previous_root_fingerprint": None,
            "last_transition": None,
        }

    @staticmethod
    def _validate_state(state: Mapping[str, Any]) -> dict[str, Any]:
        if set(state) != set(_STATE_FIELDS) or state.get("version") != LIFECYCLE_STATE_VERSION:
            raise TrustStoreCorruptError(
                "trust lifecycle state has an unsupported schema or requires upgrade",
                "trust_lifecycle_state_upgrade_required",
            )
        for field in ("root_fingerprint", "keyset_fingerprint", "previous_root_fingerprint"):
            value = state[field]
            if value is not None and (not isinstance(value, str) or not _SHA256_RE.fullmatch(value)):
                raise TrustStoreCorruptError(f"trust lifecycle state field '{field}' is invalid", "trust_store_corrupt")
        if state["keyset_expires_at"] is not None:
            _parse_utc_timestamp(state["keyset_expires_at"], "trust lifecycle keyset_expires_at")
        for field in ("keyset_id", "last_transition"):
            value = state[field]
            if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256):
                raise TrustStoreCorruptError(f"trust lifecycle state field '{field}' is invalid", "trust_store_corrupt")
        for field in ("keyset_sequence", "highest_keyset_sequence", "root_generation"):
            value = state[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TrustStoreCorruptError(f"trust lifecycle state field '{field}' is invalid", "trust_store_corrupt")
        if state["keyset_sequence"] > state["highest_keyset_sequence"]:
            raise TrustStoreCorruptError("trust lifecycle active sequence exceeds durable watermark", "trust_store_corrupt")
        fingerprints = state["key_id_fingerprints"]
        if not isinstance(fingerprints, dict) or len(fingerprints) > 256:
            raise TrustStoreCorruptError("trust lifecycle key identity history is invalid", "trust_store_corrupt")
        for key_id, fingerprint in fingerprints.items():
            if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id) or not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
                raise TrustStoreCorruptError("trust lifecycle key identity history is invalid", "trust_store_corrupt")
        for field in ("revoked_key_ids", "revoked_key_fingerprints"):
            values = state[field]
            if not isinstance(values, list) or len(values) > 256 or len(set(values)) != len(values):
                raise TrustStoreCorruptError(f"trust lifecycle field '{field}' is invalid", "trust_store_corrupt")
            if field == "revoked_key_ids":
                if any(not isinstance(value, str) or not _KEY_ID_RE.fullmatch(value) for value in values):
                    raise TrustStoreCorruptError("trust lifecycle revoked key IDs are invalid", "trust_store_corrupt")
            elif any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in values):
                raise TrustStoreCorruptError("trust lifecycle revoked key fingerprints are invalid", "trust_store_corrupt")
        return dict(state)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self._initial_state()
            state["_missing"] = True
            return state
        if _is_link_or_reparse(self.state_path):
            raise TrustStoreCorruptError("trust lifecycle state is unsafe or oversized", "trust_store_corrupt")
        try:
            if self.state_path.stat().st_size > MAX_TRUST_KEYSET_BYTES:
                raise TrustStoreCorruptError("trust lifecycle state is oversized", "trust_store_corrupt")
            state = parse_catalog_json(self.state_path.read_bytes())
        except Exception as exc:
            if isinstance(exc, TrustStoreCorruptError):
                raise
            raise TrustStoreCorruptError(f"trust lifecycle state is unreadable: {exc}", "trust_store_corrupt") from exc
        return self._validate_state(state)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        _atomic_bytes_write(self.state_path, canonicalize_jcs(self._validate_state(state)) + b"\n")

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        if _is_link_or_reparse(self.lock_path):
            raise TrustStoreCorruptError("trust lifecycle lock cannot be a symlink or reparse point", "trust_lifecycle_io_failure")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as handle:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"trust lifecycle lock failed: {exc}", "trust_lifecycle_io_failure") from exc

    def _read_file_snapshot(self, path: Path, *, maximum: int) -> dict[str, Any]:
        if _is_link_or_reparse(path):
            raise TrustStoreCorruptError("trust lifecycle file cannot be a symlink or reparse point", "trust_store_corrupt")
        if not path.exists():
            return {"exists": False, "data_base64": None}
        try:
            if not path.is_file() or path.stat().st_size > maximum:
                raise TrustStoreCorruptError("trust lifecycle file is unsafe or oversized", "trust_store_corrupt")
            data = path.read_bytes()
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"trust lifecycle file is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
        return {"exists": True, "data_base64": base64.b64encode(data).decode("ascii")}

    def _lifecycle_targets(self, *, include_root: bool) -> dict[str, Path]:
        targets = {"keyset": self.keyset_path, "state": self.state_path, "history": self.history_path}
        if include_root:
            targets["root"] = self.root_path
        if targets.get("keyset") is None:
            raise TrustStoreCorruptError("delegated catalog keyset is not configured", "keyset_unconfigured")
        return {name: path for name, path in targets.items() if path is not None}

    def _write_transaction(self, record: Mapping[str, Any]) -> None:
        _atomic_bytes_write(self.transaction_path, canonicalize_jcs(dict(record)) + b"\n")

    def _new_transaction(self, operation: str, targets: Mapping[str, Path]) -> dict[str, Any]:
        files = {
            name: self._read_file_snapshot(path, maximum=MAX_KEYSET_ENVELOPE_BYTES if name == "keyset" else MAX_TRUST_KEYSET_BYTES)
            for name, path in targets.items()
        }
        return {
            "version": 1,
            "operation": operation,
            "phase": "prepared",
            "files": files,
        }

    def _validate_transaction(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if set(record) != {"version", "operation", "phase", "files"} or record.get("version") != 1:
            raise TrustStoreCorruptError("trust lifecycle transaction has an unsupported schema", "trust_lifecycle_transaction_corrupt")
        if not isinstance(record.get("operation"), str) or not record["operation"]:
            raise TrustStoreCorruptError("trust lifecycle transaction operation is invalid", "trust_lifecycle_transaction_corrupt")
        if record.get("phase") not in {"prepared", "keyset_published", "root_published", "state_published", "history_published", "committed"}:
            raise TrustStoreCorruptError("trust lifecycle transaction phase is invalid", "trust_lifecycle_transaction_corrupt")
        files = record.get("files")
        if not isinstance(files, dict) or set(files) not in ({"keyset", "state", "history"}, {"root", "keyset", "state", "history"}):
            raise TrustStoreCorruptError("trust lifecycle transaction file set is invalid", "trust_lifecycle_transaction_corrupt")
        for name, snapshot in files.items():
            if not isinstance(snapshot, dict) or set(snapshot) != {"exists", "data_base64"} or not isinstance(snapshot["exists"], bool):
                raise TrustStoreCorruptError("trust lifecycle transaction snapshot is invalid", "trust_lifecycle_transaction_corrupt")
            encoded = snapshot["data_base64"]
            if snapshot["exists"]:
                if not isinstance(encoded, str):
                    raise TrustStoreCorruptError("trust lifecycle transaction snapshot encoding is invalid", "trust_lifecycle_transaction_corrupt")
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise TrustStoreCorruptError("trust lifecycle transaction snapshot encoding is invalid", "trust_lifecycle_transaction_corrupt") from exc
                maximum = MAX_KEYSET_ENVELOPE_BYTES if name == "keyset" else MAX_TRUST_KEYSET_BYTES
                if len(decoded) > maximum:
                    raise TrustStoreCorruptError("trust lifecycle transaction snapshot is oversized", "trust_lifecycle_transaction_corrupt")
            elif encoded is not None:
                raise TrustStoreCorruptError("trust lifecycle transaction missing-file snapshot is invalid", "trust_lifecycle_transaction_corrupt")
        return dict(record)

    def _recover_transaction_locked(self) -> None:
        if not self.transaction_path.exists():
            return
        if _is_link_or_reparse(self.transaction_path):
            raise TrustStoreCorruptError("trust lifecycle transaction is unsafe", "trust_lifecycle_transaction_corrupt")
        try:
            if self.transaction_path.stat().st_size > MAX_LIFECYCLE_TRANSACTION_BYTES:
                raise TrustStoreCorruptError("trust lifecycle transaction is oversized", "trust_lifecycle_transaction_corrupt")
            record = self._validate_transaction(_parse_transaction_json(self.transaction_path.read_bytes()))
        except TrustStoreCorruptError:
            raise
        except Exception as exc:
            raise TrustStoreCorruptError(f"trust lifecycle transaction is unreadable: {exc}", "trust_lifecycle_transaction_corrupt") from exc
        if record["phase"] == "committed":
            try:
                self.transaction_path.unlink()
            except OSError:
                # The committed generation is authoritative. Retain the marker
                # for a later cleanup attempt rather than reporting a failed
                # transition after all security-relevant files are committed.
                pass
            return
        targets = self._lifecycle_targets(include_root="root" in record["files"])
        try:
            for name, snapshot in record["files"].items():
                path = targets[name]
                if snapshot["exists"]:
                    _atomic_bytes_write(path, base64.b64decode(snapshot["data_base64"], validate=True))
                elif path.exists():
                    if _is_link_or_reparse(path):
                        raise TrustStoreCorruptError("trust lifecycle rollback target is unsafe", "trust_lifecycle_transaction_corrupt")
                    path.unlink()
            self.transaction_path.unlink()
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"trust lifecycle transaction recovery failed: {exc}", "trust_lifecycle_transaction_corrupt") from exc

    def _publish_transaction_locked(self, operation: str, new_files: Mapping[str, bytes], *, include_root: bool) -> None:
        targets = self._lifecycle_targets(include_root=include_root)
        transaction = self._new_transaction(operation, targets)
        if sum(len(str(value)) for value in transaction.values()) > MAX_LIFECYCLE_TRANSACTION_BYTES:
            raise TrustStoreCorruptError("trust lifecycle transaction is oversized", "trust_lifecycle_transaction_corrupt")
        self._write_transaction(transaction)
        phase_by_name = {"keyset": "keyset_published", "root": "root_published", "state": "state_published", "history": "history_published"}
        try:
            for name in ("keyset", "root", "state", "history"):
                if name not in new_files:
                    continue
                _atomic_bytes_write(targets[name], new_files[name])
                transaction["phase"] = phase_by_name[name]
                self._write_transaction(transaction)
            transaction["phase"] = "committed"
            self._write_transaction(transaction)
            try:
                self.transaction_path.unlink()
            except OSError:
                # A committed marker is safe to retain; a later load can retry
                # cleanup without changing the authoritative generation.
                pass
        except Exception as exc:
            try:
                self._recover_transaction_locked()
            except Exception as recovery_exc:
                raise TrustStoreCorruptError(
                    f"trust lifecycle transaction failed and recovery failed: {recovery_exc}",
                    "trust_lifecycle_transaction_corrupt",
                ) from exc
            if isinstance(exc, TrustStoreCorruptError):
                raise
            raise TrustStoreCorruptError(f"trust lifecycle transaction failed: {exc}", "trust_lifecycle_io_failure") from exc

    def _ensure_loaded(self) -> None:
        try:
            with self._lifecycle_lock():
                self._recover_transaction_locked()
                self._ensure_loaded_locked()
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"trust lifecycle I/O failed: {exc}", "trust_lifecycle_io_failure") from exc

    def _ensure_loaded_locked(self) -> None:
        if not self.root_path or not self.root_path.exists():
            self._status = TrustRootStatus(
                configured=False,
                source=str(self.root_path),
                reason_code="catalog_trust_root_unconfigured",
                provisioning_boundary="explicit_configured_file",
            )
            raise TrustStoreCorruptError("configured root is unavailable", "catalog_trust_root_unconfigured")
        if not self.keyset_path:
            self._status = TrustRootStatus(
                configured=False,
                source=str(self.root_path),
                fingerprint=None,
                reason_code="keyset_unconfigured",
                provisioning_boundary="explicit_configured_file",
            )
            raise TrustStoreCorruptError("delegated catalog keyset is not configured", "keyset_unconfigured")
        root_provider = self._root_provider()
        state = self._read_state()
        root_fingerprint = root_provider.status.fingerprint
        state_missing = bool(state.pop("_missing", False))
        self._status = TrustRootStatus(
            configured=False,
            source=str(self.root_path),
            fingerprint=root_fingerprint,
            provisioning_boundary="explicit_configured_file",
            keyset_id=state.get("keyset_id"),
            keyset_sequence=state.get("keyset_sequence") or None,
            highest_keyset_sequence=state.get("highest_keyset_sequence") or None,
            root_generation=state.get("root_generation") or None,
            keyset_expires_at=state.get("keyset_expires_at"),
            revoked_key_ids=tuple(state.get("revoked_key_ids", [])),
            last_transition=state.get("last_transition"),
        )
        if state_missing and (self.keyset_path.exists() or self.history_path.exists()):
            raise TrustStoreCorruptError(
                "trust lifecycle state is missing after prior lifecycle material exists",
                "trust_lifecycle_state_missing",
            )
        prior_root = state.get("root_fingerprint")
        if prior_root and prior_root != root_fingerprint:
            raise TrustStoreCorruptError(
                "configured root fingerprint changed without explicit operator replacement",
                "trust_root_replacement_requires_operator",
            )
        if _is_link_or_reparse(self.keyset_path) or not self.keyset_path.is_file():
            raise TrustStoreCorruptError("delegated catalog keyset is unavailable", "keyset_unconfigured")
        try:
            raw = self.keyset_path.read_bytes()
        except OSError as exc:
            raise TrustStoreCorruptError(f"delegated catalog keyset is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
        current = self._keyset
        if current is None and state.get("keyset_id") and state.get("keyset_fingerprint"):
            current = DelegatedKeyset(
                keyset_id=str(state["keyset_id"]),
                sequence=int(state["keyset_sequence"]),
                parent_keyset_id="",
                issued_at="",
                expires_at="",
                root_key_id="",
                fingerprint=str(state["keyset_fingerprint"]),
                keys={},
                revoked_key_ids=(),
                raw=b"",
            )
        parsed = _parse_delegated_keyset(
            raw,
            root_provider,
            current=current,
            historical_key_ids=set(state.get("revoked_key_ids", [])),
            historical_key_fingerprints=set(state.get("revoked_key_fingerprints", [])),
            key_id_fingerprints=state.get("key_id_fingerprints", {}),
            clock=self.clock,
        )
        same_accepted_state = state.get("keyset_fingerprint") == parsed.fingerprint and state.get("keyset_sequence") == parsed.sequence
        if not same_accepted_state and parsed.sequence <= state.get("highest_keyset_sequence", 0):
            raise TrustStoreCorruptError("delegated keyset sequence rolled back", "keyset_sequence_rollback")
        identity_history = dict(state.get("key_id_fingerprints", {}))
        revoked_ids = set(state.get("revoked_key_ids", []))
        revoked_fingerprints = set(state.get("revoked_key_fingerprints", []))
        for key_id, key in parsed.keys.items():
            fingerprint = hashlib.sha256(key.public_key).hexdigest()
            identity_history[key_id] = fingerprint
            if key.revoked:
                revoked_ids.add(key_id)
                revoked_fingerprints.add(fingerprint)
        new_state = {
            "version": LIFECYCLE_STATE_VERSION,
            "root_fingerprint": root_fingerprint,
            "keyset_fingerprint": parsed.fingerprint,
            "keyset_id": parsed.keyset_id,
            "keyset_sequence": parsed.sequence,
            "highest_keyset_sequence": max(state.get("highest_keyset_sequence", 0), parsed.sequence),
            "keyset_expires_at": parsed.expires_at,
            "root_generation": max(1, state.get("root_generation", 0)),
            "key_id_fingerprints": identity_history,
            "revoked_key_ids": sorted(revoked_ids),
            "revoked_key_fingerprints": sorted(revoked_fingerprints),
            "previous_root_fingerprint": state.get("previous_root_fingerprint"),
            "last_transition": (
                state.get("last_transition")
                if same_accepted_state and state.get("last_transition")
                else ("initial" if not state.get("keyset_id") else "delegated_rotation")
            ),
        }
        if new_state != state:
            raise TrustStoreCorruptError(
                "trust lifecycle state does not match the active verified keyset",
                "trust_store_corrupt",
            )
        self._keyset = parsed
        self._keys = dict(parsed.keys)
        now = _utc_now(self.clock)
        self._status = TrustRootStatus(
            configured=True,
            source=str(self.root_path),
            fingerprint=root_fingerprint,
            active_key_ids=tuple(
                sorted(
                    key_id
                    for key_id, key in self._keys.items()
                    if not key.revoked
                    and (not key.valid_from or _parse_utc_timestamp(key.valid_from, "trusted key.valid_from") <= now)
                    and (not key.valid_until or _parse_utc_timestamp(key.valid_until, "trusted key.valid_until") > now)
                )
            ),
            revoked_key_ids=tuple(sorted(revoked_ids)),
            provisioning_boundary="explicit_configured_file",
            keyset_id=parsed.keyset_id,
            keyset_sequence=parsed.sequence,
            highest_keyset_sequence=new_state["highest_keyset_sequence"],
            root_generation=new_state["root_generation"],
            keyset_expires_at=parsed.expires_at,
            last_transition=new_state["last_transition"],
        )

    def get_key(self, key_id: str) -> Optional[CatalogKey]:
        self._ensure_loaded()
        return self._keys.get(key_id)

    def validate_candidate(self, candidate_path: Path) -> TrustRootStatus:
        with self._lifecycle_lock():
            self._recover_transaction_locked()
            return self._validate_candidate_locked(candidate_path)

    def _validate_candidate_locked(self, candidate_path: Path) -> TrustRootStatus:
        root_provider = self._root_provider()
        state = self._read_state()
        state_missing = bool(state.pop("_missing", False))
        if state_missing and (self.keyset_path and self.keyset_path.exists() or self.history_path.exists()):
            raise TrustStoreCorruptError("trust lifecycle state is missing after prior lifecycle material exists", "trust_lifecycle_state_missing")
        current = None
        if state.get("keyset_id") and state.get("keyset_fingerprint"):
            current = DelegatedKeyset(
                keyset_id=str(state["keyset_id"]),
                sequence=int(state["keyset_sequence"]),
                parent_keyset_id="",
                issued_at="",
                expires_at="",
                root_key_id="",
                fingerprint=str(state.get("keyset_fingerprint") or ""),
                keys={},
                revoked_key_ids=(),
                raw=b"",
            )
        candidate, _raw = self._candidate(
            candidate_path,
            current=current,
            root_provider=root_provider,
            state=state,
        )
        if state.get("keyset_id") and candidate.sequence <= state.get("highest_keyset_sequence", 0):
            raise TrustStoreCorruptError("delegated keyset sequence rolled back", "keyset_sequence_rollback")
        return TrustRootStatus(
            configured=True,
            source=str(candidate_path),
            fingerprint=root_provider.status.fingerprint,
            active_key_ids=tuple(sorted(key_id for key_id, key in candidate.keys.items() if not key.revoked)),
            revoked_key_ids=candidate.revoked_key_ids,
            provisioning_boundary="candidate_not_activated",
            keyset_id=candidate.keyset_id,
            keyset_sequence=candidate.sequence,
            keyset_expires_at=candidate.expires_at,
            last_transition="candidate_validated",
        )

    def _audit_record(self, action: str, keyset: Optional[DelegatedKeyset], *, root_fingerprint: str) -> bytes:
        record = {
            "action": action,
            "at": _utc_now(self.clock).isoformat().replace("+00:00", "Z"),
            "root_fingerprint": root_fingerprint,
            "keyset_id": keyset.keyset_id if keyset else None,
            "keyset_sequence": keyset.sequence if keyset else None,
            "keyset_fingerprint": keyset.fingerprint if keyset else None,
            "revoked_key_ids": list(keyset.revoked_key_ids) if keyset else [],
        }
        if _is_link_or_reparse(self.history_path):
            raise TrustStoreCorruptError("trust lifecycle audit history cannot be a symlink or reparse point", "trust_store_corrupt")
        try:
            if self.history_path.exists() and self.history_path.stat().st_size > MAX_TRUST_KEYSET_BYTES:
                raise TrustStoreCorruptError("trust lifecycle audit history is oversized", "trust_store_corrupt")
            previous = self.history_path.read_bytes() if self.history_path.exists() else b""
        except TrustStoreCorruptError:
            raise
        except OSError as exc:
            raise TrustStoreCorruptError(f"trust lifecycle audit history is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
        for line in previous.splitlines():
            if not line.strip():
                continue
            value = _parse_transaction_json(line)
            allowed = {
                "action", "at", "root_fingerprint", "keyset_id", "keyset_sequence", "keyset_fingerprint", "revoked_key_ids"
            }
            legacy_allowed = allowed - {"revoked_key_ids"}
            if set(value) not in (allowed, legacy_allowed):
                raise TrustStoreCorruptError("trust lifecycle audit history record is malformed", "trust_lifecycle_transaction_corrupt")
            if not isinstance(value.get("action"), str) or not isinstance(value.get("at"), str):
                raise TrustStoreCorruptError("trust lifecycle audit history record is malformed", "trust_lifecycle_transaction_corrupt")
        result = previous + canonicalize_jcs(record) + b"\n"
        if len(result) > MAX_TRUST_KEYSET_BYTES:
            raise TrustStoreCorruptError("trust lifecycle audit history is oversized", "trust_store_corrupt")
        return result

    def _build_state(
        self,
        previous: Mapping[str, Any],
        keyset: DelegatedKeyset,
        *,
        root_fingerprint: str,
        action: str,
        root_replacement: bool = False,
    ) -> dict[str, Any]:
        previous_missing = bool(previous.get("_missing", False))
        identity_history = dict(previous.get("key_id_fingerprints", {}))
        revoked_ids = set(previous.get("revoked_key_ids", []))
        revoked_fingerprints = set(previous.get("revoked_key_fingerprints", []))
        for key_id, key in keyset.keys.items():
            fingerprint = hashlib.sha256(key.public_key).hexdigest()
            identity_history[key_id] = fingerprint
            if key.revoked:
                revoked_ids.add(key_id)
                revoked_fingerprints.add(fingerprint)
        return {
            "version": LIFECYCLE_STATE_VERSION,
            "root_fingerprint": root_fingerprint,
            "keyset_fingerprint": keyset.fingerprint,
            "keyset_id": keyset.keyset_id,
            "keyset_sequence": keyset.sequence,
            "highest_keyset_sequence": keyset.sequence if root_replacement else max(previous.get("highest_keyset_sequence", 0), keyset.sequence),
            "keyset_expires_at": keyset.expires_at,
            "root_generation": (previous.get("root_generation", 0) + 1) if root_replacement else max(1, previous.get("root_generation", 0)),
            "key_id_fingerprints": identity_history,
            "revoked_key_ids": sorted(revoked_ids),
            "revoked_key_fingerprints": sorted(revoked_fingerprints),
            "previous_root_fingerprint": previous.get("root_fingerprint") if root_replacement else previous.get("previous_root_fingerprint"),
            "last_transition": "initial" if previous_missing and not root_replacement else action,
        }

    def _candidate(
        self,
        candidate_path: Path,
        *,
        current: Optional[DelegatedKeyset],
        root_provider: Optional[ConfiguredCatalogKeyProvider] = None,
        state: Optional[Mapping[str, Any]] = None,
    ) -> tuple[DelegatedKeyset, bytes]:
        candidate_path = Path(candidate_path).expanduser()
        if (candidate_path.parent.exists() and _is_link_or_reparse(candidate_path.parent)) or _is_link_or_reparse(candidate_path) or not candidate_path.is_file():
            raise TrustStoreCorruptError("candidate keyset is not a regular file", "keyset_unconfigured")
        try:
            raw = candidate_path.read_bytes()
        except OSError as exc:
            raise TrustStoreCorruptError(f"candidate keyset is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
        candidate = _parse_delegated_keyset(
            raw,
            root_provider or self._root_provider(),
            current=current,
            historical_key_ids=set((state or {}).get("revoked_key_ids", [])),
            historical_key_fingerprints=set((state or {}).get("revoked_key_fingerprints", [])),
            key_id_fingerprints=(state or {}).get("key_id_fingerprints", {}),
            clock=self.clock,
        )
        return candidate, raw

    def _activate_locked(
        self,
        keyset: DelegatedKeyset,
        raw: bytes,
        *,
        action: str,
        root_fingerprint: str,
        previous_state: Mapping[str, Any],
        root_raw: Optional[bytes] = None,
    ) -> TrustRootStatus:
        new_state = self._build_state(
            previous_state,
            keyset,
            root_fingerprint=root_fingerprint,
            action=action,
            root_replacement=root_raw is not None,
        )
        history = self._audit_record(action, keyset, root_fingerprint=root_fingerprint)
        new_files: dict[str, bytes] = {
            "keyset": raw,
            "state": canonicalize_jcs(self._validate_state(new_state)) + b"\n",
            "history": history,
        }
        if root_raw is not None:
            new_files["root"] = root_raw
        self._publish_transaction_locked(action, new_files, include_root=root_raw is not None)
        self._keyset = None
        self._keys = {}
        self._ensure_loaded_locked()
        return self._status

    def _verify_root_transition(
        self,
        raw: bytes,
        current_root: ConfiguredCatalogKeyProvider,
        *,
        expected_old_fingerprint: str,
        expected_new_fingerprint: str,
        expected_keyset_fingerprint: str,
        expected_generation: int,
    ) -> None:
        try:
            envelope = parse_catalog_json(raw)
        except Exception as exc:
            if isinstance(exc, TrustStoreCorruptError):
                raise
            raise TrustStoreCorruptError(f"root transition authorization is unreadable: {exc}", "root_transition_signature_invalid") from exc
        envelope = _expect_exact_object(
            envelope,
            "root transition envelope",
            frozenset({"format", "key_id", "algorithm", "canonicalization", "payload_sha256", "payload", "signature_base64"}),
        )
        if envelope["format"] != "aetherion.catalog.root-transition.envelope.v1" or envelope["algorithm"] != "Ed25519" or envelope["canonicalization"] != "RFC8785-JCS":
            raise TrustStoreCorruptError("root transition authorization format is unsupported", "root_transition_signature_invalid")
        payload = _expect_exact_object(
            envelope["payload"],
            "root transition payload",
            frozenset({"format", "old_root_fingerprint", "new_root_fingerprint", "new_keyset_fingerprint", "root_generation", "issued_at", "expires_at"}),
        )
        if payload["format"] != "aetherion.catalog.root-transition.payload.v1":
            raise TrustStoreCorruptError("root transition authorization format is unsupported", "root_transition_signature_invalid")
        for field, expected in (
            ("old_root_fingerprint", expected_old_fingerprint),
            ("new_root_fingerprint", expected_new_fingerprint),
            ("new_keyset_fingerprint", expected_keyset_fingerprint),
        ):
            if payload[field] != expected:
                raise TrustStoreCorruptError(f"root transition {field} does not match candidate", "root_fingerprint_mismatch")
        if payload["root_generation"] != expected_generation:
            raise TrustStoreCorruptError("root transition generation does not match lifecycle state", "root_transition_unauthorized")
        issued = _parse_utc_timestamp(payload["issued_at"], "root transition issued_at")
        expires = _parse_utc_timestamp(payload["expires_at"], "root transition expires_at")
        now = _utc_now(self.clock)
        if expires <= issued or issued > now + timedelta(minutes=5) or expires <= now:
            raise TrustStoreCorruptError("root transition authorization is outside its validity interval", "root_transition_unauthorized")
        if not isinstance(envelope["payload_sha256"], str) or not _SHA256_RE.fullmatch(envelope["payload_sha256"]):
            raise TrustStoreCorruptError("root transition payload digest is invalid", "root_transition_signature_invalid")
        payload_bytes = canonicalize_jcs(payload)
        if hashlib.sha256(payload_bytes).hexdigest() != envelope["payload_sha256"]:
            raise TrustStoreCorruptError("root transition payload digest mismatch", "root_transition_signature_invalid")
        root_key_id = envelope["key_id"]
        root_key = current_root.get_key(root_key_id)
        if root_key is None or root_key.revoked or "root-replacement" not in root_key.scopes:
            raise TrustStoreCorruptError("active root lacks root-replacement authorization scope", "root_transition_unauthorized")
        try:
            signature = base64.b64decode(envelope["signature_base64"], validate=True)
            if len(signature) != 64:
                raise ValueError("invalid root transition signature length")
            Ed25519PublicKey.from_public_bytes(root_key.public_key).verify(signature, payload_bytes)
        except (InvalidSignature, ValueError, binascii.Error) as exc:
            raise TrustStoreCorruptError("root transition signature is invalid", "root_transition_signature_invalid") from exc

    def apply_candidate(
        self,
        candidate_path: Path,
        *,
        expected_root_fingerprint: str,
        confirmed: bool = False,
    ) -> TrustRootStatus:
        """Activate a root-authorized rotation only after operator confirmation."""
        with self._lifecycle_lock():
            self._recover_transaction_locked()
            if not confirmed:
                raise TrustStoreCorruptError(
                    "delegated keyset activation requires explicit operator confirmation",
                    "key_recovery_requires_operator_material",
                )
            root_provider = self._root_provider()
            if root_provider.status.fingerprint != expected_root_fingerprint:
                raise TrustStoreCorruptError(
                    "operator confirmation does not match the configured root",
                    "key_recovery_requires_operator_material",
                )
            state = self._read_state()
            state_missing = bool(state.pop("_missing", False))
            if state_missing and (self.keyset_path and self.keyset_path.exists() or self.history_path.exists()):
                raise TrustStoreCorruptError("trust lifecycle state is missing after prior lifecycle material exists", "trust_lifecycle_state_missing")
            current = None
            if state.get("keyset_id") and state.get("keyset_fingerprint"):
                current = DelegatedKeyset(
                    keyset_id=str(state["keyset_id"]),
                    sequence=int(state["keyset_sequence"]),
                    parent_keyset_id="",
                    issued_at="",
                    expires_at="",
                    root_key_id="",
                    fingerprint=str(state["keyset_fingerprint"]),
                    keys={},
                    revoked_key_ids=(),
                    raw=b"",
                )
            candidate, raw = self._candidate(candidate_path, current=current, root_provider=root_provider, state=state)
            if state.get("keyset_id") and candidate.sequence <= state.get("highest_keyset_sequence", 0):
                raise TrustStoreCorruptError("delegated keyset sequence rolled back", "keyset_sequence_rollback")
            action = "delegated_rotation" if state.get("keyset_id") else "initial"
            return self._activate_locked(
                candidate,
                raw,
                action=action,
                root_fingerprint=expected_root_fingerprint,
                previous_state=state,
            )

    def recover_candidate(
        self,
        candidate_path: Path,
        *,
        expected_root_fingerprint: str,
        confirmed: bool = False,
    ) -> TrustRootStatus:
        """Recover a root-authorized backup; rollback requires explicit confirmation."""
        with self._lifecycle_lock():
            self._recover_transaction_locked()
            if not confirmed:
                raise TrustStoreCorruptError(
                    "keyset recovery requires explicit operator confirmation",
                    "key_recovery_requires_operator_material",
                )
            root_provider = self._root_provider()
            if root_provider.status.fingerprint != expected_root_fingerprint:
                raise TrustStoreCorruptError(
                    "operator recovery does not match the configured root",
                    "key_recovery_requires_operator_material",
                )
            state = self._read_state()
            if state.pop("_missing", False) or not state.get("keyset_id"):
                raise TrustStoreCorruptError("keyset recovery requires an accepted lifecycle state", "trust_lifecycle_state_missing")
            candidate, raw = self._candidate(candidate_path, current=None, root_provider=root_provider, state=state)
            if candidate.sequence == state.get("highest_keyset_sequence") and candidate.fingerprint != state.get("keyset_fingerprint"):
                raise TrustStoreCorruptError("same-sequence recovery candidate is not the accepted keyset", "keyset_sequence_rollback")
            action = "operator_recovery_rollback" if candidate.sequence < state.get("highest_keyset_sequence", 0) else "operator_recovery"
            return self._activate_locked(
                candidate,
                raw,
                action=action,
                root_fingerprint=expected_root_fingerprint,
                previous_state=state,
            )

    def replace_root(
        self,
        root_candidate_path: Path,
        keyset_candidate_path: Path,
        *,
        expected_old_fingerprint: str,
        expected_new_fingerprint: str,
        transition_path: Path,
        confirmed: bool = False,
    ) -> TrustRootStatus:
        """Replace the configured root only with an old-root-authorized pair."""
        with self._lifecycle_lock():
            self._recover_transaction_locked()
            if not confirmed:
                raise TrustStoreCorruptError(
                    "trust-root replacement requires explicit operator confirmation",
                    "trust_root_replacement_requires_operator",
                )
            current_root = self._root_provider()
            if current_root.status.fingerprint != expected_old_fingerprint:
                raise TrustStoreCorruptError(
                    "operator root replacement does not match the active root",
                    "trust_root_replacement_requires_operator",
                )
            state = self._read_state()
            if state.pop("_missing", False) or not state.get("keyset_id") or not state.get("root_generation"):
                raise TrustStoreCorruptError("root replacement requires an accepted lifecycle state", "trust_lifecycle_state_missing")
            candidate_root_path = Path(root_candidate_path).expanduser()
            candidate_keyset_path = Path(keyset_candidate_path).expanduser()
            transition_candidate_path = Path(transition_path).expanduser()
            for path in (candidate_root_path, candidate_keyset_path, transition_candidate_path):
                if _is_link_or_reparse(path) or not path.is_file():
                    raise TrustStoreCorruptError("root replacement candidate is not a regular file", "trust_root_replacement_requires_operator")
            try:
                candidate_root_raw = candidate_root_path.read_bytes()
                candidate_keyset_raw = candidate_keyset_path.read_bytes()
                transition_raw = transition_candidate_path.read_bytes()
            except OSError as exc:
                raise TrustStoreCorruptError(f"root replacement candidate is unreadable: {exc}", "trust_lifecycle_io_failure") from exc
            candidate_root = ConfiguredCatalogKeyProvider.from_bytes(candidate_root_path, candidate_root_raw)
            if candidate_root.status.fingerprint != expected_new_fingerprint:
                raise TrustStoreCorruptError("candidate root fingerprint does not match independent expectation", "root_fingerprint_mismatch")
            if not candidate_root._keys or any("keyset" not in key.scopes for key in candidate_root._keys.values()):
                raise TrustStoreCorruptError("replacement root must contain keyset-scoped keys", "catalog_key_scope_mismatch")
            candidate_keyset = _parse_delegated_keyset(
                candidate_keyset_raw,
                candidate_root,
                current=None,
                historical_key_ids=set(state.get("revoked_key_ids", [])),
                historical_key_fingerprints=set(state.get("revoked_key_fingerprints", [])),
                key_id_fingerprints=state.get("key_id_fingerprints", {}),
                clock=self.clock,
            )
            self._verify_root_transition(
                transition_raw,
                current_root,
                expected_old_fingerprint=expected_old_fingerprint,
                expected_new_fingerprint=expected_new_fingerprint,
                expected_keyset_fingerprint=candidate_keyset.fingerprint,
                expected_generation=int(state["root_generation"]) + 1,
            )
            return self._activate_locked(
                candidate_keyset,
                candidate_keyset_raw,
                action="root_replacement",
                root_fingerprint=expected_new_fingerprint,
                previous_state=state,
                root_raw=candidate_root_raw,
            )
