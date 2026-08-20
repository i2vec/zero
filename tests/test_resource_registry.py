from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

import httpx

from zero.protocol.resources import (
    ArtifactRef, ResourceKind, ResourceLock, ResourceLockEntry, VerificationEvidence,
)
from zero.resources.errors import (
    RegistryBusinessError, RegistryConflict, RegistryIndexError,
    RegistryValidationError,
)
from zero.resources.deploy_master import BuildToolRequest, DeployMasterClient
from zero.resources.deploy_master import BuiltToolArtifact
from zero.labwright.tools import _build_tool_resource
from zero.labwright.resolver import Resolver
from zero.protocol.spec import DatasetRequest
from zero.protocol.status import DecisionCandidate, EnvironmentResponse, EnvironmentStatus
from zero.resources.cache import ResourceCache
from zero.resources.errors import DeployMasterBuildFailed, DeployMasterVerificationFailed
from zero.resources.literature_sage import LiteratureSageClient
from zero.resources.locks import ResourceLockStore, lock_digest, validate_release_lock
from zero.resources.registry import ResourceRegistry


def response(payload, status=200):
    return httpx.Response(status, json=payload)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_search_detail_shapes_expose_image_and_trisol_ids(self):
        requests = []

        async def handler(request):
            requests.append((request.url.path, json.loads(request.content)))
            kind = request.url.path.split("/")[3]
            if request.url.path.endswith("search/hybrid"):
                plural = {"tool": "tools", "dataset": "datasets", "model": "models"}[kind]
                return response({"code": 0, "data": {plural: [{
                    f"{kind}_unique_key": f"live-{kind}", "score": 0.9,
                }]}})
            item = {
                f"{kind}_unique_key": f"live-{kind}", f"{kind}_name": f"Live {kind}",
                "status": 0,
            }
            if kind == "tool":
                item.update({"docker_image_uri": "registry.dp.tech/tools/live:1",
                             "docker_image_id": "sha256:abc", "usage_entry_command": "live --help"})
            else:
                item["trisol_id"] = "2078021631912976384"
            return response({"code": 0, "data": {"items": [item]}})

        client = LiteratureSageClient("https://example.test", transport=httpx.MockTransport(handler))
        try:
            tool = (await ResourceRegistry(client).search(kind=ResourceKind.TOOL, text="live"))[0]
            dataset = (await ResourceRegistry(client).search(kind=ResourceKind.DATASET, text="live"))[0]
            model = (await ResourceRegistry(client).search(kind=ResourceKind.MODEL, text="live"))[0]
        finally:
            await client.aclose()
        self.assertEqual("registry.dp.tech/tools/live:1", tool.artifact.uri)
        self.assertEqual("live --help", tool.entry_command)
        self.assertEqual("trisol://dataset/2078021631912976384", dataset.artifact.uri)
        self.assertEqual("trisol://model/2078021631912976384", model.artifact.uri)
        for path, body in requests:
            kind = path.split("/")[3]
            if path.endswith("search/hybrid"):
                self.assertIn("k", body)
                self.assertNotIn("limit", body)
            else:
                self.assertEqual([f"live-{kind}"], body[f"{kind}_unique_keys"])

    async def test_health_accepts_prefixed_json_readiness_marker(self):
        client = LiteratureSageClient(
            "https://example.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text='ok{"status":"ok"}',
                    headers={"content-type": "text/plain"},
                )
            ),
        )
        try:
            health = await client.health()
        finally:
            await client.aclose()
        self.assertEqual({"status": "ok"}, health)

    async def test_search_always_fetches_detail_and_marks_mutable(self):
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("search/hybrid"):
                return response({"code": 0, "data": {"items": [
                    {"resource_unique_key": "torchvision_package", "score": 0.95},
                ]}})
            return response({"code": 0, "data": {"items": [{
                "resource_unique_key": "torchvision_package", "name": "torchvision",
                "status": 0, "image_uri": "registry/tool:latest", "platform": "linux/amd64",
                "capabilities": ["computer vision training"],
            }]}})

        client = LiteratureSageClient("https://example.test", transport=httpx.MockTransport(handler))
        try:
            items = await ResourceRegistry(client).search(
                kind=ResourceKind.TOOL, text="torchvision",
                required_capabilities=["computer vision training"],
                constraints={"platform": "linux/amd64"},
            )
        finally:
            await client.aclose()
        self.assertEqual(2, len(calls))
        self.assertEqual("torchvision_package", items[0].resource_unique_key)
        self.assertIn("mutable_reference", items[0].warnings)

    async def test_search_drops_metadata_only_records(self):
        def handler(request):
            if request.url.path.endswith("search/hybrid"):
                return response({"code": 0, "data": {"items": [
                    {"resource_unique_key": "no-bytes", "score": 0.99},
                ]}})
            return response({"code": 0, "data": {"items": [{
                "resource_unique_key": "no-bytes", "name": "metadata only", "status": 0,
            }]}})

        client = LiteratureSageClient("https://example.test", transport=httpx.MockTransport(handler))
        try:
            items = await ResourceRegistry(client).search(
                kind=ResourceKind.DATASET, text="metadata only",
            )
        finally:
            await client.aclose()
        self.assertEqual([], items)

    async def test_dataset_catalog_hit_with_invalid_url_is_rejected_end_to_end(self):
        """A search hit is not usable unless its bytes have a valid source URI."""
        def handler(request):
            if request.url.path.endswith("search/hybrid"):
                return response({"code": 0, "data": {"items": [
                    {"resource_unique_key": "broken-data", "score": 1.0},
                ]}})
            return response({"code": 0, "data": {"items": [{
                "resource_unique_key": "broken-data", "name": "broken-data",
                "status": 0, "url": "not a retrievable URL",
            }]}})

        client = LiteratureSageClient(
            "https://example.test", transport=httpx.MockTransport(handler),
        )
        try:
            candidates = await ResourceRegistry(client).search(
                kind=ResourceKind.DATASET, text="broken-data",
            )
        finally:
            await client.aclose()
        self.assertEqual([], candidates)

    async def test_ambiguous_dataset_candidates_end_in_needs_decision(self):
        """Multiple semantic sources must cross the Researcher decision boundary."""
        with tempfile.TemporaryDirectory() as tmp:
            resolver = Resolver(ResourceCache(Path(tmp)))
            resolver._search_hf = lambda name, kind: [  # type: ignore[method-assign]
                DecisionCandidate(id="c0", source="hf://owner/data-a", note="candidate"),
                DecisionCandidate(id="c1", source="hf://owner/data-b", note="candidate"),
            ]
            resolution = resolver.resolve_dataset(DatasetRequest(name="benchmark"))

        self.assertIsNone(resolution.resource)
        self.assertIsNotNone(resolution.decision)
        response = EnvironmentResponse(
            status=EnvironmentStatus.NEEDS_DECISION,
            request_id="request-ambiguous",
            decision=resolution.decision,
            message=resolution.decision.reason,
        )
        self.assertEqual(EnvironmentStatus.NEEDS_DECISION, response.status)
        self.assertEqual(["c0", "c1"], [c.id for c in response.decision.candidates])
        self.assertIn("实验结论", response.decision.scientific_impact)

    async def test_business_error_on_http_200(self):
        client = LiteratureSageClient("https://example.test", max_retries=0,
            transport=httpx.MockTransport(lambda _: response({"code": 123, "message": "bad"})))
        try:
            with self.assertRaises(RegistryBusinessError):
                await client.search("tool", {})
        finally:
            await client.aclose()

    async def test_503_has_bounded_retry(self):
        count = 0

        def handler(_):
            nonlocal count
            count += 1
            return response({"code": 0, "data": {}}, 503 if count < 3 else 200)

        client = LiteratureSageClient("https://example.test", max_retries=3,
                                      transport=httpx.MockTransport(handler))
        try:
            await client.search("model", {})
        finally:
            await client.aclose()
        self.assertEqual(3, count)

    async def test_index_failed_is_not_success(self):
        client = LiteratureSageClient("https://example.test", max_retries=0,
            transport=httpx.MockTransport(lambda _: response({
                "code": 0, "data": {"index_status": "failed"},
            })))
        try:
            with self.assertRaises(RegistryIndexError):
                await client.import_resource("dataset", {})
        finally:
            await client.aclose()

    async def test_publish_conflict_never_overwrites(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return response({"code": 0, "data": {"items": [{
                "resource_unique_key": "same", "name": "same", "status": 0,
                "image_uri": "registry/old@sha256:1", "digest": "sha256:1",
            }]}})

        client = LiteratureSageClient("https://example.test", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(RegistryConflict):
                await ResourceRegistry(client).publish(
                    kind=ResourceKind.TOOL, unique_key="same", metadata={},
                    artifact=ArtifactRef(type="oci_image", uri="registry/new@sha256:2", digest="sha256:2"),
                    verification=VerificationEvidence(status="passed"),
                )
        finally:
            await client.aclose()
        self.assertFalse(any(path.endswith("/import") for path in calls))

    async def test_publish_rejects_wrong_artifact_kind(self):
        client = LiteratureSageClient(
            "https://example.test",
            transport=httpx.MockTransport(lambda _: response({"code": 0, "data": {}})),
        )
        try:
            with self.assertRaises(RegistryValidationError):
                await ResourceRegistry(client).publish(
                    kind=ResourceKind.TOOL, unique_key="bad", metadata={},
                    artifact=ArtifactRef(type="url", uri="https://example.test/tool.tgz"),
                    verification=VerificationEvidence(status="passed"),
                )
        finally:
            await client.aclose()

    async def test_import_timeout_recovers_by_detail_then_search(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/import"):
                raise httpx.ReadTimeout("outcome unknown", request=request)
            if request.url.path.endswith("batch/detail"):
                # Empty before import, exact artifact after ambiguous timeout.
                imported = any(path.endswith("/import") for _, path in calls)
                return response({"code": 0, "data": {"items": ([{
                    "resource_unique_key": "data-v1", "name": "data-v1", "status": 0,
                    "url": "s3://bucket/data-v1", "digest": "sha256:abc",
                }] if imported else [])}})
            return response({"code": 0, "data": {"items": [
                {"resource_unique_key": "data-v1", "score": 1.0},
            ]}})

        client = LiteratureSageClient("https://example.test", max_retries=0,
                                      transport=httpx.MockTransport(handler))
        try:
            result = await ResourceRegistry(client).publish(
                kind=ResourceKind.DATASET, unique_key="data-v1",
                metadata={"name": "data-v1", "tag_ids": [1]},
                artifact=ArtifactRef(type="object_bundle", uri="s3://bucket/data-v1", digest="sha256:abc"),
                verification=VerificationEvidence(status="passed"),
            )
        finally:
            await client.aclose()
        self.assertEqual("data-v1", result.resource_unique_key)
        self.assertEqual(1, sum(path.endswith("/import") for _, path in calls))


class LockTests(unittest.TestCase):
    def test_atomic_canonical_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resources.lock.json"
            store = ResourceLockStore(path, "task-1")
            digest = store.put(ResourceLockEntry(
                requirement_id="dataset:data", kind=ResourceKind.DATASET,
                resource_ref="literature-sage:dataset:data", resolution="collected",
                artifact=ArtifactRef(type="url", uri="s3://bucket/data", digest="sha256:abc"),
                verification=VerificationEvidence(status="passed", results_digest="sha256:def"),
            ))
            lock = store.read()
            self.assertEqual(digest, lock_digest(lock))
            self.assertEqual(1, len(lock.entries))
            self.assertEqual(json.loads(path.read_text()), lock.model_dump(mode="json"))
            self.assertEqual([], list(Path(tmp).glob(".resources.lock.json.*")))

    def test_release_gate_checks_presence_kind_verification_and_immutability(self):
        lock = ResourceLock(task_id="task-1", entries=[ResourceLockEntry(
            requirement_id="dataset:data", kind=ResourceKind.MODEL,
            resource_ref="literature-sage:model:data", resolution="existing",
            artifact=ArtifactRef(type="url", uri="https://objects.test/data"),
            verification=VerificationEvidence(status="unknown"),
        )])
        self.assertEqual([
            "kind_mismatch:dataset:data:model!=dataset",
            "verification_not_passed:dataset:data",
            "mutable_artifact:dataset:data",
            "missing:tool:runner",
        ], validate_release_lock(
            lock, {"dataset:data": "dataset", "tool:runner": "tool"},
            require_immutable=True,
        ))


class DeployMasterTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_miss_build_verify_publish_and_lock_end_to_end(self):
        """Exercise the P1 happy path without writing to either live service."""
        catalog: dict[str, dict] = {}
        registry_calls: list[tuple[str, str]] = []

        def registry_handler(request):
            registry_calls.append((request.method, request.url.path))
            if request.url.path.endswith("search/hybrid"):
                items = [
                    {"resource_unique_key": key, "score": 1.0}
                    for key in catalog
                ]
                return response({"code": 0, "data": {"items": items}})
            if request.url.path.endswith("batch/detail"):
                return response({"code": 0, "data": {"items": [
                    item for item in catalog.values()
                ]}})
            if request.url.path.endswith("/import"):
                imported = json.loads(request.content)
                key = imported.get("tool_unique_key") or imported.get("resource_unique_key")
                catalog[key] = {
                    **imported, "tool_unique_key": key,
                    "docker_image_uri": imported.get("version", {}).get("docker_image_uri"),
                    "docker_image_id": imported.get("version", {}).get("docker_image_id"),
                    "name": imported.get("tool_name") or imported.get("name") or key, "status": 0,
                }
                return response({"code": 0, "data": {"index_status": "built"}})
            raise AssertionError(f"unexpected registry request: {request.method} {request.url.path}")

        build_polls = 0

        def deploy_handler(request):
            nonlocal build_polls
            if request.method == "POST":
                return response({"task_id": "build-e2e"}, 201)
            build_polls += 1
            if build_polls == 1:
                return response({"status": "building"})
            return response({
                "status": "succeeded",
                "docker_image_uri": "registry.test/lab/tool@sha256:abc",
                "image_digest": "sha256:abc",
                "source_commit": "deadbeef",
                "platform": "linux/amd64",
                "verification_digest": "sha256:buildcheck",
            })

        sage = LiteratureSageClient(
            "https://sage.test", transport=httpx.MockTransport(registry_handler),
        )
        deploy = DeployMasterClient(
            "https://deploy.test", poll_interval=0,
            transport=httpx.MockTransport(deploy_handler),
        )
        registry = ResourceRegistry(sage)
        try:
            self.assertEqual([], await registry.search(
                kind=ResourceKind.TOOL, text="missing-tool",
            ))
            built = await deploy.build(BuildToolRequest(
                github_url="https://github.com/acme/missing-tool/tree/deadbeef",
                verify_commands=["missing-tool --version"],
            ))
            artifact = ArtifactRef(
                type="oci_image", uri=built.image_uri, digest=built.image_digest,
                platform=built.platform,
            )
            # This evidence represents Labwright's separate, task-level sandbox
            # check; Deploy Master's build-time check alone is not sufficient.
            verified = VerificationEvidence(
                status="passed", commands=["missing-tool --version"],
                results_digest="sha256:taskcheck",
            )
            published = await registry.publish(
                kind=ResourceKind.TOOL, unique_key="missing-tool",
                metadata={"name": "missing-tool", "tag_ids": [1]}, artifact=artifact,
                verification=verified, capabilities=["version-reporting"],
            )
            with tempfile.TemporaryDirectory() as tmp:
                store = ResourceLockStore(Path(tmp) / "resources.lock.json", "task-e2e")
                digest = store.put(ResourceLockEntry(
                    requirement_id="tool:missing-tool", kind=ResourceKind.TOOL,
                    resource_ref="literature-sage:tool:missing-tool",
                    resolution="built", artifact=artifact, verification=verified,
                    provenance={"deploy_master_task_id": built.task_id},
                ))
                lock = store.read()
                self.assertEqual([], validate_release_lock(
                    lock, {"tool:missing-tool": "tool"}, require_immutable=True,
                ))
                self.assertEqual(digest, lock_digest(lock))
        finally:
            await deploy.aclose()
            await sage.aclose()

        self.assertEqual("missing-tool", published.resource_unique_key)
        self.assertEqual("build-e2e", built.task_id)
        self.assertEqual(2, build_polls)
        self.assertEqual(1, sum(path.endswith("/import") for _, path in registry_calls))

    async def test_build_polls_and_normalizes_artifact(self):
        polls = 0

        def handler(request):
            nonlocal polls
            if request.method == "POST":
                return response({"data": {"task_id": "build-1"}})
            polls += 1
            if polls == 1:
                return response({"data": {"status": "building"}})
            return response({"data": {
                "status": "succeeded", "image_uri": "registry/tool@sha256:abc",
                "image_digest": "sha256:abc", "source_commit": "deadbeef",
                "verification_digest": "sha256:def", "platform": "linux/amd64",
            }})

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            built = await client.build(BuildToolRequest(
                github_url="https://github.com/acme/tool/tree/deadbeef",
                build_instructions="Build the CLI", verify_commands=["tool --version"],
            ))
        finally:
            await client.aclose()
        self.assertEqual("sha256:abc", built.image_digest)
        self.assertEqual("deadbeef", built.source_commit)
        self.assertEqual(2, polls)

    async def test_verification_failure_is_typed(self):
        def handler(request):
            if request.method == "POST":
                return response({"data": {"task_id": "build-2"}})
            return response({"data": {
                "status": "failed", "failure_stage": "verification", "message": "bad command",
            }})

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(DeployMasterVerificationFailed):
                await client.build(BuildToolRequest(
                    github_url="https://github.com/acme/tool", build_instructions="build",
                ))
        finally:
            await client.aclose()

    async def test_live_response_fields_are_normalized(self):
        def handler(request):
            if request.method == "POST":
                return response({"task_id": "build-live"}, 201)
            return response({
                "task_id": "build-live", "status": "success",
                "docker_image_uri": "registry.dp.tech/davinci/tool:20260812",
                "dockerfile": "FROM python:3.12-slim\n",
                "verification_results": "[{\"command\":\"tool --version\",\"exit_code\":0}]",
            })

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            built = await client.build(BuildToolRequest(github_url="https://github.com/acme/tool"))
        finally:
            await client.aclose()
        self.assertEqual("registry.dp.tech/davinci/tool:20260812", built.image_uri)
        self.assertTrue(built.dockerfile_digest.startswith("sha256:"))
        self.assertTrue(built.verification_digest.startswith("sha256:"))
        self.assertEqual(["mutable_reference"], built.warnings)

    async def test_live_failure_uses_error_message(self):
        def handler(request):
            if request.method == "POST":
                return response({"task_id": "build-failed"}, 201)
            return response({
                "status": "failed", "failure_stage": "clone",
                "progress": "Failed during clone", "error_message": "repository not found",
            })

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(DeployMasterBuildFailed, "repository not found"):
                await client.build(BuildToolRequest(github_url="https://github.com/acme/missing"))
        finally:
            await client.aclose()

    async def test_deadline_is_bounded(self):
        def handler(request):
            if request.method == "POST":
                return response({"data": {"task_id": "build-3"}})
            return response({"data": {"status": "building"}})

        client = DeployMasterClient("https://deploy.test", poll_interval=0, deadline=0,
                                    transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(DeployMasterBuildFailed):
                await client.build(BuildToolRequest(
                    github_url="https://github.com/acme/tool", build_instructions="build",
                ))
        finally:
            await client.aclose()

    async def test_transient_poll_failure_is_retried_without_resubmitting(self):
        posts = 0
        gets = 0

        def handler(request):
            nonlocal posts, gets
            if request.method == "POST":
                posts += 1
                return response({"task_id": "build-retry"}, 201)
            gets += 1
            if gets == 1:
                return response({"error": "busy"}, 503)
            return response({"status": "success", "docker_image_uri": "registry/tool:retry"})

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            built = await client.build(BuildToolRequest(github_url="https://github.com/acme/tool"))
        finally:
            await client.aclose()
        self.assertEqual(1, posts)
        self.assertEqual(2, gets)
        self.assertEqual(1, built.build_attempts)

    async def test_labwright_forwards_explicit_rebuild_budget(self):
        class FakeDeployMaster:
            def __init__(self):
                self.max_rebuilds = None

            async def build(self, request, *, max_rebuilds=0):
                self.max_rebuilds = max_rebuilds
                return BuiltToolArtifact(
                    task_id="build-accepted", image_uri="registry/tool:accepted",
                    build_attempts=2, task_ids=["build-1", "build-accepted"],
                )

        deploy_master = FakeDeployMaster()
        events = []
        ctx = SimpleNamespace(
            deploy_master=deploy_master, request_id="request-1",
            emit=lambda *event: events.append(event),
        )
        result = await _build_tool_resource(ctx, {
            "github_url": "https://github.com/acme/tool", "max_rebuilds": 1,
        })
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(1, deploy_master.max_rebuilds)
        self.assertEqual(2, payload["artifact"]["build_attempts"])
        self.assertEqual(1, events[0][2]["max_rebuilds"])

    async def test_explicit_rebuild_resubmits_after_build_failure(self):
        posts = 0

        def handler(request):
            nonlocal posts
            if request.method == "POST":
                posts += 1
                return response({"task_id": f"build-{posts}"}, 201)
            task_id = request.url.path.rsplit("/", 1)[-1]
            if task_id == "build-1":
                return response({"status": "failed", "failure_stage": "build", "message": "builder lost"})
            return response({"status": "success", "docker_image_uri": "registry/tool:rebuilt"})

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            built = await client.build(
                BuildToolRequest(github_url="https://github.com/acme/tool"), max_rebuilds=1,
            )
        finally:
            await client.aclose()
        self.assertEqual(2, posts)
        self.assertEqual(2, built.build_attempts)
        self.assertEqual(["build-1", "build-2"], built.task_ids)

    async def test_verification_failure_is_not_rebuilt(self):
        posts = 0

        def handler(request):
            nonlocal posts
            if request.method == "POST":
                posts += 1
                return response({"task_id": "verify-failed"}, 201)
            return response({"status": "failed", "failure_stage": "verification", "message": "bad command"})

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(DeployMasterVerificationFailed):
                await client.build(
                    BuildToolRequest(github_url="https://github.com/acme/tool"), max_rebuilds=3,
                )
        finally:
            await client.aclose()
        self.assertEqual(1, posts)

    async def test_submission_failure_is_not_rebuilt(self):
        posts = 0

        def handler(request):
            nonlocal posts
            posts += 1
            return response({"error": "invalid repository"}, 400)

        client = DeployMasterClient("https://deploy.test", poll_interval=0,
                                    transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(DeployMasterBuildFailed, "HTTP 400"):
                await client.build(
                    BuildToolRequest(github_url="not-a-repository"), max_rebuilds=3,
                )
        finally:
            await client.aclose()
        self.assertEqual(1, posts)


if __name__ == "__main__":
    unittest.main()
