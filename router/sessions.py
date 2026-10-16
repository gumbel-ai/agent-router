"""Top-level Claude session and agent-team task lifecycle records."""

import json

from router.db import now_ms
from router.dispatch import canonical
from router.native import STALE_AFTER_MS, text
from router.redaction import redact_secrets

RECENT_HISTORY_MS = 300_000
MAX_INFLIGHT_AGE_MS = 86_400_000
UNKNOWN_WAITER = "__session__"


class SessionObserver:
    def __init__(self, store, host_id: str):
        self.store = store
        self.host_id = host_id

    def reconcile_restart(self) -> None:
        with self.store.transaction() as db:
            for row in db.execute("SELECT data FROM sessions WHERE host_id=? AND source='claude'", (self.host_id,)):
                record = json.loads(row[0])
                if record["status"] in {"running", "idle"} or record.get("needs_input"):
                    record.update(status="unknown", needs_input=False,
                                  visibility_reason="Router restarted; awaiting session lifecycle evidence",
                                  inflight_tools=[], tool_event_at={}, tool_finished_at={}, waiting_agents=[])
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
                "inflight_tools": [], "tool_event_at": {}, "tool_finished_at": {}, "waiting_agents": []}

    @staticmethod
    def _clear_waiter(record: dict, event: dict) -> bool:
        waiting = set(record.get("waiting_agents", []))
        waiting.discard(text(event.get("agent_id")) or UNKNOWN_WAITER)
        record["waiting_agents"] = sorted(waiting)
        return bool(waiting)

    def observe(self, event: dict, timeout: float = 10) -> dict:
        source = text(event.get("source"))
        session_id = text(event.get("session_id"))
        kind = text(event.get("kind"))
        at = event.get("observed_at")
        if source != "claude" or not session_id or not kind:
            raise ValueError("Unsupported session observation")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ValueError("Session observation requires a timestamp")
        with self.store.transaction(timeout) as db:
            record = self._session(db, source, session_id, at)
            if kind in {"task_created", "task_completed"}:
                self._observe_task(db, record, event, at)
            if record["status"] == "stopped" and kind != "session_start":
                return self.view(record)
            tool_changed = self._observe_tool(record, event, at)
            if at < record["last_event_at"]:
                if tool_changed:
                    self._put_session(db, record)
                return self.view(record)
            record["last_event_at"] = at
            record["last_event"] = kind
            for key, limit in (("cwd", 2000), ("model", 240), ("parent_session_id", 240)):
                value = text(event.get(key), limit)
                if value is not None:
                    record[key] = value
            if kind == "session_start":
                record.update(status="running", finished_at=None, visibility_reason=None,
                              activity="session " + (text(event.get("start_source")) or "start"), needs_input=False,
                              inflight_tools=[], tool_event_at={}, tool_finished_at={}, waiting_agents=[])
                record["started_at"] = min(record["started_at"], at)
            elif kind == "session_end":
                record.update(status="stopped", finished_at=at, activity=None, needs_input=False,
                              visibility_reason=None, inflight_tools=[], tool_event_at={}, tool_finished_at={},
                              waiting_agents=[])
            elif kind in {"tool_start", "tool_end", "tool_failure"}:
                waiting = self._clear_waiter(record, event)
                record.update(status="running", finished_at=None, activity=text(event.get("activity")) or kind,
                              needs_input=waiting,
                              visibility_reason="Claude is waiting for input" if waiting else None)
            elif kind == "prompt":
                record.update(status="running", finished_at=None, activity=kind, needs_input=False,
                              visibility_reason=None, inflight_tools=[], tool_event_at={}, tool_finished_at={},
                              waiting_agents=[])
            elif kind in {"task_created", "task_completed",
                          "subagent_start", "subagent_stop"}:
                activity = text(event.get("activity")) or kind
                waiting = self._clear_waiter(record, event)
                record.update(status="running", finished_at=None, activity=activity, needs_input=waiting,
                              visibility_reason="Claude is waiting for input" if waiting else None)
            elif kind == "stop":
                background = event.get("background_tasks")
                live = isinstance(background, list) and any(isinstance(item, dict) and item.get("status") not in {"completed", "failed", "stopped"} for item in background)
                record.update(status="running" if live else "idle", activity="background task" if live else None,
                              needs_input=False, visibility_reason=None, inflight_tools=[], tool_event_at={},
                              tool_finished_at={}, waiting_agents=[])
            elif kind == "stop_failure":
                record.update(status="idle", activity="StopFailure", needs_input=False,
                              visibility_reason="Claude reported that the turn failed", inflight_tools=[], tool_event_at={},
                              tool_finished_at={}, waiting_agents=[])
            elif kind == "notification":
                notification = text(event.get("notification_type")) or "notification"
                needs_input = notification in {"permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"}
                if needs_input:
                    waiting = set(record.get("waiting_agents", []))
                    waiting.add(text(event.get("agent_id")) or UNKNOWN_WAITER)
                    record.update(status="idle", needs_input=True, waiting_agents=sorted(waiting), activity=notification,
                                  visibility_reason="Claude is waiting for input")
                elif notification == "agent_completed":
                    waiting = self._clear_waiter(record, event)
                    record.update(status="idle", needs_input=waiting, activity=notification,
                                  visibility_reason="Claude is waiting for input" if waiting else None)
                else:
                    record["activity"] = notification
            elif kind == "teammate_idle":
                record["activity"] = "teammate idle"
            else:
                raise ValueError("Unsupported session observation")
            self._put_session(db, record)
        return self.view(record)

    @staticmethod
    def _observe_tool(record: dict, event: dict, at: int) -> bool:
        if event["kind"] not in {"tool_start", "tool_end", "tool_failure"}:
            return False
        tool_use_id = text(event.get("tool_use_id"))
        if not tool_use_id:
            return False
        event_times = dict(record.get("tool_event_at", {}))
        if at < event_times.get(tool_use_id, -1):
            return False
        finished_times = dict(record.get("tool_finished_at", {}))
        if event["kind"] == "tool_start" and at <= finished_times.get(tool_use_id, -1):
            return False
        tools = [item for item in record.get("inflight_tools", [])
                 if item.get("tool_use_id") != tool_use_id]
        if event["kind"] == "tool_start":
            tools.append({"tool_use_id": tool_use_id, "activity": text(event.get("activity")) or "tool"})
        else:
            finished_times.pop(tool_use_id, None)
            finished_times[tool_use_id] = at
        event_times.pop(tool_use_id, None)
        event_times[tool_use_id] = at
        record["inflight_tools"] = tools[-64:]
        record["tool_event_at"] = dict(list(event_times.items())[-128:])
        record["tool_finished_at"] = dict(list(finished_times.items())[-128:])
        return True

    def _observe_task(self, db, session: dict, event: dict, at: int) -> None:
        task_id = text(event.get("task_id"))
        if not task_id:
            return
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
        for source, destination in (("task_subject", "subject"), ("teammate_name", "teammate_name"), ("agent_id", "agent_id")):
            value = text(event.get(source), None if source == "task_subject" else 240)
            if value is not None:
                if source == "task_subject":
                    value = redact_secrets(value, 500)
                task[destination] = value
        db.execute("""INSERT INTO session_tasks(host_id,source,session_id,task_id,data) VALUES(?,?,?,?,?)
            ON CONFLICT(host_id,source,session_id,task_id) DO UPDATE SET data=excluded.data""",
                   (self.host_id, session["source"], session["session_id"], task_id, canonical(task)))

    @staticmethod
    def view(record: dict, at: int | None = None) -> dict:
        at = now_ms() if at is None else at
        age = max(0, at - record["last_event_at"])
        result = {key: value for key, value in record.items() if key not in {
            "inflight_tools", "tool_event_at", "tool_finished_at", "waiting_agents",
        }}
        result["observation_age_ms"] = age
        tools = record.get("inflight_tools", [])
        event_times = record.get("tool_event_at", {})
        active_tool = record["status"] == "running" and any(
            max(0, at - event_times.get(item.get("tool_use_id"), 0)) <= MAX_INFLIGHT_AGE_MS
            for item in tools
        )
        result["inflight_tool_count"] = len(tools)
        if ((record["status"] in {"running", "idle"} or record.get("needs_input"))
                and not active_tool and age > STALE_AFTER_MS):
            result.update(status="unknown", needs_input=False,
                          visibility_reason="No recent session lifecycle evidence; last observed nonterminal state")
        return result

    def sessions(self, limit: int = 100) -> list[dict]:
        return self.fleet_records(limit)[0]

    def fleet_records(self, limit: int = 100) -> tuple[list[dict], int]:
        """Return the bounded fleet page and active total from one read snapshot."""
        at = now_ms()
        cutoff = at - RECENT_HISTORY_MS
        with self.store.connect() as db:
            db.execute("BEGIN")
            records = [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM sessions WHERE host_id=? AND
                    (json_extract(data,'$.status') IN ('running','idle','unknown') OR
                     json_extract(data,'$.needs_input')=1 OR json_extract(data,'$.last_event_at')>=?)
                    ORDER BY json_extract(data,'$.last_event_at') DESC LIMIT ?""",
                (self.host_id, cutoff, limit))]
            fresh_active = db.execute("""SELECT COUNT(*) FROM sessions WHERE host_id=? AND
                    (json_extract(data,'$.status')='running' OR json_extract(data,'$.needs_input')=1) AND
                    json_extract(data,'$.last_event_at')>=?""",
                                      (self.host_id, at - STALE_AFTER_MS)).fetchone()[0]
            tool_candidates = [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM sessions WHERE host_id=? AND
                    json_extract(data,'$.status')='running' AND
                    json_extract(data,'$.last_event_at')<? AND
                    json_extract(data,'$.last_event_at')>=?""",
                (self.host_id, at - STALE_AFTER_MS, at - MAX_INFLIGHT_AGE_MS))]
        active_tools = sum(self.view(record, at)["status"] == "running" for record in tool_candidates)
        return [self.view(record, at) for record in records], fresh_active + active_tools

    def active_count(self) -> int:
        return self.fleet_records(0)[1]

    def tasks(self, limit: int = 100) -> list[dict]:
        cutoff = now_ms() - RECENT_HISTORY_MS
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                """SELECT data FROM session_tasks WHERE host_id=? AND
                    (json_extract(data,'$.status')!='completed' OR json_extract(data,'$.last_event_at')>=?)
                    ORDER BY json_extract(data,'$.last_event_at') DESC LIMIT ?""",
                (self.host_id, cutoff, limit))]
