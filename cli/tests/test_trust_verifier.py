"""Adversarial Phase 1 tests for the isolated signed-catalog verifier."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aetherion_cli import main as main_module
from aetherion_cli.plugin_manager import PluginCatalogManager
from aetherion_cli.trust_verifier import (
    CatalogAuthStatus,
    CatalogKey,
    CatalogReason,
    CatalogSubject,
    CompatibilityStatus,
    MappingCatalogKeyProvider,
    PolicyStatus,
    TrustState,
    TrustStoreCorruptError,
    canonicalize_jcs,
    parse_catalog_json,
    verify_catalog,
)
from aetherion_cli.tui.state import Plugin

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40
ARCHIVE_SHA = "b" * 64
MANIFEST_SHA = "c" * 64


def _payload(*, sequence: int = 1, issued_at: str = "2026-10-17T11:00:00Z", expires_at: str = "2026-10-18T12:00:00Z"):
    return {
        "format": "aetherion.catalog.payload.v1",
        "catalog_id": "test-catalog",
        "sequence": sequence,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "entries": [
            {
                "plugin_id": "demo-plugin",
                "repository": {
                    "canonical_url": "https://github.com/example/demo-plugin",
                    "owner": "example",
                    "name": "demo-plugin",
                },
                "selection": {"commit_sha": COMMIT, "ref_policy": "curated_commit_only"},
                "archive": {
                    "format": "github-codeload-zip",
                    "sha256": ARCHIVE_SHA,
                    "size_bytes": 1234,
                },
                "manifest": {
                    "path": "plugin.json",
                    "sha256": MANIFEST_SHA,
                    "schema_version": "source-only",
                    "plugin_api_version": "",
                },
                "policy": {
                    "compatibility": CompatibilityStatus.SOURCE_ONLY.value,
                    "execution_enabled": False,
                    "publisher_evidence": "optional",
                    "review_status": "approved",
                },
            }
        ],
    }


def _subject(**changes) -> CatalogSubject:
    values = {
        "plugin_id": "demo-plugin",
        "repository_url": "https://github.com/example/demo-plugin",
        "commit_sha": COMMIT,
        "archive_sha256": ARCHIVE_SHA,
        "archive_size_bytes": 1234,
        "manifest_path": "plugin.json",
        "manifest_sha256": MANIFEST_SHA,
        "manifest_schema_version": "source-only",
        "plugin_api_version": "",
        "compatibility": CompatibilityStatus.SOURCE_ONLY.value,
        "execution_enabled": False,
    }
    values.update(changes)
    return CatalogSubject(**values)


def _key_provider(private_key: Ed25519PrivateKey, *, key_id: str = "catalog-test", **changes):
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    values = {"key_id": key_id, "public_key": public_key}
    values.update(changes)
    return MappingCatalogKeyProvider({key_id: CatalogKey(**values)})


def _signed_envelope(payload=None, *, private_key=None, key_id: str = "catalog-test"):
    private_key = private_key or Ed25519PrivateKey.generate()
    payload = copy.deepcopy(payload or _payload())
    canonical = canonicalize_jcs(payload)
    signature = private_key.sign(canonical)
    envelope = {
        "format": "aetherion.catalog.envelope.v1",
        "key_id": key_id,
        "algorithm": "Ed25519",
        "canonicalization": "RFC8785-JCS",
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "payload": payload,
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }
    return envelope, private_key, _key_provider(private_key, key_id=key_id)


def _verify(envelope, provider, **kwargs):
    return verify_catalog(
        envelope,
        provider,
        now=NOW,
        expected_subject=_subject(),
        **kwargs,
    )


def test_valid_signature_is_qualified_and_does_not_enable_execution():
    envelope, _private, provider = _signed_envelope()
    result = _verify(envelope, provider)

    assert result.verified is True
    assert result.qualified_status == "catalog_signature_verified"
    assert result.catalog_auth_status is CatalogAuthStatus.VERIFIED
    assert result.policy_status is PolicyStatus.CATALOG_APPROVED
    assert result.reason_code is None
    assert result.execution_status.value == "disabled"
    assert result.authority_key_id == "catalog-test"


def test_one_byte_payload_change_fails_digest_before_signature():
    envelope, _private, provider = _signed_envelope()
    envelope["payload"]["catalog_id"] = "changed-catalog"

    result = _verify(envelope, provider)

    assert result.reason_code is CatalogReason.PAYLOAD_DIGEST_MISMATCH
    assert result.catalog_auth_status is CatalogAuthStatus.INVALID


def test_signature_change_fails_without_mutating_subject():
    envelope, _private, provider = _signed_envelope()
    signature = bytearray(base64.b64decode(envelope["signature_base64"]))
    signature[0] ^= 1
    envelope["signature_base64"] = base64.b64encode(signature).decode("ascii")
    original = _subject()

    result = _verify(envelope, provider)

    assert result.reason_code is CatalogReason.SIGNATURE_INVALID
    assert original.execution_enabled is False


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("repository", "canonical_url"), "https://github.com/example/other", CatalogReason.SUBJECT_MISMATCH),
        (("selection", "commit_sha"), "d" * 40, CatalogReason.SUBJECT_MISMATCH),
        (("archive", "sha256"), "e" * 64, CatalogReason.ARCHIVE_DIGEST_MISMATCH),
        (("archive", "size_bytes"), 9999, CatalogReason.ARCHIVE_DIGEST_MISMATCH),
        (("manifest", "path"), "other.json", CatalogReason.SUBJECT_MISMATCH),
        (("manifest", "sha256"), "f" * 64, CatalogReason.MANIFEST_DIGEST_MISMATCH),
        (("policy", "compatibility"), CompatibilityStatus.UNKNOWN.value, CatalogReason.SUBJECT_MISMATCH),
    ],
)
def test_signed_but_different_entry_cannot_approve_requested_subject(path, value, reason):
    payload = _payload()
    payload["entries"][0][path[0]][path[1]] = value
    if path == ("repository", "canonical_url"):
        payload["entries"][0]["repository"]["name"] = "other"
    envelope, _private, provider = _signed_envelope(payload)

    result = _verify(envelope, provider)

    assert result.reason_code is reason
    assert result.verified is False


def test_execution_policy_true_is_rejected_even_with_a_valid_signature():
    payload = _payload()
    payload["entries"][0]["policy"]["execution_enabled"] = True
    envelope, _private, provider = _signed_envelope(payload)

    result = _verify(envelope, provider)

    assert result.reason_code is CatalogReason.SCHEMA_INVALID


def test_expected_subject_cannot_request_execution():
    envelope, _private, provider = _signed_envelope()

    result = verify_catalog(
        envelope,
        provider,
        now=NOW,
        expected_subject=_subject(execution_enabled=True),
    )

    assert result.reason_code is CatalogReason.SCHEMA_INVALID


def test_unknown_wrong_revoked_and_scope_mismatched_keys_fail_closed():
    envelope, private_key, provider = _signed_envelope()
    assert _verify(envelope, MappingCatalogKeyProvider({})).reason_code is CatalogReason.KEY_UNKNOWN

    wrong_private = Ed25519PrivateKey.generate()
    wrong_provider = _key_provider(wrong_private)
    assert _verify(envelope, wrong_provider).reason_code is CatalogReason.SIGNATURE_INVALID

    revoked = _key_provider(private_key, revoked=True)
    assert _verify(envelope, revoked).reason_code is CatalogReason.KEY_REVOKED

    scoped = _key_provider(private_key, scopes=frozenset({"release"}))
    assert _verify(envelope, scoped).reason_code is CatalogReason.KEY_SCOPE_MISMATCH


def test_corrupt_trust_store_is_not_reported_as_a_signature_success():
    envelope, _private, _provider = _signed_envelope()

    class BrokenProvider:
        def get_key(self, _key_id):
            raise TrustStoreCorruptError("invalid configured keyset")

    result = _verify(envelope, BrokenProvider())

    assert result.reason_code is CatalogReason.TRUST_STORE_CORRUPT
    assert result.verified is False


def test_wrong_algorithm_canonicalization_and_malformed_signature_are_schema_invalid():
    envelope, _private, provider = _signed_envelope()
    for field, value in (
        ("algorithm", "RSA-PSS"),
        ("canonicalization", "json-sort-keys"),
        ("signature_base64", "not-base64"),
    ):
        altered = copy.deepcopy(envelope)
        altered[field] = value
        result = _verify(altered, provider)
        assert result.reason_code is CatalogReason.SCHEMA_INVALID

    altered = copy.deepcopy(envelope)
    altered["signature_base64"] = base64.b64encode(b"short").decode("ascii")
    assert _verify(altered, provider).reason_code is CatalogReason.SCHEMA_INVALID


def test_duplicate_json_keys_are_rejected_before_schema_interpretation():
    envelope, _private, provider = _signed_envelope()
    raw = json.dumps(envelope, separators=(",", ":"))
    raw = raw.replace('"key_id":"catalog-test",', '"key_id":"catalog-test","key_id":"attacker",', 1)

    result = _verify(raw.encode("utf-8"), provider)

    assert result.reason_code is CatalogReason.SCHEMA_INVALID


def test_duplicate_plugin_ids_unsupported_schema_and_critical_fields_fail():
    payload = _payload()
    payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    envelope, _private, provider = _signed_envelope(payload)
    assert _verify(envelope, provider).reason_code is CatalogReason.SCHEMA_INVALID

    unsupported = _payload()
    unsupported["format"] = "aetherion.catalog.payload.v99"
    envelope, _private, provider = _signed_envelope(unsupported)
    assert _verify(envelope, provider).reason_code is CatalogReason.SCHEMA_INVALID

    envelope, _private, provider = _signed_envelope()
    envelope["critical"] = ["unknown-field"]
    assert _verify(envelope, provider).reason_code is CatalogReason.SCHEMA_INVALID


def test_expired_not_yet_valid_and_sequence_replay_are_distinct():
    envelope, _private, provider = _signed_envelope(
        _payload(expires_at="2026-10-17T11:59:59Z")
    )
    assert _verify(envelope, provider).reason_code is CatalogReason.EXPIRED

    envelope, _private, provider = _signed_envelope(
        _payload(issued_at="2026-10-17T12:10:00Z", expires_at="2026-10-18T12:00:00Z")
    )
    assert _verify(envelope, provider).reason_code is CatalogReason.NOT_YET_VALID

    envelope, _private, provider = _signed_envelope(_payload(sequence=2))
    assert _verify(envelope, provider, highest_accepted_sequence=2).reason_code is CatalogReason.SEQUENCE_ROLLBACK


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["entries"][0]["selection"].update({"commit_sha": "bad"}),
        lambda p: p["entries"][0]["archive"].update({"sha256": "BAD"}),
        lambda p: p["entries"][0]["repository"].update({"canonical_url": "http://github.com/example/demo-plugin"}),
        lambda p: p.update({"issued_at": "not-a-timestamp"}),
        lambda p: p.update({"sequence": "1"}),
    ],
)
def test_malformed_subject_and_scalar_types_are_rejected(mutation):
    payload = _payload()
    mutation(payload)
    envelope, _private, provider = _signed_envelope(payload)

    assert _verify(envelope, provider).reason_code is CatalogReason.SCHEMA_INVALID


def test_embedded_attacker_key_cannot_bootstrap_trust_and_empty_production_root_is_honest():
    envelope, _private, _provider = _signed_envelope()
    envelope["public_key"] = "attacker-key"

    result = verify_catalog(envelope, MappingCatalogKeyProvider({}), now=NOW)

    assert result.reason_code is CatalogReason.SCHEMA_INVALID
    assert verify_catalog(_signed_envelope()[0], MappingCatalogKeyProvider({}), now=NOW).catalog_auth_status is CatalogAuthStatus.UNKNOWN_KEY


def test_missing_signature_is_unsigned_not_invalid_or_approved():
    envelope, _private, provider = _signed_envelope()
    del envelope["signature_base64"]

    result = _verify(envelope, provider)

    assert result.catalog_auth_status is CatalogAuthStatus.UNSIGNED
    assert result.reason_code is CatalogReason.SIGNATURE_MISSING
    assert result.verified is False


def test_legacy_records_default_to_unsigned_unknown_and_never_enable_execution():
    state = TrustState.from_record(
        {
            "compatibility_status": ["corrupt"],
            "catalog_auth_status": {"attempt": "upgrade"},
            "execution_enabled": True,
        }
    )

    assert state.catalog_auth_status.value == "unsigned"
    assert state.publisher_auth_status.value == "not_checked"
    assert state.policy_status.value == "unreviewed"
    assert state.compatibility_status.value == "unknown"
    assert state.execution_status.value == "disabled"


def test_manager_and_tui_defaults_expose_integrity_and_catalog_auth_separately(tmp_path):
    record = PluginCatalogManager(root=tmp_path / "plugins").info("rea")
    assert record is not None
    assert record["catalog_auth_status"] == "unsigned"
    assert record["publisher_auth_status"] == "not_checked"
    assert record["policy_status"] == "unreviewed"
    assert record["execution_status"] == "disabled"
    assert record["execution_enabled"] is False

    plugin = Plugin(
        id="rea",
        name="REA",
        description="metadata-only",
        integrity="verified",
        catalog_auth_status=record["catalog_auth_status"],
    )
    assert plugin.integrity == "verified"
    assert plugin.catalog_auth_status == "unsigned"
    assert plugin.execution_status == "disabled"


def test_existing_unsigned_catalog_records_are_read_as_unapproved(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "demo-alias": {
                        "id": "demo-alias",
                        "source_url": "https://github.com/example/demo-plugin",
                        "requested_ref": "main",
                        "resolved_commit": COMMIT,
                        "source_plugin_id": "demo-plugin",
                        "compatibility_status": "unknown",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    item = PluginCatalogManager(root=root).catalog_list()[0]

    assert item["catalog_auth_status"] == "unsigned"
    assert item["policy_status"] == "unreviewed"
    assert item["trust_reason_code"] == "compatibility_unknown"
    assert item["execution_enabled"] is False


def test_cli_labels_archive_integrity_and_catalog_auth_separately(monkeypatch):
    manager = MagicMock()
    manager.info.return_value = {
        "id": "demo-plugin",
        "name": "Demo Plugin",
        "version": "1.0.0",
        "author": "tests",
        "license": "MIT",
        "source_url": "https://github.com/example/demo-plugin",
        "catalog_origin": "user",
        "requested_ref": "main",
        "resolved_commit": COMMIT,
        "compatibility_status": "source_only",
        "integrity": "verified",
        "catalog_auth_status": "unsigned",
        "publisher_auth_status": "not_checked",
        "policy_status": "unreviewed",
        "installed": True,
        "description": "metadata-only",
    }
    monkeypatch.setattr(main_module, "_get_plugin_manager", lambda: manager)

    result = CliRunner().invoke(main_module.cli, ["plugins", "info", "demo-plugin"])

    assert result.exit_code == 0
    assert "Archive integrity:" in result.output
    assert "Catalog auth:" in result.output
    assert "Publisher auth:" in result.output
    assert "Execution:" in result.output


def test_jcs_is_order_independent_but_rejects_floats():
    assert canonicalize_jcs({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    with pytest.raises(ValueError, match="floating point"):
        canonicalize_jcs({"value": 1.0})


def test_jcs_supported_vectors_use_utf16_property_ordering_and_safe_boundaries():
    assert canonicalize_jcs({"𐀀": 1, "": 2}) == '{"𐀀":1,"":2}'.encode("utf-8")
    assert canonicalize_jcs({"max": 2**53 - 1, "min": -(2**53 - 1)}) == (
        b'{"max":9007199254740991,"min":-9007199254740991}'
    )


def test_jcs_equivalent_string_and_property_forms_have_same_bytes():
    left = {"text": "é", "nested": {"b": True, "a": [1, 2]}}
    right = {"nested": {"a": [1, 2], "b": True}, "text": "\u00e9"}

    assert canonicalize_jcs(left) == canonicalize_jcs(right)


def test_jcs_rejects_out_of_range_integers_and_lone_surrogates():
    with pytest.raises(ValueError, match="safe JSON number"):
        canonicalize_jcs({"value": 2**53})
    with pytest.raises(ValueError, match="surrogates"):
        canonicalize_jcs({"value": "\ud800"})


def test_raw_parser_rejects_nonstandard_json_constants():
    with pytest.raises(ValueError, match="non-standard"):
        parse_catalog_json("{\"value\":NaN}")
