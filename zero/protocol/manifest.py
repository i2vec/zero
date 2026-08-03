"""EnvironmentManifest: what Labwright delivers (doc section 13).

Constraints from the spec are resolved into concrete versions plus verification
results. Model/dataset entries additionally carry provenance so that even
"real-time collected" resources are reproducible.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PackageEntry(BaseModel):
    version: str
    verified: bool = False


class ToolEntry(BaseModel):
    version: Optional[str] = None
    command: Optional[str] = None
    verified: bool = False


class ModelEntry(BaseModel):
    path: str
    revision: Optional[str] = None
    precision: Optional[str] = None
    read_only: bool = True
    verified: bool = False
    # Provenance: pins the exact thing real-time collection fetched.
    source: Optional[str] = None
    sha256: Optional[str] = None
    collected_at: Optional[str] = None


class DatasetEntry(BaseModel):
    path: str
    version: Optional[str] = None
    read_only: bool = True
    verified: bool = False
    source: Optional[str] = None
    sha256: Optional[str] = None
    collected_at: Optional[str] = None


class VerificationReport(BaseModel):
    package_import: Optional[str] = None       # passed / failed / n/a
    tool_healthcheck: Optional[str] = None
    model_load: Optional[str] = None
    dataset_read: Optional[str] = None
    gpu_check: str = "skipped"                  # GPU deferred in MVP


class EnvironmentManifest(BaseModel):
    task_id: str
    experiment_id: str
    sandbox_id: str
    environment_status: str = "ready"
    workspace: str = "/workspace"
    runtime: dict[str, Optional[str]] = Field(default_factory=dict)
    packages: dict[str, PackageEntry] = Field(default_factory=dict)
    tools: dict[str, ToolEntry] = Field(default_factory=dict)
    models: dict[str, ModelEntry] = Field(default_factory=dict)
    datasets: dict[str, DatasetEntry] = Field(default_factory=dict)
    verification: VerificationReport = Field(default_factory=VerificationReport)

    # Reproducibility bindings (doc section 16).
    image_digest: Optional[str] = None
    package_lock: dict[str, str] = Field(default_factory=dict)

    def researcher_summary(self) -> str:
        """The compact view the Researcher sees (doc section 13 tail)."""
        lines = [
            "Sandbox 已就绪。",
            f"sandbox_id: {self.sandbox_id}",
            f"workspace: {self.workspace}",
        ]
        if self.tools:
            for name, t in self.tools.items():
                lines.append(f"工具 {name} 调用命令: {t.command or name}")
        for name, m in self.models.items():
            lines.append(f"模型 {name} 路径: {m.path}")
        for name, d in self.datasets.items():
            lines.append(f"数据集 {name} 路径: {d.path}")
        if self.packages:
            pkgs = ", ".join(f"{n}=={e.version}" for n, e in self.packages.items())
            lines.append(f"已安装包: {pkgs}")
        lines.append("所有资源验证通过。" if self._all_verified() else "部分资源未完全验证，见 manifest。")
        return "\n".join(lines)

    def _all_verified(self) -> bool:
        checks = [self.verification.package_import, self.verification.model_load, self.verification.dataset_read]
        return all(c in (None, "passed", "n/a") for c in checks)
