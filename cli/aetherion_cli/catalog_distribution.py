"""Bounded signed-catalog transport, cache, and last-known-good service.

The service keeps catalog transport separate from the Phase 1 verifier and from
the user-saved repository alias catalog. Network bytes are untrusted until
``verify_catalog`` succeeds. Cache status fields are advisory; reopening the
cache always re-verifies the stored signed envelope with the configured key
provider.
"""
from __future__ import annotations

import base64
import binascii
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from .trust_store import TrustRootStatus, trust_root_status
from .trust_verifier import (
    MAX_ENVELOPE_BYTES,
    CatalogAuthStatus,
    CatalogKeyProvider,
    CatalogReason,
    CatalogSubject,
    CatalogVerificationResult,
    ExecutionStatus,
    PolicyStatus,
    canonicalize_jcs,
    parse_catalog_json,
    verify_catalog,
)

CATALOG_CACHE_VERSION = 1
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_CATALOG_IDS = 64
MAX_REDIRECTS = 0


class CatalogTransportError(ValueError):
    def __init__(self, reason: CatalogReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class CatalogCacheError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogFetch:
    raw: bytes
    endpoint: str
    content_type: str


@dataclass(frozen=True)
class CachedCatalogEnvelope:
    raw: bytes
    catalog_id: str
    sequence: int
    highest_accepted_sequence: int
    issued_at: str
    expires_at: str
    key_id: str
    payload_sha256: str
    verified_at: str
    endpoint: str
    trust_root_fingerprint: Optional[str]


@dataclass(frozen=True)
class CatalogSnapshot:
    raw: Optional[bytes]
    result: CatalogVerificationResult
    source: str
    freshness: str
    catalog_id: Optional[str] = None
    issued_at: Optional[str] = None
    expires_at: Optional[str] = None
    endpoint: Optional[str] = None
    trust_root_fingerprint: Optional[str] = None
    trust_keyset_id: Optional[str] = None
    trust_keyset_sequence: Optional[int] = None
    trust_keyset_expires_at: Optional[str] = None
    trust_keyset_active_key_ids: tuple[str, ...] = ()
    trust_keyset_revoked_key_ids: tuple[str, ...] = ()
    trust_lifecycle_reason: Optional[str] = None
    trust_last_transition: Optional[str] = None
    refresh_failure_reason: Optional[str] = None

    @property
    def usable_for_curated_policy(self) -> bool:
        return (
            self.raw is not None
            and self.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
            and self.freshness in {"fresh", "cached"}
        )


def validate_catalog_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise CatalogTransportError(CatalogReason.ENDPOINT_UNCONFIGURED, "signed catalog endpoint is not configured")
    endpoint = endpoint.strip()
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogTransportError(CatalogReason.SCHEMA_INVALID, "signed catalog endpoint has an invalid port") from exc
    if parsed.scheme != "https" or parsed.hostname is None or parsed.username or parsed.password or port is not None:
        raise CatalogTransportError(
            CatalogReason.SCHEMA_INVALID,
            "signed catalog endpoint must be HTTPS without credentials or a port",
        )
    if parsed.query or parsed.fragment:
        raise CatalogTransportError(
            CatalogReason.SCHEMA_INVALID,
            "signed catalog endpoint cannot contain a query or fragment",
        )
    return endpoint


class CatalogTransport:
    """Fetch a configured HTTPS catalog with no redirect following."""

    def __init__(
        self,
        endpoint: str,
        fetcher: Optional[Callable[[str], CatalogFetch | bytes]] = None,
        timeout: float = 30.0,
        client_factory: Optional[Callable[..., httpx.Client]] = None,
    ) -> None:
        self.endpoint = validate_catalog_endpoint(endpoint)
        self._fetcher = fetcher
        self.timeout = timeout
        self._client_factory = client_factory or httpx.Client

    def fetch(self) -> CatalogFetch:
        if self._fetcher is not None:
            value = self._fetcher(self.endpoint)
            if isinstance(value, CatalogFetch):
                if len(value.raw) > MAX_ENVELOPE_BYTES:
                    raise CatalogTransportError(CatalogReason.RESPONSE_OVERSIZED, "signed catalog response exceeded the 2 MiB limit")
                content_type = value.content_type.lower().split(";", 1)[0].strip()
                if content_type != "application/json":
                    raise CatalogTransportError(
                        CatalogReason.CONTENT_TYPE_INVALID,
                        "signed catalog response must use application/json",
                    )
                return value
            if not isinstance(value, bytes) or len(value) > MAX_ENVELOPE_BYTES:
                raise CatalogTransportError(CatalogReason.RESPONSE_OVERSIZED, "signed catalog response exceeded the 2 MiB limit")
            return CatalogFetch(value, self.endpoint, "application/json")
        try:
            with self._client_factory(timeout=self.timeout, follow_redirects=False) as client:
                with client.stream(
                    "GET",
                    self.endpoint,
                    headers={"Accept": "application/json", "User-Agent": "Aetherion-CLI-signed-catalog/1.0"},
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise CatalogTransportError(
                            CatalogReason.REDIRECT_REJECTED,
                            "signed catalog redirects are rejected by policy",
                        )
                    if response.status_code != 200:
                        raise CatalogTransportError(
                            CatalogReason.TRANSPORT_FAILURE,
                            f"signed catalog endpoint returned HTTP {response.status_code}",
                        )
                    content_type = response.headers.get("content-type", "").lower().split(";", 1)[0].strip()
                    if content_type != "application/json":
                        raise CatalogTransportError(
                            CatalogReason.CONTENT_TYPE_INVALID,
                            "signed catalog response must use application/json",
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared = int(content_length)
                        except ValueError as exc:
                            raise CatalogTransportError(
                                CatalogReason.TRANSPORT_FAILURE,
                                "signed catalog response has an invalid Content-Length",
                            ) from exc
                        if declared > MAX_ENVELOPE_BYTES:
                            raise CatalogTransportError(
                                CatalogReason.RESPONSE_OVERSIZED,
                                "signed catalog response exceeded the 2 MiB limit",
                            )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > MAX_ENVELOPE_BYTES:
                            raise CatalogTransportError(
                                CatalogReason.RESPONSE_OVERSIZED,
                                "signed catalog response exceeded the 2 MiB limit",
                            )
                        chunks.append(chunk)
                    return CatalogFetch(b"".join(chunks), self.endpoint, content_type)
        except CatalogTransportError:
            raise
        except Exception as exc:
            raise CatalogTransportError(CatalogReason.TRANSPORT_FAILURE, f"signed catalog fetch failed: {exc}") from exc


def _is_link_or_reparse(path: Path) -> bool:
    try:
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        return path.is_symlink() or os.path.islink(str(path)) or bool(is_junction(str(path)))
    except OSError:
        return True


def _atomic_json_write(path: Path, data: Mapping[str, Any]) -> None:
    if _is_link_or_reparse(path):
        raise CatalogCacheError("signed catalog cache cannot be a symlink or reparse point")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(canonicalize_jcs(dict(data)) + b"\n")
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    if _is_link_or_reparse(path):
        raise CatalogCacheError("signed catalog cache lock cannot be a symlink or reparse point")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
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


class SignedCatalogCache:
    """Atomic last-known-good signed-envelope cache and sequence watermark."""

    def __init__(self, root: Path) -> None:
        requested_root = Path(root).expanduser()
        if _is_link_or_reparse(requested_root) or (requested_root.parent.exists() and _is_link_or_reparse(requested_root.parent)):
            raise CatalogCacheError("signed catalog cache root cannot be a symlink or reparse point")
        self.root = requested_root.resolve()
        self.path = self.root / "signed_catalog_cache.json"
        self.lock_path = self.root / ".signed_catalog_cache.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(self.root) or _is_link_or_reparse(self.path):
            raise CatalogCacheError("signed catalog cache root or file cannot be a symlink or reparse point")

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": CATALOG_CACHE_VERSION, "highest_sequences": {}, "last_good": None}
        if _is_link_or_reparse(self.path) or self.path.stat().st_size > MAX_CACHE_BYTES:
            raise CatalogCacheError("signed catalog cache is oversized or unsafe")
        try:
            value = parse_catalog_json(self.path.read_bytes())
        except Exception as exc:
            raise CatalogCacheError(f"signed catalog cache is unreadable: {exc}") from exc
        if set(value) != {"version", "highest_sequences", "last_good"}:
            raise CatalogCacheError("signed catalog cache has an unsupported schema")
        if value["version"] != CATALOG_CACHE_VERSION:
            raise CatalogCacheError("signed catalog cache has an unsupported version")
        highest = value["highest_sequences"]
        if not isinstance(highest, dict) or len(highest) > MAX_CATALOG_IDS:
            raise CatalogCacheError("signed catalog cache has an invalid sequence watermark")
        for catalog_id, sequence in highest.items():
            if not isinstance(catalog_id, str) or not catalog_id or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise CatalogCacheError("signed catalog cache has an invalid sequence watermark")
        last_good = value["last_good"]
        if last_good is not None and not isinstance(last_good, dict):
            raise CatalogCacheError("signed catalog cache has an invalid last-known-good record")
        return dict(value)

    @staticmethod
    def _record_from_document(document: Mapping[str, Any]) -> Optional[CachedCatalogEnvelope]:
        raw_record = document.get("last_good")
        if raw_record is None:
            return None
        required = {
            "envelope_base64",
            "catalog_id",
            "sequence",
            "issued_at",
            "expires_at",
            "key_id",
            "payload_sha256",
            "verified_at",
            "endpoint",
            "trust_root_fingerprint",
        }
        if not isinstance(raw_record, dict) or set(raw_record) != required:
            raise CatalogCacheError("signed catalog cache last-known-good record is malformed")
        encoded = raw_record["envelope_base64"]
        try:
            raw = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (ValueError, binascii.Error) as exc:
            raise CatalogCacheError("signed catalog cache envelope encoding is invalid") from exc
        if not raw or len(raw) > MAX_ENVELOPE_BYTES:
            raise CatalogCacheError("signed catalog cache envelope is missing or oversized")
        if not isinstance(raw_record["catalog_id"], str) or not raw_record["catalog_id"]:
            raise CatalogCacheError("signed catalog cache catalog ID is invalid")
        for field in ("issued_at", "expires_at", "key_id", "payload_sha256", "verified_at", "endpoint"):
            if not isinstance(raw_record[field], str) or not raw_record[field]:
                raise CatalogCacheError(f"signed catalog cache field '{field}' is invalid")
        if raw_record["trust_root_fingerprint"] is not None and not isinstance(
            raw_record["trust_root_fingerprint"], str
        ):
            raise CatalogCacheError("signed catalog cache trust-root fingerprint is invalid")
        sequence = raw_record["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CatalogCacheError("signed catalog cache sequence is invalid")
        watermark = document["highest_sequences"].get(raw_record["catalog_id"])
        if watermark != sequence:
            raise CatalogCacheError("signed catalog cache sequence watermark does not match last-known-good record")
        return CachedCatalogEnvelope(
            raw=raw,
            catalog_id=raw_record["catalog_id"],
            sequence=sequence,
            highest_accepted_sequence=watermark,
            issued_at=str(raw_record["issued_at"]),
            expires_at=str(raw_record["expires_at"]),
            key_id=str(raw_record["key_id"]),
            payload_sha256=str(raw_record["payload_sha256"]),
            verified_at=str(raw_record["verified_at"]),
            endpoint=str(raw_record["endpoint"]),
            trust_root_fingerprint=(
                str(raw_record["trust_root_fingerprint"])
                if raw_record["trust_root_fingerprint"] is not None
                else None
            ),
        )

    def read(self) -> Optional[CachedCatalogEnvelope]:
        with _cache_lock(self.lock_path):
            return self._record_from_document(self._read_document())

    def highest_sequence(self, catalog_id: str) -> Optional[int]:
        with _cache_lock(self.lock_path):
            document = self._read_document()
            value = document["highest_sequences"].get(catalog_id)
            return int(value) if value is not None else None

    def write_verified(
        self,
        raw: bytes,
        *,
        catalog_id: str,
        sequence: int,
        issued_at: str,
        expires_at: str,
        key_id: str,
        payload_sha256: str,
        endpoint: str,
        trust_root_fingerprint: Optional[str],
        verified_at: Optional[str] = None,
    ) -> CachedCatalogEnvelope:
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ENVELOPE_BYTES:
            raise CatalogCacheError("verified signed catalog envelope is missing or oversized")
        if not catalog_id or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CatalogCacheError("verified signed catalog cache metadata is invalid")
        with _cache_lock(self.lock_path):
            document = self._read_document()
            highest = dict(document["highest_sequences"])
            previous = highest.get(catalog_id, 0)
            if sequence <= previous:
                raise CatalogCacheError("signed catalog sequence is not newer than the accepted watermark")
            highest[catalog_id] = sequence
            record = {
                "envelope_base64": base64.b64encode(raw).decode("ascii"),
                "catalog_id": catalog_id,
                "sequence": sequence,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "key_id": key_id,
                "payload_sha256": payload_sha256,
                "verified_at": verified_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "endpoint": endpoint,
                "trust_root_fingerprint": trust_root_fingerprint,
            }
            new_document = {
                "version": CATALOG_CACHE_VERSION,
                "highest_sequences": highest,
                "last_good": record,
            }
            _atomic_json_write(self.path, new_document)
            return self._record_from_document(new_document)  # type: ignore[return-value]


def _candidate_catalog_id(raw: bytes) -> Optional[str]:
    try:
        envelope = parse_catalog_json(raw)
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        catalog_id = payload.get("catalog_id") if isinstance(payload, dict) else None
        return catalog_id if isinstance(catalog_id, str) and catalog_id else None
    except Exception:
        return None


def _catalog_metadata(raw: bytes) -> tuple[str, int, str, str]:
    envelope = parse_catalog_json(raw)
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise CatalogCacheError("signed catalog payload is not an object")
    catalog_id = payload.get("catalog_id")
    sequence = payload.get("sequence")
    issued_at = payload.get("issued_at")
    expires_at = payload.get("expires_at")
    if not isinstance(catalog_id, str) or not catalog_id or isinstance(sequence, bool) or not isinstance(sequence, int):
        raise CatalogCacheError("signed catalog metadata is malformed")
    if not isinstance(issued_at, str) or not isinstance(expires_at, str):
        raise CatalogCacheError("signed catalog validity metadata is malformed")
    return catalog_id, sequence, issued_at, expires_at


class SignedCatalogService:
    """Load, verify, cache, and subject-check one configured signed catalog."""

    def __init__(
        self,
        root: Path,
        endpoint: Optional[str],
        key_provider: CatalogKeyProvider,
        *,
        transport: Optional[CatalogTransport] = None,
        clock: Optional[Callable[[], datetime]] = None,
        cache: Optional[SignedCatalogCache] = None,
    ) -> None:
        self.endpoint = endpoint.strip() if isinstance(endpoint, str) and endpoint.strip() else None
        self.key_provider = key_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache = cache or SignedCatalogCache(root)
        self._transport_error: Optional[CatalogTransportError] = None
        if transport is not None:
            self.transport = transport
        elif self.endpoint:
            try:
                self.transport = CatalogTransport(self.endpoint)
            except CatalogTransportError as exc:
                self.transport = None
                self._transport_error = exc
        else:
            self.transport = None

    @property
    def trust_root_status(self) -> TrustRootStatus:
        return trust_root_status(self.key_provider)

    def _now(self) -> datetime:
        current = self.clock()
        return current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)

    def _trust_snapshot_fields(self) -> dict[str, Any]:
        status = self.trust_root_status
        return {
            "trust_root_fingerprint": status.fingerprint,
            "trust_keyset_id": status.keyset_id,
            "trust_keyset_sequence": status.keyset_sequence,
            "trust_keyset_expires_at": status.keyset_expires_at,
            "trust_keyset_active_key_ids": tuple(status.active_key_ids),
            "trust_keyset_revoked_key_ids": tuple(status.revoked_key_ids),
            "trust_lifecycle_reason": status.reason_code,
            "trust_last_transition": status.last_transition,
        }

    @staticmethod
    def _result(status: CatalogAuthStatus, reason: CatalogReason, detail: str) -> CatalogVerificationResult:
        return CatalogVerificationResult(
            catalog_auth_status=status,
            policy_status=PolicyStatus.UNREVIEWED,
            reason_code=reason,
            detail=detail,
            execution_status=ExecutionStatus.DISABLED,
        )

    def _unconfigured_snapshot(self) -> CatalogSnapshot:
        status = self.trust_root_status
        try:
            trust_reason = CatalogReason(status.reason_code) if status.reason_code else None
        except ValueError:
            trust_reason = None
        expired_trust = trust_reason in {
            CatalogReason.KEYSET_EXPIRED,
            CatalogReason.CATALOG_KEY_EXPIRED,
            CatalogReason.REVOCATION_STATUS_STALE,
        }
        if not status.configured and trust_reason not in {
            CatalogReason.TRUST_ROOT_UNCONFIGURED,
            CatalogReason.KEYSET_UNCONFIGURED,
        }:
            result = self._result(
                CatalogAuthStatus.EXPIRED if expired_trust else CatalogAuthStatus.INVALID,
                trust_reason or CatalogReason.TRUST_STORE_CORRUPT,
                f"signed catalog approval is unavailable because trust lifecycle validation failed: {status.reason_code or 'trust_store_corrupt'}",
            )
        elif not status.configured:
            result = self._result(
                CatalogAuthStatus.UNSIGNED,
                trust_reason or CatalogReason.TRUST_ROOT_UNCONFIGURED,
                "signed catalog approval is unavailable because independently provisioned trust material is incomplete",
            )
        else:
            result = self._result(
                CatalogAuthStatus.UNSIGNED,
                CatalogReason.ENDPOINT_UNCONFIGURED,
                "signed catalog approval is unavailable because no catalog endpoint is configured",
            )
        return CatalogSnapshot(None, result, "none", "unconfigured", **self._trust_snapshot_fields())

    def _verify_raw(self, raw: bytes, highest_sequence: Optional[int] = None) -> CatalogVerificationResult:
        return verify_catalog(
            raw,
            self.key_provider,
            now=self._now(),
            highest_accepted_sequence=highest_sequence,
        )

    def _snapshot_from_cached(
        self,
        cached: CachedCatalogEnvelope,
        *,
        refresh_failure_reason: Optional[str] = None,
    ) -> CatalogSnapshot:
        result = self._verify_raw(cached.raw)
        freshness = "cached"
        if result.catalog_auth_status is CatalogAuthStatus.EXPIRED:
            freshness = "expired"
        if result.catalog_auth_status is CatalogAuthStatus.VERIFIED and result.sequence != cached.highest_accepted_sequence:
            result = self._result(
                CatalogAuthStatus.INVALID,
                CatalogReason.CACHE_CORRUPT,
                "cached catalog sequence does not match the persisted watermark",
            )
            freshness = "corrupt"
        try:
            catalog_id, _sequence, issued_at, expires_at = _catalog_metadata(cached.raw)
        except CatalogCacheError:
            catalog_id, issued_at, expires_at = cached.catalog_id, cached.issued_at, cached.expires_at
        return CatalogSnapshot(
            cached.raw,
            result,
            "cache",
            freshness,
            catalog_id=catalog_id,
            issued_at=issued_at,
            expires_at=expires_at,
            endpoint=cached.endpoint,
            **self._trust_snapshot_fields(),
            refresh_failure_reason=refresh_failure_reason,
        )

    def load_cached(self, refresh_failure_reason: Optional[str] = None) -> CatalogSnapshot:
        if not self.trust_root_status.configured:
            return self._unconfigured_snapshot()
        try:
            cached = self.cache.read()
        except CatalogCacheError as exc:
            return CatalogSnapshot(
                None,
                self._result(CatalogAuthStatus.INVALID, CatalogReason.CACHE_CORRUPT, str(exc)),
                "cache",
                "corrupt",
                **self._trust_snapshot_fields(),
                refresh_failure_reason=refresh_failure_reason,
            )
        if cached is None:
            return CatalogSnapshot(
                None,
                self._result(CatalogAuthStatus.UNSIGNED, CatalogReason.TRANSPORT_FAILURE, "no verified signed catalog is cached"),
                "none",
                "unavailable",
                **self._trust_snapshot_fields(),
                refresh_failure_reason=refresh_failure_reason,
            )
        return self._snapshot_from_cached(cached, refresh_failure_reason=refresh_failure_reason)

    def refresh(self) -> CatalogSnapshot:
        if not self.endpoint:
            return self._unconfigured_snapshot()
        if not self.trust_root_status.configured:
            return self._unconfigured_snapshot()
        if self.transport is None:
            reason = self._transport_error.reason if self._transport_error else CatalogReason.ENDPOINT_UNCONFIGURED
            return self.load_cached(refresh_failure_reason=reason.value)
        try:
            fetched = self.transport.fetch()
            catalog_id = _candidate_catalog_id(fetched.raw)
            highest = self.cache.highest_sequence(catalog_id) if catalog_id else None
            result = self._verify_raw(fetched.raw, highest)
            if result.catalog_auth_status is not CatalogAuthStatus.VERIFIED:
                return self.load_cached(refresh_failure_reason=result.reason_code.value if result.reason_code else "catalog_rejected")
            catalog_id, sequence, issued_at, expires_at = _catalog_metadata(fetched.raw)
            trust_fields = self._trust_snapshot_fields()
            self.cache.write_verified(
                fetched.raw,
                catalog_id=catalog_id,
                sequence=sequence,
                issued_at=issued_at,
                expires_at=expires_at,
                key_id=str(result.authority_key_id or ""),
                payload_sha256=str(result.canonical_payload_sha256 or ""),
                endpoint=fetched.endpoint,
                trust_root_fingerprint=trust_fields["trust_root_fingerprint"],
            )
            return CatalogSnapshot(
                fetched.raw,
                result,
                "network",
                "fresh",
                catalog_id=catalog_id,
                issued_at=issued_at,
                expires_at=expires_at,
                endpoint=fetched.endpoint,
                **trust_fields,
            )
        except CatalogTransportError as exc:
            return self.load_cached(refresh_failure_reason=exc.reason.value)
        except CatalogCacheError:
            return self.load_cached(refresh_failure_reason=CatalogReason.CACHE_CORRUPT.value)
        except Exception:
            return self.load_cached(refresh_failure_reason=CatalogReason.TRANSPORT_FAILURE.value)

    def snapshot(self, *, refresh: bool = True) -> CatalogSnapshot:
        if refresh and self.endpoint and self.trust_root_status.configured:
            return self.refresh()
        return self.load_cached()

    def verify_subject(self, snapshot: CatalogSnapshot, subject: CatalogSubject) -> CatalogVerificationResult:
        if snapshot.raw is None:
            return snapshot.result
        return verify_catalog(
            snapshot.raw,
            self.key_provider,
            now=self._now(),
            expected_subject=subject,
        )
