import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from router.config import Settings, load_settings, socket_path, state_dir


class ConfigTests(unittest.TestCase):
    def test_required_operator_configuration_and_bounds(self):
        valid = {"AGENT_ROUTER_CONCURRENCY_CAP": 3, "AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS": 30,
                 "AGENT_ROUTER_MAX_ATTEMPTS": 2, "AGENT_ROUTER_OPENCODE_COMMAND": ["opencode"],
                 "AGENT_ROUTER_MODELS": {"m": "provider/exact"},
                 "AGENT_ROUTER_REPOSITORIES": {"/tmp/project": {"setup_command": "true"}}}
        cases = [{key: value for key, value in valid.items() if key != missing} for missing in valid]
        cases += [{**valid, "AGENT_ROUTER_CONCURRENCY_CAP": 0}, {**valid, "AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS": float("inf")}]
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
                self.assertNotIn("PROVIDER_TEST_KEY", config.model_dump())
