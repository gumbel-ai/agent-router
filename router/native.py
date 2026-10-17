"""Read-only native-agent observations, separate from Router-owned tasks."""

import hashlib
import json
import logging
import math
import os
import uuid
from pathlib import Path

from router.db import Store, now_ms
from router.dispatch import canonical
from router.pricing import DEFAULT_PRICES, PRICE_DATE, PRICE_SOURCE, estimate
from router.redaction import redact_secrets, redact_structure, strip_terminal_sequences
from router.validation import MAX_WIRE_INTEGER, is_wire_integer

LOGGER = logging.getLogger(__name__)

STALE_AFTER_MS = 60_000
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MAX_USAGE_INTEGER = MAX_WIRE_INTEGER
MAX_TRANSCRIPT_MESSAGES = 10_000
PREFIX_DIGEST_TAG = "sha256-prefix:"
MAX_MODELS = 64
MAX_COST = 1e12
MAX_EVENT_FUTURE_SKEW_MS = 60_000


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
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or redact_secrets(normalized) != normalized):
        raise ValueError(f"Native observation has invalid {name}")
    return normalized


def safe_text(value: object, limit: int) -> str | None:
    normalized = text(value, None)
    return redact_secrets(normalized, limit) if normalized is not None else None


def _private_path(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 2000
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or redact_secrets(value, 2001) != value):
        raise ValueError(f"Native observation has invalid {name}")
    return value


def _valid_cost(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= MAX_COST and math.isfinite(value))


class NativeObserver:
    def __init__(self, store: Store, prices=None, host_id: str | None = None):
        self.store = store
        self.host_id = identifier(host_id or "unknown", "host_id")
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        self.price_override_models = set(prices or {})

    def reconcile_restart(self) -> None:
        # A daemon restart is not evidence that host-owned execution continued.
        at = now_ms()
        with self.store.transaction() as db:
            for row in db.execute("""SELECT data FROM observations
                    WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')='claude'""",
                    (self.host_id,)).fetchall():
                record = json.loads(row[0])
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
                                  visibility_reason="Router restarted; awaiting native lifecycle evidence")
                    self.put(db, record)

    def put(self, db, record: dict) -> None:
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
        if source != "claude" or kind not in {
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
                    if (record["status"] in {"running", "idle", "needs_input"}
                            and at >= record.get("state_at", -1)):
                        segment_start = record.get("run_started_at")
                        base_duration = record.get("duration_ms") or 0
                        prior_state_at = record.get("state_at", -1)
                        if record.get("run_started_at") is not None:
                            record["duration_ms"] = base_duration + max(0, at - segment_start)
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
                "requested_model": None, "resolved_model": None, "models": [], "model_source": None,
                "status": "unknown", "last_observed_status": None, "state_at": -1, "started_at": None,
                "run_started_at": None, "finished_at": None, "duration_ms": None, "last_event_at": None,
                "continuity_uncertain": False, "runtime_partial": False,
                "result_at": -1, "result_segment_started_at": None, "result_segment_base_duration": None,
                "session_ended_at": -1, "session_end_uncertain": False,
                "session_end_segment_started_at": None, "session_end_segment_base_duration": None,
                "session_end_prior_state_at": None,
                "cwd": None, "worktree": None, "activity": None, "files_touched": [], "summary": None,
                "cost": None, "cost_source": "unavailable", "usage": None, "usage_source": "unavailable",
                "cost_note": "Token usage unavailable", "price_date": None, "price_source": None, "price_overrides_used": [],
                "price_sources": {}, "usage_partial": False, "transcript_pending": False, "transcript_eof": False,
                "usage_invalid": False, "model_history_partial": False,
                "skip_line": False, "transcript": None, "transcript_offset": 0,
                "transcript_file_id": None, "transcript_file_size": None,
                "transcript_file_mtime_ns": None, "transcript_boundary": None,
                "transcript_revision": 0,
                "transcript_error_irreversible": False, "transcript_transient_error": False,
                "messages": {}, "visibility_reason": None}
            if not row and session_ended_at >= 0:
                record.update(
                    state_at=session_ended_at,
                    last_event_at=session_ended_at,
                    session_ended_at=session_ended_at,
                    session_end_uncertain=True,
                    visibility_reason="Host session ended before agent lifecycle evidence arrived",
                )
            terminal_metadata_only = record["status"] in {"stopped", "failed"} and kind in {"launch", "result"}
            if record["status"] in {"stopped", "failed"} and kind not in {"start", "launch", "result"}:
                return self.view(record)
            metadata_can_replace = at >= (record["last_event_at"] or -1)
            if event.get("parent_agent_id") is not None:
                parent_agent_id = identifier(event["parent_agent_id"], "parent_agent_id")
                if metadata_can_replace or record.get("parent_agent_id") is None:
                    record["parent_agent_id"] = parent_agent_id
            for key in ("task", "agent_type", "requested_model", "cwd", "activity", "summary"):
                limit = 800 if key == "summary" else 2000 if key == "cwd" else 240
                value = safe_text(event.get(key), limit)
                if value is not None and (metadata_can_replace or record.get(key) is None):
                    record[key] = value
            if event.get("transcript") is not None and (metadata_can_replace or record.get("transcript") is None):
                try:
                    transcript = _private_path(event["transcript"], "transcript")
                except ValueError:
                    # A token-like path segment must not discard lifecycle
                    # evidence; only usage from that transcript is lost.
                    self._mark_usage_incomplete(record, "Transcript path unusable; usage is incomplete")
                    transcript = record.get("transcript")
                if transcript != record.get("transcript"):
                    record.update(
                        transcript=transcript,
                        transcript_offset=0,
                        transcript_pending=False,
                        transcript_eof=False,
                        transcript_error=None,
                        transcript_error_irreversible=False,
                        transcript_transient_error=False,
                        skip_line=False,
                        transcript_file_id=None,
                        transcript_file_size=None,
                        transcript_file_mtime_ns=None,
                        transcript_boundary=None,
                        transcript_revision=record.get("transcript_revision", 0) + 1,
                        messages={},
                        usage=None,
                        usage_source="unavailable",
                        usage_partial=bool(record.get("model_history_partial")),
                        usage_invalid=False,
                    )
                    if record.get("cost_source") != "host_reported_estimate":
                        record.update(
                            cost=None,
                            cost_source="unavailable",
                            cost_note="Token usage unavailable",
                            price_date=None,
                            price_source=None,
                            price_sources={},
                            price_overrides_used=[],
                        )
            if isinstance(event.get("worktree"), str) and (metadata_can_replace or record.get("worktree") is None):
                record["worktree"] = safe_text(event["worktree"], 2000)
            file = safe_text(event.get("file_path"), 2000)
            if file and file not in record["files_touched"]:
                record["files_touched"].append(file)
                record["files_touched"] = record["files_touched"][-64:]
            model = safe_text(event.get("resolved_model"), 240)
            if model and (metadata_can_replace or record.get("resolved_model") is None):
                self.model(record, model, "host_result")
            cost = event.get("cost")
            if cost is not None and (metadata_can_replace or record.get("cost") is None):
                if _valid_cost(cost):
                    record.update(
                        cost=cost,
                        cost_source="host_reported_estimate",
                        cost_note="Host-reported estimate; not billed spend",
                        price_date=None,
                        price_source=None,
                        price_sources={},
                        price_overrides_used=[],
                    )
                else:
                    record.update(
                        cost=None,
                        cost_source="unavailable",
                        cost_note="Host-reported cost is invalid or exceeds the supported bound",
                        price_date=None,
                        price_source=None,
                        price_sources={},
                        price_overrides_used=[],
                    )
            if kind == "stop":
                # The final API response may have arrived after the last poll.
                record["transcript_eof"] = False
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
                    record["transcript_eof"] = False
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
                                  visibility_reason="Claude reported that the agent needs input")
                if kind in {"start", "activity", "stop", "idle", "needs_input", "failed"}:
                    record["state_at"] = max(record.get("state_at", -1), at)
            record["last_event_at"] = max(record["last_event_at"] or 0, at)
            self.put(db, record)
        return self.view(record)

    @staticmethod
    def model(record: dict, model: str, source: str) -> None:
        record.update(resolved_model=model, model_source=source)
        if len(record["models"]) > MAX_MODELS:
            record["models"] = record["models"][:MAX_MODELS]
            record["usage_partial"] = True
            record["model_history_partial"] = True
        if model not in record["models"]:
            if len(record["models"]) < MAX_MODELS:
                record["models"].append(model)
            else:
                # The wire contract bounds string lists at 64. Mark the model
                # and usage history incomplete rather than silently producing
                # a snapshot that peers reject.
                record["usage_partial"] = True
                record["model_history_partial"] = True

    def refresh(self, task_id: str) -> dict:
        record = self.get(task_id)
        path = record.get("transcript")
        if not path:
            return record
        offset = record["transcript_offset"]
        revision = record.get("transcript_revision", 0)
        stored_file_id = record.get("transcript_file_id")
        try:
            path_info = Path(path).stat()
        except OSError:
            return self._record_transcript_failure(
                task_id, path, offset, revision,
                "Transcript unavailable; usage is incomplete",
            )
        path_file_id = self._file_id(path_info)
        if (record.get("transcript_eof")
                and not record.get("transcript_transient_error")
                and stored_file_id == path_file_id
                and path_info.st_size == offset
                and record.get("transcript_file_size") == path_info.st_size
                and record.get("transcript_file_mtime_ns") == path_info.st_mtime_ns):
            # EOF is provisional: use a bounded metadata check on every refresh
            # so a late append or atomic replacement becomes visible without
            # re-hashing the consumed prefix of an unchanged transcript.
            return record
        # File I/O stays outside SQLite's write transaction. A revision/cursor
        # check below prevents concurrent readers or a path switch from applying
        # stale results.
        try:
            with Path(path).open("rb") as stream:
                opened_info = os.fstat(stream.fileno())
                opened_file_id = self._file_id(opened_info)
                reset_reason = None
                read_offset = offset
                prefix = None
                if stored_file_id is not None and opened_file_id != stored_file_id:
                    reset_reason = "Transcript replaced; prior usage continuity is unavailable"
                    read_offset = 0
                elif opened_info.st_size < offset:
                    reset_reason = "Transcript truncated; usage is unavailable"
                    read_offset = 0
                elif offset and record.get("transcript_boundary") is not None:
                    # Same inode and size or growth do not prove the consumed
                    # prefix is unchanged; compare its full digest.
                    stored = record["transcript_boundary"]
                    if stored.startswith(PREFIX_DIGEST_TAG):
                        prefix = self._prefix_hash(stream, offset)
                        unchanged = PREFIX_DIGEST_TAG + prefix.hexdigest() == stored
                    else:
                        # Records from before the full-prefix digest hold a
                        # 64-byte suffix digest; verify that once, then upgrade.
                        start = max(0, offset - 64)
                        stream.seek(start)
                        unchanged = hashlib.sha256(stream.read(offset - start)).hexdigest() == stored
                    if not unchanged:
                        reset_reason = "Transcript rewritten; prior usage continuity is unavailable"
                        read_offset = 0
                stream.seek(read_offset)
                batch = stream.read(1_048_576)
                final_info = os.fstat(stream.fileno())
                end = batch.rfind(b"\n") + 1
                boundary_advance = len(batch) if not end and len(batch) >= 1_048_576 else end
                cursor = read_offset + boundary_advance
                if prefix is None or read_offset == 0:
                    prefix = self._prefix_hash(stream, read_offset)
                prefix.update(batch[:boundary_advance])
                boundary = PREFIX_DIGEST_TAG + prefix.hexdigest()
        except OSError:
            return self._record_transcript_failure(
                task_id, path, offset, revision,
                "Transcript unavailable; usage is incomplete",
            )
        if not end:
            if len(batch) < 1_048_576:
                return self._record_transcript_pending(
                    task_id, path, offset, revision, bool(batch),
                    opened_file_id, final_info, boundary, reset_reason,
                )
            # Bound memory and advance past an oversized line. Its usage cannot
            # be trusted, so never claim a complete usage-derived cost.
            end = len(batch)
            skipped = True
            lines = []
        else:
            skipped = record.get("skip_line", False) if reset_reason is None else False
            start = batch.find(b"\n") + 1 if skipped else 0
            lines = batch[start:end].splitlines()
        events = []
        malformed_lines = 0
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                malformed_lines += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                malformed_lines += 1
        with self.store.transaction() as db:
            current = self.get(task_id, db)
            if not self._refresh_is_current(current, path, offset, revision):
                return current
            if reset_reason is not None:
                current.update(
                    transcript_offset=0,
                    skip_line=False,
                    messages={},
                    usage=None,
                    usage_source="unavailable",
                )
                self._mark_usage_invalid(current, reset_reason)
            current["transcript_offset"] = cursor
            current.update(skip_line=skipped and not lines and b"\n" not in batch,
                           transcript_pending=current["transcript_offset"] < final_info.st_size,
                           transcript_eof=current["transcript_offset"] >= final_info.st_size,
                           transcript_file_id=opened_file_id,
                           transcript_file_size=final_info.st_size,
                           transcript_file_mtime_ns=final_info.st_mtime_ns,
                           transcript_boundary=boundary,
                           transcript_revision=revision + 1)
            self._clear_transient_error(current)
            if skipped:
                self._mark_usage_incomplete(
                    current, "Oversized transcript record skipped; usage is unavailable",
                )
            if malformed_lines:
                self._mark_usage_invalid(
                    current,
                    f"{malformed_lines} malformed transcript record(s); usage is unavailable",
                )
            for event in events:
                environment = event.get("attachment", {})
                if isinstance(environment, dict) and environment.get("type") == "environment":
                    snapshot = environment.get("snapshot", {})
                    if isinstance(snapshot, dict) and isinstance(snapshot.get("workingDirectory"), str):
                        current["cwd"] = safe_text(snapshot["workingDirectory"], 2000)
                        if snapshot.get("isWorktree") is True:
                            current["worktree"] = safe_text(snapshot["workingDirectory"], 2000)
                if event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    self._mark_usage_invalid(
                        current, "Assistant transcript record has an invalid message envelope",
                    )
                    continue
                model = safe_text(message.get("model"), 240)
                if model == "<synthetic>":
                    continue
                if model:
                    self.model(current, model, "transcript")
                identity, usage = message.get("id"), message.get("usage")
                if not isinstance(identity, str) or not isinstance(usage, dict):
                    self._mark_usage_invalid(
                        current, "Assistant transcript record lacks valid identity or usage",
                    )
                    continue
                if any(
                    not isinstance(usage.get(key), int)
                    or isinstance(usage.get(key), bool)
                    or not 0 <= usage[key] <= MAX_USAGE_INTEGER
                    for key in ("input_tokens", "output_tokens")
                ):
                    self._mark_usage_invalid(
                        current, "Assistant transcript record has incomplete or invalid usage",
                    )
                    continue
                try:
                    identity = identifier(identity, "transcript message id")
                except (TypeError, ValueError):
                    self._mark_usage_invalid(
                        current, "Assistant transcript record has an invalid identity",
                    )
                    continue
                messages = current["messages"]
                if identity not in messages and len(messages) >= MAX_TRANSCRIPT_MESSAGES:
                    self._mark_usage_invalid(
                        current, "Transcript message bound exceeded; usage is unavailable",
                    )
                    continue
                previous = messages.setdefault(identity, {})
                if model:
                    previous["model"] = model
                for key in TOKEN_KEYS:
                    amount = usage.get(key)
                    if (isinstance(amount, int) and not isinstance(amount, bool)
                            and 0 <= amount <= MAX_USAGE_INTEGER):
                        previous[key] = max(previous.get(key, 0), amount)
                    elif key in usage:
                        self._mark_usage_invalid(
                            current, "Assistant transcript usage is invalid or exceeds the supported bound",
                        )
                cache = usage.get("cache_creation", {})
                if isinstance(cache, dict):
                    for source, destination in (("ephemeral_5m_input_tokens", "cache_write_5m"), ("ephemeral_1h_input_tokens", "cache_write_1h")):
                        amount = cache.get(source)
                        if (isinstance(amount, int) and not isinstance(amount, bool)
                                and 0 <= amount <= MAX_USAGE_INTEGER):
                            previous[destination] = max(previous.get(destination, 0), amount)
                        elif source in cache:
                            self._mark_usage_invalid(
                                current, "Assistant transcript cache usage is invalid or exceeds the supported bound",
                            )
                elif "cache_creation" in usage:
                    self._mark_usage_invalid(
                        current, "Assistant transcript cache usage has an invalid shape",
                    )
                for key in ("speed", "inference_geo"):
                    if isinstance(usage.get(key), str):
                        previous[key] = safe_text(usage[key], 240)
                content = message.get("content", [])
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    current["activity"] = safe_text(block.get("name"), 240)
                    inputs = block.get("input", {})
                    file = safe_text(inputs.get("file_path"), 2000) if isinstance(inputs, dict) else None
                    if file and file not in current["files_touched"]:
                        current["files_touched"].append(file)
                        current["files_touched"] = current["files_touched"][-64:]
            self._recompute_usage(current)
            self.put(db, current)
        return current

    @staticmethod
    def _file_id(info) -> list[int]:
        return [int(info.st_dev), int(info.st_ino)]

    @staticmethod
    def _prefix_hash(stream, offset: int):
        """Digest of the consumed prefix; the stream position is preserved."""
        digest = hashlib.sha256()
        position = stream.tell()
        stream.seek(0)
        remaining = offset
        while remaining:
            chunk = stream.read(min(remaining, 1_048_576))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        stream.seek(position)
        return digest

    @staticmethod
    def _refresh_is_current(current: dict, path: str, offset: int, revision: int) -> bool:
        return (current.get("transcript") == path
                and current.get("transcript_offset") == offset
                and current.get("transcript_revision", 0) == revision)

    @staticmethod
    def _mark_usage_invalid(current: dict, error: str) -> None:
        current.update(usage=None, usage_source="unavailable", usage_partial=True, usage_invalid=True)
        if not current.get("transcript_error_irreversible"):
            current["transcript_error"] = error
        current.update(transcript_error_irreversible=True, transcript_transient_error=False)
        if current.get("cost_source") != "host_reported_estimate":
            current.update(cost=None, cost_source="unavailable", cost_note=error)

    @staticmethod
    def _mark_usage_incomplete(current: dict, error: str) -> None:
        current["usage_partial"] = True
        if not current.get("transcript_error_irreversible"):
            current["transcript_error"] = error
        current.update(transcript_error_irreversible=True, transcript_transient_error=False)
        if current.get("cost_source") != "host_reported_estimate":
            current.update(cost=None, cost_source="unavailable", cost_note=error)

    @staticmethod
    def _clear_transient_error(current: dict) -> None:
        if not current.get("transcript_transient_error"):
            return
        current["transcript_transient_error"] = False
        if not current.get("transcript_error_irreversible"):
            current["transcript_error"] = None
        if not current.get("usage_invalid"):
            current["usage_partial"] = bool(
                current.get("model_history_partial")
                or current.get("transcript_error_irreversible")
            )

    def _recompute_usage(self, current: dict) -> None:
        if not current["messages"]:
            if current.get("usage_invalid"):
                current.update(usage=None, usage_source="unavailable")
            return
        totals = dict.fromkeys(TOKEN_KEYS, 0)
        invalid_totals = len(current["messages"]) > MAX_TRANSCRIPT_MESSAGES
        for message in current["messages"].values():
            if not isinstance(message, dict):
                invalid_totals = True
                continue
            for key in TOKEN_KEYS:
                amount = message.get(key, 0)
                if (not isinstance(amount, int) or isinstance(amount, bool)
                        or not 0 <= amount <= MAX_USAGE_INTEGER
                        or totals[key] > MAX_USAGE_INTEGER - amount):
                    invalid_totals = True
                    continue
                totals[key] += amount
        if invalid_totals or sum(totals.values()) > MAX_USAGE_INTEGER:
            self._mark_usage_invalid(
                current, "Aggregate transcript usage is invalid or exceeds the supported bound",
            )
        if current.get("usage_invalid"):
            current.update(usage=None, usage_source="unavailable")
            return
        current.update(usage=totals, usage_source="transcript")
        if current["cost_source"] != "host_reported_estimate":
            cost, reason = estimate(current["messages"], self.prices)
            overrides = sorted({message.get("model") for message in current["messages"].values()} & self.price_override_models)
            sources = {message.get("model"): ("operator-configured" if message.get("model") in overrides else
                       f"catalog@{PRICE_DATE}" if message.get("model") in DEFAULT_PRICES else "unavailable")
                       for message in current["messages"].values()}
            has_catalog = any(source.startswith("catalog@") for source in sources.values())
            current.update(cost=cost, cost_source="api_token_estimate" if cost is not None else "unavailable",
                           cost_note=reason or "API-equivalent token estimate; not billed spend; excludes server-tool fees",
                           price_date=PRICE_DATE if has_catalog else None,
                           price_source="mixed" if overrides and has_catalog else "operator-configured" if overrides else PRICE_SOURCE,
                           price_sources=sources, price_overrides_used=overrides)

    def _record_transcript_failure(self, task_id: str, path: str, offset: int,
                                   revision: int, error: str) -> dict:
        """Persist cursor-related uncertainty only if the inspected cursor is current."""
        with self.store.transaction() as db:
            current = self.get(task_id, db)
            if not self._refresh_is_current(current, path, offset, revision):
                return current
            if not current.get("transcript_error_irreversible"):
                current["transcript_error"] = error
            current.update(usage=None, usage_source="unavailable", usage_partial=True,
                           transcript_transient_error=True,
                           transcript_revision=revision + 1)
            if current.get("cost_source") != "host_reported_estimate" and not current.get("usage_invalid"):
                current.update(
                    cost=None,
                    cost_source="unavailable",
                    cost_note=error,
                )
            self.put(db, current)
            return current

    def _record_transcript_pending(self, task_id: str, path: str, offset: int,
                                   revision: int, pending: bool, file_id: list[int],
                                   info, boundary: str, reset_reason: str | None) -> dict:
        """Persist an incomplete trailing record without advancing its cursor."""
        with self.store.transaction() as db:
            current = self.get(task_id, db)
            if not self._refresh_is_current(current, path, offset, revision):
                return current
            if reset_reason is not None:
                current.update(transcript_offset=0, skip_line=False, messages={},
                               usage=None, usage_source="unavailable")
                self._mark_usage_invalid(current, reset_reason)
            current.update(transcript_pending=pending, transcript_eof=not pending,
                           transcript_file_id=file_id, transcript_file_size=info.st_size,
                           transcript_file_mtime_ns=info.st_mtime_ns,
                           transcript_boundary=boundary,
                           transcript_revision=revision + 1)
            self._clear_transient_error(current)
            self._recompute_usage(current)
            self.put(db, current)
            return current

    @staticmethod
    def view(record: dict, at: int | None = None) -> dict:
        for key in ("host_id", "source", "task_id", "agent_id", "parent_session_id", "parent_agent_id"):
            if record.get(key) is not None:
                identifier(record[key], key)
        result = {key: value for key, value in record.items() if key not in {
            "messages", "transcript_offset", "state_at", "run_started_at", "skip_line", "transcript_eof",
            "continuity_uncertain", "result_at", "result_segment_started_at",
            "result_segment_base_duration", "session_ended_at", "session_end_uncertain",
            "session_end_segment_started_at", "session_end_segment_base_duration",
            "session_end_prior_state_at", "usage_invalid", "model_history_partial",
            "transcript_file_id", "transcript_file_size", "transcript_file_mtime_ns",
            "transcript_boundary", "transcript_revision", "transcript_error_irreversible",
            "transcript_transient_error"}}
        result = redact_structure(result)
        models = result.get("models")
        if isinstance(models, list) and len(models) > MAX_MODELS:
            result.update(models=models[:MAX_MODELS], usage_partial=True)
        if result.get("cost") is not None and not _valid_cost(result["cost"]):
            result.update(
                cost=None,
                cost_source="unavailable",
                cost_note="Host-reported cost is invalid or exceeds the supported bound",
            )
        if (result.get("usage_partial") or record.get("transcript_pending")) and record["cost_source"] != "host_reported_estimate":
            result.update(cost=None, cost_source="unavailable", cost_note="Transcript usage incomplete or not yet fully read")
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
            return self.view(self.refresh(task_id))
        with self.store.connect() as db:
            identities = [row[0] for row in db.execute("""SELECT task_id FROM observations
                ORDER BY CASE json_extract(data,'$.status') WHEN 'running' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                json_extract(data,'$.last_event_at') DESC LIMIT 100 OFFSET ?""", (offset,))]
        rows = []
        for identity in identities:
            try:
                view = self.view(self.refresh(identity))
            except ValueError:
                # A legacy row that fails today's identity rules must not hide
                # every other agent.
                LOGGER.warning("Skipping native observation with an invalid identity")
                continue
            rows.append({key: value for key, value in view.items() if key not in {
                "files_touched", "summary", "transcript", "price_sources"}})
        return rows

    def snapshot(self, limit: int = 100) -> list[dict]:
        """Bounded status view without transcript I/O on the snapshot hot path."""
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
        if future:
            # Clock rollback: an exact active total cannot be established, even off-page.
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
            visible.append({key: value for key, value in view.items() if key not in {
                "files_touched", "summary", "transcript", "price_sources", "messages"}})
        return visible, active

    def active_count(self) -> int | None:
        return self.fleet_records(0)[1]
