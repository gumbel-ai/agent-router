"""Durable foreground supervisor for an explicitly managed command."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from router.managed_completion import completion_payload
from router.redaction import redact_secrets


def _write_completion(path: Path, result: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(result, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _exec_command(release_fd: int, command: list[str]) -> None:
    try:
        released = os.read(release_fd, 1)
    finally:
        os.close(release_fd)
    if released != b"1":
        raise SystemExit(125)
    # The supervisor ignores these signals so the foreground CLI can forward
    # them to the process group. Ignored dispositions survive exec, so restore
    # normal command behavior in the actual child first.
    for forwarded in (
        signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
        signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU,
    ):
        signal.signal(forwarded, signal.SIG_DFL)
    try:
        os.execvp(command[0], command)
    except OSError as error:
        executable = redact_secrets(command[0], 240)
        print(f"router run: cannot execute {executable}: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(127) from error


def _supervise(release_fd: int, handshake_fd: int, completion: Path,
               command: list[str]) -> int:
    for job_control in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
        signal.signal(job_control, signal.SIG_DFL)
    for forwarded in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(forwarded, lambda *_: None)
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "exec", str(release_fd), *command],
        pass_fds=(release_fd,),
    )
    os.close(release_fd)
    try:
        os.write(handshake_fd, (json.dumps({"pid": child.pid}) + "\n").encode())
    finally:
        os.close(handshake_fd)
    _, status, usage = os.wait4(child.pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    child.returncode = exit_code
    peak_rss = int(usage.ru_maxrss)
    if sys.platform != "darwin":
        peak_rss *= 1024
    try:
        _write_completion(completion, completion_payload(
            exit_code,
            finished_at=int(time.time() * 1000),
            peak_rss_bytes=max(0, peak_rss),
            cpu_seconds=max(0, usage.ru_utime + usage.ru_stime),
        ))
    except OSError as error:
        print(
            f"router run: could not persist completion evidence: {type(error).__name__}",
            file=sys.stderr,
        )
    return exit_code if exit_code >= 0 else 128 - exit_code


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(125)
    if sys.argv[1] == "exec":
        _exec_command(int(sys.argv[2]), sys.argv[3:])
        return
    if sys.argv[1] != "supervise" or len(sys.argv) < 6:
        raise SystemExit(125)
    raise SystemExit(
        _supervise(int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]), sys.argv[5:])
    )


if __name__ == "__main__":
    main()
