"""Read-only native-agent observations, separate from Router-owned tasks."""

import json
import logging
import uuid

from router.db import Store, now_ms
from router.dispatch import canonical
from router.sanitize import bound_text, sanitize_structure, strip_terminal_sequences
from router.validation import MAX_WIRE_INTEGER, is_wire_integer

LOGGER = logging.getLogger(__name__)

STALE_AFTER_MS = 60_000
SOURCES = frozenset({"claude", "codex"})
MAX_EVENT_FUTURE_SKEW_MS = 60_000
# Lifecycle fields only. Content, token usage, and cost belong to Langfuse; older rows
# may still hold such fields, so views project onto this allowlist.
VIEW_FIELDS = (
    "host_id", "source", "task_id", "agent_id", "executor", "owner", "control_mode",
    "parent_session_id", "parent_agent_id", "task", "agent_type", "requested_model",
    "resolved_model", "model_source", "status", "last_observed_status", "started_at",
    "finished_at", "duration_ms", "last_event_at", "runtime_partial", "cwd",
    "activity", "visibility_reason",
)
# Collected by older versions; stripped from stored rows so content does not linger.
RETIRED_FIELDS = frozenset({
    "summary", "files_touched", "messages", "models", "model_history_partial", "cost", "cost_source",
    "cost_note", "usage", "usage_source", "usage_partial", "usage_invalid", "price_date", "price_source",
    "price_sources", "price_overrides_used", "transcript", "transcript_offset", "transcript_pending",
    "transcript_eof", "transcript_error", "transcript_error_irreversible", "transcript_transient_error",
    "transcript_file_id", "transcript_file_size", "transcript_file_mtime_ns", "transcript_boundary",
    "transcript_revision", "skip_line",
    # Only ever discovered from transcripts; hooks report the agent's cwd instead.
    "worktree",
})


def text(value: object, limit: int | None = 240) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(
        character for character in strip_terminal_sequences(value)
        if character.isprintable() or character.isspace()
    ).split())
    return (cleaned[:limit] if limit is not None else cleaned) or None


def identifier(value: object, name: str, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Native observation requires {name}")
    normalized = text(value, None)
    if (normalized is None or normalized != value or len(normalized) > limit
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Native observation has invalid {name}")
    return normalized


def safe_text(value: object, limit: int) -> str | None:
    normalized = text(value, None)
    return bound_text(normalized, limit) if normalized is not None else None


class NativeObserver:
    def __init__(self, store: Store, host_id: str | None = None):
        self.store = store
        self.host_id = identifier(host_id or "unknown", "host_id")

    def reconcile_restart(self) -> None:
        # A daemon restart is not evidence that host-owned execution continued.
        at = now_ms()
        with self.store.transaction() as db:
            for row in db.execute("""SELECT data FROM observations
                    WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.source') IN ('claude','codex')""",
                    (self.host_id,)).fetchall():
                record = json.loads(row[0])
                if RETIRED_FIELDS & record.keys() or record.get("model_source") == "transcript":
                    self.put(db, record)  # One-time cleanup of older rows; put() strips.
                record.setdefault("session_ended_at", -1)
                record.setdefault("session_end_uncertain", False)
                record.setdefault("session_end_segment_started_at", None)
                record.setdefault("session_end_segment_base_duration", None)
                record.setdefault("session_end_prior_state_at", None)
                if record["status"] in {"running", "idle", "needs_input"}:
                    if record.get("run_started_at") is not None:
                        last_event_at = record.get("last_event_at")
                        observed_until = at if last_event_at is None else last_event_at
                        record["duration_ms"] = ((record.get("duration_ms") or 0)
                                                 + max(0, observed_until - record["run_started_at"]))
                    record.update(status="unknown", run_started_at=None, runtime_partial=True,
                                  continuity_uncertain=True,
                                  # Unresolved, not stopped: capacity cannot be exact until then.
                                  restart_uncertain_until=min(
                                      MAX_WIRE_INTEGER, (record.get("last_event_at") or 0) + STALE_AFTER_MS),
                                  visibility_reason="Router restarted; awaiting native lifecycle evidence")
                    self.put(db, record)

    def put(self, db, record: dict) -> None:
        for key in RETIRED_FIELDS & record.keys():
            del record[key]
        if record.get("model_source") == "transcript":
            # Transcript-derived models are Langfuse's role; keep only host-reported ones.
            record.update(resolved_model=None, model_source=None)
        db.execute("""INSERT INTO observations(task_id,session_id,data) VALUES(?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET data=excluded.data""",
                   (record["task_id"], record["parent_session_id"], canonical(record)))

    def get(self, task_id: str, db=None) -> dict:
        if db is None:
            with self.store.connect() as connection:
                return self.get(task_id, connection)
        row = db.execute("SELECT data FROM observations WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown observed agent")
        return json.loads(row[0])

    def observe(self, event: dict, timeout: float = 10) -> dict:
        source, raw_session, kind = (event.get(key) for key in ("source", "session_id", "kind"))
        at = event.get("observed_at")
        if source not in SOURCES or kind not in {
                "start", "launch", "result", "activity", "stop", "session_end", "idle", "needs_input", "failed"}:
            raise ValueError("Unsupported native observation")
        session = identifier(raw_session, "session_id")
        if not is_wire_integer(at) or at > now_ms() + MAX_EVENT_FUTURE_SKEW_MS:
            raise ValueError("Native observation requires a timestamp")
        with self.store.transaction(timeout) as db:
            if kind == "session_end":
                db.execute("""INSERT INTO native_session_watermarks(host_id,source,session_id,ended_at)
                        VALUES(?,?,?,?) ON CONFLICT(host_id,source,session_id) DO UPDATE SET
                        ended_at=MAX(ended_at,excluded.ended_at)""",
                           (self.host_id, source, session, at))
                for row in db.execute("""SELECT data FROM observations WHERE session_id=?
                        AND json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')=?""",
                        (session, self.host_id, source)).fetchall():
                    record = json.loads(row[0])
                    previous_end = record.get("session_ended_at", -1)
                    if at < previous_end:
                        continue
                    record["session_ended_at"] = at
                    # Codex subagents end only with their session, so an agent left unknown by a
                    # Router restart is still closed by the session's end.
                    open_states = {"running", "idle", "needs_input"} | ({"unknown"} if source == "codex" else set())
                    if record["status"] in open_states and at >= record.get("state_at", -1):
                        segment_start = record.get("run_started_at")
                        base_duration = record.get("duration_ms") or 0
                        prior_state_at = record.get("state_at", -1)
                        if record.get("run_started_at") is not None:
                            record["duration_ms"] = base_duration + max(0, at - segment_start)
                        if source == "codex":
                            # Codex has no subagent-end hook; its subagents end with the root session.
                            record.update(status="stopped", last_observed_status="stopped", finished_at=at,
                                          run_started_at=None, continuity_uncertain=False,
                                          visibility_reason="Codex session ended",
                                          state_at=at, last_event_at=max(record["last_event_at"] or 0, at))
                            self.put(db, record)
                            continue
                        record.update(status="unknown", continuity_uncertain=True,
                                      run_started_at=None, runtime_partial=True,
                                      session_end_uncertain=True,
                                      session_end_segment_started_at=segment_start,
                                      session_end_segment_base_duration=base_duration,
                                      session_end_prior_state_at=prior_state_at,
                                      visibility_reason="Host session ended without observed agent stop",
                                      state_at=at, last_event_at=max(record["last_event_at"] or 0, at))
                    self.put(db, record)
                return {"observed": True}
            agent = identifier(event.get("agent_id"), "agent_id")
            task_id = "native-" + uuid.uuid5(uuid.NAMESPACE_URL, canonical([source, session, agent])).hex
            row = db.execute("""SELECT data FROM observations
                    WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')=?
                      AND session_id=? AND json_extract(data,'$.agent_id')=?""",
                    (self.host_id, source, session, agent)).fetchone()
            session_row = db.execute("""SELECT ended_at FROM native_session_watermarks
                    WHERE host_id=? AND source=? AND session_id=?""",
                                     (self.host_id, source, session)).fetchone()
            session_ended_at = session_row[0] if session_row is not None else -1
            record = json.loads(row[0]) if row else {
                "host_id": self.host_id, "source": source,
                "task_id": task_id, "agent_id": agent, "executor": source, "owner": source, "control_mode": "observe",
                "parent_session_id": session, "parent_agent_id": None, "task": None, "agent_type": None,
                "requested_model": None, "resolved_model": None, "model_source": None,
                "status": "unknown", "last_observed_status": None, "state_at": -1, "started_at": None,
                "run_started_at": None, "finished_at": None, "duration_ms": None, "last_event_at": None,
                "continuity_uncertain": False, "runtime_partial": False,
                "result_at": -1, "result_segment_started_at": None, "result_segment_base_duration": None,
                "session_ended_at": -1, "session_end_uncertain": False,
                "session_end_segment_started_at": None, "session_end_segment_base_duration": None,
                "session_end_prior_state_at": None,
                "cwd": None, "activity": None, "visibility_reason": None}
            live_kinds = {"start", "activity", "idle", "needs_input"}
            if source == "codex" and kind in live_kinds and session_ended_at >= 0:
                # Async Codex hooks are stamped when they run, so an agent event can arrive after
                # its session ended. Only a later root SessionStart (resume) makes it live again.
                session_state = db.execute("""SELECT json_extract(data,'$.status'),
                        json_extract(data,'$.root_started_at') FROM sessions
                        WHERE host_id=? AND source=? AND session_id=?""",
                                           (self.host_id, source, session)).fetchone()
                resumed_at = session_state[1] if session_state is not None else None
                if (session_state is None or session_state[0] == "stopped"
                        or isinstance(resumed_at, int) and at <= resumed_at):
                    at = min(at, session_ended_at)
                elif (row and record["status"] == "stopped" and at > session_ended_at
                      and record.get("visibility_reason") == "Codex session ended"):
                    record["status"] = "unknown"  # Real activity in the resumed session.
            if not row and session_ended_at >= 0 and source == "codex" and at <= session_ended_at:
                # Codex subagents end with their session; a late first event is already over.
                record.update(status="stopped", last_observed_status="stopped", finished_at=session_ended_at,
                              state_at=session_ended_at, last_event_at=session_ended_at,
                              session_ended_at=session_ended_at, visibility_reason="Codex session ended")
            elif not row and session_ended_at >= 0:
                record.update(
                    state_at=session_ended_at,
                    last_event_at=session_ended_at,
                    session_ended_at=session_ended_at,
                    session_end_uncertain=True,
                    visibility_reason="Host session ended before agent lifecycle evidence arrived",
                )
            terminal_metadata_only = record["status"] in {"stopped", "failed"} and kind in {"launch", "result"}
            if record["status"] in {"stopped", "failed"} and kind not in {"start", "launch", "result"}:
                if not row:
                    self.put(db, record)  # A row first created as terminal must still be stored.
                return self.view(record)
            metadata_can_replace = at >= (record["last_event_at"] or -1)
            if event.get("parent_agent_id") is not None:
                parent_agent_id = identifier(event["parent_agent_id"], "parent_agent_id")
                if metadata_can_replace or record.get("parent_agent_id") is None:
                    record["parent_agent_id"] = parent_agent_id
            for key in ("task", "agent_type", "requested_model", "cwd", "activity"):
                limit = 2000 if key == "cwd" else 240
                value = safe_text(event.get(key), limit)
                if value is not None and (metadata_can_replace or record.get(key) is None):
                    record[key] = value
            model = safe_text(event.get("resolved_model"), 240)
            if model and (metadata_can_replace or record.get("resolved_model") is None
                          or record.get("model_source") == "transcript"):
                record.update(resolved_model=model, model_source="host_result")
            if terminal_metadata_only and kind == "result":
                # A terminal record keeps its state, but its result watermark
                # must still block older or tied delayed starts.
                record["result_at"] = max(record.get("result_at", -1), at)
            result_at = record.get("result_at", -1)
            session_ended_at = record.get("session_ended_at", -1)
            result_blocks = kind in {"start", "launch", "activity", "idle", "needs_input"} and at <= result_at
            session_end_blocks = kind in {"start", "launch", "activity", "idle", "needs_input"} and at <= session_ended_at
            terminal_at_same_time = (record["status"] in {"stopped", "failed"}
                                     and kind in {"start", "launch", "activity", "idle", "needs_input"}
                                     and at <= record["state_at"])
            session_end_prior_state_at = record.get("session_end_prior_state_at")
            if not isinstance(session_end_prior_state_at, int) or isinstance(session_end_prior_state_at, bool):
                session_end_prior_state_at = -1
            delayed_session_stop = (kind in {"stop", "failed"} and record.get("session_end_uncertain", False)
                                    and session_end_prior_state_at <= at <= session_ended_at)
            if (not terminal_metadata_only and not result_blocks and not session_end_blocks
                    and not terminal_at_same_time
                    and (at >= record.get("state_at", -1) or delayed_session_stop)
                    and not (kind == "launch" and record["last_observed_status"] == "stopped")):
                if kind == "result":
                    if record["status"] != "stopped":
                        segment_start = record.get("run_started_at")
                        if segment_start is not None:
                            base_duration = record.get("duration_ms") or 0
                            record.update(
                                duration_ms=base_duration + max(0, at - segment_start),
                                result_segment_started_at=segment_start,
                                result_segment_base_duration=base_duration,
                            )
                        record.update(status="unknown", continuity_uncertain=True, run_started_at=None,
                                      runtime_partial=True,
                                      visibility_reason="Host returned a result without observed stop")
                    record["result_at"] = max(result_at, at)
                elif kind in {"start", "launch", "activity"}:
                    if record["last_observed_status"] != "running" or record.get("continuity_uncertain"):
                        if record.get("continuity_uncertain"):
                            record["runtime_partial"] = True
                        record["run_started_at"] = at
                    record.update(status="running", last_observed_status="running", finished_at=None, continuity_uncertain=False, visibility_reason=None)
                    record.update(result_segment_started_at=None, result_segment_base_duration=None,
                                  session_end_uncertain=False,
                                  session_end_segment_started_at=None,
                                  session_end_segment_base_duration=None,
                                  session_end_prior_state_at=None)
                    if record["started_at"] is None:
                        record["started_at"] = at
                elif kind in {"stop", "failed"}:
                    # Delayed terminal evidence of either kind closes the
                    # segment left open by a result or session end.
                    session_segment_start = record.get("session_end_segment_started_at")
                    result_segment_start = record.get("result_segment_started_at")
                    if delayed_session_stop and session_segment_start is not None:
                        record["duration_ms"] = ((record.get("session_end_segment_base_duration") or 0)
                                                 + max(0, at - session_segment_start))
                    elif result_segment_start is not None and at <= record.get("result_at", -1):
                        record["duration_ms"] = ((record.get("result_segment_base_duration") or 0)
                                                 + max(0, at - result_segment_start))
                    elif record["status"] != "stopped" and record["run_started_at"] is not None:
                        record["duration_ms"] = ((record["duration_ms"] or 0)
                                                 + max(0, at - record["run_started_at"]))
                    terminal = "stopped" if kind == "stop" else "failed"
                    reason = None if kind == "stop" else (
                        safe_text(event.get("failure_reason"), 500) or "Agent turn failed")
                    record.update(status=terminal, last_observed_status=terminal, finished_at=at, run_started_at=None,
                                  continuity_uncertain=False, visibility_reason=reason,
                                  result_segment_started_at=None, result_segment_base_duration=None,
                                  session_end_uncertain=False,
                                  session_end_segment_started_at=None,
                                  session_end_segment_base_duration=None,
                                  session_end_prior_state_at=None)
                elif kind == "idle":
                    if record.get("run_started_at") is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="idle", last_observed_status="idle", run_started_at=None, visibility_reason=None)
                elif kind == "needs_input":
                    if record.get("run_started_at") is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="needs_input", last_observed_status="needs_input", run_started_at=None,
                                  visibility_reason="The host reported that the agent needs input")
                if kind in {"start", "activity", "stop", "idle", "needs_input", "failed"}:
                    record["state_at"] = max(record.get("state_at", -1), at)
            record["last_event_at"] = max(record["last_event_at"] or 0, at)
            self.put(db, record)
        return self.view(record)

    @staticmethod
    def view(record: dict, at: int | None = None) -> dict:
        for key in ("host_id", "source", "task_id", "agent_id", "parent_session_id", "parent_agent_id"):
            if record.get(key) is not None:
                identifier(record[key], key)
        result = sanitize_structure({key: record[key] for key in VIEW_FIELDS if key in record})
        if result.get("model_source") == "transcript":
            # Older versions learned models from transcripts; that is Langfuse's role now.
            result.update(resolved_model=None, model_source=None)
        at = now_ms() if at is None else at
        age = max(0, at - record["last_event_at"]) if record["last_event_at"] is not None else None
        result["observation_age_ms"] = age
        result["elapsed_ms"] = None
        clock_rollback = record["last_event_at"] is not None and record["last_event_at"] > at
        if record["status"] in {"running", "idle", "needs_input"} and clock_rollback:
            # Mirrors sessions: a future timestamp is not fresh evidence.
            result.update(status="unknown",
                          visibility_reason="Native clock moved backwards; lifecycle freshness is unavailable")
        elif record["status"] in {"running", "idle", "needs_input"} and age is not None and age > STALE_AFTER_MS:
            result.update(status="unknown", visibility_reason="No recent native lifecycle evidence; last observed nonterminal state")
        elif record["status"] == "running" and record["run_started_at"] is not None:
            result["elapsed_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
        elif record["status"] in {"stopped", "failed"}:
            result["elapsed_ms"] = record["duration_ms"]
        return result

    def status(self, task_id: str | None = None, offset: int = 0) -> dict | list:
        if task_id:
            return self.view(self.get(task_id))
        with self.store.connect() as db:
            identities = [row[0] for row in db.execute("""SELECT task_id FROM observations
                ORDER BY CASE json_extract(data,'$.status') WHEN 'running' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                json_extract(data,'$.last_event_at') DESC LIMIT 100 OFFSET ?""", (offset,))]
        rows = []
        for identity in identities:
            try:
                view = self.view(self.get(identity))
            except ValueError:
                # A legacy row that fails today's identity rules must not hide
                # every other agent.
                LOGGER.warning("Skipping native observation with an invalid identity")
                continue
            rows.append(view)
        return rows

    def snapshot(self, limit: int = 100) -> list[dict]:
        """Bounded fleet page of native agents."""
        return self.fleet_records(limit)[0]

    def fleet_records(self, limit: int = 100) -> tuple[list[dict], int | None]:
        """Return the bounded fleet page and active total from one read snapshot."""
        at = now_ms()
        cutoff = at - 300_000
        with self.store.connect() as db:
            db.execute("BEGIN")
            records = [json.loads(row[0]) for row in db.execute("""SELECT data FROM observations
                WHERE json_extract(data,'$.host_id')=? AND
                    (json_extract(data,'$.status') IN ('running','idle','needs_input','unknown') OR
                     json_extract(data,'$.last_event_at')>=?)
                ORDER BY CASE json_extract(data,'$.status')
                    WHEN 'needs_input' THEN 0 WHEN 'unknown' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                    json_extract(data,'$.last_event_at') DESC LIMIT ?""", (self.host_id, cutoff, limit))]
            active = db.execute("""SELECT COUNT(*) FROM observations
                WHERE json_extract(data,'$.host_id')=?
                  AND json_extract(data,'$.status') IN ('running','needs_input')
                  AND json_extract(data,'$.last_event_at')>=?""",
                                (self.host_id, at - STALE_AFTER_MS)).fetchone()[0]
            future = db.execute("""SELECT COUNT(*) FROM observations
                WHERE json_extract(data,'$.host_id')=?
                  AND json_extract(data,'$.status') IN ('running','idle','needs_input')
                  AND json_extract(data,'$.last_event_at')>?""", (self.host_id, at)).fetchone()[0]
            unresolved = db.execute("""SELECT COUNT(*) FROM observations
                WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.status')='unknown'
                  AND json_extract(data,'$.restart_uncertain_until')>=?""", (self.host_id, at)).fetchone()[0]
        if future or unresolved:
            # Clock rollback or restart uncertainty: an exact active total cannot be
            # established, even off-page, so capacity becomes unknown.
            active = None
        visible = []
        for record in records:
            try:
                view = self.view(record, at)
            except ValueError:
                LOGGER.warning("Skipping native observation with an invalid identity")
                # The omitted row may be active; report the total as unknown so
                # the snapshot is visibly partial.
                active = None
                continue
            visible.append(view)
        return visible, active

    def active_count(self) -> int | None:
        return self.fleet_records(0)[1]
