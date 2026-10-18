"""Opt-in paid check: a real Claude parent and one native subagent."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from router.cli import table
from router.client import request
from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="haiku")
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="ar-native-live-")).resolve()
    repository = directory / "repo"
    repository.mkdir()
    (repository / PROJECT_MARKER).write_text(json.dumps(PROJECT_MARKER_CONTENT))  # Router observes opted-in projects only.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_ROUTER_")}
    environment.update(AGENT_ROUTER_STATE_DIR=str(directory / "state"), AGENT_ROUTER_SOCKET=str(directory / "router.sock"),
                       AGENT_ROUTER_ENV_FILE=str(directory / "missing.env"), AGENT_ROUTER_CONCURRENCY_CAP="1",
                       AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS="30", AGENT_ROUTER_MAX_ATTEMPTS="1",
                       AGENT_ROUTER_OPENCODE_COMMAND='["opencode"]', AGENT_ROUTER_MODELS='{"unused":"provider/exact"}',
                       AGENT_ROUTER_REPOSITORIES=json.dumps({str(repository): {"setup_command": "true"}}))
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
    plugin = Path(__file__).resolve().parents[1] / "router" / "plugin"
    parent = None
    with (directory / "daemon.log").open("w") as daemon_output, (directory / "parent.json").open("w") as parent_output:
        daemon = subprocess.Popen([sys.executable, "-m", "router.cli", "daemon", "start"], env=environment,
                                  stdout=daemon_output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while True:
                try:
                    request("status", {}, Path(environment["AGENT_ROUTER_SOCKET"]))
                    break
                except ValueError:
                    if daemon.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Native smoke daemon did not start") from None
                    time.sleep(.05)
            deadline = time.monotonic() + 90
            parent = subprocess.Popen(["claude", "-p", "--model", args.model, "--tools", "Agent,Bash",
                "--allowedTools", "Agent,Bash", "--setting-sources", "", "--plugin-dir", str(plugin),
                "--output-format", "json", ("Spawn exactly one general-purpose native subagent with description 'Native visibility smoke'. "
                f"Request model {args.model}. Tell it to run Bash command 'sleep 3' exactly once and then reply 'native smoke done'. "
                "Do not write any files and do not use router start. Wait for its completion, then answer 'smoke complete'.")],
                cwd=repository, env=environment, stdout=parent_output, stderr=subprocess.STDOUT)
            seen_running = False
            while parent.poll() is None:
                rows = request("status", {}, Path(environment["AGENT_ROUTER_SOCKET"]))
                seen_running |= any(row["status"] == "running" and row.get("control_mode") == "observe" for row in rows)
                if time.monotonic() > deadline:
                    raise RuntimeError("Native smoke parent timed out")
                time.sleep(.1)
            if parent.returncode:
                raise RuntimeError("Claude native smoke failed; inspect parent.json")
            rows = request("status", {}, Path(environment["AGENT_ROUTER_SOCKET"]))
            assert seen_running, "No native running observation"
            assert len(rows) == 1 and rows[0]["control_mode"] == "observe", rows
            record = rows[0]
            assert record["status"] == "stopped", record
            assert record["parent_agent_id"] == "main" and record["task"] == "Native visibility smoke", record
            assert record["elapsed_ms"] > 0, record
            # Native records are lifecycle only; content, usage, and cost belong to Langfuse.
            retired = {"usage", "cost", "cost_source", "summary", "files_touched", "transcript", "worktree"}
            assert not retired & record.keys(), record
            assert record.get("resolved_model") is None or record["model_source"] == "host_result", record
            (directory / "observed.json").write_text(json.dumps(record, indent=2))
            print(table(rows))
            print("Native visibility smoke passed. Scratch:", directory)
        finally:
            if parent is not None and parent.poll() is None:
                stop_process(parent)
            if daemon.poll() is None:
                try:
                    request("daemon_stop", {}, Path(environment["AGENT_ROUTER_SOCKET"]))
                    daemon.wait(timeout=15)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    stop_process(daemon)


if __name__ == "__main__":
    main()
