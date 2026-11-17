"""Deterministic Kubernetes release-gate tests; no kubectl or cluster is used."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.deploy_k8s import (
    CommandResult,
    KubernetesReleaseGate,
    ReleaseConfig,
    ReleaseGateError,
    validate_manifest_contracts,
)


BACKEND_IMAGE = "registry.example/aetherion/backend:release-20261018"
FRONTEND_IMAGE = "registry.example/aetherion/frontend-auth:release-20261018"


def _config() -> ReleaseConfig:
    return ReleaseConfig(
        namespace="aetherion",
        release_id="release-20261018",
        backend_image=BACKEND_IMAGE,
        frontend_image=FRONTEND_IMAGE,
        manifest_dir=Path(__file__).parents[1] / "deploy" / "k8s",
        timeout_seconds=90,
    )


class FakeKubectl:
    def __init__(self, *, migration_failure=False, migration_timeout=False, schema_failure=False, backend_failure=False, job_suffix="a1b2c3"):
        self.calls: list[list[str]] = []
        self.migration_failure = migration_failure
        self.migration_timeout = migration_timeout
        self.schema_failure = schema_failure
        self.backend_failure = backend_failure
        self.created_jobs = iter(
            [
                f"job.batch/aetherion-db-migrate-release-{job_suffix}",
                f"job.batch/aetherion-db-schema-check-release-{job_suffix}",
            ]
        )

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(list(args))
        if args[:2] == ["get", "secret/aetherion-secrets"]:
            return CommandResult(
                0,
                json.dumps({"data": {key: "redacted" for key in ("DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "POSTGRES_PASSWORD")}}),
            )
        if args and args[0] == "create":
            return CommandResult(0, next(self.created_jobs))
        if args and args[0] == "wait" and "job/" in " ".join(args):
            if self.migration_failure and "db-migrate" in " ".join(args):
                if self.migration_timeout:
                    return CommandResult(1, "", "timed out")
                return CommandResult(1, "", "failed")
            return CommandResult(0)
        if args and args[0] == "get" and "job/" in " ".join(args):
            failed = 1 if self.migration_failure and not self.migration_timeout else 0
            return CommandResult(0, json.dumps({"status": {"failed": failed}}))
        if args and args[0] == "logs":
            if self.schema_failure:
                return CommandResult(0, json.dumps({"ok": False, "status": "MIGRATIONS_PENDING"}))
            return CommandResult(
                0,
                json.dumps(
                    {
                        "ok": True,
                        "status": "UP_TO_DATE",
                        "missing_extensions": [],
                        "extension_schema_compatibility": {"ok": True, "version": 2, "expected": 2},
                    }
                ),
            )
        if args and args[0] == "rollout":
            if self.backend_failure and "aetherion-backend" in " ".join(args):
                return CommandResult(1, "", "rollout failed")
            return CommandResult(0)
        if args and args[0] == "get" and "deployment/" in " ".join(args):
            return CommandResult(0, json.dumps({"spec": {"replicas": 2}, "status": {"availableReplicas": 2}}))
        return CommandResult(0)


def _first_index(calls: list[list[str]], predicate) -> int:
    return next(index for index, call in enumerate(calls) if predicate(call))


def test_manifest_contracts_require_migration_and_schema_jobs():
    contracts = validate_manifest_contracts(_config().manifest_dir)
    assert contracts["backend_image"] == "aetherion/backend:1.0.0"


def test_postgres_secret_reference_is_structurally_nested():
    yaml = pytest.importorskip("yaml")
    documents = list(
        yaml.safe_load_all(
            (_config().manifest_dir / "postgres.yaml").read_text(encoding="utf-8")
        )
    )
    stateful_set = next(document for document in documents if document.get("kind") == "StatefulSet")
    secret_ref = stateful_set["spec"]["template"]["spec"]["containers"][0]["env"][2]["valueFrom"]["secretKeyRef"]
    assert secret_ref == {"name": "aetherion-secrets", "key": "POSTGRES_PASSWORD"}


def test_release_jobs_are_bounded_and_use_fresh_generated_names():
    yaml = pytest.importorskip("yaml")
    for filename, expected_command in (
        ("db-migrate-job.yaml", ["python", "/app/scripts/db_migrate.py", "--up"]),
        ("db-schema-check-job.yaml", ["python", "/app/scripts/db_migrate.py", "--check", "--json"]),
    ):
        document = yaml.safe_load((_config().manifest_dir / filename).read_text(encoding="utf-8"))
        assert document["kind"] == "Job"
        assert document["metadata"]["generateName"]
        assert document["spec"]["parallelism"] == 1
        assert document["spec"]["completions"] == 1
        assert document["spec"]["activeDeadlineSeconds"] > 0
        container = document["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == expected_command
        assert document["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_successful_release_orders_migration_schema_check_then_backend():
    runner = FakeKubectl()
    result = KubernetesReleaseGate(_config(), runner).run()

    assert result["status"] == "SUCCEEDED"
    create_indices = [index for index, call in enumerate(runner.calls) if call and call[0] == "create"]
    backend_apply_index = _first_index(
        runner.calls,
        lambda call: call and call[0] == "apply" and any("aetherion-release-" in value for value in call),
    )
    backend_rollout_index = _first_index(
        runner.calls,
        lambda call: call[:2] == ["rollout", "status"] and "deployment/aetherion-backend" in call,
    )
    assert len(create_indices) == 2
    assert create_indices[0] < create_indices[1] < backend_apply_index < backend_rollout_index


def test_migration_failure_stops_before_backend_rollout():
    runner = FakeKubectl(migration_failure=True)
    with pytest.raises(ReleaseGateError, match="migration Job failed"):
        KubernetesReleaseGate(_config(), runner).run()

    assert not any(call[:2] == ["rollout", "status"] for call in runner.calls)
    assert not any(call and call[0] == "apply" and any("aetherion-release-" in value for value in call) for call in runner.calls)


def test_migration_timeout_stops_before_backend_rollout():
    runner = FakeKubectl(migration_failure=True, migration_timeout=True)
    with pytest.raises(ReleaseGateError, match="timed out"):
        KubernetesReleaseGate(_config(), runner).run()

    assert not any("deployment/aetherion-backend" in call for call in runner.calls)


def test_schema_status_failure_stops_before_backend_rollout():
    runner = FakeKubectl(schema_failure=True)
    with pytest.raises(ReleaseGateError, match="schema status check"):
        KubernetesReleaseGate(_config(), runner).run()

    assert not any("deployment/aetherion-backend" in call for call in runner.calls)


def test_backend_rollout_failure_is_returned_as_failed_release():
    runner = FakeKubectl(backend_failure=True)
    with pytest.raises(ReleaseGateError, match="aetherion-backend rollout failed"):
        KubernetesReleaseGate(_config(), runner).run()


def test_invalid_release_identifier_fails_before_any_cluster_command():
    runner = FakeKubectl()
    config = ReleaseConfig(
        namespace="aetherion",
        release_id="Bad/Release",
        backend_image=BACKEND_IMAGE,
        frontend_image=FRONTEND_IMAGE,
        manifest_dir=_config().manifest_dir,
    )
    with pytest.raises(ReleaseGateError, match="release_id"):
        KubernetesReleaseGate(config, runner)
    assert runner.calls == []


def test_missing_secret_key_fails_before_database_wait():
    runner = FakeKubectl()

    def missing_secret(args):
        runner.calls.append(list(args))
        if args[:2] == ["get", "secret/aetherion-secrets"]:
            return CommandResult(0, json.dumps({"data": {"DATABASE_URL": "redacted"}}))
        return runner(args)

    with pytest.raises(ReleaseGateError, match="missing required database"):
        KubernetesReleaseGate(_config(), missing_secret).run()
    assert not any(call and call[0] == "wait" for call in runner.calls)


def test_new_release_uses_fresh_job_names_and_release_identity(tmp_path):
    # A new release always calls kubectl create and renders its own release
    # identity; it never waits on a previous Job name.
    observed_templates: list[str] = []
    observed_second_templates: list[str] = []
    runner = FakeKubectl(job_suffix="old111")

    original = runner.__call__

    def inspect_create(args):
        if args and args[0] == "create":
            observed_templates.append(Path(args[args.index("-f") + 1]).read_text(encoding="utf-8"))
        return original(args)

    config = _config()
    first_result = KubernetesReleaseGate(config, inspect_create).run()
    second_runner = FakeKubectl(job_suffix="new222")
    second_config = ReleaseConfig(
        namespace=config.namespace,
        release_id="release-20261019",
        backend_image=config.backend_image,
        frontend_image=config.frontend_image,
        manifest_dir=config.manifest_dir,
        timeout_seconds=config.timeout_seconds,
    )
    second_original = second_runner.__call__

    def inspect_second_create(args):
        if args and args[0] == "create":
            observed_second_templates.append(Path(args[args.index("-f") + 1]).read_text(encoding="utf-8"))
        return second_original(args)

    second_result = KubernetesReleaseGate(second_config, inspect_second_create).run()
    assert len(observed_templates) == 2
    assert len(observed_second_templates) == 2
    assert all(config.release_id in content for content in observed_templates)
    assert all(second_config.release_id in content for content in observed_second_templates)
    assert all("__AETHERION_RELEASE_ID__" not in content for content in observed_templates + observed_second_templates)
    assert first_result["migration_job"] != second_result["migration_job"]
