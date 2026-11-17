"""Bounded, optional publisher evidence for immutable GitHub commits.

This module deliberately models GitHub's REST response as provider-reported
evidence. It does not perform local GPG/SSH/S/MIME verification, does not
establish signer identity, and cannot approve a catalog or enable execution.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol
from urllib.parse import urlsplit

import httpx

from .trust_verifier import PublisherAuthStatus, canonicalize_jcs

MAX_EVIDENCE_BYTES = 512 * 1024
MAX_EVIDENCE_FIELD_BYTES = 16 * 1024
MAX_EVIDENCE_RECORDS = 256
MAX_REQUEST_TIMEOUT = 60.0
EVIDENCE_MAX_AGE = timedelta(hours=24)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}


class EvidenceObservationStatus(str, Enum):
    OBSERVED = "observed"
    NOT_CHECKED = "not_checked"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    INVALID_RESPONSE = "invalid_response"


class GithubProviderStatus(str, Enum):
    VERIFIED = "verified"
    UNSIGNED = "unsigned"
    UNVERIFIED = "unverified"
    INVALID = "invalid"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    PROVIDER_ERROR = "provider_error"
    UNKNOWN = "unknown"


class LocalSignatureStatus(str, Enum):
    NOT_PERFORMED = "not_performed"
    VERIFIED = "locally_verified"
    FAILED = "locally_failed"
    UNSUPPORTED = "unsupported_format"


class IdentityPolicyStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    NOT_EVALUATED = "not_evaluated"
    MATCHED = "matched"
    MISMATCHED = "mismatched"


class PublisherEvidenceError(ValueError):
    """Invalid evidence subject or evidence-store state."""

    def __init__(self, message: str, reason_code: str = "publisher_evidence_invalid") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class PublisherEvidenceSemanticError(PublisherEvidenceError):
    """A well-formed record contains a status this adapter cannot establish."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "publisher_evidence_semantics_invalid")


class PublisherEvidenceAdapter(Protocol):
    def check(self, subject: "PublisherEvidenceSubject") -> "PublisherEvidenceResult":
        ...


@dataclass(frozen=True)
class PublisherEvidenceSubject:
    repository_url: str
    commit_sha: str

    def __post_init__(self) -> None:
        from .plugin_manager import canonicalize_github_url

        try:
            canonical = canonicalize_github_url(self.repository_url)
        except Exception as exc:
            raise PublisherEvidenceError(str(exc), "publisher_subject_invalid") from exc
        if self.repository_url != canonical or not isinstance(self.commit_sha, str) or not _SHA_RE.fullmatch(self.commit_sha):
            raise PublisherEvidenceError("publisher evidence subject is not canonical", "publisher_subject_invalid")

    @classmethod
    def create(cls, repository_url: str, commit_sha: str) -> "PublisherEvidenceSubject":
        from .plugin_manager import canonicalize_github_url

        try:
            canonical = canonicalize_github_url(repository_url)
        except Exception as exc:
            raise PublisherEvidenceError(str(exc), "publisher_subject_invalid") from exc
        if not isinstance(commit_sha, str) or not _SHA_RE.fullmatch(commit_sha.lower()):
            raise PublisherEvidenceError("publisher evidence requires a full 40-character commit SHA", "publisher_subject_invalid")
        return cls(canonical, commit_sha.lower())

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.repository_url}\n{self.commit_sha}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GithubResponse:
    raw: bytes
    content_type: str = "application/json"
    status_code: int = 200
    headers: Mapping[str, str] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PublisherEvidenceResult:
    subject: PublisherEvidenceSubject
    observation_status: EvidenceObservationStatus
    provider_status: Optional[GithubProviderStatus]
    publisher_auth_status: PublisherAuthStatus
    reason_code: Optional[str]
    provider_reason: Optional[str]
    identity: Mapping[str, Any]
    identity_policy_status: IdentityPolicyStatus
    local_signature_status: LocalSignatureStatus
    signature_format: Optional[str]
    signature_digest: Optional[str]
    payload_digest: Optional[str]
    observed_at: str
    provider_verified_at: Optional[str]
    freshness: str
    provider: str = "github"
    evidence_type: str = "github_commit_signature"
    coverage: str = "git_commit"
    adapter_version: str = "github-commit-signature.v1"

    @property
    def evidence_digest(self) -> str:
        evidence = self.to_dict(include_digest=False)
        evidence.pop("freshness", None)
        return hashlib.sha256(canonicalize_jcs(evidence)).hexdigest()

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        _validate_result_semantics(self)
        result: dict[str, Any] = {
            "schema_version": 1,
            "provider": self.provider,
            "evidence_type": self.evidence_type,
            "adapter_version": self.adapter_version,
            "coverage": self.coverage,
            "repository_url": self.subject.repository_url,
            "commit_sha": self.subject.commit_sha,
            "observation_status": self.observation_status.value,
            "provider_status": self.provider_status.value if self.provider_status else None,
            "publisher_auth_status": self.publisher_auth_status.value,
            "reason_code": self.reason_code,
            "provider_reason": self.provider_reason,
            "identity": dict(self.identity),
            "identity_policy_status": self.identity_policy_status.value,
            "local_signature_status": self.local_signature_status.value,
            "signature_format": self.signature_format,
            "signature_digest": self.signature_digest,
            "payload_digest": self.payload_digest,
            "observed_at": self.observed_at,
            "provider_verified_at": self.provider_verified_at,
            "freshness": self.freshness,
        }
        if include_digest:
            result["evidence_digest"] = self.evidence_digest
        return result


def _now(clock: Optional[Callable[[], datetime]]) -> datetime:
    value = clock() if clock else datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise PublisherEvidenceError(f"{field} is not a strict UTC timestamp", "publisher_evidence_invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PublisherEvidenceError(f"{field} is invalid", "publisher_evidence_invalid") from exc


def _bounded_text(value: Any, field: str, *, nullable: bool = True) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_EVIDENCE_FIELD_BYTES:
        raise PublisherEvidenceError(f"{field} is invalid or oversized", "publisher_response_invalid")
    return value


def _strict_json(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"unsupported JSON constant: {constant}")),
        )
    except Exception as exc:
        raise PublisherEvidenceError(f"GitHub evidence JSON is invalid: {exc}", "publisher_response_invalid") from exc
    if not isinstance(value, dict):
        raise PublisherEvidenceError("GitHub evidence response must be an object", "publisher_response_invalid")
    return value


def _identity_observation(value: Any, prefix: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PublisherEvidenceError(f"{prefix} identity is invalid", "publisher_response_invalid")
    result: dict[str, Any] = {}
    for source, target in (("name", "name"), ("email", "email"), ("date", "date"), ("login", "login"), ("id", "id"), ("html_url", "html_url"), ("type", "type")):
        if source in value:
            item = value[source]
            if source == "id":
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise PublisherEvidenceError(f"{prefix}.{source} is invalid", "publisher_response_invalid")
                result[target] = item
            else:
                result[target] = _bounded_text(item, f"{prefix}.{source}")
    return result


def _commit_url_matches(repository_url: str, commit_sha: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in _GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    expected = [part for part in urlsplit(repository_url).path.split("/") if part]
    return len(parts) == 4 and parts[:2] == expected and parts[2] == "commit" and parts[3].lower() == commit_sha


class GithubCommitSignatureAdapter:
    """Read-only GitHub commit-signature evidence; no local crypto claims."""

    def __init__(
        self,
        *,
        response_fetcher: Optional[Callable[[str], GithubResponse]] = None,
        timeout: float = 20.0,
        clock: Optional[Callable[[], datetime]] = None,
        token: Optional[str] = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_REQUEST_TIMEOUT:
            raise PublisherEvidenceError(
                f"publisher evidence timeout must be finite and between 0 and {MAX_REQUEST_TIMEOUT} seconds",
                "publisher_timeout_invalid",
            )
        self._response_fetcher = response_fetcher
        self.timeout = float(timeout)
        self.clock = clock
        self.token = token

    @staticmethod
    def endpoint(subject: PublisherEvidenceSubject) -> str:
        parts = [part for part in urlsplit(subject.repository_url).path.split("/") if part]
        return f"https://api.github.com/repos/{parts[0]}/{parts[1]}/commits/{subject.commit_sha}"

    def _http_fetch(self, endpoint: str) -> GithubResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Aetherion-CLI-publisher-evidence/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                with client.stream("GET", endpoint, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        return GithubResponse(b"", status_code=response.status_code, headers=dict(response.headers))
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > MAX_EVIDENCE_BYTES:
                            return GithubResponse(b"", content_type="oversized", status_code=413, headers={})
                        chunks.append(chunk)
                    return GithubResponse(
                        b"".join(chunks),
                        content_type=response.headers.get("content-type", ""),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                    )
        except Exception:
            return GithubResponse(b"", status_code=599, headers={})

    def _result(
        self,
        subject: PublisherEvidenceSubject,
        observation: EvidenceObservationStatus,
        *,
        provider_status: Optional[GithubProviderStatus] = None,
        publisher_auth: PublisherAuthStatus = PublisherAuthStatus.NOT_CHECKED,
        reason: Optional[str] = None,
        provider_reason: Optional[str] = None,
        identity: Optional[Mapping[str, Any]] = None,
        signature_format: Optional[str] = None,
        signature_digest: Optional[str] = None,
        payload_digest: Optional[str] = None,
        provider_verified_at: Optional[str] = None,
    ) -> PublisherEvidenceResult:
        return PublisherEvidenceResult(
            subject=subject,
            observation_status=observation,
            provider_status=provider_status,
            publisher_auth_status=publisher_auth,
            reason_code=reason,
            provider_reason=provider_reason,
            identity=dict(identity or {}),
            identity_policy_status=IdentityPolicyStatus.UNCONFIGURED,
            local_signature_status=LocalSignatureStatus.NOT_PERFORMED,
            signature_format=signature_format,
            signature_digest=signature_digest,
            payload_digest=payload_digest,
            observed_at=_timestamp(_now(self.clock)),
            provider_verified_at=provider_verified_at,
            freshness="fresh" if observation is EvidenceObservationStatus.OBSERVED else "unavailable",
        )

    def check(self, subject: PublisherEvidenceSubject) -> PublisherEvidenceResult:
        endpoint = self.endpoint(subject)
        try:
            response = self._response_fetcher(endpoint) if self._response_fetcher else self._http_fetch(endpoint)
        except Exception:
            return self._result(subject, EvidenceObservationStatus.UNAVAILABLE, reason="publisher_transport_unavailable")
        if not isinstance(response, GithubResponse):
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason="publisher_response_invalid")
        if len(response.raw) > MAX_EVIDENCE_BYTES:
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason="publisher_response_oversized")
        if 300 <= response.status_code < 400:
            return self._result(subject, EvidenceObservationStatus.UNAVAILABLE, reason="publisher_redirect_rejected")
        if response.status_code == 404:
            return self._result(subject, EvidenceObservationStatus.NOT_FOUND, reason="publisher_commit_not_found")
        if response.status_code in {401, 403}:
            return self._result(subject, EvidenceObservationStatus.ACCESS_DENIED, reason="publisher_access_denied")
        if response.status_code == 413:
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason="publisher_response_oversized")
        if response.status_code == 429 or response.status_code >= 500 or response.status_code == 599:
            return self._result(subject, EvidenceObservationStatus.UNAVAILABLE, reason="publisher_transport_unavailable")
        if response.status_code != 200:
            return self._result(subject, EvidenceObservationStatus.UNAVAILABLE, reason="publisher_http_error")
        if response.content_type.lower().split(";", 1)[0].strip() != "application/json":
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason="publisher_content_type_invalid")
        try:
            document = _strict_json(response.raw)
            actual_sha = document.get("sha")
            if not isinstance(actual_sha, str) or actual_sha.lower() != subject.commit_sha:
                raise PublisherEvidenceError("GitHub response commit SHA does not match the requested commit", "publisher_subject_mismatch")
            if not _commit_url_matches(subject.repository_url, subject.commit_sha, document.get("html_url")):
                raise PublisherEvidenceError("GitHub response commit URL does not match the requested repository", "publisher_subject_mismatch")
            commit = document.get("commit")
            if not isinstance(commit, dict):
                raise PublisherEvidenceError("GitHub response commit object is invalid", "publisher_response_invalid")
            if "verification" not in commit or commit.get("verification") is None:
                return self._result(
                    subject,
                    EvidenceObservationStatus.OBSERVED,
                    provider_status=GithubProviderStatus.UNSIGNED,
                    publisher_auth=PublisherAuthStatus.UNSIGNED,
                    reason="publisher_signature_missing",
                )
            if not isinstance(commit.get("verification"), dict):
                raise PublisherEvidenceError("GitHub response verification object is invalid", "publisher_response_invalid")
            verification = commit["verification"]
            if not verification:
                return self._result(
                    subject,
                    EvidenceObservationStatus.OBSERVED,
                    provider_status=GithubProviderStatus.UNSIGNED,
                    publisher_auth=PublisherAuthStatus.UNSIGNED,
                    reason="publisher_signature_missing",
                )
            required = {"verified", "reason", "signature", "payload", "verified_at"}
            if not {"verified", "reason"}.issubset(verification) or not isinstance(verification["verified"], bool):
                raise PublisherEvidenceError("GitHub verification object has an unsupported schema", "publisher_response_invalid")
            provider_reason = _bounded_text(verification["reason"], "verification.reason", nullable=False)
            signature = _bounded_text(verification.get("signature"), "verification.signature")
            payload = _bounded_text(verification.get("payload"), "verification.payload")
            verified_at = verification.get("verified_at")
            if set(verification) != required:
                if not signature or not payload:
                    return self._result(
                        subject,
                        EvidenceObservationStatus.OBSERVED,
                        provider_status=GithubProviderStatus.UNSIGNED,
                        publisher_auth=PublisherAuthStatus.UNSIGNED,
                        reason="publisher_signature_missing",
                        provider_reason=provider_reason,
                    )
                raise PublisherEvidenceError("GitHub verification object has an unsupported schema", "publisher_response_invalid")
            if not signature or not payload:
                return self._result(
                    subject,
                    EvidenceObservationStatus.OBSERVED,
                    provider_status=GithubProviderStatus.UNSIGNED,
                    publisher_auth=PublisherAuthStatus.UNSIGNED,
                    reason="publisher_signature_missing",
                    provider_reason=provider_reason,
                )
            if verified_at is not None:
                _parse_timestamp(verified_at, "verification.verified_at")
            identity = {
                "author_git": _identity_observation(commit.get("author"), "commit.author"),
                "committer_git": _identity_observation(commit.get("committer"), "commit.committer"),
                "author_github": _identity_observation(document.get("author"), "author"),
                "committer_github": _identity_observation(document.get("committer"), "committer"),
            }
            if verification["verified"] is not (provider_reason == "valid"):
                raise PublisherEvidenceError("GitHub verification fields contradict each other", "publisher_response_invalid")
            provider_status = _provider_status(provider_reason)
            if provider_status is GithubProviderStatus.VERIFIED:
                publisher_auth = PublisherAuthStatus.UNKNOWN_IDENTITY
            elif provider_status is GithubProviderStatus.UNSIGNED:
                publisher_auth = PublisherAuthStatus.UNSIGNED
            elif provider_status in {
                GithubProviderStatus.UNAVAILABLE,
                GithubProviderStatus.PROVIDER_ERROR,
                GithubProviderStatus.UNSUPPORTED,
                GithubProviderStatus.UNKNOWN,
            }:
                publisher_auth = PublisherAuthStatus.NOT_CHECKED
            else:
                publisher_auth = PublisherAuthStatus.INVALID
            return self._result(
                subject,
                EvidenceObservationStatus.OBSERVED,
                provider_status=provider_status,
                publisher_auth=publisher_auth,
                provider_reason=provider_reason,
                identity=identity,
                signature_format=None,
                signature_digest=hashlib.sha256(signature.encode("utf-8")).hexdigest(),
                payload_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                provider_verified_at=verified_at,
            )
        except PublisherEvidenceError as exc:
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason=exc.reason_code)
        except Exception:
            return self._result(subject, EvidenceObservationStatus.INVALID_RESPONSE, reason="publisher_response_invalid")


def _provider_status(reason: str) -> GithubProviderStatus:
    if reason == "valid":
        return GithubProviderStatus.VERIFIED
    if reason == "unsigned":
        return GithubProviderStatus.UNSIGNED
    if reason == "invalid":
        return GithubProviderStatus.INVALID
    if reason == "malformed_signature":
        return GithubProviderStatus.MALFORMED
    if reason == "unknown_signature_type":
        return GithubProviderStatus.UNSUPPORTED
    if reason == "gpgverify_unavailable":
        return GithubProviderStatus.UNAVAILABLE
    if reason == "gpgverify_error":
        return GithubProviderStatus.PROVIDER_ERROR
    if reason in {"expired_key", "not_signing_key", "no_user", "unverified_email", "bad_email", "unknown_key"}:
        return GithubProviderStatus.UNVERIFIED
    return GithubProviderStatus.UNKNOWN


def _validate_result_semantics(result: PublisherEvidenceResult) -> None:
    if result.publisher_auth_status is PublisherAuthStatus.VERIFIED:
        raise PublisherEvidenceSemanticError("publisher evidence cannot authorize an Aetherion publisher")
    if result.identity_policy_status is IdentityPolicyStatus.MATCHED:
        raise PublisherEvidenceSemanticError("publisher evidence cannot establish signer identity policy")
    if result.local_signature_status is LocalSignatureStatus.VERIFIED:
        raise PublisherEvidenceSemanticError("publisher evidence cannot claim local cryptographic verification")

    expected_auth: Optional[PublisherAuthStatus] = None
    if result.provider_status is GithubProviderStatus.VERIFIED:
        if result.provider_reason != "valid":
            raise PublisherEvidenceSemanticError("verified provider evidence has an invalid provider reason")
        expected_auth = PublisherAuthStatus.UNKNOWN_IDENTITY
    elif result.provider_status is GithubProviderStatus.UNSIGNED:
        expected_auth = PublisherAuthStatus.UNSIGNED
    elif result.provider_status in {
        GithubProviderStatus.INVALID,
        GithubProviderStatus.UNVERIFIED,
        GithubProviderStatus.MALFORMED,
    }:
        expected_auth = PublisherAuthStatus.INVALID
    elif result.provider_status in {
        GithubProviderStatus.UNSUPPORTED,
        GithubProviderStatus.UNAVAILABLE,
        GithubProviderStatus.PROVIDER_ERROR,
        GithubProviderStatus.UNKNOWN,
        None,
    }:
        expected_auth = PublisherAuthStatus.NOT_CHECKED
    if expected_auth is not None and result.publisher_auth_status is not expected_auth:
        raise PublisherEvidenceSemanticError("publisher evidence status mapping is inconsistent")
    if result.provider_status is not None and result.observation_status is not EvidenceObservationStatus.OBSERVED:
        raise PublisherEvidenceSemanticError("provider status exists without an observed response")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        is_junction = getattr(os, "isjunction", lambda value: False)
        return path.is_symlink() or os.path.islink(str(path)) or bool(is_junction(str(path)))
    except OSError:
        return True


def _assert_safe_path(path: Path) -> None:
    """Reject symlink/reparse components across the complete existing path."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = absolute
    while True:
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current):
                raise PublisherEvidenceError("publisher evidence path is unsafe", "publisher_evidence_store_corrupt")
        if current.parent == current:
            break
        current = current.parent


def _bounded_read_file(path: Path, limit: int) -> bytes:
    """Read a regular file with the limit enforced on the opened stream."""
    _assert_safe_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
        _assert_safe_path(path)
    except OSError as exc:
        raise PublisherEvidenceError(f"publisher evidence store is unreadable: {exc}", "publisher_evidence_store_io_failure") from exc
    except PublisherEvidenceError:
        os.close(descriptor)
        raise
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PublisherEvidenceError("publisher evidence store is not a regular file", "publisher_evidence_store_corrupt")
        if file_stat.st_size > limit:
            raise PublisherEvidenceError("publisher evidence store is oversized", "publisher_evidence_store_corrupt")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit - len(data) + 1))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                raise PublisherEvidenceError("publisher evidence store is oversized", "publisher_evidence_store_corrupt")
        return bytes(data)
    except PublisherEvidenceError:
        raise
    except OSError as exc:
        raise PublisherEvidenceError(f"publisher evidence store is unreadable: {exc}", "publisher_evidence_store_io_failure") from exc
    finally:
        os.close(descriptor)


def _strict_store_json(raw: bytes) -> dict[str, Any]:
    return _strict_json(raw)


def _atomic_store_write(path: Path, data: bytes) -> None:
    _assert_safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(path.parent)
    temp_name: Optional[str] = None
    replaced = False
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=os.fspath(path.parent))
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_path(path.parent)
        _assert_safe_path(path)
        os.replace(temp_name, os.fspath(path))
        replaced = True
    except OSError as exc:
        raise PublisherEvidenceError(f"publisher evidence store write failed: {exc}", "publisher_evidence_store_io_failure") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if not replaced:
                    raise PublisherEvidenceError(
                        f"publisher evidence temporary-file cleanup failed: {exc}",
                        "publisher_evidence_store_io_failure",
                    ) from exc


@contextmanager
def _store_lock(path: Path) -> Iterator[None]:
    _assert_safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(path.parent)
    descriptor: Optional[int] = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.fspath(path), flags, 0o600)
        _assert_safe_path(path)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PublisherEvidenceError("publisher evidence lock is not a regular file", "publisher_evidence_store_corrupt")
        with os.fdopen(descriptor, "a+b", closefd=True) as handle:
            descriptor = None
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
    except PublisherEvidenceError:
        raise
    except OSError as exc:
        raise PublisherEvidenceError(f"publisher evidence lock failed: {exc}", "publisher_evidence_store_io_failure") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _result_from_dict(value: Mapping[str, Any], *, expected: Optional[PublisherEvidenceSubject] = None) -> PublisherEvidenceResult:
    if not isinstance(value, Mapping):
        raise PublisherEvidenceError("publisher evidence record is invalid", "publisher_evidence_store_corrupt")
    required = {
        "schema_version", "provider", "evidence_type", "adapter_version", "coverage",
        "repository_url", "commit_sha", "observation_status", "provider_status",
        "publisher_auth_status", "reason_code", "provider_reason", "identity",
        "identity_policy_status", "local_signature_status", "signature_format",
        "signature_digest", "payload_digest", "observed_at", "provider_verified_at",
        "freshness", "evidence_digest",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise PublisherEvidenceError("publisher evidence record has an unsupported schema", "publisher_evidence_store_corrupt")
    if not isinstance(value["repository_url"], str) or not isinstance(value["commit_sha"], str):
        raise PublisherEvidenceError("publisher evidence subject fields are invalid", "publisher_evidence_store_corrupt")
    subject = PublisherEvidenceSubject.create(value["repository_url"], value["commit_sha"])
    if expected is not None and subject != expected:
        raise PublisherEvidenceError("publisher evidence subject does not match request", "publisher_subject_mismatch")
    if not isinstance(value["identity"], dict):
        raise PublisherEvidenceError("publisher evidence identity is invalid", "publisher_evidence_store_corrupt")
    identity_keys = {"author_git", "committer_git", "author_github", "committer_github"}
    if set(value["identity"]) != identity_keys:
        raise PublisherEvidenceError("publisher evidence identity schema is invalid", "publisher_evidence_store_corrupt")
    identity_fields = {"name", "email", "date", "login", "id", "html_url", "type"}
    for identity_name, identity_value in value["identity"].items():
        if not isinstance(identity_value, dict) or not set(identity_value).issubset(identity_fields):
            raise PublisherEvidenceError(f"publisher evidence identity '{identity_name}' is invalid", "publisher_evidence_store_corrupt")
        for field, field_value in identity_value.items():
            if field == "id":
                if isinstance(field_value, bool) or not isinstance(field_value, int) or field_value < 0:
                    raise PublisherEvidenceError(f"publisher evidence identity '{identity_name}.{field}' is invalid", "publisher_evidence_store_corrupt")
            elif field_value is not None:
                _bounded_text(field_value, f"{identity_name}.{field}")
    for field in ("provider", "evidence_type", "adapter_version", "coverage", "freshness"):
        if not isinstance(value[field], str) or not value[field] or len(value[field].encode("utf-8")) > 256:
            raise PublisherEvidenceError(f"publisher evidence field '{field}' is invalid", "publisher_evidence_store_corrupt")
    if value["provider"] != "github" or value["evidence_type"] != "github_commit_signature":
        raise PublisherEvidenceError("publisher evidence provider/type is unsupported", "publisher_evidence_store_corrupt")
    for field in ("signature_digest", "payload_digest"):
        digest = value[field]
        if digest is not None and (not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)):
            raise PublisherEvidenceError(f"publisher evidence field '{field}' is invalid", "publisher_evidence_store_corrupt")
    if not isinstance(value["evidence_digest"], str) or not _DIGEST_RE.fullmatch(value["evidence_digest"]):
        raise PublisherEvidenceError("publisher evidence digest is invalid", "publisher_evidence_store_corrupt")
    try:
        observation = EvidenceObservationStatus(value["observation_status"])
        provider_status = GithubProviderStatus(value["provider_status"]) if value["provider_status"] else None
        publisher_auth = PublisherAuthStatus(value["publisher_auth_status"])
        identity_status = IdentityPolicyStatus(value["identity_policy_status"])
        local_status = LocalSignatureStatus(value["local_signature_status"])
    except (TypeError, ValueError) as exc:
        raise PublisherEvidenceError("publisher evidence enum value is invalid", "publisher_evidence_store_corrupt") from exc
    _parse_timestamp(value["observed_at"], "observed_at")
    if value["provider_verified_at"] is not None:
        _parse_timestamp(value["provider_verified_at"], "provider_verified_at")
    result = PublisherEvidenceResult(
        subject=subject,
        observation_status=observation,
        provider_status=provider_status,
        publisher_auth_status=publisher_auth,
        reason_code=_bounded_text(value["reason_code"], "reason_code"),
        provider_reason=_bounded_text(value["provider_reason"], "provider_reason"),
        identity=dict(value["identity"]),
        identity_policy_status=identity_status,
        local_signature_status=local_status,
        signature_format=_bounded_text(value["signature_format"], "signature_format"),
        signature_digest=_bounded_text(value["signature_digest"], "signature_digest"),
        payload_digest=_bounded_text(value["payload_digest"], "payload_digest"),
        observed_at=value["observed_at"],
        provider_verified_at=value["provider_verified_at"],
        freshness=_bounded_text(value["freshness"], "freshness", nullable=False) or "stale",
        provider=str(value["provider"]),
        evidence_type=str(value["evidence_type"]),
        coverage=str(value["coverage"]),
        adapter_version=str(value["adapter_version"]),
    )
    if result.evidence_digest != value["evidence_digest"]:
        raise PublisherEvidenceError("publisher evidence digest mismatch", "publisher_evidence_store_corrupt")
    _validate_result_semantics(result)
    return result


class PublisherEvidenceStore:
    """Strict, subject-bound, bounded local observation cache."""

    def __init__(self, root: Path, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        requested_root = Path(root).expanduser()
        _assert_safe_path(requested_root)
        self.root = requested_root
        self.path = self.root / "publisher_evidence.json"
        self.lock_path = self.root / ".publisher_evidence.lock"
        self.clock = clock

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            value = _strict_store_json(_bounded_read_file(self.path, MAX_EVIDENCE_BYTES))
        except PublisherEvidenceError:
            raise
        except OSError as exc:
            raise PublisherEvidenceError(f"publisher evidence store is unreadable: {exc}", "publisher_evidence_store_io_failure") from exc
        except Exception as exc:
            raise PublisherEvidenceError(f"publisher evidence store is unreadable: {exc}", "publisher_evidence_store_corrupt") from exc
        if set(value) != {"version", "records"} or value["version"] != 1 or not isinstance(value["records"], dict):
            raise PublisherEvidenceError("publisher evidence store has an unsupported schema", "publisher_evidence_store_corrupt")
        if len(value["records"]) > MAX_EVIDENCE_RECORDS:
            raise PublisherEvidenceError("publisher evidence store has too many records", "publisher_evidence_store_corrupt")
        value["records"] = {
            key: record
            for key, record in value["records"].items()
            if isinstance(key, str) and _DIGEST_RE.fullmatch(key)
        }
        return value

    def write(self, result: PublisherEvidenceResult) -> None:
        with _store_lock(self.lock_path):
            document = self._read()
            records = dict(document["records"])
            candidate = result.to_dict()
            _result_from_dict(candidate, expected=result.subject)
            records[result.subject.key] = candidate
            if len(records) > MAX_EVIDENCE_RECORDS:
                raise PublisherEvidenceError("publisher evidence store has too many records", "publisher_evidence_store_limit")
            encoded = canonicalize_jcs({"version": 1, "records": records}) + b"\n"
            if len(encoded) > MAX_EVIDENCE_BYTES:
                raise PublisherEvidenceError("publisher evidence store exceeds the size limit", "publisher_evidence_store_limit")
            _atomic_store_write(self.path, encoded)

    def get(self, subject: PublisherEvidenceSubject) -> Optional[PublisherEvidenceResult]:
        with _store_lock(self.lock_path):
            value = self._read()["records"].get(subject.key)
            if value is None:
                return None
            try:
                result = _result_from_dict(value, expected=subject)
            except PublisherEvidenceError:
                # A single malformed or semantically impossible record must not
                # deny unrelated subjects or project a forged positive state.
                return None
            age = _now(self.clock) - _parse_timestamp(result.observed_at, "observed_at")
            freshness = "cached" if timedelta(0) <= age <= EVIDENCE_MAX_AGE else "stale"
            if result.freshness == freshness:
                return result
            return PublisherEvidenceResult(**{**result.__dict__, "freshness": freshness})
