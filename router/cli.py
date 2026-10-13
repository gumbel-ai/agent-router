"""Small operator CLI and JSON client for the five task operations."""

import argparse
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from router.client import request
from router.config import load_settings, socket_path, state_dir
from router.dispatch import MUTATIONS


def table(rows: list[dict]) -> str:
    lines = ["TASK                                  STATE             ATTEMPTS  MODEL"]
    for row in rows:
        lines.append(f"{row['task_id']:<37} {row['status']:<17} {row['attempts']}/{row['max_attempts']:<7} {row.get('resolved_model', '-')}")
        if row.get("last_event_at"):
            lines.append(f"  last event {max(0, int(time.time() - row['last_event_at'] / 1000))}s ago")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="router", description="Agent Router: local, parent-supervised agent delegation")
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in sorted(MUTATIONS):
        command = commands.add_parser(operation)
        command.add_argument("--json", required=True, help="Literal invocation JSON object")
    status = commands.add_parser("status")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--filter", choices=["needs_decision"])
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    daemon = commands.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop"])
    commands.add_parser("plugin-path", help="Print the bundled Claude plugin directory")
    hook = commands.add_parser("hook", help="Host hook entry point (not a task operation)")
    hook.add_argument("event", choices=["submit", "authorize"])
    args = parser.parse_args()
    try:
        if args.command == "hook":
            from router.hooks import main as hook_main
            sys.argv = [sys.argv[0], args.event]
            hook_main()
        elif args.command == "plugin-path":
            print(Path(__file__).with_name("plugin"))
        elif args.command == "daemon":
            if args.action == "stop":
                print(json.dumps(request("daemon_stop", {})))
            else:
                from router.daemon import serve
                settings, environment = load_settings()
                directory = state_dir()
                serve(settings, environment, directory, socket_path())
        elif args.command == "status":
            payload = {key: value for key, value in {"task_id": args.task_id, "filter": args.filter}.items() if value}
            while True:
                result = request("status", payload)
                if args.json or isinstance(result, dict):
                    print(json.dumps(result, indent=2))
                else:
                    if args.watch and sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                    print(table(result), flush=True)
                if not args.watch:
                    break
                time.sleep(2)
        else:
            payload = json.loads(args.json)
            if not isinstance(payload, dict):
                raise ValueError("Invocation must be a JSON object")
            print(json.dumps(request(args.command, payload), indent=2))
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) + ": " + item["msg"] for item in error.errors(include_input=False))
        parser.exit(1, "Invalid Agent Router configuration: " + fields + "\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Agent Router: {error}\n")
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
