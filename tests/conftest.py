from __future__ import annotations

from pathlib import Path

import pytest

from m1_local.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return Settings(
        _env_file=None,
        openai_api_key="",
        headless=True,
        checkpoint_db=tmp_path / "checkpoints.sqlite",
        metadata_db=tmp_path / "app.sqlite",
        artifact_root=tmp_path / "artifacts",
        phoenix_working_dir=tmp_path / "phoenix",
        allowed_file_dirs=[str(allowed)],
        max_graph_steps=10,
        max_model_calls=20,
        max_replans=1,
    )

