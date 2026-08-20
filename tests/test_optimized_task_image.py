from pathlib import Path

import pytest

from zero.orchestrator.orchestrator import Orchestrator


def _package(root: Path, name: str, *, docker_image_lines: int = 1) -> Path:
    package = root / name
    (package / "environment").mkdir(parents=True)
    lines = ['schema_version = "1.3"', "", "[environment]"]
    lines.extend(
        'docker_image = "registry.example/old:tag"'
        for _ in range(docker_image_lines)
    )
    (package / "task.toml").write_text("\n".join(lines) + "\n")
    (package / "environment" / "Dockerfile").write_text(
        "FROM registry.example/old:tag\nWORKDIR /app\n"
    )
    return package


def test_bind_image_updates_both_frozen_task_copies(tmp_path: Path) -> None:
    finalized = _package(tmp_path, "finalized_task")
    optimized = _package(tmp_path, "optimized_task")
    image = "registry.dp.tech/project/task@sha256:abc123"

    updated = Orchestrator._bind_image_to_optimized_tasks(tmp_path, image)

    assert len(updated) == 4
    for package in (finalized, optimized):
        assert f'docker_image = "{image}"' in (package / "task.toml").read_text()
        assert (package / "environment" / "Dockerfile").read_text().startswith(
            f"FROM {image}\n"
        )


def test_bind_image_validates_every_file_before_writing(tmp_path: Path) -> None:
    finalized = _package(tmp_path, "finalized_task")
    _package(tmp_path, "optimized_task", docker_image_lines=2)
    original_toml = (finalized / "task.toml").read_text()
    original_dockerfile = (finalized / "environment" / "Dockerfile").read_text()

    with pytest.raises(ValueError, match="exactly one docker_image"):
        Orchestrator._bind_image_to_optimized_tasks(
            tmp_path, "registry.dp.tech/project/task:latest"
        )

    assert (finalized / "task.toml").read_text() == original_toml
    assert (finalized / "environment" / "Dockerfile").read_text() == original_dockerfile


@pytest.mark.parametrize("image", ["", "registry.example/bad image:tag"])
def test_bind_image_rejects_invalid_url(tmp_path: Path, image: str) -> None:
    _package(tmp_path, "optimized_task")
    with pytest.raises(ValueError, match="invalid image URL"):
        Orchestrator._bind_image_to_optimized_tasks(tmp_path, image)
