"""Controlled CLI process for lifecycle tests; no provider calls or import stubs."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    version_directory = os.environ.get("ROUTER_FIXTURE_VERSION_DIRECTORY")
    if version_directory:
        with Path(version_directory, "launches").open("a") as launches:
            launches.write("version\n")
        ready = Path(version_directory, "helper.ready")
        helper = subprocess.Popen([sys.executable, "-c",
                                   ("import signal,time,sys; from pathlib import Path; "
                                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                    "Path(sys.argv[1]).touch(); time.sleep(60)"), str(ready)])
        while not ready.exists():
            time.sleep(.04)
        Path(version_directory, "helper.pid").write_text(str(helper.pid))
        if os.environ.get("ROUTER_FIXTURE_VERSION_MODE") == "wait":
            while not Path(version_directory, "release").exists():
                time.sleep(.04)
    print(os.environ.get("ROUTER_FIXTURE_CLAUDE_VERSION", "2.1.303") + " (Claude Code)")
    sys.exit(0)
claude = "--print" in sys.argv
codex = "exec" in sys.argv
if claude:
    option = "--resume" if "--resume" in sys.argv else "--session-id"
    session = sys.argv[sys.argv.index(option) + 1]
elif codex:
    session = sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else "12345678-1234-4321-8765-123456789abc"
else:
    session = sys.argv[sys.argv.index("--session") + 1]
model = sys.argv[sys.argv.index("--model") + 1]
prompt = sys.argv[-1]
sleeper = None


def interrupt(*_):
    if sleeper is not None and sleeper.poll() is None:
        sleeper.terminate()
        sleeper.wait(timeout=5)
    sys.exit(130)


if sys.stdin.read() != "":
    raise RuntimeError("stdin should be closed")
record = Path("session.json")
persistence_disabled = claude and bool(os.environ.get("CLAUDE_CODE_CHILD_SESSION"))
if claude and "--resume" in sys.argv and (persistence_disabled or not record.exists()):
    raise RuntimeError("Native session persistence was disabled; cannot resume")
previous = json.loads(record.read_text()) if record.exists() else {"sessions": [], "models": []}
previous["sessions"].append(session)
previous["models"].append(model)
previous.setdefault("commands", []).append(sys.argv[1:])
if not persistence_disabled:
    record.write_text(json.dumps(previous))
Path("result.txt").write_text("corrected\n" if "Parent feedback" in prompt else "first\n")
if codex or claude:
    if "ignore interrupt" in prompt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        Path("interrupt-ready").touch()
    else:
        signal.signal(signal.SIGINT, interrupt)
if claude:
    observed = "87654321-1234-4321-8765-123456789abc" if "wrong session" in prompt else session
    print(json.dumps({"type": "system", "subtype": "init", "session_id": observed,
                      "model": "claude-opus-4-6" if "wrong model" in prompt else model}), flush=True)
if codex and "no thread" not in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
    print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "run tests"}}), flush=True)
if Path(".venv/bin/python").exists():
    subprocess.run([str(Path(".venv/bin/python").absolute()), "-m", "unittest", "discover", "-s", "tests"], check=True)
if "hang" in prompt:
    separate_group = claude or codex and "ignore interrupt" not in prompt
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=separate_group)
    if separate_group:
        Path("sleeper.pid").write_text(str(sleeper.pid))
    if "crash" in prompt:
        os.kill(os.getpid(), signal.SIGKILL)
    if "silent" not in prompt:
        print(json.dumps({"type": "text", "part": {"text": "working"}}), flush=True)
    time.sleep(60)
if claude:
    count = len(previous["sessions"])
    if "no completion" not in prompt:
        event = {"type": "result", "session_id": session, "subtype": "error_during_execution" if "error" in prompt else "success",
                 "is_error": "error" in prompt, "result": "Finished " + session,
                 "total_cost_usd": .25 * count,
                 "modelUsage": {"claude-opus-4-6" if "wrong usage model" in prompt else model: {"inputTokens": 100 * count, "outputTokens": 20 * count,
                                         "cacheReadInputTokens": 40 * count, "cacheCreationInputTokens": 10 * count}}}
        print(json.dumps(event), flush=True)
    sys.exit(0)
if "error" in prompt:
    sys.stdout.write(json.dumps({"type": "turn.failed" if codex else "error", "error": {"message": "controlled failure"}}))
    sys.stdout.flush()
    sys.exit(0)
if "flood" in prompt:
    for stream in [sys.stdout, sys.stderr]:
        for _ in range(1000):
            stream.write("x" * 400 + "\n")
        stream.flush()
if codex:
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Finished " + session}}), flush=True)
    if "no completion" not in prompt:
        count = len(previous["sessions"])
        total = 100 * count + 50 * (count - 1)
        usage = {"input_tokens": total, "cached_input_tokens": total * 2 // 5,
                 "output_tokens": total // 5, "reasoning_output_tokens": 5 * count}
        if "partial usage" in prompt:
            usage.pop("cached_input_tokens")
        print(json.dumps({"type": "turn.completed", "usage": usage}), flush=True)
else:
    print(json.dumps({"type": "text", "part": {"text": "Finished " + session}}), flush=True)
    print(json.dumps({"type": "step_finish", "part": {"cost": 0.25, "tokens": {"input": 3, "output": 2, "cache": {"read": 1}}}}), flush=True)
