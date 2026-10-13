import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from router.config import Settings, load_settings, socket_path, state_dir
from router.paths import runtime_pointer


class ConfigTests(unittest.TestCase):
    def test_observation_needs_no_executor_model_or_repository_configuration(self):
        with tempfile.TemporaryDirectory(prefix="ar-observe-config-") as temp:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
            environment.update(AGENT_ROUTER_ENV_FILE=str(Path(temp) / "missing.env"), AGENT_ROUTER_STATE_DIR=temp)
            with patch.dict(os.environ, environment, clear=True):
                config, _ = load_settings()
                self.assertFalse(config.delegation_enabled)
                self.assertEqual(config.repositories, {})
                self.assertEqual(config.models, {})

    def test_required_operator_configuration_and_bounds(self):
        valid = {"AGENT_ROUTER_CONCURRENCY_CAP": 3, "AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS": 30,
                 "AGENT_ROUTER_MAX_ATTEMPTS": 2, "AGENT_ROUTER_OPENCODE_COMMAND": ["opencode"],
                 "AGENT_ROUTER_MODELS": {"m": "provider/exact"},
                 "AGENT_ROUTER_REPOSITORIES": {"/tmp/project": {"setup_command": "true"}}}
        cases = [{key: value for key, value in valid.items() if key != missing} for missing in valid]
        cases += [{**valid, "AGENT_ROUTER_CONCURRENCY_CAP": 0}, {**valid, "AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS": float("inf")}]
        cases += [{**valid, "AGENT_ROUTER_REPOSITORIES": {"/tmp/project": {"setup_command": "true", "codex_sandbox": "danger-full-access"}}},
                  {**valid, "AGENT_ROUTER_CODEX_COMMAND": [""]}]
        cases += [{**valid, "AGENT_ROUTER_MODEL_PRICES": {"m": []}},
                  {**valid, "AGENT_ROUTER_MODEL_PRICES": {"m": [{"input": float("nan"), "output": 1,
                      "cache_read": 0, "cache_write_5m": 1, "cache_write_1h": 1}]}}]
        def check(values):
            with self.assertRaises(ValidationError):
                Settings(**values)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(check, cases))

    def test_common_env_preserves_provider_variables_and_paths(self):
        with tempfile.TemporaryDirectory(prefix="ar-env-") as temp:
            root = Path(temp)
            env_file = root / "router.env"
            env_file.write_text(
                "AGENT_ROUTER_CONCURRENCY_CAP=2\nAGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=20\n"
                "AGENT_ROUTER_MAX_ATTEMPTS=3\nAGENT_ROUTER_OPENCODE_COMMAND='[\"opencode\"]'\n"
                "AGENT_ROUTER_MODELS='{\"m\":\"provider/exact\"}'\n"
                "AGENT_ROUTER_REPOSITORIES='{\"/tmp/project\":{\"setup_command\":\"true\"}}'\n"
                f"AGENT_ROUTER_STATE_DIR={root}/state\nAGENT_ROUTER_SOCKET={root}/router.sock\n"
                "PROVIDER_TEST_KEY=not-a-real-credential\n")
            with patch.dict(os.environ, {"AGENT_ROUTER_ENV_FILE": str(env_file)}):
                config, environment = load_settings()
                self.assertEqual(config.concurrency_cap, 2)
                self.assertEqual(environment["PROVIDER_TEST_KEY"], "not-a-real-credential")
                self.assertEqual(state_dir(), root / "state")
                self.assertEqual(socket_path(), root / "router.sock")
                self.assertEqual(runtime_pointer(), Path(os.environ.get("AGENT_ROUTER_STATE_DIR", "~/.agent-router")).expanduser() / "python-path")
                self.assertNotIn("PROVIDER_TEST_KEY", config.model_dump())
