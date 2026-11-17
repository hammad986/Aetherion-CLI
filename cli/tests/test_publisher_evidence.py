"""Deterministic GitHub commit-signature evidence tests."""
from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aetherion_cli.publisher_evidence as publisher_evidence
from aetherion_cli.plugin_manager import PluginCatalogManager, PluginManagerError
from aetherion_cli.publisher_evidence import (
    EVIDENCE_MAX_AGE,
    EvidenceObservationStatus,
    GithubCommitSignatureAdapter,
    GithubProviderStatus,
    GithubResponse,
    IdentityPolicyStatus,
    LocalSignatureStatus,
    PublisherEvidenceError,
    PublisherEvidenceStore,
    PublisherEvidenceSubject,
)
from aetherion_cli.trust_verifier import PublisherAuthStatus, canonicalize_jcs


NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
REPOSITORY = "https://github.com/example/demo"
SHA = "a" * 40


def _document(
    *,
    sha: str = SHA,
    reason: str = "valid",
    verified: bool = True,
    repository: str = REPOSITORY,
    signature="bounded-signature",
    payload="bounded-payload",
) -> dict:
    owner, name = repository.removeprefix("https://github.com/").split("/", 1)
    return {
        "sha": sha,
        "html_url": f"https://github.com/{owner}/{name}/commit/{sha}",
        "commit": {
            "author": {"name": "Example", "email": "example@example.test", "date": "2026-10-17T11:00:00Z"},
            "committer": {"name": "Example", "email": "example@example.test", "date": "2026-10-17T11:00:00Z"},
            "verification": {
                "verified": verified,
                "reason": reason,
                "signature": signature,
                "payload": payload,
                "verified_at": "2026-10-17T11:59:00Z",
            },
        },
        "author": {"login": "example", "id": 1, "html_url": "https://github.com/example", "type": "User"},
        "committer": {"login": "example", "id": 1, "html_url": "https://github.com/example", "type": "User"},
    }


def _adapter(document: dict, *, now=NOW, content_type="application/json", status=200):
    return GithubCommitSignatureAdapter(
        response_fetcher=lambda _url: GithubResponse(
            canonicalize_jcs(document),
            content_type=content_type,
            status_code=status,
        ),
        clock=lambda: now,
    )


def test_subject_requires_canonical_github_url_and_full_commit_sha():
    subject = PublisherEvidenceSubject.create("https://github.com/example/demo.git", SHA.upper())
    assert subject.repository_url == REPOSITORY
    assert subject.commit_sha == SHA
    with pytest.raises(PublisherEvidenceError, match="full 40-character"):
        PublisherEvidenceSubject.create(REPOSITORY, "main")
    with pytest.raises(PublisherEvidenceError):
        PublisherEvidenceSubject.create("https://evil.example/example/demo", SHA)


def test_adapter_constructs_exact_endpoint_and_preserves_provider_only_verification():
    seen = []
    adapter = GithubCommitSignatureAdapter(
        response_fetcher=lambda url: seen.append(url) or GithubResponse(canonicalize_jcs(_document())),
        clock=lambda: NOW,
    )

    result = adapter.check(PublisherEvidenceSubject.create(REPOSITORY, SHA))

    assert seen == [f"https://api.github.com/repos/example/demo/commits/{SHA}"]
    assert result.observation_status is EvidenceObservationStatus.OBSERVED
    assert result.provider_status is GithubProviderStatus.VERIFIED
    assert result.publisher_auth_status is PublisherAuthStatus.UNKNOWN_IDENTITY
    assert result.identity_policy_status is IdentityPolicyStatus.UNCONFIGURED
    assert result.local_signature_status is LocalSignatureStatus.NOT_PERFORMED
    assert result.coverage == "git_commit"
    assert result.signature_digest
    assert result.payload_digest
    assert "bounded-signature" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("reason", "verified", "provider_status", "publisher_status"),
    [
        ("unsigned", False, GithubProviderStatus.UNSIGNED, PublisherAuthStatus.UNSIGNED),
        ("invalid", False, GithubProviderStatus.INVALID, PublisherAuthStatus.INVALID),
        ("unknown_key", False, GithubProviderStatus.UNVERIFIED, PublisherAuthStatus.INVALID),
        ("unknown_signature_type", False, GithubProviderStatus.UNSUPPORTED, PublisherAuthStatus.NOT_CHECKED),
    ],
)
def test_provider_reason_mapping_stays_separate_from_catalog_policy(reason, verified, provider_status, publisher_status):
    result = _adapter(_document(reason=reason, verified=verified)).check(PublisherEvidenceSubject.create(REPOSITORY, SHA))

    assert result.provider_status is provider_status
    assert result.publisher_auth_status is publisher_status
    assert result.observation_status is EvidenceObservationStatus.OBSERVED


@pytest.mark.parametrize(
    "document_factory",
    [
        lambda: _document(signature=None, payload=None),
        lambda: _document(signature="", payload=""),
        lambda: {**_document(), "commit": {"verification": {}}},
        lambda: {**_document(), "commit": {key: value for key, value in _document()["commit"].items() if key != "verification"}},
    ],
)
def test_absent_signature_metadata_is_observed_as_unsigned(document_factory):
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)

    result = _adapter(document_factory()).check(subject)

    assert result.observation_status is EvidenceObservationStatus.OBSERVED
    assert result.provider_status is GithubProviderStatus.UNSIGNED
    assert result.publisher_auth_status is PublisherAuthStatus.UNSIGNED
    assert result.reason_code == "publisher_signature_missing"
    assert result.signature_digest is None
    assert result.payload_digest is None


@pytest.mark.parametrize("reason", ["gpgverify_unavailable", "gpgverify_error", "future_provider_reason"])
def test_provider_unavailable_or_unknown_is_not_reported_as_invalid(reason):
    result = _adapter(_document(reason=reason, verified=False)).check(
        PublisherEvidenceSubject.create(REPOSITORY, SHA)
    )

    assert result.provider_status in {
        GithubProviderStatus.UNAVAILABLE,
        GithubProviderStatus.PROVIDER_ERROR,
        GithubProviderStatus.UNKNOWN,
    }
    assert result.publisher_auth_status is PublisherAuthStatus.NOT_CHECKED


def test_subject_constructor_and_commit_url_reject_noncanonical_authority():
    with pytest.raises(PublisherEvidenceError):
        PublisherEvidenceSubject(REPOSITORY, SHA.upper())

    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    document = _document()
    document["html_url"] = f"https://user@github.com:4443/example/demo/commit/{SHA}"
    assert _adapter(document).check(subject).reason_code == "publisher_subject_mismatch"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), None, 61.0])
def test_adapter_rejects_unbounded_timeout(timeout):
    with pytest.raises(PublisherEvidenceError, match="timeout"):
        GithubCommitSignatureAdapter(timeout=timeout)


def test_subject_mismatch_redirect_size_content_type_and_transport_states():
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    mismatch = _adapter(_document(sha="b" * 40)).check(subject)
    assert mismatch.reason_code == "publisher_subject_mismatch"
    redirect = _adapter(_document(), status=302).check(subject)
    assert redirect.reason_code == "publisher_redirect_rejected"
    content_type = _adapter(_document(), content_type="text/html").check(subject)
    assert content_type.reason_code == "publisher_content_type_invalid"
    oversized = GithubCommitSignatureAdapter(
        response_fetcher=lambda _url: GithubResponse(b"x" * (512 * 1024 + 1)),
        clock=lambda: NOW,
    ).check(subject)
    assert oversized.reason_code == "publisher_response_oversized"
    unavailable = GithubCommitSignatureAdapter(
        response_fetcher=lambda _url: GithubResponse(b"", status_code=429),
        clock=lambda: NOW,
    ).check(subject)
    assert unavailable.observation_status is EvidenceObservationStatus.UNAVAILABLE
    assert unavailable.publisher_auth_status is PublisherAuthStatus.NOT_CHECKED


def test_duplicate_json_and_contradictory_provider_fields_fail_closed():
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    duplicate = b'{"sha":"' + SHA.encode() + b'","sha":"' + ("b" * 40).encode() + b'"}'
    adapter = GithubCommitSignatureAdapter(
        response_fetcher=lambda _url: GithubResponse(duplicate),
        clock=lambda: NOW,
    )
    assert adapter.check(subject).reason_code == "publisher_response_invalid"
    contradiction = _document(verified=True, reason="invalid")
    assert _adapter(contradiction).check(subject).reason_code == "publisher_response_invalid"


def test_store_binds_subject_digest_and_marks_old_observations_stale(tmp_path: Path):
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    result = _adapter(_document(), now=NOW).check(subject)
    clock_value = [NOW]
    store = PublisherEvidenceStore(tmp_path, clock=lambda: clock_value[0])
    store.write(result)
    cached = store.get(subject)
    assert cached is not None
    assert cached.freshness == "cached"
    clock_value[0] = NOW + EVIDENCE_MAX_AGE + timedelta(seconds=1)
    stale = store.get(subject)
    assert stale is not None
    assert stale.freshness == "stale"
    assert store.get(PublisherEvidenceSubject.create(REPOSITORY, "b" * 40)) is None


def test_store_rejects_duplicate_oversized_and_symlinked_state(tmp_path: Path):
    store = PublisherEvidenceStore(tmp_path / "store")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"version":1,"version":1,"records":{}}', encoding="utf-8")
    with pytest.raises(PublisherEvidenceError):
        store.get(PublisherEvidenceSubject.create(REPOSITORY, SHA))
    store.path.write_bytes(b"x" * (512 * 1024 + 1))
    with pytest.raises(PublisherEvidenceError):
        store.get(PublisherEvidenceSubject.create(REPOSITORY, SHA))
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(PublisherEvidenceError):
        PublisherEvidenceStore(linked)


def test_store_rejects_post_write_size_and_record_limits(tmp_path: Path, monkeypatch):
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    result = _adapter(_document(), now=NOW).check(subject)
    store = PublisherEvidenceStore(tmp_path / "store")

    monkeypatch.setattr(publisher_evidence, "MAX_EVIDENCE_BYTES", 128)
    with pytest.raises(PublisherEvidenceError, match="size limit"):
        store.write(result)

    monkeypatch.setattr(publisher_evidence, "MAX_EVIDENCE_BYTES", 512 * 1024)
    monkeypatch.setattr(publisher_evidence, "MAX_EVIDENCE_RECORDS", 1)
    store.write(result)
    second_subject = PublisherEvidenceSubject.create(REPOSITORY, "b" * 40)
    second = _adapter(_document(sha=second_subject.commit_sha), now=NOW).check(second_subject)
    with pytest.raises(PublisherEvidenceError, match="too many records"):
        store.write(second)


@pytest.mark.parametrize(
    "field_value",
    [
        {"publisher_auth_status": PublisherAuthStatus.VERIFIED},
        {"identity_policy_status": IdentityPolicyStatus.MATCHED},
        {"local_signature_status": LocalSignatureStatus.VERIFIED},
    ],
)
def test_store_rejects_recomputed_semantically_positive_records(tmp_path: Path, field_value):
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    result = _adapter(_document(), now=NOW).check(subject)
    forged = replace(result, **field_value)
    store = PublisherEvidenceStore(tmp_path / "store")

    with pytest.raises(PublisherEvidenceError, match="publisher evidence"):
        store.write(forged)

    store.root.mkdir(parents=True, exist_ok=True)
    forged_record = result.to_dict()
    for key, value in field_value.items():
        forged_record[key] = value.value
    digest_basis = dict(forged_record)
    digest_basis.pop("evidence_digest", None)
    digest_basis.pop("freshness", None)
    forged_record["evidence_digest"] = hashlib.sha256(canonicalize_jcs(digest_basis)).hexdigest()
    store.path.write_bytes(canonicalize_jcs({"version": 1, "records": {subject.key: forged_record}}) + b"\n")
    assert store.get(subject) is None


def test_store_rejects_symlinked_nested_ancestor_and_lock(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(PublisherEvidenceError):
        PublisherEvidenceStore(linked / "nested" / "store")

    store = PublisherEvidenceStore(tmp_path / "regular")
    store.root.mkdir(parents=True, exist_ok=True)
    lock_target = tmp_path / "lock-target"
    lock_target.write_bytes(b"lock")
    try:
        store.lock_path.symlink_to(lock_target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with pytest.raises(PublisherEvidenceError):
        store.get(PublisherEvidenceSubject.create(REPOSITORY, SHA))


def test_store_cleans_exclusive_temp_file_when_replace_fails(tmp_path: Path, monkeypatch):
    subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    result = _adapter(_document(), now=NOW).check(subject)
    store = PublisherEvidenceStore(tmp_path / "store")

    def fail_replace(_source, _destination):
        raise OSError("replacement blocked")

    monkeypatch.setattr(publisher_evidence.os, "replace", fail_replace)
    with pytest.raises(PublisherEvidenceError, match="write failed"):
        store.write(result)
    assert list(store.root.glob(".publisher_evidence.json.*.tmp")) == []


def test_bounded_read_rejects_bytes_after_open_even_when_stat_is_small(tmp_path: Path, monkeypatch):
    path = tmp_path / "publisher_evidence.json"
    path.write_bytes(b"x")
    requested_sizes = []

    def oversized_read(_descriptor, size):
        requested_sizes.append(size)
        return b"x" * (size + 1)

    monkeypatch.setattr(publisher_evidence.os, "read", oversized_read)
    with pytest.raises(PublisherEvidenceError, match="oversized"):
        publisher_evidence._bounded_read_file(path, 4)
    assert requested_sizes == [5]


class _FakeEvidenceAdapter:
    def __init__(self, result):
        self.result = result
        self.subjects = []

    def check(self, subject):
        self.subjects.append(subject)
        return self.result


def test_manager_evidence_is_metadata_only_and_does_not_change_catalog_or_registry(tmp_path: Path):
    subject = PublisherEvidenceSubject.create(
        "https://github.com/morluto/rea",
        "4e14452ddbf4642235c8a68435f72837510b2075",
    )
    result = _adapter(_document(sha=subject.commit_sha, repository=subject.repository_url), now=NOW).check(subject)
    adapter = _FakeEvidenceAdapter(result)
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        publisher_evidence_adapter=adapter,
    )
    catalog_before = (tmp_path / "plugins" / "catalog.json").read_bytes() if (tmp_path / "plugins" / "catalog.json").exists() else None
    registry_before = (tmp_path / "plugins" / "registry.json").read_bytes() if (tmp_path / "plugins" / "registry.json").exists() else None

    observed = manager.check_publisher_evidence("rea")

    assert adapter.subjects == [subject]
    assert observed["publisher_auth_status"] == "unknown_identity"
    assert observed["publisher_evidence_provider_status"] == "verified"
    assert observed["catalog_auth_status"] == "unsigned"
    assert observed["policy_status"] == "unreviewed"
    assert observed["execution_enabled"] is False
    assert (tmp_path / "plugins" / "publisher_evidence.json").is_file()
    assert not (tmp_path / "plugins" / "registry.json").exists() or (tmp_path / "plugins" / "registry.json").read_bytes() == registry_before
    assert not (tmp_path / "plugins" / "catalog.json").exists() or (tmp_path / "plugins" / "catalog.json").read_bytes() == catalog_before

    info = manager.info("rea")
    assert info is not None
    assert info["publisher_evidence_provider_status"] == "verified"
    assert info["catalog_auth_status"] == "unsigned"
    assert info["execution_enabled"] is False


def test_manager_rejects_adapter_result_for_a_different_subject(tmp_path: Path):
    wrong_subject = PublisherEvidenceSubject.create(REPOSITORY, SHA)
    wrong_result = _adapter(_document(), now=NOW).check(wrong_subject)
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        publisher_evidence_adapter=_FakeEvidenceAdapter(wrong_result),
    )

    with pytest.raises(PluginManagerError, match="different subject"):
        manager.check_publisher_evidence("rea")


def test_manager_raw_ref_evidence_does_not_fetch_archive_or_mutate_registry(tmp_path: Path):
    source = "https://github.com/example/demo"
    commit = "b" * 40
    archive_called = False

    def json_fetcher(url):
        assert url.endswith("/commits/main")
        return {"sha": commit}

    def bytes_fetcher(_url):
        nonlocal archive_called
        archive_called = True
        raise AssertionError("publisher evidence must not fetch a source archive")

    subject = PublisherEvidenceSubject.create(source, commit)
    result = _adapter(_document(sha=commit, repository=source), now=NOW).check(subject)
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=json_fetcher,
        bytes_fetcher=bytes_fetcher,
        publisher_evidence_adapter=_FakeEvidenceAdapter(result),
    )

    observed = manager.check_publisher_evidence(source, ref="main")

    assert observed["execution_enabled"] is False
    assert archive_called is False
    assert not (tmp_path / "plugins" / "registry.json").exists()
    assert not (tmp_path / "plugins" / "catalog.json").exists()


def _interrupted_install_root(root: Path) -> dict[str, bytes]:
    root.mkdir()
    staging = root / ".staging"
    target = root / "demo"
    backup = staging / "backup"
    candidate = staging / "candidate"
    for path, content in ((target, b"managed-source"), (backup, b"backup-source"), (candidate, b"candidate-source")):
        path.mkdir(parents=True)
        (path / "sentinel.txt").write_bytes(content)
    (root / "registry.json").write_text('{"version":1,"plugins":{}}', encoding="utf-8")
    journal = {
        "plugin_id": "demo",
        "operation": "install",
        "phase": "backup-moved",
        "backup": ".staging/backup",
        "candidate": ".staging/candidate",
        "new_archive_sha256": "f" * 64,
    }
    (root / ".transaction.json").write_text(json.dumps(journal), encoding="utf-8")
    return {
        "journal": (root / ".transaction.json").read_bytes(),
        "target": (target / "sentinel.txt").read_bytes(),
        "backup": (backup / "sentinel.txt").read_bytes(),
        "candidate": (candidate / "sentinel.txt").read_bytes(),
    }


def test_evidence_only_manager_does_not_recover_or_create_lifecycle_state(tmp_path: Path):
    root = tmp_path / "plugins"
    before = _interrupted_install_root(root)
    source = "https://github.com/example/demo"
    subject = PublisherEvidenceSubject.create(source, SHA)
    result = _adapter(_document(repository=source), now=NOW).check(subject)
    manager = PluginCatalogManager(
        root=root,
        evidence_only=True,
        publisher_evidence_adapter=_FakeEvidenceAdapter(result),
    )

    observed = manager.check_publisher_evidence(source, ref=SHA)

    assert observed["execution_enabled"] is False
    assert (root / ".transaction.json").read_bytes() == before["journal"]
    assert (root / "demo" / "sentinel.txt").read_bytes() == before["target"]
    assert (root / ".staging" / "backup" / "sentinel.txt").read_bytes() == before["backup"]
    assert (root / ".staging" / "candidate" / "sentinel.txt").read_bytes() == before["candidate"]
    assert not (root / ".manager.lock").exists()
    assert not (root / "signed_catalog_cache.json").exists()
    assert (root / "publisher_evidence.json").is_file()


def test_evidence_only_storage_failure_does_not_recover_lifecycle_state(tmp_path: Path):
    root = tmp_path / "plugins"
    before = _interrupted_install_root(root)
    source = "https://github.com/example/demo"
    subject = PublisherEvidenceSubject.create(source, SHA)
    result = _adapter(_document(repository=source), now=NOW).check(subject)

    class FailingStore:
        def write(self, _result):
            raise PublisherEvidenceError("evidence storage unavailable")

        def get(self, _subject):
            return None

    manager = PluginCatalogManager(
        root=root,
        evidence_only=True,
        publisher_evidence_adapter=_FakeEvidenceAdapter(result),
        publisher_evidence_store=FailingStore(),
    )

    with pytest.raises(PluginManagerError, match="storage unavailable"):
        manager.check_publisher_evidence(source, ref=SHA)
    assert (root / ".transaction.json").read_bytes() == before["journal"]
    assert (root / "demo" / "sentinel.txt").read_bytes() == before["target"]
    assert (root / ".staging" / "backup" / "sentinel.txt").read_bytes() == before["backup"]
    assert (root / ".staging" / "candidate" / "sentinel.txt").read_bytes() == before["candidate"]
    assert not (root / ".manager.lock").exists()


def test_evidence_only_manager_creates_only_evidence_state_on_fresh_root(tmp_path: Path):
    root = tmp_path / "fresh-plugins"
    source = "https://github.com/example/demo"
    subject = PublisherEvidenceSubject.create(source, SHA)
    result = _adapter(_document(repository=source), now=NOW).check(subject)
    manager = PluginCatalogManager(
        root=root,
        evidence_only=True,
        publisher_evidence_adapter=_FakeEvidenceAdapter(result),
    )

    assert not root.exists()
    manager.check_publisher_evidence(source, ref=SHA)

    assert (root / "publisher_evidence.json").is_file()
    assert not (root / ".staging").exists()
    assert not (root / ".manager.lock").exists()
    assert not (root / ".transaction.json").exists()
    assert not (root / "signed_catalog_cache.json").exists()
