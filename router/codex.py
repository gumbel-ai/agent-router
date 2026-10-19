"""Translate Codex exec JSONL into durable, explicitly partial Router telemetry."""

import uuid

from router.pricing import CODEX_PRICE_SOURCE, CODEX_PRICES, PRICE_DATE


def price_metadata(model: str, overrides: dict) -> dict:
    tiers = overrides.get(model)
    if tiers:
        # Aggregate turn usage cannot select a per-request context tier safely.
        rates = (tiers[0].input, tiers[0].cache_read, tiers[0].output, tiers[0].cache_write) if len(tiers) == 1 else None
        return {"token_prices": rates, "price_source": "operator-configured", "price_date": None,
                "price_overrides_used": [model]}
    return {"token_prices": CODEX_PRICES.get(model), "price_source": CODEX_PRICE_SOURCE if model in CODEX_PRICES else None,
            "price_date": PRICE_DATE if model in CODEX_PRICES else None, "price_overrides_used": []}


def collect(task: dict, event: dict) -> None:
    kind = event.get("type")
    if kind == "thread.started":
        try:
            session = str(uuid.UUID(event.get("thread_id", "")))
        except (ValueError, AttributeError, TypeError):
            task["execution_error"] = True
            return
        if task.get("child_session_id") not in {None, session}:
            task["execution_error"] = True
        else:
            task["child_session_id"] = session
            task["thread_observed"] = True
            tracing = task.get("tracing")
            if isinstance(tracing, dict) and isinstance(tracing.get("correlation"), dict):
                # Codex assigns the native thread ID after launch. Reconcile it
                # into the Router/Langfuse join tuple without changing the owned
                # session or accepting a replacement thread.
                tracing["correlation"]["session_id"] = session
    elif kind in {"error", "turn.failed"}:
        task["execution_error"] = True
    elif kind in {"item.started", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict):
            return
        if isinstance(item.get("type"), str):
            task["executor_activity"] = item["type"][:80]
        if kind == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            task["summary"] = "\n".join(filter(None, (task.get("summary"), item["text"])))[-8000:]
    elif kind == "turn.completed":
        task["executor_completed"] = True
        raw = event.get("usage")
        raw = raw if isinstance(raw, dict) else {}
        totals = {key: value for key, value in raw.items() if key.endswith("_tokens") and type(value) is int and value >= 0}
        baseline = task.get("codex_usage_baseline", {})
        complete = all(key in totals for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
        complete = complete and totals["cached_input_tokens"] + totals.get("cache_write_input_tokens", 0) <= totals["input_tokens"]
        resuming = task.get("attempt_number", 1) > 1
        deltas = {key: value - baseline.get(key, 0) for key, value in totals.items() if not resuming or key in baseline}
        complete = complete and all(key in deltas for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
        complete = complete and all(value >= 0 for value in deltas.values()) and task.get("codex_baseline_complete", True)
        if complete:
            complete = deltas["cached_input_tokens"] + deltas.get("cache_write_input_tokens", 0) <= deltas["input_tokens"]
        if resuming and any(key not in baseline for key in totals):
            complete = False
        task["usage_complete"] = complete
        task["codex_usage_total"] = totals
        task["usage"] = {key: value for key, value in deltas.items() if value >= 0}
        usage = task["usage"]
        prices = task.get("token_prices")
        if not task["usage_complete"] or not prices:
            task.pop("cost", None)
            task["cost_note"] = "Codex input/cache/output usage incomplete" if not task["usage_complete"] else "Exact flat model price unavailable; aggregate usage cannot select context tiers"
        else:
            # Cached input is included in input_tokens. Reasoning is part of output,
            # not an additional charge. Cache writes replace the ordinary input rate.
            write = usage.get("cache_write_input_tokens")
            write_rate = prices[3]
            if write is None and write_rate == prices[0]:
                write = 0  # The input rate already covers all non-cached input.
            if write is None or write_rate is None and write:
                task.pop("cost", None)
                task["cost_note"] = "Cache-write usage or exact rate unavailable"
                return
            uncached = usage["input_tokens"] - usage["cached_input_tokens"] - write
            task["cost"] = round((uncached * prices[0] + usage["cached_input_tokens"] * prices[1] +
                                  usage["output_tokens"] * prices[2] + write * (write_rate or 0)) / 1_000_000, 10)
            task["cost_source"] = "token_estimate"
            task["cost_note"] = "Standard short-context input/cache-read/cache-write/output token estimate using configured model; excludes tool fees and long-context premiums; not billed spend"
