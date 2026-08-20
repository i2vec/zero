from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zero.export import _write_asset_summary, export_run
from zero.labwright.inventory import _tools_from_manifest, validate_inventory_lock_consistency
from zero.protocol.environment_inventory import EnvironmentInventory, MountInventoryEntry
from zero.protocol.manifest import DatasetEntry, EnvironmentManifest
from zero.protocol.resources import (
    ArtifactRef, ResourceKind, ResourceLock, ResourceLockEntry, VerificationEvidence,
)
from zero.resources.locks import lock_digest


class InventoryConsistencyTests(unittest.TestCase):
    def test_export_run_includes_structured_environment(self):
        class ConfigStub:
            export_max_file_mb = 1

            def __init__(self, root: Path):
                self.root = root

            def ensure_run_dirs(self, task_id: str) -> Path:
                run = self.root / task_id
                (run / "trace").mkdir(parents=True)
                return run

        with tempfile.TemporaryDirectory() as tmp:
            environment = {"runtime": {"python": "3.11"}}
            run = export_run(
                ConfigStub(Path(tmp)), object(), task_id="run-1",
                workspace="/workspace", sandbox_ids=[], prompt="solve",
                status="completed", backend="local", environment=environment,
                pull_deliverables=False,
            )
            assert run is not None
            payload = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(environment, payload["environment"])

    def test_inventory_probes_baseline_tools_when_manifest_has_none(self):
        class Result:
            exit_code = 0

            def __init__(self, stdout: str):
                self.stdout = stdout

        class Manager:
            def exec(self, sandbox_id, command, timeout):
                tool = command.split()[2]
                return Result(f"/usr/bin/{tool}\n{tool} 1.0\n")

        tools = _tools_from_manifest(
            EnvironmentManifest(task_id="t", experiment_id="e", sandbox_id="s"),
            Manager(), "s",
        )
        self.assertEqual(["python", "pip", "bash", "git"], [tool.name for tool in tools])

    def test_asset_summary_exposes_task_image_tool_uri_and_trisol_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "resources.lock.json").write_text(json.dumps({"entries": [
                {"requirement_id": "tool:x", "kind": "tool",
                 "resource_ref": "literature-sage:tool:x",
                 "artifact": {"type": "oci_image", "uri": "registry/x:1", "digest": "sha256:a"},
                 "verification": {"status": "passed"}, "provenance": {}},
                {"requirement_id": "dataset:y", "kind": "dataset",
                 "resource_ref": "literature-sage:dataset:y",
                 "artifact": {"type": "object_bundle", "uri": "trisol://dataset/123/2"},
                 "verification": {"status": "passed"},
                 "provenance": {"trisol_id": "123", "trisol_version": "2", "trisol_team": "team"}},
            ]}), encoding="utf-8")
            path = _write_asset_summary(
                run, {"image": {"status": "ready", "url": "registry/task:1"}},
            )
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("registry/task:1", payload["task_image"]["uri"])
        self.assertEqual("registry/x:1", payload["tools"][0]["artifact_uri"])
        self.assertEqual("123", payload["datasets"][0]["trisol_id"])

    def _fixtures(self):
        lock = ResourceLock(task_id="task-1", entries=[ResourceLockEntry(
            requirement_id="dataset:data", kind=ResourceKind.DATASET,
            resource_ref="literature-sage:dataset:data", resolution="existing",
            artifact=ArtifactRef(
                type="object_bundle", uri="s3://bucket/data", digest="sha256:abc",
            ),
            verification=VerificationEvidence(status="passed"),
        )])
        digest = lock_digest(lock)
        manifest = EnvironmentManifest(
            task_id="task-1", experiment_id="exp-1", sandbox_id="env-1",
            datasets={"data": DatasetEntry(
                path="/datasets/data/v1", source="s3://bucket/data", sha256="sha256:abc",
                verified=True,
            )},
            resources_lock_digest=digest,
        )
        inventory = EnvironmentInventory(
            environment_id="sha256:env", task_id="task-1", sandbox_id="env-1",
            backend="local", resources_lock_digest=digest,
            mounts=[MountInventoryEntry(
                kind="dataset", name="data", path="/datasets/data/v1",
                source="s3://bucket/data", sha256="sha256:abc",
            )],
        )
        return lock, manifest, inventory

    def test_consistent_lock_manifest_inventory(self):
        lock, manifest, inventory = self._fixtures()
        self.assertEqual([], validate_inventory_lock_consistency(manifest, inventory, lock))

    def test_detects_digest_and_mount_drift(self):
        lock, manifest, inventory = self._fixtures()
        manifest.resources_lock_digest = "sha256:wrong"
        inventory.mounts[0].sha256 = "sha256:wrong"
        self.assertEqual([
            "manifest_lock_digest_mismatch",
            "artifact_digest_mismatch:dataset:data",
        ], validate_inventory_lock_consistency(manifest, inventory, lock))


if __name__ == "__main__":
    unittest.main()
