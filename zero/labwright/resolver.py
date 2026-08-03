"""Real-time resource resolution + collection (doc sections 6.1, 26).

Packages are semantically neutral -> Labwright resolves them silently. Models
and datasets are semantically loaded: if the public-internet lookup is
ambiguous (multiple candidates and no explicit source), resolution returns a
DecisionRequest instead of guessing (doc section 6.4 / principle 6).
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from zero.protocol.spec import DatasetRequest, ModelRequest, PackageRequest
from zero.protocol.status import DecisionCandidate, DecisionRequest
from zero.resources.cache import CachedResource, ResourceCache


@dataclass
class Resolution:
    """Either a concrete cached resource, or a decision the Researcher must make."""

    resource: Optional[CachedResource] = None
    decision: Optional[DecisionRequest] = None
    unavailable: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.resource is not None


def pip_spec(pkg: PackageRequest) -> str:
    return f"{pkg.name}{pkg.constraint}" if pkg.constraint else pkg.name


class Resolver:
    def __init__(self, cache: ResourceCache):
        self._cache = cache

    # ----- models --------------------------------------------------------- #
    def resolve_model(self, req: ModelRequest, override_source: Optional[str] = None) -> Resolution:
        revision = req.revision or "main"
        cached = self._cache.get("model", req.name, revision)
        if cached and override_source is None:
            return Resolution(resource=cached, notes=["cache hit"])

        source = override_source or req.source
        if source is None:
            candidates = self._search_hf(req.name, kind="model")
            if len(candidates) == 0:
                return Resolution(unavailable=f"model '{req.name}' not found on HuggingFace")
            if len(candidates) > 1:
                return Resolution(decision=self._decision("model", req.name, candidates,
                                                          "量化/来源不同可能影响模型精度与实验可比性"))
            source = candidates[0].source
        return self._collect_model(req, source, revision)

    def _collect_model(self, req: ModelRequest, source: str, revision: str) -> Resolution:
        dest = self._cache.path_for("model", req.name, revision)
        try:
            repo_id = source.split("hf://", 1)[-1].split("@", 1)[0]
            from huggingface_hub import snapshot_download
            local = snapshot_download(repo_id=repo_id, revision=req.revision, local_dir=str(dest))
            sha = _dir_digest(Path(local))
            res = self._cache.record(CachedResource(
                kind="model", name=req.name, version=revision, host_path=str(dest),
                source=f"hf://{repo_id}@{req.revision or revision}", sha256=sha,
            ))
            return Resolution(resource=res)
        except Exception as exc:  # noqa: BLE001
            return Resolution(unavailable=f"model collection failed: {exc}")

    # ----- datasets ------------------------------------------------------- #
    def resolve_dataset(self, req: DatasetRequest, override_source: Optional[str] = None) -> Resolution:
        version = req.version or "main"
        cached = self._cache.get("dataset", req.name, version)
        if cached and override_source is None:
            return Resolution(resource=cached, notes=["cache hit"])

        source = override_source or req.source
        if source is None:
            candidates = self._search_hf(req.name, kind="dataset")
            if len(candidates) == 0:
                return Resolution(unavailable=f"dataset '{req.name}' not found on HuggingFace")
            if len(candidates) > 1:
                return Resolution(decision=self._decision("dataset", req.name, candidates,
                                                          "不同数据集版本/来源可能改变实验结论"))
            source = candidates[0].source
        return self._collect_dataset(req, source, version)

    def _collect_dataset(self, req: DatasetRequest, source: str, version: str) -> Resolution:
        dest = self._cache.path_for("dataset", req.name, version)
        try:
            if source.startswith("http://") or source.startswith("https://"):
                self._download_url(source, dest)
                pinned = source
            else:
                repo_id = source.split("hf://", 1)[-1].split("@", 1)[0]
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id=repo_id, repo_type="dataset",
                                  revision=req.version, local_dir=str(dest))
                pinned = f"hf://{repo_id}@{req.version or version}"
            sha = _dir_digest(dest)
            res = self._cache.record(CachedResource(
                kind="dataset", name=req.name, version=version, host_path=str(dest),
                source=pinned, sha256=sha,
            ))
            return Resolution(resource=res)
        except Exception as exc:  # noqa: BLE001
            return Resolution(unavailable=f"dataset collection failed: {exc}")

    # ----- helpers -------------------------------------------------------- #
    def _search_hf(self, name: str, kind: str) -> list[DecisionCandidate]:
        try:
            from huggingface_hub import HfApi
            api = HfApi()
            if kind == "model":
                hits = list(api.list_models(search=name, limit=5))
                ids = [h.id for h in hits]
            else:
                hits = list(api.list_datasets(search=name, limit=5))
                ids = [h.id for h in hits]
        except Exception:  # noqa: BLE001 - offline / blocked
            return []
        # Exact match short-circuits ambiguity.
        exact = [i for i in ids if i.split("/")[-1].lower() == name.lower() or i.lower() == name.lower()]
        chosen = exact if len(exact) == 1 else ids
        return [
            DecisionCandidate(id=f"c{n}", source=f"hf://{i}", note="exact match" if i in exact else "candidate")
            for n, i in enumerate(chosen)
        ]

    def _decision(self, rtype: str, rname: str, candidates: list[DecisionCandidate], impact: str) -> DecisionRequest:
        return DecisionRequest(
            resource_type=rtype, resource_name=rname,
            reason=f"'{rname}' 在公网存在多个候选来源，需 Researcher 确认",
            candidates=candidates, scientific_impact=impact,
        )

    def _download_url(self, url: str, dest: Path) -> None:
        import httpx
        dest.mkdir(parents=True, exist_ok=True)
        fname = url.rstrip("/").split("/")[-1] or "download.bin"
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with (dest / fname).open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)


def _dir_digest(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:32]
