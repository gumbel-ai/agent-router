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

MAX_DISPLAYED_SNAPSHOT_ERRORS = 5
MAX_DISPLAYED_SNAPSHOT_ERROR_LENGTH = 160


def table(rows: list[dict]) -> str:
    from router.native import text
    lines = ["AGENT / TASK                                OWNER / MODE      STATE             MODEL                    TIME       EST. USD"]
    if any(row.get("control_mode") == "observe" for row in rows):
        lines.append("Native observations: up to 100 per page; use --native-offset N for more. Router tasks are not paged.")
    for row in rows:
        task_id = _cell(row["task_id"], 43)
        owner = _cell(f"{row.get('executor', '?')}/{row.get('control_mode', 'control')}", 17)
        state = _cell(row["status"], 17)
        model = _cell(row.get("resolved_model") or "unknown", 24)
        elapsed = row.get("elapsed_ms")
        duration = f"{elapsed / 1000:.1f}s" if elapsed is not None else "unknown"
        cost = f"{row['cost']:.6f}" if row.get("cost") is not None else "unknown"
        lines.append(f"{task_id:<43} {owner:<17} {state:<17} {model:<24} {duration:<10} {cost}")
        lines.append(f"  parent {text(row.get('parent_session_id')) or 'unknown'}/{text(row.get('parent_agent_id')) or 'unknown'}"
                     f" | work: {text(row.get('task')) or 'unknown'}")
        if row.get("activity"):
            lines.append(f"  activity: {text(row['activity'])}")
        if row.get("cleanup_error") is not None:
            lines.append(f"  cleanup failed: {text(row['cleanup_error'])}")
        elif row.get("cleanup_pending"):
            lines.append("  cleanup pending")
        if "attempts" in row:
            lines.append(f"  attempts: {row['attempts']}/{row['max_attempts']}")
        if row.get("cwd"):
            lines.append(f"  directory: {text(row['cwd'])}")
        if row.get("usage") is not None:
            partial = " (partial)" if row.get("usage_complete") is False or row.get("usage_partial") else ""
            lines.append(f"  tokens{partial}: {json.dumps(row['usage'], sort_keys=True)}")
        if row.get("cost") is not None or row.get("cost_note"):
            lines.append(f"  cost source: {_cell(row.get('cost_source', 'unavailable'))} | {_cell(row.get('cost_note', ''))}")
        if row.get("last_event_at"):
            lines.append(f"  last event {max(0, int(time.time() - row['last_event_at'] / 1000))}s ago")
        if row.get("visibility_reason"):
            lines.append(f"  visibility: {_cell(row['visibility_reason'])}")
        if row.get("runtime_partial"):
            lines.append("  runtime: observed segments only; an unconfirmed interval is omitted")
    return "\n".join(lines)


def _size(value: int | None) -> str:
    if value is None:
        return "—"
    for suffix in ("B", "K", "M", "G", "T"):
        if value < 1024 or suffix == "T":
            return f"{value:.0f}{suffix}" if suffix == "B" else f"{value:.1f}{suffix}"
        value /= 1024
    return "—"


def _cell(value: object, limit: int | None = None) -> str:
    cleaned = "".join(character for character in str(value)
                      if not (ord(character) < 32 or 127 <= ord(character) <= 159))
    return cleaned[:limit] if limit is not None else cleaned


def _partial_snapshot_lines(snapshot: dict) -> list[str]:
    if snapshot.get("partial") is not True:
        return []
    lines = ["  warning: partial snapshot; some data may be unavailable"]
    errors = snapshot.get("errors")
    if not isinstance(errors, dict):
        return lines
    displayed = list(errors.items())[:MAX_DISPLAYED_SNAPSHOT_ERRORS]
    for section, error in displayed:
        lines.append(
            f"  {_cell(section, 24)} error: "
            f"{_cell(error, MAX_DISPLAYED_SNAPSHOT_ERROR_LENGTH)}"
        )
    omitted = len(errors) - len(displayed)
    if omitted:
        lines.append(f"  ... {omitted} more section errors omitted")
    return lines


def fleet_table(result: dict, capacity_only: bool = False) -> str:
    lines = ["HOST       CAPACITY   SESSION              AGENT                STATE        ACTIVITY                 CPU    LOAD   MEM FREE DISK FREE"]
    for record in result["hosts"]:
        name = _cell(record["name"], 10)
        if not record["ok"]:
            error = _cell("error: " + record["error"], 24)
            lines.append(f"{name:<10} {'offline':<10} {'—':<20} {'—':<20} {'—':<12} {error:<24} {'—':<6} {'—':<6} {'—':<8} {'—'}")
            continue
        snapshot = record["snapshot"]
        capacity = snapshot.get("capacity", {})
        state = _cell(capacity.get("capacity_state", "unknown"), 10)
        cpu = f"{capacity['cpu_busy_pct']:.0f}%" if isinstance(capacity.get("cpu_busy_pct"), (int, float)) else "—"
        load = f"{capacity['load_1m']:.1f}" if isinstance(capacity.get("load_1m"), (int, float)) else "—"
        memory = _size(capacity.get("memory_available_bytes"))
        disk = _size(capacity.get("disk_available_bytes"))
        partial_lines = _partial_snapshot_lines(snapshot)
        if capacity_only:
            reason = _cell("; ".join(capacity.get("capacity_reasons", [])) or "capacity thresholds satisfied", 24)
            lines.append(f"{name:<10} {state:<10} {'—':<20} {'—':<20} {'—':<12} {reason:<24} {cpu:<6} {load:<6} {memory:<8} {disk}")
            lines.extend(partial_lines)
            continue
        sessions = snapshot.get("sessions", [])
        agents = snapshot.get("agents", [])
        if not sessions and not agents:
            activity = "partial snapshot" if partial_lines else "no active sessions"
            lines.append(f"{name:<10} {state:<10} {'—':<20} {'—':<20} {'—':<12} {activity:<24} {cpu:<6} {load:<6} {memory:<8} {disk}")
            lines.extend(partial_lines)
            continue
        for index, session in enumerate(sessions):
            display_state = _cell("needs_input" if session.get("needs_input") else session.get("status", "unknown"), 12)
            activity = _cell(session.get("activity") or f"last: {session.get('last_event') or 'unknown'}", 24)
            lines.append(f"{name if index == 0 else '':<10} {state if index == 0 else '':<10} "
                         f"{_cell(session.get('session_id', '—'), 20):<20} {'main':<20} {display_state:<12} "
                         f"{activity:<24} {cpu if index == 0 else '—':<6} {load if index == 0 else '—':<6} "
                         f"{memory if index == 0 else '—':<8} {disk if index == 0 else '—'}")
        for index, agent in enumerate(agents):
            first = not sessions and index == 0
            lines.append(f"{name if first else '':<10} {state if first else '':<10} "
                         f"{_cell(agent.get('parent_session_id', '—'), 20):<20} "
                         f"{_cell(agent.get('agent_type') or agent.get('agent_id') or 'unknown', 20):<20} "
                         f"{_cell(agent.get('status', 'unknown'), 12):<12} "
                         f"{_cell(agent.get('activity') or '—', 24):<24} "
                         f"{cpu if first else '—':<6} {load if first else '—':<6} "
                         f"{memory if first else '—':<8} {disk if first else '—'}")
        lines.extend(partial_lines)
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
    snapshot = commands.add_parser("snapshot", help="Print a bounded read-only local host snapshot")
    snapshot.add_argument("--json", action="store_true", required=True)
    snapshot.add_argument("--history-limit", type=int, default=100)
    fleet = commands.add_parser("fleet", help="Read configured local and SSH host snapshots")
    fleet_commands = fleet.add_subparsers(dest="fleet_command", required=True)
    fleet_status = fleet_commands.add_parser("status")
    fleet_status.add_argument("--json", action="store_true")
    fleet_status.add_argument("--watch", action="store_true")
    fleet_status.add_argument("--strict", action="store_true")
    fleet_status.add_argument("--config", type=Path)
    fleet_capacity = fleet_commands.add_parser("capacity")
    fleet_capacity.add_argument("--json", action="store_true")
    fleet_capacity.add_argument("--strict", action="store_true")
    fleet_capacity.add_argument("--config", type=Path)
    fleet_host = fleet_commands.add_parser("host")
    fleet_host.add_argument("name")
    fleet_host.add_argument("--json", action="store_true")
    fleet_host.add_argument("--strict", action="store_true")
    fleet_host.add_argument("--config", type=Path)
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
    recover = commands.add_parser("recover", help="Interactively acknowledge an inspected Codex shutdown; daemon must be stopped")
    recover.add_argument("task_id")
    recover.add_argument("--reason", required=True, help="What was inspected and stopped; retained in task history")
    init = commands.add_parser("init", help="Opt this Git project into external delegation")
    init.add_argument("--executor", choices=["opencode", "codex"], default="opencode")
    init.add_argument("--sandbox", choices=["read-only", "workspace-write"], help="Explicit Codex sandbox; required for noninteractive Codex init")
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
        elif args.command == "recover":
            from router.service import recover
            print(json.dumps(recover(args.task_id, args.reason)))
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
        elif args.command == "snapshot":
            print(json.dumps(request("snapshot", {"history_limit": args.history_limit}), separators=(",", ":")))
        elif args.command == "fleet":
            from router.fleet import (
                DEFAULT_CONFIG,
                capacity_projection,
                fleet_exit_ok,
                load_fleet_config,
                query_fleet,
            )
            from router.fleet_snapshot import AlertHistory
            config = load_fleet_config(args.config or DEFAULT_CONFIG)
            only = args.name if args.fleet_command == "host" else None
            alert_history = AlertHistory()
            while True:
                result = query_fleet(config, only=only, alert_history=alert_history)
                if args.json:
                    output = capacity_projection(result) if args.fleet_command == "capacity" else result
                    if getattr(args, "watch", False):
                        print(json.dumps(output, separators=(",", ":")), flush=True)
                    else:
                        print(json.dumps(output, indent=2), flush=True)
                else:
                    if getattr(args, "watch", False) and sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                    print(fleet_table(result, capacity_only=args.fleet_command == "capacity"), flush=True)
                if not fleet_exit_ok(result, args.strict) and not getattr(args, "watch", False):
                    raise ValueError("Fleet snapshot failed for all hosts" if not any(host["ok"] for host in result["hosts"])
                                     else "A required fleet host is unavailable")
                if not getattr(args, "watch", False):
                    break
                time.sleep(config.refresh_seconds)
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
