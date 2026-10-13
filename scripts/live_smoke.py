"""Opt-in real-host/provider verification on a generated, disposable repository.

Run with the installed router environment: python scripts/live_smoke.py --model
<exact provider/model> [--executor codex]. This makes four Claude turns and two
child runs. It uses existing CLI authentication and may incur provider charges.
Artifacts remain in the printed scratch directory for diagnosis.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from router.client import request
from router.db import Store
from router.snapshot import apply_delta, git


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--executor", choices=["opencode", "codex"], default="opencode")
    parser.add_argument("--parent-model", default="haiku")
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="ar-live-", dir="/tmp")).resolve()
    repo = directory / "repo"
    repo.mkdir()
    print(f"Live smoke artifacts: {directory}", flush=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Agent Router smoke test")
    (repo / ".gitignore").write_text(".venv/\n.env\n")
    (repo / "app.py").write_text("def answer():\n    return 1\n")
    (repo / "notes.txt").write_text("original parent\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("import unittest\nfrom app import answer\nclass AppTest(unittest.TestCase):\n def test_answer(self):\n  self.assertEqual(answer(), 2)\n")
    (repo / "opencode.json").write_text(json.dumps({"permission": {"*": "allow", "external_directory": "deny"}}))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "smoke baseline")
    (repo / "notes.txt").write_text("staged parent\n")
    git(repo, "add", "notes.txt")
    (repo / "notes.txt").write_text("staged parent\nunstaged parent\n")
    git(repo, "status", "--porcelain")
    index = repo / git(repo, "rev-parse", "--git-path", "index").strip()
    index_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    endpoint = directory / "router.sock"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
    environment.update(AGENT_ROUTER_STATE_DIR=str(directory / "state"), AGENT_ROUTER_SOCKET=str(endpoint),
                       AGENT_ROUTER_ENV_FILE=str(directory / "missing.env"),
                       AGENT_ROUTER_CONCURRENCY_CAP="1", AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS="120",
                       AGENT_ROUTER_MAX_ATTEMPTS="2", AGENT_ROUTER_OPENCODE_COMMAND='["opencode"]',
                       AGENT_ROUTER_MODELS=json.dumps({"live": args.model}),
                       AGENT_ROUTER_REPOSITORIES=json.dumps({str(repo): {"setup_command": f"{shlex.quote(sys.executable)} -m venv .venv"}}))
    if args.executor == "codex":
        environment.pop("AGENT_ROUTER_OPENCODE_COMMAND", None)
        environment["AGENT_ROUTER_CODEX_COMMAND"] = '["codex"]'
        environment["AGENT_ROUTER_REPOSITORIES"] = json.dumps({str(repo): {
            "setup_command": f"{shlex.quote(sys.executable)} -m venv .venv", "codex_sandbox": "workspace-write"}})
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", os.defpath)
    plugin = Path(__file__).resolve().parents[1] / "router" / "plugin"
    session = str(uuid.uuid4())
    token = "ROUTER-MEMORY-" + uuid.uuid4().hex[:8]
    daemon_out = (directory / "daemon.stdout").open("w")
    daemon_err = (directory / "daemon.stderr").open("w")
    daemon = subprocess.Popen([sys.executable, "-m", "router.cli", "daemon", "start"], env=environment,
                              stdout=daemon_out, stderr=daemon_err)
    turn = 0

    def parent(prompt: str):
        nonlocal turn
        turn += 1
        command = ["claude", "-p", "--plugin-dir", str(plugin), "--model", args.parent_model,
                   "--setting-sources", "", "--permission-mode", "dontAsk", "--allowedTools", "Bash(router *)",
                   "--tools", "Bash", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--session-id" if turn == 1 else "--resume", session, prompt]
        print(f"Claude parent turn {turn}", flush=True)
        result = subprocess.run(command, cwd=repo, env=environment, capture_output=True, text=True, timeout=90, check=False)
        (directory / f"parent-{turn}.stdout").write_text(result.stdout)
        (directory / f"parent-{turn}.stderr").write_text(result.stderr)
        if result.returncode:
            raise RuntimeError(f"Claude turn {turn} failed; inspect parent-{turn}.stderr")

    def wait_task(task_id):
        end = time.monotonic() + 140
        previous = None
        while time.monotonic() < end:
            task = request("status", {"task_id": task_id}, endpoint)
            if task["status"] != previous:
                print(f"{task_id}: {task['status']}", flush=True)
                previous = task["status"]
            if task["status"] in {"awaiting_review", "failed", "interrupted", "cancelled"}:
                return task
            time.sleep(0.3)
        raise RuntimeError("Task did not finish within its configured bound")

    try:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                request("status", {}, endpoint)
                break
            except ValueError:
                if daemon.poll() is not None:
                    raise RuntimeError("Daemon failed; inspect daemon.stderr")
                time.sleep(0.05)
        invocation = {"request_version": "1", "request_id": "live-start", "instruction_id": "USE_THE_HOOK_SUPPLIED_ID",
                      "executor": args.executor, "model": "live", "parent_repo": str(repo), "parent_worktree": str(repo), "mode": "write",
                      "task": "Change app.answer() to return 2. Run .venv/bin/python -m unittest discover -s tests. Do not commit. Remember the token " + token + " for the next attempt."}
        try:
            request("start", invocation, endpoint)
            raise AssertionError("Unauthorized start was accepted")
        except ValueError:
            pass
        parent("Run exactly the following standalone Bash command. Replace only USE_THE_HOOK_SUPPLIED_ID with the ROUTER_INSTRUCTION_ID supplied in hook context. "
               "Do not probe the environment or inspect the CLI: the command below already supplies the exact binary and JSON argument syntax. "
               "Report the returned task ID and stop; do not execute the child task yourself. Command: " +
               shlex.join(["router", "start", "--json", json.dumps(invocation)]))
        store = Store(directory / "state" / "router.sqlite3")
        all_tasks = store.all()
        if len(all_tasks) != 1:
            raise RuntimeError("Real host did not register exactly one task; inspect parent-1.stdout")
        task_id = all_tasks[0]["task_id"]
        first = wait_task(task_id)
        assert first["status"] == "awaiting_review", first
        assert first["resolved_model"] == args.model
        assert (Path(first["worktree"]) / "app.py").read_text() != (repo / "app.py").read_text()
        assert not (repo / ".venv").exists()
        original = store.get(task_id)["invocation"]
        assert request("start", original, endpoint)["task_id"] == task_id
        try:
            request("start", {**original, "task": "different payload"}, endpoint)
            raise AssertionError("Changed replay was accepted")
        except ValueError:
            pass
        review = {"task_id": task_id, "request_id": "live-reject", "expected_attempt": 1, "outcome": "rejected",
                  "feedback": "Add a docstring to answer(). Write the token I asked you to remember in the previous attempt into continuity.txt. Run the repository tests again. Do not commit."}
        parent("Run exactly this command through Bash, then stop: " + shlex.join(["router", "review", "--json", json.dumps(review)]))
        second = wait_task(task_id)
        assert second["status"] == "awaiting_review" and second["attempts"] == 2, second
        assert second["child_session_id"] == first["child_session_id"] and second["worktree"] == first["worktree"]
        if args.executor == "codex":
            stdout = next((Path(second["artifact_dir"]) / "attempt-2" / "child").glob("*.stdout"))
            events = [json.loads(line) for line in stdout.read_text().splitlines() if line.startswith("{")]
            reported = [event["usage"] for event in events if event.get("type") == "turn.completed"][-1]
            assert second["usage"] == reported, "Router totals must equal the latest cumulative Codex counters"
            assert second["usage_complete"] and second["cost"] is not None, second
            assert second["cost_source"] == "token_estimate", second
            print(f"Codex usage: {json.dumps(second['usage'], sort_keys=True)}; estimated USD {second['cost']:.8f}", flush=True)
        assert (Path(second["worktree"]) / "continuity.txt").read_text().strip() == token
        subprocess.run([str(Path(second["worktree"]) / ".venv" / "bin" / "python"), "-m", "unittest", "discover", "-s", "tests"], cwd=second["worktree"], check=True)
        accept = {"task_id": task_id, "request_id": "live-accept", "expected_attempt": 2, "outcome": "accepted"}
        parent("Run exactly this command through Bash, then stop: " + shlex.join(["router", "review", "--json", json.dumps(accept)]))
        assert request("status", {"task_id": task_id}, endpoint)["status"] == "accepted"
        (repo / "later.txt").write_text("unrelated parent edit\n")
        apply_delta(repo, Path(second["patch"]))
        assert (repo / "later.txt").read_text() == "unrelated parent edit\n"
        assert (repo / "notes.txt").read_text() == "staged parent\nunstaged parent\n"
        assert hashlib.sha256(index.read_bytes()).hexdigest() == index_hash
        close = {"task_id": task_id, "request_id": "live-close", "disposition": "integrated"}
        parent("Run exactly this command through Bash, then stop: " + shlex.join(["router", "close", "--json", json.dumps(close)]))
        end = time.monotonic() + 10
        while time.monotonic() < end and request("status", {"task_id": task_id}, endpoint).get("cleanup_pending"):
            time.sleep(0.1)
        closed = request("status", {"task_id": task_id}, endpoint)
        assert closed["status"] == "closed" and not closed["cleanup_pending"], closed
        assert not Path(second["worktree"]).exists()
        print("PASS: real hooks, trusted registration, exact model, isolated setup, native resume, parent-index preservation, integration, and cleanup", flush=True)
        (directory / "verification.json").write_text(json.dumps({"task_id": task_id, "model": args.model, "status": "passed", "attempts": 2,
            "child_session_id": second["child_session_id"], "usage": second["usage"], "cost": second["cost"], "cost_source": second["cost_source"]}, indent=2))
    finally:
        if daemon.poll() is None:
            request("daemon_stop", {}, endpoint)
            daemon.wait(timeout=20)
        daemon_out.close()
        daemon_err.close()


if __name__ == "__main__":
    main()
