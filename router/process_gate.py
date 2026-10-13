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
    command = sys.argv[3:]
    try:
        release = os.read(fd, 1)
    finally:
        os.close(fd)
    if release != b"1":
        sys.exit(125)
    # Remain the identifiable leader while the whole group receives SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        code = child.wait()
        result = {"exit_code": code, "finished_at": int(time.time() * 1000)}
    except OSError as error:
        result = {"exit_code": 127, "error": type(error).__name__, "finished_at": int(time.time() * 1000)}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(destination)
    sys.exit(0)


if __name__ == "__main__":
    main()
