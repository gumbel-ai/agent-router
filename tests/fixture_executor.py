"""Controlled CLI process for lifecycle tests; no provider calls or import stubs."""

import json
import subprocess
import sys
import time
from pathlib import Path

session = sys.argv[sys.argv.index("--session") + 1]
model = sys.argv[sys.argv.index("--model") + 1]
prompt = sys.argv[-1]
if sys.stdin.read() != "":
    raise RuntimeError("stdin should be closed")
record = Path("session.json")
previous = json.loads(record.read_text()) if record.exists() else {"sessions": [], "models": []}
previous["sessions"].append(session)
previous["models"].append(model)
record.write_text(json.dumps(previous))
Path("result.txt").write_text("corrected\n" if "Parent feedback" in prompt else "first\n")
if Path(".venv/bin/python").exists():
    subprocess.run([str(Path(".venv/bin/python").absolute()), "-m", "unittest", "discover", "-s", "tests"], check=True)
if "hang" in prompt:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if "silent" not in prompt:
        print(json.dumps({"type": "text", "part": {"text": "working"}}), flush=True)
    time.sleep(60)
if "error" in prompt:
    sys.stdout.write(json.dumps({"type": "error", "error": {"message": "controlled failure"}}))
    sys.stdout.flush()
    sys.exit(0)
if "flood" in prompt:
    for stream in [sys.stdout, sys.stderr]:
        for _ in range(1000):
            stream.write("x" * 400 + "\n")
        stream.flush()
print(json.dumps({"type": "text", "part": {"text": "Finished " + session}}), flush=True)
print(json.dumps({"type": "step_finish", "part": {"cost": 0.25, "tokens": {"input": 3, "output": 2, "cache": {"read": 1}}}}), flush=True)
