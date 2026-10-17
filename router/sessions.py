"""Top-level Claude session and agent-team task lifecycle records."""

import json

from router.db import now_ms
from router.dispatch import canonical
from router.native import (
    MAX_EVENT_FUTURE_SKEW_MS,
    STALE_AFTER_MS,
    identifier,
    safe_text,
    text,
)
from router.sanitize import sanitize_structure
from router.validation import MAX_WIRE_INTEGER, is_wire_integer

RECENT_HISTORY_MS = 300_000
MAX_INFLIGHT_AGE_MS = 86_400_000
MAX_DISPLAYED_INFLIGHT_TOOLS = 64
MAX_EXACT_INFLIGHT_TOOLS = 1024
UNKNOWN_WAITER = "__session__"


class SessionObserver:
    def __init__(self, store, host_id: str):
        self.store = store
        self.host_id = identifier(host_id, "host_id")

    def reconcile_restart(self) -> None:
        with self.store.transaction() as db:
            for row in db.execute("SELECT data FROM sessions WHERE host_id=? AND source='claude'", (self.host_id,)):
                record = json.loads(row[0])
                if record["status"] in {"running", "idle"} or record.get("needs_input"):
                    # Work that would still count as active is unresolved, not stopped:
                    # until fresh evidence or this deadline, capacity cannot be exact.
                    deadlines = [(record.get("last_event_at") or 0) + STALE_AFTER_MS]
                    if is_wire_integer(record.get("tool_active_until")):
                        deadlines.append(record["tool_active_until"])
                    record["restart_uncertain_until"] = min(MAX_WIRE_INTEGER, max(deadlines))
                    record.update(status="unknown", needs_input=False,
                                  visibility_reason="Router restarted; awaiting session lifecycle evidence",
                                  inflight_tools=[], tool_event_at={}, tool_finished_at={}, waiting_agents=[],
                                  tool_active_until=None, tool_overflow_until=None,
                                  active_tool_times={}, tool_correlation_partial=False,
                                  tool_correlation_partial_until=None,
                                  tool_overflow_active=False, tool_terminal_floor=-1,
                                  tool_history_partial=False)
                    self._put_session(db, record)

    def _put_session(self, db, record: dict) -> None:
        db.execute("""INSERT INTO sessions(host_id,source,session_id,data) VALUES(?,?,?,?)
            ON CONFLICT(host_id,source,session_id) DO UPDATE SET data=excluded.data""",
                   (record["host_id"], record["source"], record["session_id"], canonical(record)))

    def _session(self, db, source: str, session_id: str, at: int) -> dict:
        row = db.execute("SELECT data FROM sessions WHERE host_id=? AND source=? AND session_id=?",
                         (self.host_id, source, session_id)).fetchone()
        if row:
            return json.loads(row[0])
        return {"host_id": self.host_id, "source": source, "session_id": session_id,
                "parent_session_id": None, "status": "unknown", "cwd": None,
                "started_at": at, "last_event_at": at, "finished_at": None, "model": None,
                "coordinator_role": "unknown", "visibility_reason": "SessionStart was not observed",
                "activity": None, "needs_input": False, "last_event": None,
                "inflight_tools": [], "tool_event_at": {}, "tool_finished_at": {},
                "tool_reset_at": -1, "tool_active_until": None,
                "tool_overflow_until": None, "waiting_agents": [],
                "active_tool_times": {}, "tool_correlation_partial": False,
                "tool_correlation_partial_until": None,
                "tool_overflow_active": False, "tool_terminal_floor": -1,
                "tool_history_partial": False}

    @staticmethod
    def _reset_tools(record: dict) -> None:
        record.update(
            inflight_tools=[], tool_event_at={}, tool_finished_at={},
            active_tool_times={}, tool_active_until=None, tool_overflow_until=None,
            tool_correlation_partial=False, tool_correlation_partial_until=None,
            tool_overflow_active=False, tool_terminal_floor=-1,
            tool_history_partial=False,
        )

    @staticmethod
    def _active_tool_times(record: dict) -> dict[str, int]:
        """Return exact active correlations, migrating bounded legacy records."""
        stored = record.get("active_tool_times")
        if isinstance(stored, dict):
            return {
                key: value for key, value in stored.items()
                if isinstance(key, str) and is_wire_integer(value)
            }
        event_times = record.get("tool_event_at", {})
        active = {
            item["tool_use_id"]: event_times.get(item["tool_use_id"], record.get("last_event_at", 0))
            for item in record.get("inflight_tools", [])
            if isinstance(item, dict) and isinstance(item.get("tool_use_id"), str)
        }
        record["active_tool_times"] = active
        if record.get("tool_overflow_active"):
            record["tool_correlation_partial"] = True
            record["tool_correlation_partial_until"] = record.get("tool_overflow_until")
        return active

    @staticmethod
    def _clear_waiter(record: dict, event: dict) -> bool:
        waiting = set(record.get("waiting_agents", []))
        agent = event.get("agent_id")
        waiting.discard(identifier(agent, "agent_id") if agent is not None else UNKNOWN_WAITER)
        record["waiting_agents"] = sorted(waiting)
        return bool(waiting)

    def observe(self, event: dict, timeout: float = 10) -> dict:
        source = event.get("source")
        session_id = event.get("session_id")
        kind = text(event.get("kind"))
        at = event.get("observed_at")
        if source != "claude" or not kind:
            raise ValueError("Unsupported session observation")
        source = identifier(source, "source")
        session_id = identifier(session_id, "session_id")
        if not is_wire_integer(at) or at > now_ms() + MAX_EVENT_FUTURE_SKEW_MS:
            raise ValueError("Session observation requires a timestamp")
        with self.store.transaction(timeout) as db:
            record = self._session(db, source, session_id, at)
            if kind in {"task_created", "task_completed"}:
                self._observe_task(db, record, event, at)
            if record["status"] == "stopped" and (kind != "session_start" or at <= record["last_event_at"]):
                return self.view(record, max(now_ms(), at))
            tool_changed, tool_state_evidence = self._observe_tool(record, event, at)
            if at < record["last_event_at"]:
                if tool_changed:
                    self._put_session(db, record)
                return self.view(record, max(now_ms(), at))
            if kind in {"tool_end", "tool_failure"} and not tool_state_evidence:
                # An unmatched completion is still a useful correlation watermark:
                # it prevents a delayed start for the same tool from becoming live.
                # It is not evidence that the session resumed after a prompt/stop
                # reset, and must not refresh session liveness.
                if tool_changed:
                    self._put_session(db, record)
                return self.view(record, max(now_ms(), at))
            if kind == "tool_start" and not tool_state_evidence:
                return self.view(record, max(now_ms(), at))
            if (at == record["last_event_at"] and record.get("last_event") == "session_end"
                    and kind != "session_end"):
                return self.view(record, max(now_ms(), at))
            if (at == record["last_event_at"] and record.get("last_event") in {"stop", "stop_failure"}
                    and kind in {"task_created", "task_completed", "subagent_start", "subagent_stop"}):
                # A same-time tie must not reopen a turn that already ended.
                return self.view(record, max(now_ms(), at))
            record["last_event_at"] = at
            record["last_event"] = kind
            for key, limit in (("cwd", 2000), ("model", 240)):
                value = safe_text(event.get(key), limit)
                if value is not None:
                    record[key] = value
            if event.get("parent_session_id") is not None:
                record["parent_session_id"] = identifier(event["parent_session_id"], "parent_session_id")
            if kind == "session_start":
                record.update(status="running", finished_at=None, visibility_reason=None,
                              activity="session " + (safe_text(event.get("start_source"), 240) or "start"),
                              needs_input=False, waiting_agents=[])
                self._reset_tools(record)
                record["started_at"] = min(record["started_at"], at)
            elif kind == "session_end":
                record.update(status="stopped", finished_at=at, activity=None, needs_input=False,
                              visibility_reason=None, waiting_agents=[])
                self._reset_tools(record)
            elif kind in {"tool_start", "tool_end", "tool_failure"}:
                waiting = self._clear_waiter(record, event)
                reason = ("Inflight tool correlation exceeded the exact tracking bound"
                          if record.get("tool_correlation_partial") else
                          "Tool correlation history exceeded the 128-completion bound"
                          if record.get("tool_history_partial") else
                          "Claude is waiting for input" if waiting else None)
                record.update(status="running", finished_at=None, activity=safe_text(event.get("activity"), 240) or kind,
                              needs_input=waiting,
                              visibility_reason=reason)
            elif kind == "prompt":
                record.update(status="running", finished_at=None, activity=kind, needs_input=False,
                              visibility_reason=None, waiting_agents=[])
                self._reset_tools(record)
            elif kind in {"task_created", "task_completed",
                          "subagent_start", "subagent_stop"}:
                activity = safe_text(event.get("activity"), 240) or kind
                waiting = self._clear_waiter(record, event)
                record.update(status="running", finished_at=None, activity=activity, needs_input=waiting,
                              visibility_reason="Claude is waiting for input" if waiting else None)
            elif kind == "stop":
                background = event.get("background_tasks")
                live = isinstance(background, list) and any(isinstance(item, dict) and item.get("status") not in {"completed", "failed", "stopped"} for item in background)
                record.update(status="running" if live else "idle", activity="background task" if live else None,
                              needs_input=False, visibility_reason=None, waiting_agents=[])
                self._reset_tools(record)
            elif kind == "stop_failure":
                record.update(status="idle", activity="StopFailure", needs_input=False,
                              visibility_reason="Claude reported that the turn failed", waiting_agents=[])
                self._reset_tools(record)
            elif kind == "notification":
                notification = safe_text(event.get("notification_type"), 240) or "notification"
                needs_input = notification in {"permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"}
                if needs_input:
                    waiting = set(record.get("waiting_agents", []))
                    agent = event.get("agent_id")
                    waiting.add(identifier(agent, "agent_id") if agent is not None else UNKNOWN_WAITER)
                    record.update(status="idle", needs_input=True, waiting_agents=sorted(waiting), activity=notification,
                                  visibility_reason="Claude is waiting for input")
                elif notification == "agent_completed":
                    waiting = self._clear_waiter(record, event)
                    live_tool = self._has_active_tool(record, at)
                    reason = ("Inflight tool correlation exceeded the exact tracking bound"
                              if record.get("tool_correlation_partial") else
                              "Tool correlation history exceeded the 128-completion bound"
                              if record.get("tool_history_partial") else
                              "Claude is waiting for input" if waiting else None)
                    record.update(status="running" if live_tool else "idle", needs_input=waiting,
                                  activity=(record.get("activity") if live_tool else notification),
                                  visibility_reason=reason)
                else:
                    record["activity"] = notification
            elif kind == "teammate_idle":
                record["activity"] = "teammate idle"
            else:
                raise ValueError("Unsupported session observation")
            if kind in {"session_start", "session_end", "prompt", "stop", "stop_failure"}:
                record["tool_reset_at"] = max(record.get("tool_reset_at", -1), at)
            self._put_session(db, record)
        return self.view(record, max(now_ms(), at))

    @staticmethod
    def _observe_tool(record: dict, event: dict, at: int) -> tuple[bool, bool]:
        if event["kind"] not in {"tool_start", "tool_end", "tool_failure"}:
            return False, False
        if event.get("tool_use_id") is None:
            # Older/partial hook payloads lack a correlation ID. They still
            # establish activity for a new/running session, but cannot safely
            # reopen an idle session after Stop or prove a completion match.
            return False, event["kind"] == "tool_start" and record["status"] != "idle"
        tool_use_id = identifier(event["tool_use_id"], "tool_use_id")
        if event["kind"] == "tool_start" and at <= record.get("tool_reset_at", -1):
            return False, False
        event_times = dict(record.get("tool_event_at", {}))
        finished_times = dict(record.get("tool_finished_at", {}))
        # tool_event_at is bounded to 128 entries; a long-running tool's start may
        # be evicted from it while active_tool_times still holds it. Order against
        # every retained timestamp so a delayed event cannot clear newer state.
        if at < max(event_times.get(tool_use_id, -1), finished_times.get(tool_use_id, -1),
                    SessionObserver._active_tool_times(record).get(tool_use_id, -1)):
            return False, False
        if event["kind"] == "tool_start" and at <= finished_times.get(tool_use_id, -1):
            return False, False
        if (event["kind"] == "tool_start"
                and at <= record.get("tool_terminal_floor", -1)):
            # The matching completion may be one of the bounded tombstones that
            # were evicted. Exact liveness is unavailable; do not manufacture an
            # active tool that a later completion can never clear.
            record["tool_correlation_partial"] = True
            record["tool_correlation_partial_until"] = max(
                record.get("tool_correlation_partial_until") or -1,
                min(MAX_WIRE_INTEGER, at + MAX_INFLIGHT_AGE_MS),
            )
            record["tool_active_until"] = max(
                record.get("tool_active_until") or -1,
                record["tool_correlation_partial_until"],
            )
            return True, True
        active_tools = SessionObserver._active_tool_times(record)
        matched_active = tool_use_id in active_tools
        if event["kind"] == "tool_start":
            if not matched_active and len(active_tools) >= MAX_EXACT_INFLIGHT_TOOLS:
                record["tool_correlation_partial"] = True
                record["tool_correlation_partial_until"] = max(
                    record.get("tool_correlation_partial_until") or -1,
                    min(MAX_WIRE_INTEGER, at + MAX_INFLIGHT_AGE_MS),
                )
            else:
                active_tools[tool_use_id] = at
        else:
            active_tools.pop(tool_use_id, None)
            finished_times.pop(tool_use_id, None)
            finished_times[tool_use_id] = at
        event_times.pop(tool_use_id, None)
        event_times[tool_use_id] = at
        record["active_tool_times"] = active_tools
        activity_by_id = {
            item.get("tool_use_id"): item.get("activity")
            for item in record.get("inflight_tools", []) if isinstance(item, dict)
        }
        if event["kind"] == "tool_start" and tool_use_id in active_tools:
            activity_by_id[tool_use_id] = safe_text(event.get("activity"), 240) or "tool"
        record["inflight_tools"] = [
            {"tool_use_id": identity, "activity": activity_by_id.get(identity) or "tool"}
            for identity in list(active_tools)[:MAX_DISPLAYED_INFLIGHT_TOOLS]
        ]
        record["tool_event_at"] = dict(list(event_times.items())[-128:])
        if len(finished_times) > 128:
            evicted = list(finished_times.items())[:-128]
            record["tool_terminal_floor"] = max(
                record.get("tool_terminal_floor", -1),
                *(timestamp for _, timestamp in evicted),
            )
            record["tool_history_partial"] = True
        record["tool_finished_at"] = dict(list(finished_times.items())[-128:])
        deadlines = [min(MAX_WIRE_INTEGER, started_at + MAX_INFLIGHT_AGE_MS)
                     for started_at in active_tools.values()]
        partial_until = record.get("tool_correlation_partial_until")
        if record.get("tool_correlation_partial") and isinstance(partial_until, int):
            deadlines.append(partial_until)
        record["tool_active_until"] = max(deadlines, default=None)
        return True, event["kind"] == "tool_start" or matched_active

    @staticmethod
    def _has_active_tool(record: dict, at: int) -> bool:
        if record.get("tool_correlation_partial"):
            return False
        deadline = record.get("tool_active_until")
        if isinstance(deadline, int) and not isinstance(deadline, bool):
            return at <= deadline
        # Compatibility for records written before the indexed deadline existed.
        if record.get("tool_overflow_active"):
            return max(0, at - record.get("last_event_at", 0)) <= MAX_INFLIGHT_AGE_MS
        active_tools = record.get("active_tool_times")
        if isinstance(active_tools, dict):
            return any(0 <= at - started_at <= MAX_INFLIGHT_AGE_MS
                       for started_at in active_tools.values() if is_wire_integer(started_at))
        event_times = record.get("tool_event_at", {})
        return any(0 <= at - event_times.get(item.get("tool_use_id"), 0) <= MAX_INFLIGHT_AGE_MS
                   for item in record.get("inflight_tools", []))

    def _observe_task(self, db, session: dict, event: dict, at: int) -> None:
        if event.get("task_id") is None:
            return
        task_id = identifier(event["task_id"], "task_id")
        row = db.execute("SELECT data FROM session_tasks WHERE host_id=? AND source=? AND session_id=? AND task_id=?",
                         (self.host_id, session["source"], session["session_id"], task_id)).fetchone()
        task = json.loads(row[0]) if row else {
            "host_id": self.host_id, "source": session["source"], "session_id": session["session_id"],
            "task_id": task_id, "created_at": at,
        }
        previous_at = task.get("last_event_at", -1)
        if at < previous_at:
            return
        completing = event["kind"] == "task_completed"
        if task.get("status") == "completed" and not completing:
            return
        task.update(status="completed" if completing else "created", last_event_at=at)
        if completing:
            task["completed_at"] = at
        subject = safe_text(event.get("task_subject"), 500)
        if subject is not None:
            task["subject"] = subject
        teammate = safe_text(event.get("teammate_name"), 240)
        if teammate is not None:
            task["teammate_name"] = teammate
        if event.get("agent_id") is not None:
            task["agent_id"] = identifier(event["agent_id"], "agent_id")
        db.execute("""INSERT INTO session_tasks(host_id,source,session_id,task_id,data) VALUES(?,?,?,?,?)
            ON CONFLICT(host_id,source,session_id,task_id) DO UPDATE SET data=excluded.data""",
                   (self.host_id, session["source"], session["session_id"], task_id, canonical(task)))

    @staticmethod
    def view(record: dict, at: int | None = None) -> dict:
        for key in ("host_id", "source", "session_id", "parent_session_id"):
            if record.get(key) is not None:
                identifier(record[key], key)
        at = now_ms() if at is None else at
        age = max(0, at - record["last_event_at"])
        result = {key: value for key, value in record.items() if key not in {
            "inflight_tools", "tool_event_at", "tool_finished_at", "waiting_agents",
            "tool_reset_at", "tool_active_until", "tool_overflow_until", "tool_overflow_active",
            "active_tool_times", "tool_correlation_partial", "tool_correlation_partial_until",
            "tool_terminal_floor", "tool_history_partial", "restart_uncertain_until",
        }}
        result = sanitize_structure(result)
        clock_rollback = record["last_event_at"] > at
        result["observation_age_ms"] = age
        active_times = record.get("active_tool_times")
        exact_active_count = (len(active_times) if isinstance(active_times, dict)
                              else len(record.get("inflight_tools", [])))
        correlation_partial = bool(record.get("tool_correlation_partial")
                                   or record.get("tool_overflow_active"))
        # A waiting notification sets "idle" but cannot complete a running tool.
        active_tool = (not clock_rollback and record["status"] in {"running", "idle"}
                       and SessionObserver._has_active_tool(record, at))
        result["inflight_tool_count"] = exact_active_count
        result["tool_visibility_partial"] = bool(
            correlation_partial or record.get("tool_history_partial")
            or exact_active_count > MAX_DISPLAYED_INFLIGHT_TOOLS
        )
        if clock_rollback:
            result.update(status="unknown", needs_input=False,
                          visibility_reason="Session clock moved backwards; lifecycle freshness is unavailable")
        elif correlation_partial:
            result.update(status="unknown", needs_input=False,
                          visibility_reason="Inflight tool correlation exceeded the exact tracking bound")
        elif ((record["status"] in {"running", "idle"} or record.get("needs_input"))
                and not active_tool and age > STALE_AFTER_MS):
            result.update(status="unknown", needs_input=False,
                          visibility_reason="No recent session lifecycle evidence; last observed nonterminal state")
        return result

    def sessions(self, limit: int = 100) -> list[dict]:
        return self.fleet_records(limit)[0]

    def fleet_records(self, limit: int = 100) -> tuple[list[dict], int | None]:
        """Return the bounded fleet page and active total from one read snapshot."""
        at = now_ms()
        cutoff = at - RECENT_HISTORY_MS
        with self.store.connect() as db:
            db.execute("BEGIN")
            records = [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM sessions WHERE host_id=? AND
                    (json_extract(data,'$.status') IN ('running','idle','unknown') OR
                     json_extract(data,'$.needs_input')=1 OR json_extract(data,'$.last_event_at')>=?)
                    ORDER BY CASE WHEN json_extract(data,'$.needs_input')=1 THEN 0
                        WHEN json_extract(data,'$.status')='unknown' THEN 1 ELSE 2 END,
                        json_extract(data,'$.last_event_at') DESC LIMIT ?""",
                (self.host_id, cutoff, limit))]
            future_or_partial = db.execute("""SELECT COUNT(*) FROM sessions WHERE host_id=? AND (
                    json_extract(data,'$.last_event_at')>? OR
                    json_extract(data,'$.tool_overflow_active')=1 OR
                    (json_extract(data,'$.tool_correlation_partial')=1 AND
                     COALESCE(json_extract(data,'$.tool_correlation_partial_until'),?)>=?) OR
                    (json_extract(data,'$.status')='unknown' AND
                     json_extract(data,'$.restart_uncertain_until')>=?))""",
                                           (self.host_id, at, at, at, at)).fetchone()[0]
            # One predicate, so each session counts once: fresh running/waiting
            # work, or any unexpired tool (a reordered tool_start can leave a
            # live tool under a newer, fresh "idle" status).
            active_sessions = db.execute("""SELECT COUNT(*) FROM sessions WHERE host_id=? AND (
                    ((json_extract(data,'$.status')='running' OR json_extract(data,'$.needs_input')=1) AND
                     json_extract(data,'$.last_event_at') BETWEEN ? AND ?) OR
                    (json_extract(data,'$.status') IN ('running','idle') AND
                     json_extract(data,'$.last_event_at')<=? AND
                     json_extract(data,'$.tool_active_until')>=?))""",
                                         (self.host_id, at - STALE_AFTER_MS, at, at, at)).fetchone()[0]
        active = None if future_or_partial else active_sessions
        return [self.view(record, at) for record in records], active

    def active_count(self) -> int | None:
        return self.fleet_records(0)[1]

    def tasks(self, limit: int = 100) -> list[dict]:
        cutoff = now_ms() - RECENT_HISTORY_MS
        with self.store.connect() as db:
            return [self.task_view(json.loads(row[0])) for row in db.execute(
                """SELECT data FROM session_tasks WHERE host_id=? AND
                    (json_extract(data,'$.status')!='completed' OR json_extract(data,'$.last_event_at')>=?)
                    ORDER BY json_extract(data,'$.last_event_at') DESC LIMIT ?""",
                (self.host_id, cutoff, limit))]

    @staticmethod
    def task_view(record: dict) -> dict:
        for key in ("host_id", "source", "session_id", "task_id", "agent_id"):
            if record.get(key) is not None:
                identifier(record[key], key)
        return sanitize_structure(record)
