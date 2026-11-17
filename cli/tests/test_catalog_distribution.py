"""Offline Phase 2 signed-catalog transport, cache, and LKG tests."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aetherion_cli.catalog_distribution import (
    CatalogFetch,
    CatalogCacheError,
    CatalogTransport,
    CatalogTransportError,
    SignedCatalogCache,
    SignedCatalogService,
)
from aetherion_cli.plugin_manager import CURATED_CATALOG, PluginCatalogManager, PluginManagerError
from aetherion_cli.trust_store import UnconfiguredCatalogKeyProvider
from aetherion_cli.trust_verifier import (
    CatalogAuthStatus,
    CatalogKey,
    CatalogReason,
    MappingCatalogKeyProvider,
    canonicalize_jcs,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
KEY_ID = "catalog-test"
COMMIT = "a" * 40
ARCHIVE_SHA = "b" * 64
MANIFEST_SHA = "c" * 64
ENDPOINT = "https://catalog.example.test/catalog.json"


def _payload(*, sequence: int = 1, issued_at: str = "2026-10-17T11:00:00Z", expires_at: str = "2026-10-18T12:00:00Z"):
    return {
        "format": "aetherion.catalog.payload.v1",
        "catalog_id": "aetherion-test",
        "sequence": sequence,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "entries": [
            {
                "plugin_id": "rea",
                "repository": {
                    "canonical_url": "https://github.com/morluto/rea",
                    "owner": "morluto",
                    "name": "rea",
                },
                "selection": {"commit_sha": COMMIT, "ref_policy": "curated_commit_only"},
                "archive": {
                    "format": "github-codeload-zip",
                    "sha256": ARCHIVE_SHA,
                    "size_bytes": 1234,
                },
                "manifest": {
                    "path": "package.json",
                    "sha256": MANIFEST_SHA,
                    "schema_version": "",
                    "plugin_api_version": "",
                },
                "policy": {
                    "compatibility": "unknown",
                    "execution_enabled": False,
                    "publisher_evidence": "optional",
                    "review_status": "approved",
                },
            }
        ],
    }


def _provider(private_key: Ed25519PrivateKey, *, revoked: bool = False):
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return MappingCatalogKeyProvider(
        {
            KEY_ID: CatalogKey(
                key_id=KEY_ID,
                public_key=public_key,
                scopes=frozenset({"catalog"}),
                revoked=revoked,
            )
        }
    )


def _signed(payload=None, private_key=None):
    private_key = private_key or Ed25519PrivateKey.generate()
    payload = copy.deepcopy(payload or _payload())
    canonical = canonicalize_jcs(payload)
    envelope = {
        "format": "aetherion.catalog.envelope.v1",
        "key_id": KEY_ID,
        "algorithm": "Ed25519",
        "canonicalization": "RFC8785-JCS",
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "payload": payload,
        "signature_base64": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    return envelope, private_key


def _service(tmp_path: Path, raw: bytes, private_key: Ed25519PrivateKey, *, now=NOW, content_type="application/json"):
    transport = CatalogTransport(
        ENDPOINT,
        fetcher=lambda _endpoint: CatalogFetch(raw, ENDPOINT, content_type),
    )
    return SignedCatalogService(
        tmp_path / "plugins",
        ENDPOINT,
        _provider(private_key),
        transport=transport,
        clock=lambda: now,
    )


def test_valid_signed_catalog_is_verified_and_cached_atomically(tmp_path):
    envelope, private_key = _signed()
    raw = json.dumps(envelope).encode("utf-8")
    service = _service(tmp_path, raw, private_key)

    snapshot = service.refresh()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert snapshot.freshness == "fresh"
    assert snapshot.result.sequence == 1
    assert snapshot.result.authority_key_id == KEY_ID
    assert (tmp_path / "plugins" / "signed_catalog_cache.json").is_file()
    assert list((tmp_path / "plugins").glob(".*.tmp")) == []


def test_cached_envelope_reverifies_after_restart(tmp_path):
    envelope, private_key = _signed()
    raw = json.dumps(envelope).encode("utf-8")
    _service(tmp_path, raw, private_key).refresh()

    restarted = SignedCatalogService(
        tmp_path / "plugins",
        ENDPOINT,
        _provider(private_key),
        transport=CatalogTransport(ENDPOINT, fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("offline"))),
        clock=lambda: NOW,
    )
    snapshot = restarted.load_cached()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert snapshot.freshness == "cached"
    assert snapshot.source == "cache"


def test_unknown_root_never_fetches_or_approves(tmp_path):
    called = []
    transport = CatalogTransport(ENDPOINT, fetcher=lambda _url: called.append(True) or b"{}")
    service = SignedCatalogService(
        tmp_path / "plugins",
        ENDPOINT,
        UnconfiguredCatalogKeyProvider(),
        transport=transport,
        clock=lambda: NOW,
    )

    snapshot = service.refresh()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.UNSIGNED
    assert snapshot.result.reason_code is CatalogReason.TRUST_ROOT_UNCONFIGURED
    assert called == []


@pytest.mark.parametrize("content_type", ["text/html", "application/octet-stream", "text/plain", ""])
def test_catalog_transport_rejects_non_json_content_type(content_type):
    transport = CatalogTransport(
        ENDPOINT,
        fetcher=lambda _url: CatalogFetch(b"{}", ENDPOINT, content_type),
    )

    with pytest.raises(CatalogTransportError) as error:
        transport.fetch()
    assert error.value.reason is CatalogReason.CONTENT_TYPE_INVALID


def test_catalog_transport_rejects_oversized_injected_body():
    transport = CatalogTransport(
        ENDPOINT,
        fetcher=lambda _url: CatalogFetch(b"x" * (2 * 1024 * 1024 + 1), ENDPOINT, "application/json"),
    )

    with pytest.raises(CatalogTransportError) as error:
        transport.fetch()
    assert error.value.reason is CatalogReason.RESPONSE_OVERSIZED


@pytest.mark.parametrize("status_code", [204, 301, 302, 307, 308, 404, 500])
def test_http_catalog_transport_rejects_non_success_and_redirect_responses(status_code):
    def handler(_request):
        headers = {"location": "https://other.example/catalog.json"} if 300 <= status_code < 400 else {}
        return httpx.Response(status_code, headers=headers, content=b"{}")

    transport = CatalogTransport(
        ENDPOINT,
        client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(CatalogTransportError) as error:
        transport.fetch()
    assert error.value.reason in {CatalogReason.TRANSPORT_FAILURE, CatalogReason.REDIRECT_REJECTED}


def test_invalid_refresh_retains_last_known_good_cache(tmp_path):
    envelope, private_key = _signed()
    raw = json.dumps(envelope).encode("utf-8")
    service = _service(tmp_path, raw, private_key)
    assert service.refresh().result.verified
    before = (tmp_path / "plugins" / "signed_catalog_cache.json").read_bytes()

    invalid = copy.deepcopy(envelope)
    invalid["signature_base64"] = base64.b64encode(b"x" * 64).decode("ascii")
    failed = _service(tmp_path, json.dumps(invalid).encode("utf-8"), private_key).refresh()

    assert failed.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert failed.refresh_failure_reason == CatalogReason.SIGNATURE_INVALID.value
    assert failed.refresh_failure_reason == CatalogReason.SIGNATURE_INVALID.value
    assert failed.freshness == "cached"
    assert (tmp_path / "plugins" / "signed_catalog_cache.json").read_bytes() == before


def test_sequence_rollback_retains_last_known_good_cache(tmp_path):
    first, private_key = _signed(_payload(sequence=2))
    _service(tmp_path, json.dumps(first).encode(), private_key).refresh()
    rollback, _ = _signed(_payload(sequence=1), private_key)

    snapshot = _service(tmp_path, json.dumps(rollback).encode(), private_key).refresh()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert snapshot.refresh_failure_reason == CatalogReason.SEQUENCE_ROLLBACK.value
    assert snapshot.freshness == "cached"
    assert snapshot.result.sequence == 2


def test_expired_cached_catalog_is_not_approval(tmp_path):
    envelope, private_key = _signed(
        _payload(expires_at="2026-10-17T11:59:59Z")
    )
    raw = json.dumps(envelope).encode()
    _service(
        tmp_path,
        raw,
        private_key,
        now=datetime(2026, 10, 17, 11, 0, tzinfo=timezone.utc),
    ).refresh()
    snapshot = _service(tmp_path, raw, private_key).load_cached()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.EXPIRED
    assert snapshot.freshness == "expired"
    assert snapshot.usable_for_curated_policy is False


def test_forged_cache_metadata_does_not_override_signed_envelope(tmp_path):
    envelope, private_key = _signed()
    raw = json.dumps(envelope).encode()
    service = _service(tmp_path, raw, private_key)
    service.refresh()
    cache_path = tmp_path / "plugins" / "signed_catalog_cache.json"
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["last_good"]["key_id"] = "attacker-key"
    document["last_good"]["payload_sha256"] = "0" * 64
    cache_path.write_text(json.dumps(document), encoding="utf-8")

    snapshot = service.load_cached()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert snapshot.result.authority_key_id == KEY_ID
    assert snapshot.result.canonical_payload_sha256 == envelope["payload_sha256"]


def _rea_archive(manifest: bytes) -> bytes:
    buffer = io.BytesIO()
    root = f"rea-{CURATED_CATALOG[0].verified_commit[:8]}"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root}/package.json", manifest)
    return buffer.getvalue()


def test_valid_signed_curated_statement_authorizes_exact_metadata_only_install(tmp_path):
    manifest = json.dumps(
        {
            "name": "rea-agents",
            "version": "6.5.0",
            "description": "REA metadata fixture",
            "author": "morluto",
            "license": "MIT",
            "repository": {"url": "https://github.com/morluto/rea.git"},
        }
    ).encode("utf-8")
    archive = _rea_archive(manifest)
    payload = _payload()
    payload["entries"][0]["selection"]["commit_sha"] = CURATED_CATALOG[0].verified_commit
    payload["entries"][0]["archive"]["sha256"] = hashlib.sha256(archive).hexdigest()
    payload["entries"][0]["archive"]["size_bytes"] = len(archive)
    payload["entries"][0]["manifest"]["sha256"] = hashlib.sha256(manifest).hexdigest()
    envelope, private_key = _signed(payload)
    raw = json.dumps(envelope).encode("utf-8")
    service = _service(tmp_path, raw, private_key)

    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("curated pin must not resolve a mutable ref")),
        bytes_fetcher=lambda url: archive,
        catalog_service=service,
    )
    installed = manager.install("rea", ref="main")

    assert installed["resolved_commit"] == CURATED_CATALOG[0].verified_commit
    assert installed["catalog_auth_status"] == "verified"
    assert installed["policy_status"] == "catalog_approved"
    assert installed["integrity"] == "verified"
    assert installed["execution_enabled"] is False


def test_curated_archive_digest_mismatch_is_rejected_before_promotion(tmp_path):
    manifest = json.dumps(
        {
            "name": "rea-agents",
            "version": "6.5.0",
            "license": "MIT",
            "repository": {"url": "https://github.com/morluto/rea.git"},
        }
    ).encode("utf-8")
    signed_archive = _rea_archive(manifest)
    payload = _payload()
    payload["entries"][0]["selection"]["commit_sha"] = CURATED_CATALOG[0].verified_commit
    payload["entries"][0]["archive"]["sha256"] = hashlib.sha256(signed_archive).hexdigest()
    payload["entries"][0]["archive"]["size_bytes"] = len(signed_archive)
    payload["entries"][0]["manifest"]["sha256"] = hashlib.sha256(manifest).hexdigest()
    envelope, private_key = _signed(payload)
    service = _service(tmp_path, json.dumps(envelope).encode(), private_key)
    changed_archive = signed_archive + b"changed"
    manager = PluginCatalogManager(
        root=tmp_path / "plugins",
        json_fetcher=lambda _url: (_ for _ in ()).throw(AssertionError("curated pin must not resolve a mutable ref")),
        bytes_fetcher=lambda _url: changed_archive,
        catalog_service=service,
    )

    with pytest.raises(PluginManagerError, match="requires a valid signed catalog"):
        manager.install("rea")
    assert not (tmp_path / "plugins" / "rea").exists()


def test_forged_user_catalog_fields_cannot_bootstrap_curated_approval(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "user-alias": {
                        "id": "user-alias",
                        "source_url": "https://github.com/example/demo-plugin",
                        "requested_ref": "main",
                        "resolved_commit": COMMIT,
                        "source_plugin_id": "demo-plugin",
                        "compatibility_status": "unknown",
                        "catalog_auth_status": "verified",
                        "policy_status": "catalog_approved",
                        "catalog_key_id": "attacker-key",
                        "catalog_sequence": 999,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    item = PluginCatalogManager(root=root).catalog_list()[0]

    assert item["catalog_auth_status"] == "unsigned"
    assert item["policy_status"] == "unreviewed"
    assert item.get("catalog_sequence") is None
    assert item.get("signed_catalog_id") is None


def test_cache_publication_failure_preserves_last_known_good(monkeypatch, tmp_path):
    first, private_key = _signed(_payload(sequence=1))
    raw_first = json.dumps(first).encode()
    service = _service(tmp_path, raw_first, private_key)
    assert service.refresh().result.verified
    cache_path = tmp_path / "plugins" / "signed_catalog_cache.json"
    before = cache_path.read_bytes()

    second, _ = _signed(_payload(sequence=2), private_key)
    service = _service(tmp_path, json.dumps(second).encode(), private_key)
    import aetherion_cli.catalog_distribution as distribution

    def fail_replace(_source, _target):
        raise OSError("simulated cache publication failure")

    monkeypatch.setattr(distribution.os, "replace", fail_replace)
    snapshot = service.refresh()

    assert snapshot.freshness == "cached"
    assert snapshot.result.sequence == 1
    assert cache_path.read_bytes() == before


def test_corrupt_cache_is_not_treated_as_signed_approval(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "signed_catalog_cache.json").write_text('{"catalog_auth_status":"verified"}', encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    service = SignedCatalogService(
        root,
        ENDPOINT,
        _provider(private_key),
        transport=CatalogTransport(ENDPOINT, fetcher=lambda _url: b"{}"),
        clock=lambda: NOW,
    )

    snapshot = service.load_cached()

    assert snapshot.result.catalog_auth_status is CatalogAuthStatus.INVALID
    assert snapshot.result.reason_code is CatalogReason.CACHE_CORRUPT


def test_cache_rejects_symlinked_root_before_resolution(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(CatalogCacheError):
        SignedCatalogCache(linked)
