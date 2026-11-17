#!/usr/bin/env python3
"""Fail-closed Kubernetes release gate for the Aetherion control plane.

The script is intentionally a thin orchestration layer around the repository's
existing Kubernetes YAML and ``scripts/db_migrate.py`` controller.  It can be
run in ``--plan`` mode without contacting a cluster.  Normal execution uses an
injected/real ``kubectl`` command runner, creates fresh release-scoped Jobs,
and never applies the backend Deployment until migration and schema checks
have completed successfully.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s"
DEFAULT_BACKEND_IMAGE = "aetherion/backend:1.0.0"
DEFAULT_FRONTEND_IMAGE = "aetherion/frontend-auth:1.0.0"
EXPECTED_JOB_OUTPUT_RE = re.compile(r"(?:job\.batch/)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$")
RELEASE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:@_-]*$")


class ReleaseGateError(RuntimeError):
    """Raised when a release prerequisite or authoritative command fails."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], CommandResult]


class KubectlRunner:
    """Subprocess boundary kept injectable so tests never contact a cluster."""

    def __call__(self, arguments: list[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                ["kubectl", *arguments],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return CommandResult(127, "", "kubectl unavailable")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class ReleaseConfig:
    namespace: str
    release_id: str
    backend_image: str
    frontend_image: str
    timeout_seconds: int = 900
    manifest_dir: Path = DEFAULT_MANIFEST_DIR


REQUIRED_MANIFESTS = (
    "namespace.yaml",
    "configmap.yaml",
    "secrets.yaml",
    "postgres.yaml",
    "backend.yaml",
    "frontend-auth.yaml",
    "ingress.yaml",
    "db-migrate-job.yaml",
    "db-schema-check-job.yaml",
)


def validate_release_id(release_id: str) -> str:
    value = str(release_id or "").strip()
    if not value or len(value) > 40 or not RELEASE_ID_RE.fullmatch(value):
        raise ReleaseGateError(
            "release_id must be a lowercase Kubernetes-safe label of at most 40 characters"
        )
    return value


def validate_image_ref(image: str, name: str) -> str:
    value = str(image or "").strip()
    if not value or len(value) > 253 or not IMAGE_RE.fullmatch(value) or value.endswith(":latest"):
        raise ReleaseGateError(f"{name} must be a non-latest Kubernetes image reference")
    return value


def validate_release_config(config: ReleaseConfig) -> ReleaseConfig:
    if config.namespace != config.namespace.strip() or not RELEASE_ID_RE.fullmatch(config.namespace):
        raise ReleaseGateError("namespace must be a lowercase Kubernetes-safe label")
    if len(config.namespace) > 63:
        raise ReleaseGateError("namespace is too long for a Kubernetes resource name")
    if config.timeout_seconds < 30 or config.timeout_seconds > 3600:
        raise ReleaseGateError("timeout_seconds must be between 30 and 3600")
    return ReleaseConfig(
        namespace=config.namespace,
        release_id=validate_release_id(config.release_id),
        backend_image=validate_image_ref(config.backend_image, "backend_image"),
        frontend_image=validate_image_ref(config.frontend_image, "frontend_image"),
        timeout_seconds=config.timeout_seconds,
        manifest_dir=Path(config.manifest_dir).resolve(),
    )


def _require_text(text: str, needle: str, description: str) -> None:
    if needle not in text:
        raise ReleaseGateError(f"manifest contract missing {description}")


def _first_image(manifest_text: str) -> str:
    match = re.search(r"^\s+image:\s*([^\s#]+)\s*$", manifest_text, flags=re.MULTILINE)
    if not match:
        raise ReleaseGateError("manifest contract missing an image reference")
    return match.group(1)


def validate_manifest_contracts(manifest_dir: Path) -> dict[str, str]:
    """Validate required static contracts without contacting Kubernetes."""
    paths = {name: manifest_dir / name for name in REQUIRED_MANIFESTS}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ReleaseGateError("missing Kubernetes manifest(s): " + ", ".join(sorted(missing)))

    texts = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    _require_text(texts["namespace.yaml"], "kind: Namespace", "a namespace resource")
    _require_text(texts["configmap.yaml"], 'AETHERION_RELEASE_SCOPE: "control-plane"', "control-plane scope")
    _require_text(texts["configmap.yaml"], 'AETHERION_EXECUTION_DISABLED: "1"', "execution-disabled policy")
    _require_text(texts["configmap.yaml"], "name: aetherion-config", "the authoritative ConfigMap name")
    _require_text(texts["secrets.yaml"], "name: aetherion-secrets", "the authoritative Secret name")
    for key in ("DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "POSTGRES_PASSWORD"):
        _require_text(texts["secrets.yaml"], f"{key}:", f"Secret key {key}")

    _require_text(texts["postgres.yaml"], "kind: StatefulSet", "the PostgreSQL StatefulSet")
    _require_text(texts["postgres.yaml"], "name: postgres", "the PostgreSQL resource name")
    _require_text(texts["postgres.yaml"], "key: POSTGRES_PASSWORD", "the PostgreSQL password Secret reference")
    _require_text(texts["backend.yaml"], "path: /livez", "backend liveness probe")
    _require_text(texts["backend.yaml"], "path: /readyz", "backend readiness probe")

    for name in ("db-migrate-job.yaml", "db-schema-check-job.yaml"):
        _require_text(texts[name], "name: aetherion-config", f"{name} ConfigMap reference")
        _require_text(texts[name], "name: aetherion-secrets", f"{name} Secret reference")
        for key in ("DATABASE_URL", "SECRET_KEY", "JWT_SECRET"):
            _require_text(texts[name], f"key: {key}", f"{name} Secret key {key}")
        _require_text(texts[name], "runAsNonRoot: true", f"{name} non-root security context")
        _require_text(texts[name], "activeDeadlineSeconds:", f"{name} deadline")
        _require_text(texts[name], "aetherion/backend:1.0.0", f"{name} backend image convention")
        _require_text(texts[name], "__AETHERION_RELEASE_ID__", f"{name} release identity template")

    _require_text(texts["db-migrate-job.yaml"], '"--up"', "the supported migration --up command")
    _require_text(texts["db-schema-check-job.yaml"], '"--check", "--json"', "the supported schema status command")

    backend_image = _first_image(texts["backend.yaml"])
    migration_image = _first_image(texts["db-migrate-job.yaml"])
    schema_image = _first_image(texts["db-schema-check-job.yaml"])
    if backend_image != migration_image or backend_image != schema_image:
        raise ReleaseGateError("backend, migration, and schema-check manifests use different default images")

    return {
        "backend_image": backend_image,
        "migration_image": migration_image,
        "schema_check_image": schema_image,
    }


def render_manifest(source: Path, config: ReleaseConfig, *, image: Optional[str] = None) -> str:
    """Render only release-safe substitutions into a temporary manifest."""
    rendered = source.read_text(encoding="utf-8")
    rendered = rendered.replace("__AETHERION_RELEASE_ID__", config.release_id)
    if image:
        default_image = "aetherion/backend:1.0.0" if source.name.startswith("db-") or source.name == "backend.yaml" else "aetherion/frontend-auth:1.0.0"
        if default_image not in rendered:
            raise ReleaseGateError(f"{source.name} does not contain its expected image convention")
        rendered = rendered.replace(default_image, image)
    if "__AETHERION_" in rendered:
        raise ReleaseGateError(f"unresolved release template in {source.name}")
    return rendered


class KubernetesReleaseGate:
    """Apply the existing resources in a migration-before-workloads order."""

    def __init__(self, config: ReleaseConfig, runner: Optional[CommandRunner] = None):
        self.config = validate_release_config(config)
        self.runner = runner or KubectlRunner()
        self.commands: list[list[str]] = []

    def _run(self, arguments: list[str]) -> CommandResult:
        self.commands.append(list(arguments))
        result = self.runner(arguments)
        if not isinstance(result, CommandResult):
            raise ReleaseGateError("command runner returned an invalid result")
        return result

    def _checked(self, arguments: list[str], step: str) -> CommandResult:
        result = self._run(arguments)
        if result.returncode != 0:
            raise ReleaseGateError(f"{step} failed with exit code {result.returncode}")
        return result

    def _apply(self, filename: str, rendered: Optional[str] = None) -> None:
        if rendered is None:
            path = self.config.manifest_dir / filename
            arguments = ["apply", "-f", str(path), "-n", self.config.namespace]
            self._checked(arguments, f"apply {filename}")
            return
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".yaml", prefix="aetherion-release-", delete=False
        ) as handle:
            handle.write(rendered)
            temp_path = Path(handle.name)
        try:
            self._checked(
                ["apply", "-f", str(temp_path), "-n", self.config.namespace],
                f"apply rendered {filename}",
            )
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _verify_secret(self) -> None:
        result = self._checked(
            [
                "get",
                "secret/aetherion-secrets",
                "-n",
                self.config.namespace,
                "-o",
                "json",
            ],
            "verify production Secret reference",
        )
        try:
            payload = json.loads(result.stdout)
            keys = set((payload.get("data") or {}).keys())
        except (TypeError, ValueError):
            raise ReleaseGateError("production Secret lookup did not return structured JSON")
        required = {"DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "POSTGRES_PASSWORD"}
        if not required.issubset(keys):
            raise ReleaseGateError("production Secret is missing required database or application keys")

    def _wait_for_postgres(self) -> None:
        self._checked(
            [
                "wait",
                "--for=condition=ready",
                "pod",
                "-l",
                "app=postgres",
                "-n",
                self.config.namespace,
                f"--timeout={self.config.timeout_seconds}s",
            ],
            "wait for PostgreSQL readiness",
        )

    @staticmethod
    def _created_job_name(stdout: str) -> str:
        for token in stdout.split():
            candidate = token.strip()
            if candidate.startswith("job.batch/"):
                candidate = candidate.split("/", 1)[1]
            if EXPECTED_JOB_OUTPUT_RE.fullmatch(candidate):
                return candidate
        raise ReleaseGateError("kubectl create did not return a safe Job name")

    def _create_job(self, filename: str, rendered: str, step: str) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".yaml", prefix="aetherion-release-", delete=False
        ) as handle:
            handle.write(rendered)
            temp_path = Path(handle.name)
        try:
            result = self._checked(
                [
                    "create",
                    "-f",
                    str(temp_path),
                    "-n",
                    self.config.namespace,
                    "--output=name",
                ],
                f"create {step} Job",
            )
            return self._created_job_name(result.stdout)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _wait_for_job(self, job_name: str, step: str, timeout_seconds: int) -> None:
        result = self._run(
            [
                "wait",
                "--for=condition=complete",
                f"job/{job_name}",
                "-n",
                self.config.namespace,
                f"--timeout={timeout_seconds}s",
            ]
        )
        if result.returncode == 0:
            return

        status = self._run(
            ["get", f"job/{job_name}", "-n", self.config.namespace, "-o", "json"]
        )
        if status.returncode == 0:
            try:
                job_status = (json.loads(status.stdout) or {}).get("status") or {}
                if int(job_status.get("failed") or 0) > 0:
                    raise ReleaseGateError(f"{step} Job failed")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        raise ReleaseGateError(f"{step} Job timed out or did not complete")

    def _verify_schema_payload(self, job_name: str) -> None:
        result = self._checked(
            ["logs", f"job/{job_name}", "-n", self.config.namespace],
            "read schema status output",
        )
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ReleaseGateError("schema status Job did not emit structured JSON")
        compatibility = payload.get("extension_schema_compatibility") or {}
        if (
            payload.get("ok") is not True
            or payload.get("status") != "UP_TO_DATE"
            or compatibility.get("ok") is not True
            or payload.get("missing_extensions")
        ):
            raise ReleaseGateError("schema status check did not confirm the required extension/workspace schema")

    def _wait_for_deployment(self, name: str) -> None:
        self._checked(
            [
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                self.config.namespace,
                f"--timeout={self.config.timeout_seconds}s",
            ],
            f"wait for {name} rollout",
        )
        result = self._checked(
            ["get", f"deployment/{name}", "-n", self.config.namespace, "-o", "json"],
            f"verify {name} rollout status",
        )
        try:
            payload = json.loads(result.stdout)
            replicas = int((payload.get("spec") or {}).get("replicas") or 0)
            available = int((payload.get("status") or {}).get("availableReplicas") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ReleaseGateError(f"{name} rollout status was not structured JSON")
        if replicas < 1 or available < replicas:
            raise ReleaseGateError(f"{name} rollout is not safely available")

    def plan(self) -> dict[str, object]:
        contracts = validate_manifest_contracts(self.config.manifest_dir)
        return {
            "status": "PLAN",
            "namespace": self.config.namespace,
            "release_id": self.config.release_id,
            "backend_image": self.config.backend_image,
            "frontend_image": self.config.frontend_image,
            "manifest_contracts": contracts,
            "migration_precedes_backend": True,
            "live_cluster_contacted": False,
        }

    def run(self) -> dict[str, object]:
        validate_manifest_contracts(self.config.manifest_dir)
        with tempfile.TemporaryDirectory(prefix="aetherion-k8s-release-"):
            # The Secret manifest is a placeholder template.  Verify that the
            # approved Secret already exists instead of applying placeholders.
            self._apply("namespace.yaml")
            self._apply("configmap.yaml")
            self._apply("postgres.yaml")
            self._verify_secret()
            self._wait_for_postgres()

            migration = render_manifest(
                self.config.manifest_dir / "db-migrate-job.yaml",
                self.config,
                image=self.config.backend_image,
            )
            migration_job = self._create_job("db-migrate-job.yaml", migration, "migration")
            self._wait_for_job(migration_job, "migration", self.config.timeout_seconds)

            schema_check = render_manifest(
                self.config.manifest_dir / "db-schema-check-job.yaml",
                self.config,
                image=self.config.backend_image,
            )
            schema_job = self._create_job("db-schema-check-job.yaml", schema_check, "schema-check")
            self._wait_for_job(schema_job, "schema-check", min(self.config.timeout_seconds, 300))
            self._verify_schema_payload(schema_job)

            backend = render_manifest(
                self.config.manifest_dir / "backend.yaml",
                self.config,
                image=self.config.backend_image,
            )
            self._apply("backend.yaml", backend)
            self._wait_for_deployment("aetherion-backend")

            frontend = render_manifest(
                self.config.manifest_dir / "frontend-auth.yaml",
                self.config,
                image=self.config.frontend_image,
            )
            self._apply("frontend-auth.yaml", frontend)
            self._wait_for_deployment("aetherion-frontend-auth")
            self._apply("ingress.yaml")

        return {
            "status": "SUCCEEDED",
            "namespace": self.config.namespace,
            "release_id": self.config.release_id,
            "migration_job": migration_job,
            "schema_check_job": schema_job,
            "backend_image": self.config.backend_image,
            "frontend_image": self.config.frontend_image,
            "migration_precedes_backend": True,
            "live_cluster_contacted": True,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed Aetherion Kubernetes release gate")
    parser.add_argument("--release-id", required=True, help="Unique lowercase release identifier")
    parser.add_argument(
        "--backend-image",
        default=os.environ.get("AETHERION_BACKEND_IMAGE", ""),
        help="Immutable-compatible backend image reference",
    )
    parser.add_argument(
        "--frontend-image",
        default=os.environ.get("AETHERION_FRONTEND_AUTH_IMAGE", ""),
        help="Immutable-compatible frontend image reference",
    )
    parser.add_argument("--namespace", default="aetherion")
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--plan", action="store_true", help="Validate and print the release plan without kubectl")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = ReleaseConfig(
            namespace=args.namespace,
            release_id=args.release_id,
            backend_image=args.backend_image,
            frontend_image=args.frontend_image,
            timeout_seconds=args.timeout_seconds,
            manifest_dir=args.manifest_dir,
        )
        gate = KubernetesReleaseGate(config)
        result = gate.plan() if args.plan else gate.run()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ReleaseGateError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "live_cluster_contacted": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
