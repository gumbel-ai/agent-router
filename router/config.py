"""Startup-only configuration; provider credentials never enter task records."""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from router.pricing import PriceTier


def path_setting(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    if value is None:
        env_file = Path(os.environ.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser()
        value = dotenv_values(env_file).get(name)
    return Path(value or fallback).expanduser()


def state_dir() -> Path:
    return path_setting("AGENT_ROUTER_STATE_DIR", "~/.agent-router")


def socket_path() -> Path:
    return path_setting("AGENT_ROUTER_SOCKET", str(state_dir() / "router.sock"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    concurrency_cap: int = Field(gt=0, validation_alias="AGENT_ROUTER_CONCURRENCY_CAP")
    attempt_timeout_seconds: float = Field(gt=0, allow_inf_nan=False, validation_alias="AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS")
    max_attempts: int = Field(gt=0, validation_alias="AGENT_ROUTER_MAX_ATTEMPTS")
    opencode_command: list[str] = Field(min_length=1, validation_alias="AGENT_ROUTER_OPENCODE_COMMAND")
    models: dict[str, str] = Field(min_length=1, validation_alias="AGENT_ROUTER_MODELS")
    repositories: dict[str, dict[str, str]] = Field(min_length=1, validation_alias="AGENT_ROUTER_REPOSITORIES")
    model_prices: dict[str, list[PriceTier]] = Field(default_factory=dict, validation_alias="AGENT_ROUTER_MODEL_PRICES")

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

    @field_validator("opencode_command")
    @classmethod
    def command_parts(cls, value: list[str]) -> list[str]:
        if any(not part.strip() for part in value):
            raise ValueError("Executor command arguments cannot be empty")
        return value

    @field_validator("repositories")
    @classmethod
    def configured_repositories(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        result = {}
        for root, config in value.items():
            if not Path(root).expanduser().is_absolute() or not config.get("setup_command", "").strip():
                raise ValueError("Each repository requires an absolute path and setup_command")
            result[str(Path(root).expanduser().resolve())] = config
        return result


def load_settings() -> tuple[Settings, dict[str, str]]:
    env_file = Path(os.environ.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser()
    settings = Settings(_env_file=env_file)
    # python-dotenv is the dotenv loader required by pydantic-settings.
    environment = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    environment.update(os.environ)
    return settings, environment
