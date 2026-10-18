import json
import subprocess
import tempfile
import tomllib
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
from router.setup import register_codex_marketplace

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

    def install(self, version, declarations=HOOKS):
        hooks = self.home.joinpath("plugins", "cache", "agent-router", "agent-router", version, HOOKS_FILE)
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(json.dumps(declarations))

    def configure(self, plugin="enabled = true\n", entries=None, extra=""):
        lines = [extra]
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
        # Router stamps events on receipt; background hooks could arrive out of order.
        self.assertFalse(any(handler.get("async") for groups in HOOKS["hooks"].values()
                             for group in groups for handler in group["hooks"]))
        self.assertTrue((ROOT / "router" / "plugin" / "codex" / "hook.sh").is_file())
        self.assertEqual(len(expected_trust(HOOKS)), len(HOOKS["hooks"]))
        # Codex caches the plugin under its manifest version; keep it tied to the package.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(manifest["version"], project["project"]["version"])

    def test_availability_names_the_missing_prerequisite(self):
        trusted = {key: {"trusted_hash": value} for key, value in expected_trust(HOOKS).items()}
        first = next(iter(trusted))
        self.assertEqual(self.configure(entries=trusted), "plugin_not_installed")
        self.install("0.9.0")
        self.assertEqual(self.configure(plugin=None), "plugin_not_installed")
        self.assertEqual(self.configure("enabled = false\n", trusted), "plugin_disabled")
        self.assertEqual(self.configure(entries={}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {"trusted_hash": "sha256:old"}}), "hooks_untrusted")
        self.assertEqual(self.configure(entries={**trusted, first: {**trusted[first], "enabled": False}}),
                         "hooks_disabled")
        self.assertEqual(self.configure(entries=trusted), "ready")
        for extra, state in (("[features]\nhooks = false\n", "hooks_disabled"),
                             ("[features]\ncodex_hooks = false\n", "hooks_disabled"),
                             ("[features]\ncodex_hooks = false\nhooks = true\n", "ready"),
                             ("plugins = 1\n", "unknown"), ("hooks = true\n", "unknown")):
            with self.subTest(extra=extra):
                self.assertEqual(self.configure(entries=trusted, extra=extra), state)
        # Codex trusts the copy it loads: the highest cached version, not Router's package.
        changed = json.loads(json.dumps(HOOKS))
        next(iter(changed["hooks"].values()))[0]["hooks"][0]["timeout"] = 3
        self.install("0.10.0", changed)
        self.assertEqual(self.configure(entries=trusted), "hooks_untrusted")
        retrusted = {key: {"trusted_hash": value} for key, value in expected_trust(changed).items()}
        self.assertEqual(self.configure(entries=retrusted), "ready")
        # SemVer order, as Codex sorts it: 0.10.0 still wins over 0.10.0-beta.1 and 0.9.0.
        self.install("0.10.0-beta.1")
        self.assertEqual(self.configure(entries=retrusted), "ready")
        self.install("0.11.0-beta.1")
        self.assertEqual(self.configure(entries=retrusted), "hooks_untrusted")
        # Every lifecycle event must keep a handler, or observation is incomplete.
        without_end = {"hooks": {event: groups for event, groups in HOOKS["hooks"].items() if event != "SessionEnd"}}
        for declarations in (without_end, {"hooks": {}}):
            self.install("0.11.0-beta.1", declarations)
            trusted_rest = {key: {"trusted_hash": value} for key, value in expected_trust(declarations).items()}
            self.assertEqual(self.configure(entries=trusted_rest), "unknown")
        (self.home / "config.toml").write_bytes(b"\xff")
        self.assertEqual(availability(self.home)["state"], "unknown")
        (self.home / "config.toml").write_text("[plugins\n")
        self.assertEqual(availability(self.home)["state"], "unknown")
        with patch("router.codex_plugin.shutil.which", return_value=None):
            self.assertEqual(availability(self.home)["state"], "codex_not_installed")

    def test_hook_answers_codex_even_without_router(self):
        state = self.home / "state"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENT_ROUTER_STATE_DIR": str(state)}
        for pointer in (None, str(self.home / "removed" / "python")):
            if pointer:
                state.mkdir()
                (state / "python-path").write_text(pointer + "\n")
            with self.subTest(pointer=pointer):
                result = subprocess.run(["sh", str(ROOT / "router" / "plugin" / "codex" / "hook.sh")], env=environment,
                                        input='{"hook_event_name":"Stop"}', capture_output=True, text=True,
                                        timeout=30, check=False)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "{}"), result.stderr)

    def test_setup_registration_is_best_effort_and_never_enables_plugins(self):
        for codes, installed in (([0], True), ([1], False)):
            results = [subprocess.CompletedProcess([], code) for code in codes]
            with self.subTest(codes=codes), patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                    patch("router.setup.subprocess.run", side_effect=results) as command, \
                    patch("builtins.print") as output:
                register_codex_marketplace("owner/repo")
            self.assertEqual([call.args[0] for call in command.call_args_list],
                             [["codex", "plugin", "marketplace", "add", "owner/repo"]])
            self.assertEqual("registered" in output.call_args.args[0], installed)
        for failure in (subprocess.TimeoutExpired("codex", 120), OSError("bad interpreter")):
            with self.subTest(failure=failure), patch("router.setup.shutil.which", return_value="/usr/bin/codex"), \
                    patch("router.setup.subprocess.run", side_effect=failure), \
                    patch("builtins.print") as output:
                register_codex_marketplace("owner/repo")
            self.assertIn("Register it later", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
