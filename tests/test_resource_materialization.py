from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zero.labwright.resolver import Resolver
from zero.protocol.spec import DatasetRequest, ModelRequest
from zero.resources.cache import ResourceCache
from zero.resources.trisol import TrisolAsset


class MaterializationTests(unittest.TestCase):
    def test_trisol_id_is_resolved_to_versioned_uri_and_hashed(self):
        class FakeTrisol:
            def resolve(self, kind, asset_id, requested_version=None):
                return TrisolAsset(kind, asset_id, "dataset-name", "team-1", "2", "v2", ("train.jsonl",), 4)

            def materialize(self, source, destination):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "train.jsonl").write_text("row\n", encoding="utf-8")
                return self.resolve("dataset", "123")

        with tempfile.TemporaryDirectory() as tmp:
            resolver = Resolver(ResourceCache(Path(tmp)), trisol=FakeTrisol())
            result = resolver.resolve_dataset(
                DatasetRequest(name="benchmark", source="trisol://dataset/123"),
                "trisol://dataset/123",
            )
        self.assertTrue(result.resolved)
        assert result.resource is not None
        self.assertEqual("2", result.resource.version)
        self.assertEqual(
            "trisol://dataset/123/2?team=team-1&name=dataset-name&split=train.jsonl",
            result.resource.source,
        )

    def test_model_url_is_materialized_and_content_hashed(self):
        payload = b"fixed model bytes"
        with tempfile.TemporaryDirectory() as tmp:
            resolver = Resolver(ResourceCache(Path(tmp)))

            def fake_download(_url: str, dest: Path) -> None:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "weights.bin").write_bytes(payload)

            with patch.object(resolver, "_download_url", side_effect=fake_download):
                result = resolver.resolve_model(ModelRequest(
                    name="model", revision="v1", source="https://objects.test/model-v1.bin",
                ))

            self.assertTrue(result.resolved)
            assert result.resource is not None
            expected = hashlib.sha256()
            expected.update(b"weights.bin\0")
            expected.update(payload)
            self.assertEqual("sha256:" + expected.hexdigest(), result.resource.sha256)
            self.assertEqual("https://objects.test/model-v1.bin", result.resource.source)


if __name__ == "__main__":
    unittest.main()
