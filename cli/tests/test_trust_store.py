"""Tests for explicit, non-catalog trust-root provisioning."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aetherion_cli.trust_store import (
    ConfiguredCatalogKeyProvider,
    CorruptCatalogKeyProvider,
    LifecycleCatalogKeyProvider,
    UnconfiguredCatalogKeyProvider,
    load_catalog_key_provider,
)
from aetherion_cli.trust_verifier import TrustStoreCorruptError, canonicalize_jcs


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _keyset(private_key: Ed25519PrivateKey):
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "format": "aetherion.catalog.trust-root.v1",
        "keyset_id": "test-keyset",
        "keys": [
            {
                "key_id": "catalog-test",
                "algorithm": "Ed25519",
                "scopes": ["catalog"],
                "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                "revoked": False,
            }
        ],
    }


def test_explicit_keyset_loads_with_fingerprint_and_catalog_scope(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "catalog-trust-root.json"
    path.write_text(json.dumps(_keyset(private_key)), encoding="utf-8")

    provider = ConfiguredCatalogKeyProvider.from_file(path)

    assert provider.status.configured is True
    assert provider.status.provisioning_boundary == "explicit_configured_file"
    assert provider.status.fingerprint
    assert provider.get_key("catalog-test") is not None
    assert provider.get_key("catalog-test").scopes == frozenset({"catalog"})


def test_missing_path_is_unconfigured_and_not_a_developer_default(tmp_path):
    provider = load_catalog_key_provider("")
    missing = load_catalog_key_provider(str(tmp_path / "missing.json"))

    assert isinstance(provider, UnconfiguredCatalogKeyProvider)
    assert provider.status.configured is False
    assert isinstance(missing, CorruptCatalogKeyProvider)
    assert missing.status.reason_code == "trust_store_corrupt"


def test_malformed_keyset_is_corrupt_and_not_partially_loaded(tmp_path):
    path = tmp_path / "catalog-trust-root.json"
    path.write_text(
        json.dumps(
            {
                "format": "aetherion.catalog.trust-root.v1",
                "keyset_id": "bad",
                "keys": [
                    {
                        "key_id": "catalog-test",
                        "algorithm": "RSA",
                        "scopes": ["catalog"],
                        "public_key_base64": "bad",
                        "revoked": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    provider = load_catalog_key_provider(str(path))

    assert isinstance(provider, CorruptCatalogKeyProvider)
    try:
        provider.get_key("catalog-test")
    except TrustStoreCorruptError:
        pass
    else:
        raise AssertionError("corrupt trust root unexpectedly returned a key")


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _root_document(private_key: Ed25519PrivateKey, key_id: str = "root-key") -> dict:
    return {
        "format": "aetherion.catalog.trust-root.v1",
        "keyset_id": "offline-root-v1",
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "scopes": ["keyset", "root-replacement"],
                "public_key_base64": base64.b64encode(_public_key(private_key)).decode("ascii"),
                "revoked": False,
            }
        ],
    }


def _delegated_keyset(
    root_private_key: Ed25519PrivateKey,
    catalog_private_key: Ed25519PrivateKey,
    *,
    keyset_id: str,
    sequence: int,
    parent_keyset_id: str,
    root_key_id: str = "root-key",
    revoked: bool = False,
    expires_at: str = "2026-02-01T00:00:00Z",
) -> dict:
    payload = {
        "format": "aetherion.catalog.keyset.payload.v1",
        "keyset_id": keyset_id,
        "sequence": sequence,
        "parent_keyset_id": parent_keyset_id,
        "issued_at": "2025-12-31T00:00:00Z",
        "expires_at": expires_at,
        "keys": [
            {
                "key_id": "catalog-key",
                "algorithm": "Ed25519",
                "scopes": ["catalog"],
                "public_key_base64": base64.b64encode(_public_key(catalog_private_key)).decode("ascii"),
                "revoked": revoked,
                "valid_from": None,
                "valid_until": None,
                "revocation_reason": "operator revocation" if revoked else "",
            }
        ],
    }
    payload_bytes = canonicalize_jcs(payload)
    return {
        "format": "aetherion.catalog.keyset.envelope.v1",
        "key_id": root_key_id,
        "algorithm": "Ed25519",
        "canonicalization": "RFC8785-JCS",
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload": payload,
        "signature_base64": base64.b64encode(root_private_key.sign(payload_bytes)).decode("ascii"),
    }


def _write_json(path, value):
    path.write_bytes(canonicalize_jcs(value) + b"\n")


def _root_transition(
    root_private_key: Ed25519PrivateKey,
    *,
    old_root_fingerprint: str,
    new_root_fingerprint: str,
    new_keyset_fingerprint: str,
    root_generation: int,
    root_key_id: str = "root-key",
) -> dict:
    payload = {
        "format": "aetherion.catalog.root-transition.payload.v1",
        "old_root_fingerprint": old_root_fingerprint,
        "new_root_fingerprint": new_root_fingerprint,
        "new_keyset_fingerprint": new_keyset_fingerprint,
        "root_generation": root_generation,
        "issued_at": "2025-12-31T00:00:00Z",
        "expires_at": "2026-02-01T00:00:00Z",
    }
    payload_bytes = canonicalize_jcs(payload)
    return {
        "format": "aetherion.catalog.root-transition.envelope.v1",
        "key_id": root_key_id,
        "algorithm": "Ed25519",
        "canonicalization": "RFC8785-JCS",
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload": payload,
        "signature_base64": base64.b64encode(root_private_key.sign(payload_bytes)).decode("ascii"),
    }


def test_root_signed_keyset_is_explicitly_activated_and_persisted(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(candidate_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))

    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    loaded = load_catalog_key_provider(
        str(root_path),
        str(keyset_path),
        str(state_path),
        clock=lambda: NOW,
    )
    assert isinstance(loaded, LifecycleCatalogKeyProvider)

    candidate_status = provider.validate_candidate(candidate_path)
    assert candidate_status.provisioning_boundary == "candidate_not_activated"
    assert not keyset_path.exists()

    status = provider.apply_candidate(candidate_path, expected_root_fingerprint=root_fingerprint, confirmed=True)

    assert status.configured is True
    assert status.keyset_id == "ks-1"
    assert status.keyset_sequence == 1
    assert provider.get_key("catalog-key") is not None
    assert state_path.exists()
    assert provider.status.keyset_sequence == 1


def test_keyset_signature_tampering_and_expiry_fail_closed(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    tampered_path = tmp_path / "tampered.json"
    _write_json(root_path, _root_document(root_private))
    candidate = _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id="")
    _write_json(candidate_path, candidate)
    tampered = dict(candidate)
    tampered["signature_base64"] = base64.b64encode(b"x" * 64).decode("ascii")
    _write_json(tampered_path, tampered)
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)

    try:
        provider.validate_candidate(tampered_path)
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "keyset_signature_invalid"
    else:
        raise AssertionError("tampered delegated keyset was accepted")

    provider.apply_candidate(
        candidate_path,
        expected_root_fingerprint=ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint,
        confirmed=True,
    )
    expired = LifecycleCatalogKeyProvider(
        root_path,
        keyset_path,
        state_path,
        clock=lambda: datetime(2026, 2, 2, tzinfo=timezone.utc),
    )
    assert expired.status.configured is False
    assert expired.status.reason_code == "keyset_expired"


def test_rotation_rejects_rollback_but_explicit_recovery_can_restore_backup(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(second_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-2", sequence=2, parent_keyset_id="ks-1"))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint

    provider.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    provider.apply_candidate(second_path, expected_root_fingerprint=root_fingerprint, confirmed=True)

    try:
        provider.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "keyset_sequence_rollback"
    else:
        raise AssertionError("ordinary keyset activation accepted a rollback")

    recovered = provider.recover_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    assert recovered.keyset_sequence == 1
    assert recovered.last_transition == "operator_recovery_rollback"
    assert "operator_recovery_rollback" in (tmp_path / "state.history.jsonl").read_text(encoding="utf-8")


def test_root_replacement_requires_confirmation_and_validates_new_pair(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    new_root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    new_root_path = tmp_path / "new-root.json"
    new_keyset_path = tmp_path / "new-keyset.json"
    transition_path = tmp_path / "transition.json"
    _write_json(root_path, _root_document(root_private, "root-key"))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(new_root_path, _root_document(new_root_private, "new-root-key"))
    _write_json(new_keyset_path, _delegated_keyset(new_root_private, catalog_private, keyset_id="ks-new", sequence=1, parent_keyset_id="", root_key_id="new-root-key"))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    old_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    provider.apply_candidate(first_path, expected_root_fingerprint=old_fingerprint, confirmed=True)
    new_fingerprint = ConfiguredCatalogKeyProvider.from_file(new_root_path).status.fingerprint
    new_keyset_fingerprint = hashlib.sha256(canonicalize_jcs(json.loads(new_keyset_path.read_text(encoding="utf-8")))).hexdigest()
    _write_json(
        transition_path,
        _root_transition(
            root_private,
            old_root_fingerprint=old_fingerprint,
            new_root_fingerprint=new_fingerprint,
            new_keyset_fingerprint=new_keyset_fingerprint,
            root_generation=2,
        ),
    )

    try:
        provider.replace_root(
            new_root_path,
            new_keyset_path,
            expected_old_fingerprint=old_fingerprint,
            expected_new_fingerprint="0" * 64,
            transition_path=transition_path,
            confirmed=True,
        )
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "root_fingerprint_mismatch"
    else:
        raise AssertionError("wrong independently expected new root fingerprint was accepted")

    try:
        provider.replace_root(
            new_root_path,
            new_keyset_path,
            expected_old_fingerprint=old_fingerprint,
            expected_new_fingerprint=new_fingerprint,
            transition_path=transition_path,
        )
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "trust_root_replacement_requires_operator"
    else:
        raise AssertionError("root replacement was allowed without confirmation")

    status = provider.replace_root(
        new_root_path,
        new_keyset_path,
        expected_old_fingerprint=old_fingerprint,
        expected_new_fingerprint=new_fingerprint,
        transition_path=transition_path,
        confirmed=True,
    )
    assert status.keyset_id == "ks-new"
    assert status.last_transition == "root_replacement"
    assert status.fingerprint != old_fingerprint


def test_recovery_and_future_rotation_cannot_resurrect_revoked_key(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    revoked_path = tmp_path / "revoked.json"
    reintroduced_path = tmp_path / "reintroduced.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(revoked_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-2", sequence=2, parent_keyset_id="ks-1", revoked=True))
    _write_json(reintroduced_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-3", sequence=3, parent_keyset_id="ks-2", revoked=False))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint

    provider.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    provider.apply_candidate(revoked_path, expected_root_fingerprint=root_fingerprint, confirmed=True)

    for candidate in (first_path, reintroduced_path):
        try:
            if candidate is first_path:
                provider.recover_candidate(candidate, expected_root_fingerprint=root_fingerprint, confirmed=True)
            else:
                provider.apply_candidate(candidate, expected_root_fingerprint=root_fingerprint, confirmed=True)
        except TrustStoreCorruptError as exc:
            assert exc.reason_code == "key_revocation_rollback"
        else:
            raise AssertionError("revoked catalog key regained authority")

    assert provider.status.keyset_sequence == 2
    assert provider.get_key("catalog-key").revoked is True


def test_expired_active_keyset_can_be_replaced_by_valid_higher_sequence(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(
        second_path,
        _delegated_keyset(
            root_private,
            catalog_private,
            keyset_id="ks-2",
            sequence=2,
            parent_keyset_id="ks-1",
            expires_at="2026-03-01T00:00:00Z",
        ),
    )
    now = [NOW]
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: now[0])
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    provider.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    now[0] = datetime(2026, 2, 2, tzinfo=timezone.utc)

    status = provider.apply_candidate(second_path, expected_root_fingerprint=root_fingerprint, confirmed=True)

    assert status.keyset_sequence == 2
    assert status.configured is True


def test_missing_state_after_activation_fails_closed(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(candidate_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    provider.apply_candidate(candidate_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    state_path.unlink()

    restarted = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)

    assert restarted.status.configured is False
    assert restarted.status.reason_code == "trust_lifecycle_state_missing"


def test_stale_provider_cannot_overwrite_newer_persisted_sequence(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(second_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-2", sequence=2, parent_keyset_id="ks-1"))
    root_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    provider_a = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    provider_b = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    provider_a.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    provider_a.get_key("catalog-key")
    provider_b.apply_candidate(second_path, expected_root_fingerprint=root_fingerprint, confirmed=True)

    try:
        provider_a.apply_candidate(first_path, expected_root_fingerprint=root_fingerprint, confirmed=True)
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "keyset_sequence_rollback"
    else:
        raise AssertionError("stale provider overwrote newer lifecycle state")
    assert provider_b.status.keyset_sequence == 2


def test_root_replacement_requires_old_root_signed_transition(tmp_path):
    root_private = Ed25519PrivateKey.generate()
    new_root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    new_root_path = tmp_path / "new-root.json"
    new_keyset_path = tmp_path / "new-keyset.json"
    transition_path = tmp_path / "transition.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(new_root_path, _root_document(new_root_private, "new-root-key"))
    _write_json(new_keyset_path, _delegated_keyset(new_root_private, catalog_private, keyset_id="ks-new", sequence=1, parent_keyset_id="", root_key_id="new-root-key"))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    old_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    new_fingerprint = ConfiguredCatalogKeyProvider.from_file(new_root_path).status.fingerprint
    provider.apply_candidate(first_path, expected_root_fingerprint=old_fingerprint, confirmed=True)
    new_keyset_fingerprint = hashlib.sha256(canonicalize_jcs(json.loads(new_keyset_path.read_text(encoding="utf-8")))).hexdigest()
    _write_json(
        transition_path,
        _root_transition(
            new_root_private,
            old_root_fingerprint=old_fingerprint,
            new_root_fingerprint=new_fingerprint,
            new_keyset_fingerprint=new_keyset_fingerprint,
            root_generation=2,
            root_key_id="new-root-key",
        ),
    )

    try:
        provider.replace_root(
            new_root_path,
            new_keyset_path,
            expected_old_fingerprint=old_fingerprint,
            expected_new_fingerprint=new_fingerprint,
            transition_path=transition_path,
            confirmed=True,
        )
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "root_transition_unauthorized"
    else:
        raise AssertionError("self-authorized root replacement was accepted")


def test_failed_root_publication_rolls_back_complete_previous_pair(tmp_path, monkeypatch):
    import aetherion_cli.trust_store as trust_store_module

    root_private = Ed25519PrivateKey.generate()
    new_root_private = Ed25519PrivateKey.generate()
    catalog_private = Ed25519PrivateKey.generate()
    root_path = tmp_path / "root.json"
    keyset_path = tmp_path / "keyset.json"
    state_path = tmp_path / "state.json"
    first_path = tmp_path / "first.json"
    new_root_path = tmp_path / "new-root.json"
    new_keyset_path = tmp_path / "new-keyset.json"
    transition_path = tmp_path / "transition.json"
    _write_json(root_path, _root_document(root_private))
    _write_json(first_path, _delegated_keyset(root_private, catalog_private, keyset_id="ks-1", sequence=1, parent_keyset_id=""))
    _write_json(new_root_path, _root_document(new_root_private, "new-root-key"))
    _write_json(new_keyset_path, _delegated_keyset(new_root_private, catalog_private, keyset_id="ks-new", sequence=1, parent_keyset_id="", root_key_id="new-root-key"))
    provider = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    old_fingerprint = ConfiguredCatalogKeyProvider.from_file(root_path).status.fingerprint
    new_fingerprint = ConfiguredCatalogKeyProvider.from_file(new_root_path).status.fingerprint
    provider.apply_candidate(first_path, expected_root_fingerprint=old_fingerprint, confirmed=True)
    new_keyset_fingerprint = hashlib.sha256(canonicalize_jcs(json.loads(new_keyset_path.read_text(encoding="utf-8")))).hexdigest()
    _write_json(
        transition_path,
        _root_transition(
            root_private,
            old_root_fingerprint=old_fingerprint,
            new_root_fingerprint=new_fingerprint,
            new_keyset_fingerprint=new_keyset_fingerprint,
            root_generation=2,
        ),
    )
    real_atomic_write = trust_store_module._atomic_bytes_write
    failed = {"value": False}

    def fail_on_root(path, data):
        if Path(path) == root_path and not failed["value"]:
            failed["value"] = True
            raise OSError("injected root publication failure")
        return real_atomic_write(path, data)

    monkeypatch.setattr(trust_store_module, "_atomic_bytes_write", fail_on_root)
    try:
        provider.replace_root(
            new_root_path,
            new_keyset_path,
            expected_old_fingerprint=old_fingerprint,
            expected_new_fingerprint=new_fingerprint,
            transition_path=transition_path,
            confirmed=True,
        )
    except TrustStoreCorruptError as exc:
        assert exc.reason_code == "trust_lifecycle_io_failure"
    else:
        raise AssertionError("injected root publication failure unexpectedly succeeded")

    restarted = LifecycleCatalogKeyProvider(root_path, keyset_path, state_path, clock=lambda: NOW)
    assert restarted.status.configured is True
    assert restarted.status.keyset_id == "ks-1"
    assert restarted.status.fingerprint == old_fingerprint
