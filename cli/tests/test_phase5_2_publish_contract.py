from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aetherion_cli.skill_library import SkillLibrary, SkillLibraryError
from aetherion_cli.skill_publish import (
    PublishProtocolError,
    validate_authoritative_get,
    validate_publish_response,
)

CONTENT = "---\nname: contract-demo\nversion: 1.0.0\n---\n\nreference data\n"
EXPECTED = {
    "skill_id": "contract-demo",
    "name": "Contract Demo",
    "description": "Reference metadata",
    "version": "1.0.0",
    "content_sha256": hashlib.sha256(CONTENT.encode()).hexdigest(),
}


def _skill() -> dict:
    return {
        "id": EXPECTED["skill_id"],
        "name": EXPECTED["name"],
        "description": EXPECTED["description"],
        "version": EXPECTED["version"],
        "content_hash": EXPECTED["content_sha256"],
        "source_kind": "authenticated_publish",
        "is_enabled": False,
    }


def test_publish_contract_rejects_application_failure_and_malformed_success() -> None:
    for response in ({"ok": False}, "not-json", {"ok": True, "outcome": "created"}):
        with pytest.raises(PublishProtocolError):
            validate_publish_response(response, EXPECTED)


def test_publish_contract_requires_authoritative_digest_and_metadata() -> None:
    response = {
        "ok": True,
        "outcome": "created",
        "status": "published_to_tenant",
        "execution_enabled": False,
        "skill": _skill(),
    }
    assert validate_publish_response(response, EXPECTED)["skill"]["id"] == "contract-demo"
    bad = dict(response, skill={**_skill(), "content_hash": "0" * 64})
    with pytest.raises(PublishProtocolError, match="digest"):
        validate_publish_response(bad, EXPECTED)
    bad_metadata = dict(response, skill={**_skill(), "version": "9.9.9"})
    with pytest.raises(PublishProtocolError, match="metadata|version"):
        validate_publish_response(bad_metadata, EXPECTED)
    with pytest.raises(PublishProtocolError):
        validate_authoritative_get({"ok": True, "skill": {**_skill(), "is_enabled": True}}, EXPECTED)


def test_skill_library_rejects_tampered_external_installed_path(tmp_path: Path) -> None:
    library = SkillLibrary(root=tmp_path / "library", builtin_root=tmp_path / "empty-builtin")
    custom = library.create_custom("Contract Demo", "Reference metadata", "reference data")
    outside = tmp_path / "outside.md"
    outside.write_text("server secret", encoding="utf-8")
    state = json.loads(library.state_path.read_text(encoding="utf-8"))
    state["installed"][custom.skill_id]["path"] = str(outside)
    library.state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(SkillLibraryError, match="outside"):
        library.get(custom.skill_id)
