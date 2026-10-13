"""Small operator CLI and JSON client for the five task operations."""

import argparse
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from router.client import request
from router.config import load_settings, socket_path, state_dir
from router.dispatch import MUTATIONS


def table(rows: list[dict]) -> str:
    from router.native import text
    lines = ["AGENT / TASK                                OWNER / MODE      STATE             MODEL                    TIME       EST. USD"]
    if any(row.get("control_mode") == "observe" for row in rows):
        lines.append("Native observations: up to 100 per page; use --native-offset N for more. Router tasks are not paged.")
    for row in rows:
        owner = f"{row.get('executor', '?')}/{row.get('control_mode', 'control')}"
        elapsed = row.get("elapsed_ms")
        duration = f"{elapsed / 1000:.1f}s" if elapsed is not None else "unknown"
        cost = f"{row['cost']:.6f}" if row.get("cost") is not None else "unknown"
        lines.append(f"{row['task_id']:<43} {owner:<17} {row['status']:<17} {row.get('resolved_model') or 'unknown':<24} {duration:<10} {cost}")
        lines.append(f"  parent {text(row.get('parent_session_id')) or 'unknown'}/{text(row.get('parent_agent_id')) or 'unknown'}"
                     f" | work: {text(row.get('task')) or 'unknown'}")
        if row.get("activity"):
            lines.append(f"  activity: {text(row['activity'])}")
        if "attempts" in row:
            lines.append(f"  attempts: {row['attempts']}/{row['max_attempts']}")
        if row.get("cwd"):
            lines.append(f"  directory: {text(row['cwd'])}")
        if row.get("usage") is not None:
            lines.append(f"  tokens: {json.dumps(row['usage'], sort_keys=True)}")
        if row.get("cost") is not None or row.get("cost_note"):
            lines.append(f"  cost source: {row.get('cost_source', 'unavailable')} | {row.get('cost_note', '')}")
        if row.get("last_event_at"):
            lines.append(f"  last event {max(0, int(time.time() - row['last_event_at'] / 1000))}s ago")
        if row.get("visibility_reason"):
            lines.append(f"  visibility: {row['visibility_reason']}")
        if row.get("runtime_partial"):
            lines.append("  runtime: observed segments only; an unconfirmed interval is omitted")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="router", description="Agent Router: local, parent-supervised agent delegation")
    parser.add_argument("--version", action="version", version=version("agent-router"))
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in sorted(MUTATIONS):
        command = commands.add_parser(operation)
        command.add_argument("--json", required=True, help="Literal invocation JSON object")
    status = commands.add_parser("status")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--filter", choices=["needs_decision"])
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--native-offset", type=int, default=0, help="Native observation page offset (100 per page)")
    daemon = commands.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop"])
    commands.add_parser("plugin-path", help="Print the bundled Claude plugin directory")
    setup = commands.add_parser("setup", help="Install the global Claude plugin and user background service")
    setup.add_argument("--yes", action="store_true", help="Approve installation without prompting")
    setup.add_argument("--marketplace", default="gumbel-ai/agent-router", help="Official Claude marketplace source (or a local source for development)")
    uninstall = commands.add_parser("uninstall", help="Remove plugin, service and CLI, retaining private history")
    uninstall.add_argument("--yes", action="store_true")
    service = commands.add_parser("service", help="Manage the explicitly installed user service")
    service.add_argument("action", choices=["start", "stop", "restart", "status"])
    init = commands.add_parser("init", help="Opt this Git project into OpenCode delegation")
    init.add_argument("--model", help="alias=provider/exact-model")
    init.add_argument("--setup-command", help="Child dependency setup command; not run during init")
    init.add_argument("--permissions", help="Explicit allow/deny JSON; never overwrites existing permissions")
    init.add_argument("--concurrency", type=int)
    init.add_argument("--timeout", type=float)
    init.add_argument("--attempts", type=int)
    init.add_argument("--yes", action="store_true", help="Approve supplied settings and displayed limit defaults")
    hook = commands.add_parser("hook", help="Host hook entry point (not a task operation)")
    hook.add_argument("event", choices=["submit", "authorize", "observe"])
    args = parser.parse_args()
    try:
        if args.command in {"setup", "init", "uninstall"}:
            from router import setup as installation
            if args.command == "init":
                installation.init(args)
            elif args.command == "setup":
                installation.setup(args.yes, args.marketplace)
            else:
                getattr(installation, args.command)(args.yes)
        elif args.command == "service":
            from router import service as services
            if args.action in {"start", "restart"}:
                print(json.dumps(services.start(restart=args.action == "restart")))
            elif args.action == "stop":
                services.stop()
            else:
                print(json.dumps(services.health() or {"running": False}))
        elif args.command == "hook":
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
            payload = {key: value for key, value in {"task_id": args.task_id, "filter": args.filter, "native_offset": args.native_offset}.items() if value}
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
