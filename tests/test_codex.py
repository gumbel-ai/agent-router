import unittest
from pathlib import Path

from router.child import child_environment, executor_command
from router.codex import collect, price_metadata
from router.config import Settings
from router.pricing import PriceTier


class CodexTelemetryTests(unittest.TestCase):
    def test_cumulative_usage_replaces_previous_events_and_prices_cache_writes(self):
        task = price_metadata("openai/gpt-6-luna", {})
        usage = {"input_tokens": 93987, "cached_input_tokens": 74878, "cache_write_input_tokens": 19094,
                 "output_tokens": 576, "reasoning_output_tokens": 178}
        for _ in range(2):
            collect(task, {"type": "turn.completed", "usage": usage})
        self.assertEqual(task["usage"], usage)
        self.assertAlmostEqual(task["cost"], (15 * .1 + 74878 * .01 + 19094 * .125 + 576 * .5) / 1_000_000)
        collect(task, {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
        self.assertNotIn("cost", task)
        self.assertIn("Cache-write", task["cost_note"])
        partial = {**price_metadata("openai/gpt-6-luna", {}), "attempt_number": 2,
                   "codex_usage_baseline": {"input_tokens": 50, "cached_input_tokens": 25}}
        collect(partial, {"type": "turn.completed", "usage": {"input_tokens": 60, "cached_input_tokens": 30, "output_tokens": 10}})
        self.assertEqual(partial["usage"], {"input_tokens": 10, "cached_input_tokens": 5})
        self.assertFalse(partial["usage_complete"])
        self.assertNotIn("cost", partial)

    def test_codex_uses_own_login_not_router_provider_overrides(self):
        environment = {"CODEX_API_KEY": "test-override", "OPENAI_BASE_URL": "https://example.invalid",
                       "OPENAI_API_KEY": "test-key", "CODEX_HOME": "/tmp/codex-config", "PATH": "/usr/bin"}
        actual = child_environment(environment, {"executor": "codex", "worktree": str(Path("/tmp/child")), "permission_profile": {"sandbox": "workspace-write"}})
        self.assertNotIn("CODEX_API_KEY", actual)
        self.assertNotIn("OPENAI_BASE_URL", actual)
        self.assertNotIn("OPENAI_API_KEY", actual)
        self.assertEqual(actual["CODEX_HOME"], environment["CODEX_HOME"])

    def test_operator_rates_and_partial_turns_preserve_unknown_total_cost(self):
        tier = PriceTier(input=1, cache_read=.1, output=2, cache_write_5m=0, cache_write_1h=0, cache_write=1)
        task = price_metadata("openai/custom", {"openai/custom": [tier]})
        collect(task, {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2}})
        self.assertAlmostEqual(task["cost"], .0000095)
        self.assertEqual(task["price_source"], "operator-configured")
        collect(task, {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 30, "output_tokens": 4}})
        self.assertFalse(task["usage_complete"])
        self.assertNotIn("cost", task)
        self.assertEqual(task["usage"]["input_tokens"], 20)
        tiered = price_metadata("openai/custom", {"openai/custom": [tier, tier.model_copy(update={"above_input_tokens": 100})]})
        collect(tiered, {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2}})
        self.assertNotIn("cost", tiered)
        self.assertTrue(tiered["usage_complete"])

    def test_invalid_or_changed_session_cannot_replace_an_owned_child(self):
        session = "12345678-1234-4321-8765-123456789abc"
        for observed in ("invalid", "87654321-1234-4321-8765-123456789abc"):
            task = {"child_session_id": session}
            collect(task, {"type": "thread.started", "thread_id": observed})
            self.assertTrue(task["execution_error"])
            self.assertEqual(task["child_session_id"], session)
        with self.assertRaisesRegex(ValueError, "cannot resume"):
            executor_command(Settings(), {"executor": "codex", "invocation": {"task": "do work"}, "attempts": 1})
