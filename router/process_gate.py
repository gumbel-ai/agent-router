"""Release-gated group leader; completion evidence survives daemon death."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    fd = int(sys.argv[1])
    destination = Path(sys.argv[2])
    disposable = sys.argv[3] == "--dispose-group"
    command = sys.argv[4:] if disposable else sys.argv[3:]
    if disposable and os.getpgrp() != os.getpid():
        raise RuntimeError("Disposable phase requires an independently owned process group")
    try:
        release = os.read(fd, 1)
    finally:
        os.close(fd)
    if release != b"1":
        sys.exit(125)
    # Remain the identifiable leader during graceful interruption/termination.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        code = child.wait()
        result = {"exit_code": code, "finished_at": int(time.time() * 1000)}
    except OSError as error:
        result = {"exit_code": 127, "error": type(error).__name__, "finished_at": int(time.time() * 1000)}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(destination)
    if disposable:
        # Keep the identifiable leader until the entire disposable group is killed.
        # Completion remains durable if the daemon dies during bounded cleanup.
        os.killpg(os.getpid(), signal.SIGTERM)
        time.sleep(0.5)
        os.killpg(os.getpid(), signal.SIGKILL)
    sys.exit(0)


if __name__ == "__main__":
    main()
