import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from router.snapshot import git


class EnableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-enable-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "example-project"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-qm", "initial")
        self.calls = self.root / "calls.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        installer = self.bin / "claude"
        installer.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            "with Path(os.environ['ROUTER_TEST_CALLS']).open('a') as stream:\n"
            " stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('ROUTER_TEST_INSTALL_FAIL'):\n"
            " print('fixture install failed', file=sys.stderr)\n sys.exit(1)\n")
        installer.chmod(0o700)
        self.home = self.root / "home"
        self.codex = self.root / "selected-codex"
        self.codex.mkdir()
        self.global_config = self.codex / "config.toml"
        self.global_config.write_text('[plugins."example@fixture"]\nenabled = true\n')
        self.environment = {**os.environ, "HOME": str(self.home), "CODEX_HOME": str(self.codex),
                            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                            "ROUTER_TEST_CALLS": str(self.calls)}

    def enable(self, cwd=None, **environment):
        return subprocess.run([sys.executable, "-m", "router.cli", "enable"], cwd=cwd or self.repo,
                              env={**self.environment, **environment}, capture_output=True,
                              text=True, timeout=30, check=False)

    def test_enable_merges_project_configuration_idempotently_from_subdirectories_and_worktrees(self):
        claude = self.repo / ".claude" / "settings.json"
        codex = self.repo / ".codex" / "config.toml"
        claude.parent.mkdir()
        codex.parent.mkdir()
        claude.write_text(json.dumps({"env": {"EXAMPLE": "keep"}, "enabledPlugins": {"example@fixture": False}}))
        original = '# keep this comment\nmodel = "example-model"\n[plugins."example@fixture"]\nenabled = true\n'
        codex.write_text(original)
        subdirectory = self.repo / "subdirectory"
        subdirectory.mkdir()
        local_settings = subdirectory / ".claude/settings.json"
        local_settings.parent.mkdir()
        local_settings.write_text('{"env": {"LOCAL": "keep"}}')
        for cwd in (self.repo, subdirectory):
            result = self.enable(cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.repo / ".agent-router.json").read_bytes(), b'{"observe": true}\n')
            self.assertEqual(json.loads(claude.read_text()), {"env": {"EXAMPLE": "keep"},
                "enabledPlugins": {"example@fixture": False, "agent-router@agent-router": True}})
            self.assertIn(original, codex.read_text())
            config = tomllib.loads(codex.read_text())
            self.assertIs(config["plugins"]["agent-router@agent-router"]["enabled"], True)
            self.assertNotIn("hooks", config)
            self.assertEqual(self.global_config.read_text(), '[plugins."example@fixture"]\nenabled = true\n')
        self.assertEqual(json.loads(local_settings.read_text()),
                         {"env": {"LOCAL": "keep"}, "enabledPlugins": {"agent-router@agent-router": True}})
        codex.write_text('# inline configuration\nplugins = {"agent-router@agent-router" = {enabled = false}, '
                         '"example@fixture" = {enabled = false}}\n')
        result = self.enable()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tomllib.loads(codex.read_text())["plugins"],
                         {"agent-router@agent-router": {"enabled": True}, "example@fixture": {"enabled": False}})
        self.assertIn("# inline configuration", codex.read_text())
        before = (claude.read_bytes(), codex.read_bytes())
        self.assertEqual(self.enable().returncode, 0)
        self.assertEqual((claude.read_bytes(), codex.read_bytes()), before)
        git(self.repo, "add", ".agent-router.json", ".claude/settings.json", ".codex/config.toml")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Enable observation")
        worktree = self.root / "worktree"
        git(self.repo, "worktree", "add", "-qb", "worker-a", str(worktree))
        result = self.enable(worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worktree / ".agent-router.json").read_bytes(), b'{"observe": true}\n')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertTrue(all(call == ["plugin", "install", "agent-router@agent-router", "--scope", "project"] for call in calls))
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_enable_refuses_malformed_or_linked_configuration_before_installing(self):
        cases = ((".agent-router.json", '{"observe": false}'), (".claude/settings.json", "[]"),
                 (".claude/settings.json", '{"enabledPlugins": []}'), (".codex/config.toml", "[broken"),
                 (".claude/settings.json", '{"env": {"EXAMPLE": "first", "EXAMPLE": "second"}}'),
                 (".codex/config.toml", 'plugins = "invalid"'), (".codex/config.toml", "symlink"))
        for name, content in cases:
            with self.subTest(path=name, content=content):
                target = self.repo / name
                target.parent.mkdir(exist_ok=True)
                if content == "symlink":
                    target.symlink_to(self.global_config)
                else:
                    target.write_text(content)
                try:
                    result = self.enable()
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(name, result.stderr)
                    self.assertFalse(self.calls.exists())
                    self.assertEqual(target.read_text(), self.global_config.read_text() if content == "symlink" else content)
                finally:
                    for path in (self.repo / ".agent-router.json", self.repo / ".claude/settings.json",
                                 self.repo / ".codex/config.toml", self.calls):
                        path.unlink(missing_ok=True)
        with self.subTest(path="linked directory"):
            directory = self.repo / ".claude"
            directory.rmdir()
            directory.symlink_to(self.codex, target_is_directory=True)
            result = self.enable()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(".claude", result.stderr)
            self.assertFalse(self.calls.exists())
            self.assertFalse((self.codex / "settings.json").exists())

    def test_broken_installer_does_not_admit_project_or_write_project_configuration(self):
        result = self.enable(ROUTER_TEST_INSTALL_FAIL="1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("fixture install failed", result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse((self.repo / ".agent-router.json").exists())
        self.assertFalse((self.repo / ".codex/config.toml").exists())
