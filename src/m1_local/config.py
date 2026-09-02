from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    orchestrator_model: str = "gpt-4.1"
    websurfer_model: str = "gpt-4.1"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    phoenix_host: str = "127.0.0.1"
    phoenix_port: int = 6006
    phoenix_base_url: str = "http://127.0.0.1:6006"
    phoenix_otlp_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    phoenix_project_name: str = "m1-local-lab"
    phoenix_working_dir: Path = Path("./data/phoenix")
    phoenix_allow_external_resources: bool = False
    langsmith_tracing: bool = False
    langgraph_strict_msgpack: bool = True
    headless: bool = False
    max_active_runs: int = 1
    max_graph_steps: int = 30
    max_model_calls: int = 40
    max_replans: int = 2
    model_timeout_seconds: int = 90
    run_timeout_seconds: int = 600
    enable_code_execution: bool = False
    allowed_web_hosts: CsvList = Field(
        default_factory=lambda: ["playwright.dev", "microsoft.github.io", "developers.openai.com"]
    )
    allowed_file_dirs: CsvList = Field(default_factory=lambda: ["./datasets", "./docs"])

    checkpoint_db: Path = Path("./data/checkpoints.sqlite")
    metadata_db: Path = Path("./data/app.sqlite")
    artifact_root: Path = Path("./data/artifacts")
    max_file_bytes: int = 2_000_000

    @field_validator(
        "phoenix_working_dir", "checkpoint_db", "metadata_db", "artifact_root", mode="after"
    )
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("allowed_file_dirs", mode="after")
    @classmethod
    def resolve_allowed_dirs(cls, values: list[str]) -> list[str]:
        return [str((PROJECT_ROOT / value).resolve()) if not Path(value).is_absolute() else str(Path(value).resolve()) for value in values]

    @field_validator("app_host", "phoenix_host")
    @classmethod
    def local_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MVP services may only bind to loopback")
        return value

    @field_validator("max_active_runs")
    @classmethod
    def one_active_run(cls, value: int) -> int:
        if value != 1:
            raise ValueError("MVP requires MAX_ACTIVE_RUNS=1")
        return value

    @property
    def api_key_configured(self) -> bool:
        return bool(self.openai_api_key.get_secret_value().strip())

    def ensure_directories(self) -> None:
        for path in (self.checkpoint_db.parent, self.metadata_db.parent, self.artifact_root, self.phoenix_working_dir):
            path.mkdir(parents=True, exist_ok=True)

    def safe_summary(self) -> dict[str, object]:
        return {
            "project_root": str(PROJECT_ROOT),
            "api_key_configured": self.api_key_configured,
            "openai_base_url": self.openai_base_url,
            "orchestrator_model": self.orchestrator_model,
            "websurfer_model": self.websurfer_model,
            "app": f"http://{self.app_host}:{self.app_port}",
            "phoenix": self.phoenix_base_url,
            "headless": self.headless,
            "code_execution": self.enable_code_execution,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

