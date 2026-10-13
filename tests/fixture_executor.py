"""Controlled CLI process for lifecycle tests; no provider calls or import stubs."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

codex = "exec" in sys.argv
if codex:
    session = sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else "12345678-1234-4321-8765-123456789abc"
else:
    session = sys.argv[sys.argv.index("--session") + 1]
model = sys.argv[sys.argv.index("--model") + 1]
prompt = sys.argv[-1]
if sys.stdin.read() != "":
    raise RuntimeError("stdin should be closed")
record = Path("session.json")
previous = json.loads(record.read_text()) if record.exists() else {"sessions": [], "models": []}
previous["sessions"].append(session)
previous["models"].append(model)
previous.setdefault("commands", []).append(sys.argv[1:])
record.write_text(json.dumps(previous))
Path("result.txt").write_text("corrected\n" if "Parent feedback" in prompt else "first\n")
if codex:
    if "ignore interrupt" in prompt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        Path("interrupt-ready").touch()
    else:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
if codex and "no thread" not in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
    print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "run tests"}}), flush=True)
if Path(".venv/bin/python").exists():
    subprocess.run([str(Path(".venv/bin/python").absolute()), "-m", "unittest", "discover", "-s", "tests"], check=True)
if "hang" in prompt:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if "silent" not in prompt:
        print(json.dumps({"type": "text", "part": {"text": "working"}}), flush=True)
    time.sleep(60)
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
