import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from router.codex_plugin import (
    HOOKS_FILE,
    PLUGIN_ID,
    availability,
    expected_trust,
    hook_hash,
)
from router.setup import install_codex_plugin

ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((ROOT / HOOKS_FILE).read_text())


class CodexPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-codex-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        which = patch("router.codex_plugin.shutil.which", return_value="/usr/bin/codex")
        which.start()
        self.addCleanup(which.stop)

    def configure(self, plugin="enabled = true\n", entries=None):
        lines = []
        if plugin is not None:
            lines.append(f'[plugins."{PLUGIN_ID}"]\n{plugin}')
        for key, entry in (entries or {}).items():
            lines.append(f'[hooks.state."{key}"]\n' + "".join(f"{name} = {json.dumps(value)}\n"
                                                               for name, value in entry.items()))
        (self.home / "config.toml").write_text("\n".join(lines))
        return availability(self.home)["state"]

    def test_hash_matches_a_hook_trusted_by_codex(self):
        vector = json.loads((ROOT / "tests" / "codex_trust_vector.json").read_text())
        self.assertEqual(hook_hash(vector["event"], vector["group"]), vector["trusted_hash"])

    def test_plugin_manifest_points_at_hooks_whose_command_exists(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["hooks"], "./" + HOOKS_FILE)
        commands = {handler["command"] for groups in HOOKS["hooks"].values()
                    for group in groups for handler in group["hooks"]}
        self.assertEqual(commands, {'sh "${PLUGIN_ROOT}/router/plugin/codex/hook.sh"'})
        self.assertTrue((ROOT / "router" / "plugin" / "codex" / "hook.sh").is_file())
        self.assertEqual(len(expected_trust()), len(HOOKS["hooks"]))

    def test_availability_names_the_missing_prerequisite(self):
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust().items()}
        first = next(iter(trusted))
        self.assertEqual(self.configure(plugin=None), "plugin_not_installed")
        self.assertEqual(self.configure("enabled = false\n", trusted), "plugin_disabled")
        self.assertEqual(self.configure(entries={}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {"trusted_hash": "sha256:old"}}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {**trusted[first], "enabled": False}}),
                         "hooks_disabled")
        self.assertEqual(self.configure(entries=trusted), "ready")
        (self.home / "config.toml").write_text("[plugins\n")
        self.assertEqual(availability(self.home)["state"], "unknown")
        with patch("router.codex_plugin.shutil.which", return_value=None):
            self.assertEqual(availability(self.home)["state"], "codex_not_installed")

    def test_hook_answers_codex_even_without_router(self):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENT_ROUTER_STATE_DIR": str(self.home / "none")}
        result = subprocess.run(["sh", str(ROOT / "router" / "plugin" / "codex" / "hook.sh")], env=environment,
                                input='{"hook_event_name":"Stop"}', capture_output=True, text=True,
                                timeout=30, check=False)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "{}"), result.stderr)

    def test_setup_install_is_best_effort(self):
        for codes, installed in (([0, 0], True), ([0, 1], False)):
            results = [subprocess.CompletedProcess([], code) for code in codes]
            with self.subTest(codes=codes), patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                    patch("router.setup.subprocess.run", side_effect=results) as command, \
                    patch("builtins.print") as output:
                install_codex_plugin("owner/repo")
            self.assertEqual([call.args[0] for call in command.call_args_list],
                             [["codex", "plugin", "marketplace", "add", "owner/repo"],
                              ["codex", "plugin", "add", PLUGIN_ID]])
            self.assertEqual("/hooks" in output.call_args.args[0], installed)
        with patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                patch("router.setup.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 120)), \
                patch("builtins.print") as output:
            install_codex_plugin("owner/repo")
        self.assertIn("Install it later", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
