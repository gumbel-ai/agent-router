"""Read-only native-agent observations, separate from Router-owned tasks."""

import json
import math
import uuid
from pathlib import Path

from router.db import Store, now_ms
from router.dispatch import canonical
from router.pricing import DEFAULT_PRICES, PRICE_DATE, PRICE_SOURCE, estimate
from router.redaction import redact_secrets

STALE_AFTER_MS = 60_000
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def text(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join("".join(character for character in value if character.isprintable() or character.isspace()).split())[:limit] or None


class NativeObserver:
    def __init__(self, store: Store, prices=None, host_id: str | None = None):
        self.store = store
        self.host_id = host_id or "unknown"
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        self.price_override_models = set(prices or {})

    def reconcile_restart(self) -> None:
        # A daemon restart is not evidence that host-owned execution continued.
        with self.store.transaction() as db:
            for row in db.execute("""SELECT data FROM observations
                    WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')='claude'""",
                    (self.host_id,)).fetchall():
                record = json.loads(row[0])
                if record["status"] in {"running", "idle", "needs_input"}:
                    record.update(status="unknown", visibility_reason="Router restarted; awaiting native lifecycle evidence")
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
        source, session, kind = (event.get(key) for key in ("source", "session_id", "kind"))
        at = event.get("observed_at")
        if source != "claude" or not text(session) or kind not in {
                "start", "launch", "result", "activity", "stop", "session_end", "idle", "needs_input", "failed"}:
            raise ValueError("Unsupported native observation")
        if not isinstance(at, int) or isinstance(at, bool) or at < 0:
            raise ValueError("Native observation requires a timestamp")
        with self.store.transaction(timeout) as db:
            if kind == "session_end":
                for row in db.execute("""SELECT data FROM observations WHERE session_id=?
                        AND json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')=?""",
                        (session, self.host_id, source)).fetchall():
                    record = json.loads(row[0])
                    if record["status"] in {"running", "idle", "needs_input"} and at >= record["state_at"]:
                        record.update(status="unknown", continuity_uncertain=True,
                                      visibility_reason="Host session ended without observed agent stop",
                                      state_at=at, last_event_at=max(record["last_event_at"] or 0, at))
                        self.put(db, record)
                return {"observed": True}
            agent = event.get("agent_id")
            if not text(agent):
                raise ValueError("Native observation requires agent_id")
            task_id = "native-" + uuid.uuid5(uuid.NAMESPACE_URL, canonical([source, session, agent])).hex
            row = db.execute("""SELECT data FROM observations
                    WHERE json_extract(data,'$.host_id')=? AND json_extract(data,'$.source')=?
                      AND session_id=? AND json_extract(data,'$.agent_id')=?""",
                    (self.host_id, source, session, agent)).fetchone()
            record = json.loads(row[0]) if row else {
                "host_id": self.host_id, "source": source,
                "task_id": task_id, "agent_id": agent, "executor": source, "owner": source, "control_mode": "observe",
                "parent_session_id": session, "parent_agent_id": None, "task": None, "agent_type": None,
                "requested_model": None, "resolved_model": None, "models": [], "model_source": None,
                "status": "unknown", "last_observed_status": None, "state_at": -1, "started_at": None,
                "run_started_at": None, "finished_at": None, "duration_ms": None, "last_event_at": None,
                "continuity_uncertain": False, "runtime_partial": False,
                "cwd": None, "worktree": None, "activity": None, "files_touched": [], "summary": None,
                "cost": None, "cost_source": "unavailable", "usage": None, "usage_source": "unavailable",
                "cost_note": "Token usage unavailable", "price_date": None, "price_source": None, "price_overrides_used": [],
                "price_sources": {}, "usage_partial": False, "transcript_pending": False, "transcript_eof": False,
                "skip_line": False, "transcript": None, "transcript_offset": 0, "messages": {}, "visibility_reason": None}
            terminal_metadata_only = record["status"] in {"stopped", "failed"} and kind == "result"
            if record["status"] in {"stopped", "failed"} and kind not in {"start", "result"}:
                return self.view(record)
            for key in ("parent_agent_id", "task", "agent_type", "requested_model", "cwd", "activity", "summary"):
                value = text(event.get(key), 800 if key == "summary" else 240)
                if value is not None:
                    if key == "task":
                        value = redact_secrets(value, 240)
                    record[key] = value
            for key in ("transcript", "worktree"):
                if isinstance(event.get(key), str):
                    record[key] = event[key]
            file = text(event.get("file_path"), 2000)
            if file and file not in record["files_touched"]:
                record["files_touched"].append(file)
                record["files_touched"] = record["files_touched"][-64:]
            model = text(event.get("resolved_model"))
            if model:
                self.model(record, model, "host_result")
            cost = event.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
                record.update(cost=cost, cost_source="host_reported_estimate", cost_note="Host-reported estimate; not billed spend",
                              price_date=None, price_source=None, price_sources={}, price_overrides_used=[])
            if kind == "stop":
                # The final API response may have arrived after the last poll.
                record["transcript_eof"] = False
            if (not terminal_metadata_only and at >= record["state_at"]
                    and not (kind == "launch" and record["last_observed_status"] == "stopped")):
                if kind == "result":
                    if record["status"] != "stopped":
                        record.update(status="unknown", continuity_uncertain=True, visibility_reason="Host returned a result without observed stop")
                elif kind in {"start", "launch", "activity"}:
                    if record["last_observed_status"] != "running" or record.get("continuity_uncertain"):
                        if record.get("continuity_uncertain") and record["run_started_at"] is not None:
                            record["runtime_partial"] = True
                        record["run_started_at"] = at
                    record.update(status="running", last_observed_status="running", finished_at=None, continuity_uncertain=False, visibility_reason=None)
                    record["transcript_eof"] = False
                    if record["started_at"] is None:
                        record["started_at"] = at
                elif kind == "stop":
                    if record["status"] != "stopped" and record["run_started_at"] is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="stopped", last_observed_status="stopped", finished_at=at, run_started_at=None,
                                  continuity_uncertain=False, visibility_reason=None)
                elif kind == "idle":
                    if record.get("run_started_at") is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="idle", last_observed_status="idle", run_started_at=None, visibility_reason=None)
                elif kind == "needs_input":
                    if record.get("run_started_at") is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="needs_input", last_observed_status="needs_input", run_started_at=None,
                                  visibility_reason="Claude reported that the agent needs input")
                else:
                    if record.get("run_started_at") is not None:
                        record["duration_ms"] = (record["duration_ms"] or 0) + max(0, at - record["run_started_at"])
                    record.update(status="failed", last_observed_status="failed", finished_at=at, run_started_at=None,
                                  visibility_reason=text(event.get("failure_reason"), 500) or "Agent turn failed")
                if kind in {"start", "activity", "stop", "idle", "needs_input", "failed"}:
                    record["state_at"] = at
            record["last_event_at"] = max(record["last_event_at"] or 0, at)
            self.put(db, record)
        return self.view(record)

    @staticmethod
    def model(record: dict, model: str, source: str) -> None:
        record.update(resolved_model=model, model_source=source)
        if model not in record["models"]:
            record["models"].append(model)

    def refresh(self, task_id: str) -> dict:
        record = self.get(task_id)
        path = record.get("transcript")
        if not path or (record["status"] == "stopped" and record.get("transcript_eof")):
            return record
        # File I/O stays outside SQLite's write transaction. A cursor check below
        # prevents concurrent status readers from counting the same batch twice.
        offset = record["transcript_offset"]
        try:
            with Path(path).open("rb") as stream:
                size = stream.seek(0, 2)
                if size < offset:
                    record["transcript_error"] = "Transcript truncated; prior usage retained"
                    record["usage_partial"] = True
                    return record
                stream.seek(offset)
                batch = stream.read(1_048_576)
        except OSError:
            record["transcript_error"] = "Transcript unavailable"
            return record
        end = batch.rfind(b"\n") + 1
        if not end:
            if len(batch) < 1_048_576:
                record["transcript_pending"] = bool(batch)
                return record
            # Bound memory and advance past an oversized line. Its usage cannot
            # be trusted, so retain known tokens but never claim a complete cost.
            end = len(batch)
            skipped = True
            lines = []
        else:
            skipped = record.get("skip_line", False)
            start = batch.find(b"\n") + 1 if skipped else 0
            lines = batch[start:end].splitlines()
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        with self.store.transaction() as db:
            current = self.get(task_id, db)
            if current["transcript_offset"] != offset or current["transcript"] != path:
                return current
            current["transcript_offset"] += end
            current.update(skip_line=skipped and not lines and b"\n" not in batch,
                           transcript_pending=offset + end < size, transcript_eof=offset + end == size)
            if skipped:
                current["usage_partial"] = True
            for event in events:
                environment = event.get("attachment", {})
                if isinstance(environment, dict) and environment.get("type") == "environment":
                    snapshot = environment.get("snapshot", {})
                    if isinstance(snapshot, dict) and isinstance(snapshot.get("workingDirectory"), str):
                        current["cwd"] = snapshot["workingDirectory"]
                        if snapshot.get("isWorktree") is True:
                            current["worktree"] = snapshot["workingDirectory"]
                if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
                    continue
                message = event["message"]
                model = text(message.get("model"))
                if model == "<synthetic>":
                    continue
                if model:
                    self.model(current, model, "transcript")
                identity, usage = message.get("id"), message.get("usage")
                if isinstance(identity, str) and isinstance(usage, dict):
                    previous = current["messages"].setdefault(identity, {})
                    if model:
                        previous["model"] = model
                    for key in TOKEN_KEYS:
                        amount = usage.get(key)
                        if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                            previous[key] = max(previous.get(key, 0), amount)
                    cache = usage.get("cache_creation", {})
                    if isinstance(cache, dict):
                        for source, destination in (("ephemeral_5m_input_tokens", "cache_write_5m"), ("ephemeral_1h_input_tokens", "cache_write_1h")):
                            amount = cache.get(source)
                            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                                previous[destination] = max(previous.get(destination, 0), amount)
                    for key in ("speed", "inference_geo"):
                        if isinstance(usage.get(key), str):
                            previous[key] = usage[key]
                content = message.get("content", [])
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    current["activity"] = text(block.get("name"))
                    inputs = block.get("input", {})
                    file = text(inputs.get("file_path"), 2000) if isinstance(inputs, dict) else None
                    if file and file not in current["files_touched"]:
                        current["files_touched"].append(file)
                        current["files_touched"] = current["files_touched"][-64:]
            if current["messages"]:
                current["usage"] = {key: sum(message.get(key, 0) for message in current["messages"].values()) for key in TOKEN_KEYS}
                current["usage_source"] = "transcript"
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
            self.put(db, current)
        return current

    @staticmethod
    def view(record: dict) -> dict:
        result = {key: value for key, value in record.items() if key not in {
            "messages", "transcript_offset", "state_at", "run_started_at", "skip_line", "transcript_eof", "continuity_uncertain"}}
        if isinstance(result.get("task"), str):
            result["task"] = redact_secrets(result["task"], 240)
        if (record.get("usage_partial") or record.get("transcript_pending")) and record["cost_source"] != "host_reported_estimate":
            result.update(cost=None, cost_source="unavailable", cost_note="Transcript usage incomplete or not yet fully read")
        now = now_ms()
        age = max(0, now - record["last_event_at"]) if record["last_event_at"] is not None else None
        result["observation_age_ms"] = age
        result["elapsed_ms"] = None
        if record["status"] in {"running", "idle", "needs_input"} and age is not None and age > STALE_AFTER_MS:
            result.update(status="unknown", visibility_reason="No recent native lifecycle evidence; last observed nonterminal state")
        elif record["status"] == "running" and record["run_started_at"] is not None:
            result["elapsed_ms"] = (record["duration_ms"] or 0) + max(0, now - record["run_started_at"])
        elif record["status"] == "stopped":
            result["elapsed_ms"] = record["duration_ms"]
        return result

    def status(self, task_id: str | None = None, offset: int = 0) -> dict | list:
        if task_id:
            return self.view(self.refresh(task_id))
        with self.store.connect() as db:
            identities = [row[0] for row in db.execute("""SELECT task_id FROM observations
                ORDER BY CASE json_extract(data,'$.status') WHEN 'running' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                json_extract(data,'$.last_event_at') DESC LIMIT 100 OFFSET ?""", (offset,))]
        return [{key: value for key, value in self.view(self.refresh(identity)).items() if key not in {
            "files_touched", "summary", "transcript", "price_sources"}} for identity in identities]

    def snapshot(self, limit: int = 100) -> list[dict]:
        """Bounded status view without transcript I/O on the snapshot hot path."""
        cutoff = now_ms() - 300_000
        with self.store.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("""SELECT data FROM observations
                WHERE json_extract(data,'$.host_id')=? AND
                    (json_extract(data,'$.status') IN ('running','idle','needs_input') OR
                     json_extract(data,'$.last_event_at')>=?)
                ORDER BY CASE json_extract(data,'$.status')
                    WHEN 'running' THEN 0 WHEN 'needs_input' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END,
                    json_extract(data,'$.last_event_at') DESC LIMIT ?""", (self.host_id, cutoff, limit))]
        return [{key: value for key, value in self.view(record).items() if key not in {
            "files_touched", "summary", "transcript", "price_sources", "messages"}} for record in records]
