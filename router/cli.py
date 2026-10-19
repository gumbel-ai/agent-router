"""Small operator CLI and JSON client for the five task operations."""

import argparse
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from router.client import CompletionBusy, CompletionRefused, request
from router.codex_plugin import availability as codex_availability
from router.config import load_settings, socket_path, state_dir
from router.dispatch import MUTATIONS
from router.sanitize import sanitize_output_text

MAX_DISPLAYED_SNAPSHOT_ERRORS = 5
MAX_DISPLAYED_SNAPSHOT_ERROR_LENGTH = 160
MAX_DISPLAYED_FLEET_ALERTS = 8
MAX_DISPLAYED_FLEET_ALERT_LENGTH = 160


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
        # Native agents carry no usage or cost; Langfuse owns that.
        cost = ("n/a" if row.get("control_mode") == "observe"
                else f"{row['cost']:.6f}" if row.get("cost") is not None else "unknown")
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


def _sourced(record: dict, identity: object) -> str:
    return f"{record.get('source') or '?'}:{identity or '—'}"


def sessions_table(sessions: list[dict], error: object = None) -> str:
    """Top-level Claude Code and Codex sessions on this host."""
    if error:
        warning = f"warning: session list is incomplete: {_cell(error, 200)}"
        if not sessions:
            return warning
    lines = ["SESSION                                     SOURCE   STATE        ACTIVITY"]
    for session in sessions:
        state = "needs_input" if session.get("needs_input") else session.get("status", "unknown")
        lines.append(f"{_cell(session.get('session_id', '—'), 43):<43} {_cell(session.get('source', '?'), 8):<8} "
                     f"{_cell(state, 12):<12} {_cell(session.get('activity') or '—', 40)}")
    if error:
        lines.append(warning)
    return "\n".join(lines) if sessions else "No observed sessions."


def _cell(value: object, limit: int | None = None) -> str:
    cleaned = sanitize_output_text(str(value))
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


def _fleet_alert_lines(record: dict, snapshot: dict | None = None) -> list[str]:
    wrapper_alerts = record.get("alerts", [])
    snapshot_alerts = snapshot.get("alerts", []) if isinstance(snapshot, dict) else []
    values = [
        alert for alert in [*wrapper_alerts, *snapshot_alerts]
        if isinstance(alert, dict)
    ]
    lines = []
    for alert in values[:MAX_DISPLAYED_FLEET_ALERTS]:
        severity = _cell(alert.get("severity", "warning"), 16)
        kind = _cell(alert.get("kind", "unknown"), 40)
        evidence = _cell(alert.get("evidence", "No evidence supplied"),
                         MAX_DISPLAYED_FLEET_ALERT_LENGTH)
        lines.append(f"  alert [{severity}] {kind}: {evidence}")
    omitted = len(values) - len(lines)
    if omitted:
        lines.append(f"  ... {omitted} more alerts omitted")
    return lines


def _router_task_activity(task: dict) -> str:
    # Conditions that need an operator come before the executor's last activity.
    if task.get("shutdown_unconfirmed"):
        return "shutdown unconfirmed"
    if task.get("cleanup_failed"):
        return "cleanup failed"
    if task.get("status") in {"failed", "interrupted", "blocked"} and task.get("failure_reason"):
        return task["failure_reason"]
    return task.get("activity") or task.get("failure_reason") or "—"


def _process_summary(snapshot: dict) -> list[str]:
    inferred = snapshot.get("inferred_processes", [])
    if not inferred:
        return []
    def total(key: str, label: str, render) -> str:
        # A missing measurement is unknown, not zero; say how many are missing.
        values = [process[key] for process in inferred if isinstance(process.get(key), (int, float))]
        missing = len(inferred) - len(values)
        if not values:
            return f"{label} unknown"
        return f"{label} {render(sum(values))}" + (f" + {missing} unmeasured" if missing else "")

    sessions = len({(process.get("source"), process.get("session_id")) for process in inferred})
    # Totals cover the processes in this snapshot; a truncated page is flagged separately.
    scope = " (shown only)" if "inferred_processes" in snapshot.get("errors", {}) else ""
    cpu = total("cpu_pct", "CPU", lambda value: f"{value:.0f}%")
    rss = total("rss_bytes", "RSS", _size)
    return [f"{'':<10} session processes{scope}: {len(inferred)} in {sessions} session(s), {cpu}, {rss}"]


def _managed_activity(process: dict) -> str:
    if process.get("status") == "running":
        return f"pid {process['pid']}" if process.get("pid") else "running"
    if process.get("status") == "unknown" or process.get("group_outlived"):
        return process.get("visibility_reason") or "outcome unknown"
    if process.get("signal") is None and process.get("exit_code") is None:
        return process.get("status") or "unknown"
    how = f"signal {process['signal']}" if process.get("signal") else f"exit {process.get('exit_code')}"
    elapsed = process.get("elapsed_ms")
    return how + (f" after {elapsed / 1000:.1f}s" if isinstance(elapsed, int) else "")


def fleet_table(result: dict, capacity_only: bool = False) -> str:
    lines = ["HOST       CAPACITY   SESSION              AGENT                STATE        ACTIVITY                 CPU    LOAD   MEM FREE DISK FREE"]
    for record in result["hosts"]:
        name = _cell(record["name"], 10)
        if not record["ok"]:
            error = _cell("error: " + record["error"], 24)
            lines.append(f"{name:<10} {'offline':<10} {'—':<20} {'—':<20} {'—':<12} {error:<24} {'—':<6} {'—':<6} {'—':<8} {'—'}")
            lines.extend(_fleet_alert_lines(record))
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
            lines.extend(_fleet_alert_lines(record, snapshot))
            continue
        sessions = snapshot.get("sessions", [])
        agents = snapshot.get("agents", [])
        router_tasks = snapshot.get("router_tasks", [])
        managed = snapshot.get("managed_processes", [])
        if not sessions and not agents and not router_tasks and not managed:
            activity = "partial snapshot" if partial_lines else "no active sessions"
            lines.append(f"{name:<10} {state:<10} {'—':<20} {'—':<20} {'—':<12} {activity:<24} {cpu:<6} {load:<6} {memory:<8} {disk}")
            lines.extend(_process_summary(snapshot))
            lines.extend(partial_lines)
            lines.extend(_fleet_alert_lines(record, snapshot))
            continue
        for index, session in enumerate(sessions):
            display_state = _cell("needs_input" if session.get("needs_input") else session.get("status", "unknown"), 12)
            activity = _cell(session.get("activity") or f"last: {session.get('last_event') or 'unknown'}", 24)
            lines.append(f"{name if index == 0 else '':<10} {state if index == 0 else '':<10} "
                         f"{_cell(_sourced(session, session.get('session_id')), 20):<20} {'main':<20} {display_state:<12} "
                         f"{activity:<24} {cpu if index == 0 else '—':<6} {load if index == 0 else '—':<6} "
                         f"{memory if index == 0 else '—':<8} {disk if index == 0 else '—'}")
        for index, agent in enumerate(agents):
            first = not sessions and index == 0
            lines.append(f"{name if first else '':<10} {state if first else '':<10} "
                         f"{_cell(_sourced(agent, agent.get('parent_session_id')), 20):<20} "
                         f"{_cell(agent.get('agent_type') or agent.get('agent_id') or 'unknown', 20):<20} "
                         f"{_cell(agent.get('status', 'unknown'), 12):<12} "
                         f"{_cell(agent.get('activity') or '—', 24):<24} "
                         f"{cpu if first else '—':<6} {load if first else '—':<6} "
                         f"{memory if first else '—':<8} {disk if first else '—'}")
        for index, task in enumerate(router_tasks):
            first = not sessions and not agents and index == 0
            lines.append(f"{name if first else '':<10} {state if first else '':<10} "
                         f"{_cell('router:' + str(task.get('parent_session_id') or '—'), 20):<20} "
                         f"{_cell(str(task.get('executor') or '?') + ':' + str(task.get('task_id')), 20):<20} "
                         f"{_cell(task.get('status', 'unknown'), 12):<12} "
                         f"{_cell(_router_task_activity(task), 24):<24} "
                         f"{cpu if first else '—':<6} {load if first else '—':<6} "
                         f"{memory if first else '—':<8} {disk if first else '—'}")
        for index, process in enumerate(managed):
            first = not sessions and not agents and not router_tasks and index == 0
            lines.append(f"{name if first else '':<10} {state if first else '':<10} "
                         f"{_cell('run:' + str(process.get('session_id') or '—'), 20):<20} "
                         f"{_cell(process.get('label') or process.get('executable') or '?', 20):<20} "
                         f"{_cell(process.get('status', 'unknown'), 12):<12} "
                         f"{_cell(_managed_activity(process), 24):<24} "
                         f"{cpu if first else '—':<6} {load if first else '—':<6} "
                         f"{memory if first else '—':<8} {disk if first else '—'}")
        lines.extend(_process_summary(snapshot))
        lines.extend(partial_lines)
        lines.extend(_fleet_alert_lines(record, snapshot))
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
    status.add_argument("--native-offset", type=int, help="Native observation page offset (100 per page)")
    status.add_argument("--codex", action="store_true",
                        help="With --json only: print Codex observation readiness for this directory, without the daemon")
    task_diff = commands.add_parser("task-diff", help="Read one retained task patch as a bounded JSON receipt")
    task_diff.add_argument("task_id")
    task_diff.add_argument("--attempt", type=int, required=True)
    task_diff.add_argument("--json", action="store_true", required=True)
    task_diff.add_argument("--host-id", help="Expected serving Router host identity")
    for field in ("snapshot-commit", "snapshot-tree", "final-tree"):
        task_diff.add_argument("--" + field)
    run = commands.add_parser("run", help="Run a command in its own process group and record completion evidence")
    run.add_argument("--label", help="Short name shown in status and fleet views")
    run.add_argument("--session", help="Launching Claude Code or Codex session ID")
    run.add_argument("--agent", help="Launching agent ID (with --session: exact attribution)")
    work_item_id = run.add_argument("--work-item-id", help="Explicit caller work-item identity; never inferred from the label")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="-- command [arguments]")
    wait = commands.add_parser("wait", help="Wait for the oldest unacknowledged managed-command completion")
    wait.add_argument("--consumer", required=True)
    wait.add_argument("--timeout", type=float, required=True, help="Deadline in seconds, greater than 0 and at most 3600")
    wait.add_argument("--include-subscription", action="store_true", help="Include the durable serving subscription binding")
    ack = commands.add_parser("ack", help="Acknowledge a completion after durably handling it")
    ack.add_argument("--consumer", required=True)
    ack.add_argument("event_id")
    ack.add_argument("--host-id")
    ack.add_argument("--event-host-id")
    ack.add_argument("--subscription-id")
    ack.add_argument("--json-errors", action="store_true", help="Emit typed acknowledgment refusal JSON; retain exit code 1")
    consumer = commands.add_parser("consumer", help="Inspect and remove completion subscriptions")
    consumer_commands = consumer.add_subparsers(dest="consumer_command", required=True)
    consumer_list = consumer_commands.add_parser("list")
    consumer_list.add_argument("--limit", type=int, default=100)
    consumer_list.add_argument("--offset", type=int, default=0)
    consumer_remove = consumer_commands.add_parser("remove")
    consumer_remove.add_argument("name")
    correlation = commands.add_parser("correlation", help="Export bounded identifiers and lifecycle metadata without content")
    correlation.add_argument("--json", action="store_true", required=True)
    correlation.add_argument("--limit", type=int, default=100)
    correlation.add_argument("--cursor")
    snapshot = commands.add_parser("snapshot", help="Print a bounded read-only local host snapshot")
    snapshot.add_argument("--json", action="store_true", required=True)
    snapshot.add_argument("--history-limit", type=int, default=100)
    fleet = commands.add_parser("fleet", help="Query configured local and SSH hosts")
    fleet_commands = fleet.add_subparsers(dest="fleet_command", required=True)
    fleet_diff = fleet_commands.add_parser("task-diff", help="Read a retained patch from one explicit configured host")
    fleet_diff.add_argument("task_id")
    fleet_diff.add_argument("--attempt", type=int, required=True)
    fleet_diff.add_argument("--json", action="store_true", required=True)
    fleet_diff.add_argument("--host", required=True)
    fleet_diff.add_argument("--host-id", required=True)
    fleet_diff.add_argument("--config", type=Path)
    for field in ("snapshot-commit", "snapshot-tree", "final-tree"):
        fleet_diff.add_argument("--" + field)
    fleet_correlation = fleet_commands.add_parser("correlation", help="Export bounded content-free fleet metadata")
    fleet_correlation.add_argument("--json", action="store_true", required=True)
    fleet_correlation.add_argument("--config", type=Path)
    fleet_correlation.add_argument("--host", dest="correlation_host")
    fleet_correlation.add_argument("--limit", type=int, default=100)
    fleet_correlation.add_argument("--cursor")
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
    fleet_wait = fleet_commands.add_parser("wait", help="Wait once on each configured completion host")
    fleet_wait.add_argument("--consumer", required=True)
    fleet_wait.add_argument("--timeout", type=float, required=True)
    fleet_wait.add_argument("--host")
    fleet_wait.add_argument("--config", type=Path)
    fleet_ack = fleet_commands.add_parser("ack", help="Acknowledge an event on its verified host")
    fleet_ack.add_argument("--consumer", required=True)
    fleet_ack.add_argument("--host", required=True)
    fleet_ack.add_argument("--host-id", required=True)
    fleet_ack.add_argument("--event-host-id", required=True)
    fleet_ack.add_argument("--subscription-id", required=True)
    fleet_ack.add_argument("--config", type=Path)
    fleet_ack.add_argument("event_id")
    daemon = commands.add_parser("daemon")
    daemon.add_argument("action", choices=["start", "stop"])
    commands.add_parser("plugin-path", help="Print the bundled Claude plugin directory")
    commands.add_parser("enable", help="Enable observation in this Git project for Claude and Codex")
    setup = commands.add_parser("setup", help="Install the CLI, register marketplaces, and install a macOS user service")
    setup.add_argument("--yes", action="store_true", help="Approve installation without prompting")
    setup.add_argument("--marketplace", default="gumbel-ai/agent-router", help="Official Claude marketplace source (or a local source for development)")
    uninstall = commands.add_parser("uninstall", help="Remove plugin, service and CLI, retaining private history")
    uninstall.add_argument("--yes", action="store_true")
    service = commands.add_parser("service", help="Manage the explicitly installed user service")
    service.add_argument("action", choices=["start", "stop", "restart", "status"])
    recover = commands.add_parser("recover", help="Interactively acknowledge an inspected executor shutdown; daemon must be stopped")
    recover.add_argument("task_id")
    recover.add_argument("--reason", required=True, help="What was inspected and stopped; retained in task history")
    init = commands.add_parser("init", help="Opt this Git project into external delegation")
    init.add_argument("--executor", choices=["opencode", "codex", "claude"], default="opencode",
                      help="Execution adapter; Claude configuration accepts one executable path without arguments")
    init.add_argument("--sandbox", choices=["read-only", "workspace-write"], help="Explicit Codex sandbox; required for noninteractive Codex init")
    init.add_argument("--model", help="alias=provider/exact-model")
    init.add_argument("--setup-command", help="Child dependency setup command; not run during init")
    init.add_argument("--permissions", help="Explicit executor permission JSON; never overwrites OpenCode permissions")
    init.add_argument("--concurrency", type=int)
    init.add_argument("--timeout", type=float)
    init.add_argument("--attempts", type=int)
    init.add_argument("--yes", action="store_true", help="Approve supplied settings and displayed limit defaults")
    hook = commands.add_parser("hook", help="Host hook entry point (not a task operation)")
    hook.add_argument("event", choices=["submit", "authorize", "observe"])
    args = parser.parse_args()
    try:
        if args.command == "task-diff" or args.command == "fleet" and args.fleet_command == "task-diff":
            from router.task_diff import fetch, options
            payload = {"task_id": args.task_id, "attempt_number": args.attempt}
            for field in ("host_id", "snapshot_commit", "snapshot_tree", "final_tree"):
                if getattr(args, field) is not None:
                    payload[field] = getattr(args, field)
            try:
                payload = options(payload)
            except ValueError as error:
                parser.exit(2, f"Invalid task-diff selection: {error}\n")
            host, timeout = None, 15
            if args.command == "fleet":
                from router.fleet import DEFAULT_CONFIG, load_fleet_config
                config = load_fleet_config(args.config or DEFAULT_CONFIG)
                selected = [entry for entry in config.hosts if entry.name == args.host]
                if len(selected) != 1:
                    parser.exit(2, "Task-diff requires one known configured host\n")
                host, timeout = selected[0], config.ssh_timeout_seconds
            result = fetch(payload, host=host, timeout=timeout)
            print(json.dumps(result, separators=(",", ":")), flush=True)
            if result["status"] != "ok":
                parser.exit(1, f"Task diff unavailable: {result['error']['code']}; task unchanged.\n")
        elif args.command == "enable":
            from router.project_enablement import enable
            enable()
        elif args.command in {"setup", "init", "uninstall"}:
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
        elif args.command == "run":
            from router.managed import run as run_managed
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            sys.exit(run_managed(argv, args.label, args.session, args.agent, state_dir(), work_item_id=args.work_item_id))
        elif args.command == "wait":
            from router.managed import wait as wait_managed
            failure_code = 1
            try:
                result = wait_managed(args.consumer, args.timeout, include_subscription=args.include_subscription)
            except ValueError as error:
                parser.error(str(error))
            except KeyboardInterrupt:
                print("Agent Router completion wait interrupted; pending events remain unacknowledged", file=sys.stderr)
                result = {"status": "unavailable", "consumer": args.consumer}
                failure_code = 130
            print(json.dumps(result, separators=(",", ":")))
            if result["status"] == "unavailable":
                sys.exit(failure_code)
        elif args.command == "ack":
            from router.managed import _completion_sequence, _identifier
            try:
                _identifier(args.consumer, "consumer")
                _completion_sequence(args.event_id)
                if (args.host_id is None) != (args.event_host_id is None):
                    raise ValueError("--host-id and --event-host-id must be supplied together")
                if args.subscription_id is not None:
                    if args.host_id is None:
                        raise ValueError("--subscription-id needs --host-id and --event-host-id")
                    _identifier(args.subscription_id, "subscription")
                if args.host_id is not None:
                    _identifier(args.host_id, "host")
                    _identifier(args.event_host_id, "event host")
            except ValueError as error:
                parser.error(str(error))
            payload = {"consumer": args.consumer, "event_id": args.event_id}
            if args.host_id is not None:
                payload.update(host_id=args.host_id, event_host_id=args.event_host_id)
            if args.subscription_id is not None:
                payload["subscription_id"] = args.subscription_id
            try:
                result = request("completion_ack", payload)
            except (CompletionBusy, CompletionRefused) as error:
                if args.json_errors:
                    code = "database_busy" if isinstance(error, CompletionBusy) else "acknowledgment_refused"
                    print(json.dumps({"schema_version": 1, **payload, "error": {"code": code}}, separators=(",", ":")))
                raise
            print(json.dumps(result, separators=(",", ":")))
        elif args.command == "consumer":
            operation, payload = (("completion_consumer_list", {"limit": args.limit, "offset": args.offset})
                                  if args.consumer_command == "list" else
                                  ("completion_consumer_remove", {"consumer": args.name}))
            print(json.dumps(request(operation, payload), separators=(",", ":")))
        elif args.command == "correlation":
            payload = {"limit": args.limit}
            if args.cursor is not None:
                payload["cursor"] = args.cursor
            print(json.dumps(request("correlation", payload, timeout=15, deadline=True), separators=(",", ":")))
        elif args.command == "snapshot":
            print(json.dumps(request("snapshot", {"history_limit": args.history_limit}), separators=(",", ":")))
        elif args.command == "fleet" and args.fleet_command == "correlation":
            from router.fleet import DEFAULT_CONFIG, load_fleet_config
            from router.fleet_correlation import query_correlation
            result = query_correlation(load_fleet_config(args.config or DEFAULT_CONFIG), only=args.correlation_host,
                                       limit=args.limit, cursor=args.cursor)
            print(json.dumps(result, separators=(",", ":")), flush=True)
            if result["partial"]:
                raise ValueError("Fleet correlation export is partial; inspect host results")
        elif args.command == "fleet" and args.fleet_command in {"wait", "ack"}:
            from router.fleet import DEFAULT_CONFIG, load_fleet_config
            from router.fleet_completions import (
                acknowledge_completion,
                query_completions,
            )
            config = load_fleet_config(args.config or DEFAULT_CONFIG)
            try:
                if args.fleet_command == "wait":
                    result = query_completions(config, args.consumer, args.timeout, only=args.host)
                    failed = result["partial"]
                else:
                    result = acknowledge_completion(config, args.host, args.consumer, args.event_id,
                                                    args.host_id, args.event_host_id, args.subscription_id)
                    failed = not result["ok"]
            except ValueError as error:
                parser.error(str(error))
            print(json.dumps(result, separators=(",", ":")))
            if failed:
                parser.exit(1, "Agent Router: completion delivery failed; inspect per-host errors.\n")
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
        elif args.command == "status" and args.codex:
            # A separate document: plain `status --json` is a list whose shape stays unchanged.
            if not args.json or args.task_id is not None or args.filter or args.watch or args.native_offset is not None:
                parser.error("status --codex requires --json and no task ID, --filter, --watch or --native-offset")
            # Read from the `run` parser's own option, so the capability cannot drift from what `run` accepts.
            print(json.dumps({"codex_observation": codex_availability(),
                              "supports_work_item_id": "--work-item-id" in work_item_id.option_strings}, indent=2))
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
                    if not payload:
                        snapshot = request("snapshot", {"history_limit": 50})
                        print("\n" + sessions_table(snapshot.get("sessions", []),
                                                    snapshot.get("errors", {}).get("sessions")), flush=True)
                        codex = codex_availability()
                        if codex["state"] != "codex_not_installed":
                            print(f"Codex observation: {codex['state']} - {_cell(codex['detail'], 160)}", flush=True)
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
        if args.command == "task-diff" or args.command == "fleet" and args.fleet_command == "task-diff":
            parser.exit(130, "Agent Router task-diff read interrupted; task unchanged. Repeat the same read.\n")
        if args.command == "fleet" and args.fleet_command in {"wait", "ack"}:
            parser.exit(130, "Agent Router fleet completion interrupted; events remain durable; "
                        "acknowledgment commit state is unknown. Retry the same delivery, never the command.\n")
        if args.command == "correlation" or args.command == "fleet" and args.fleet_command == "correlation":
            parser.exit(130, "Agent Router correlation export interrupted; no state changed.\n")
        if args.command == "ack":
            parser.exit(130, "Agent Router acknowledgment interrupted; commit state is unknown. "
                        "Retry the same consumer and event ID.\n")
        return


if __name__ == "__main__":
    main()
