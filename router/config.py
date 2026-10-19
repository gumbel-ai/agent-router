"""Startup-only configuration; provider credentials never enter task records."""

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from router.paths import configuration, path_setting, socket_path, state_dir  # noqa: F401 - public path API
from router.pricing import PriceTier


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    concurrency_cap: int | None = Field(default=None, gt=0, validation_alias="AGENT_ROUTER_CONCURRENCY_CAP")
    attempt_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False, validation_alias="AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS")
    max_attempts: int | None = Field(default=None, gt=0, validation_alias="AGENT_ROUTER_MAX_ATTEMPTS")
    opencode_command: list[str] | None = Field(default=None, min_length=1, validation_alias="AGENT_ROUTER_OPENCODE_COMMAND")
    codex_command: list[str] | None = Field(default=None, min_length=1, validation_alias="AGENT_ROUTER_CODEX_COMMAND")
    claude_command: list[str] | None = Field(default=None, min_length=1, max_length=1,
        description="One Claude executable path without arguments; use an operator forwarding wrapper if needed",
        validation_alias="AGENT_ROUTER_CLAUDE_COMMAND")
    models: dict[str, str] = Field(default_factory=dict, validation_alias="AGENT_ROUTER_MODELS")
    repositories: dict[str, dict[str, str]] = Field(default_factory=dict, validation_alias="AGENT_ROUTER_REPOSITORIES")
    model_prices: dict[str, list[PriceTier]] = Field(default_factory=dict, validation_alias="AGENT_ROUTER_MODEL_PRICES")
    # Whether this host's fleet snapshot includes task text written by users and agents.
    fleet_share_text: bool = Field(default=True, validation_alias="AGENT_ROUTER_FLEET_SHARE_TEXT")

    @property
    def delegation_enabled(self) -> bool:
        return bool(self.models and self.repositories and (self.opencode_command or self.codex_command or self.claude_command) and self.concurrency_cap
                    and self.attempt_timeout_seconds and self.max_attempts)

    @model_validator(mode="after")
    def complete_delegation(self):
        values = (self.models, self.repositories, self.opencode_command, self.codex_command, self.claude_command, self.concurrency_cap,
                  self.attempt_timeout_seconds, self.max_attempts)
        if any(values) and not self.delegation_enabled:
            raise ValueError("Delegation requires models, repositories, executor command, concurrency, timeout, and attempt limits; run router init")
        return self

    @field_validator("model_prices")
    @classmethod
    def ordered_price_tiers(cls, value: dict[str, list[PriceTier]]) -> dict[str, list[PriceTier]]:
        for model, tiers in value.items():
            thresholds = [tier.above_input_tokens for tier in tiers]
            if not model.strip() or not thresholds or thresholds[0] != 0 or thresholds != sorted(set(thresholds)):
                raise ValueError("Each exact model price needs ascending tiers beginning at zero")
        return value

    @field_validator("models")
    @classmethod
    def exact_models(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or "/" not in model or not model.strip() for key, model in value.items()):
            raise ValueError("Each model alias requires an exact provider/model ID")
        return value

    @field_validator("opencode_command", "codex_command", "claude_command")
    @classmethod
    def command_parts(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not part.strip() for part in value):
            raise ValueError("Executor command arguments cannot be empty")
        return value

    @field_validator("repositories")
    @classmethod
    def configured_repositories(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        result = {}
        for root, config in value.items():
            if not Path(root).expanduser().is_absolute() or not config.get("setup_command", "").strip():
                raise ValueError("Each repository requires an absolute path and setup_command")
            if "codex_sandbox" in config and config["codex_sandbox"] not in {"read-only", "workspace-write"}:
                raise ValueError("Codex sandbox must be read-only or workspace-write; unrestricted access is unsupported")
            if "tracing" in config and config["tracing"] != "requested":
                raise ValueError("Repository tracing must be exactly \"requested\" when present; omit it to leave tracing off")
            result[str(Path(root).expanduser().resolve())] = config
        return result


def load_settings(registry_values: dict | None = None) -> tuple[Settings, dict[str, str]]:
    values, environment = configuration(registry_values)
    settings = Settings(**values)
    return settings, environment
