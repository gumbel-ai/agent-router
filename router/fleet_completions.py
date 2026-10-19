"""Bounded completion delivery through configured transports; never launches work."""

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor

from router.client import CompletionBusy, CompletionRefused, request
from router.fleet import (
    MAX_HOSTS,
    FleetConfig,
    FleetHost,
    SSHQueryError,
    _config_text,
    ssh_stdout,
)
from router.managed import (
    MAX_WAIT_SECONDS,
    _completion_sequence,
    _identifier,
    _valid_identifier,
    wait,
)
from router.validation import is_wire_integer

MAX_COMPLETION_BYTES = 16 * 1024


def _selected(config: FleetConfig, only: str | None) -> list[FleetHost]:
    hosts = [host for host in config.hosts if only is None or host.name == only]
    if not 1 <= len(hosts) <= MAX_HOSTS:
        raise ValueError("Select one to 32 configured completion hosts")
    for host in hosts:
        _config_text(host.name, "fleet name", 120)
    return hosts


def _result(raw: bytes, consumer: str, ack: dict | None = None) -> dict:
    if len(raw) > MAX_COMPLETION_BYTES:
        raise ValueError("Completion response exceeds 16 KiB")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("consumer") != consumer:
        raise ValueError("Invalid completion consumer response")
    if ack is not None:
        event_id, host_id, subscription_id = (ack[key] for key in ("event_id", "host_id", "subscription_id"))
        if value.get("acknowledged") is not True or value.get("event_id") != event_id \
                or value.get("host_id") != host_id or value.get("subscription_id") != subscription_id:
            raise ValueError("Invalid completion acknowledgment response")
        return {"acknowledged": True, "consumer": consumer, "event_id": event_id, "host_id": host_id,
                "subscription_id": subscription_id}
    status = value.get("status")
    binding = {}
    if status != "unavailable" or "host_id" in value or "subscription_id" in value:
        binding = {"host_id": _identifier(value.get("host_id"), "host"),
                   "subscription_id": _identifier(value.get("subscription_id"), "subscription")}
    if status in {"deadline", "unavailable"}:
        return {"status": status, "consumer": consumer, **binding}
    if status not in {"completed", "failed", "uncertain"} or not isinstance(value.get("event"), dict):
        raise ValueError("Invalid completion outcome response")
    host_id = _identifier(value.get("host_id"), "host")
    source = value["event"]
    _completion_sequence(source.get("event_id"))
    event = {"event_id": source["event_id"], "host_id": _identifier(source.get("host_id"), "event host")}
    invalid = []
    for key in ("managed_id", "session_id", "agent_id", "attribution", "finished_at", "uncertain_at"):
        if key not in source:
            continue
        item = source[key]
        valid = (item is None or
                 key in {"managed_id", "session_id", "agent_id"} and _valid_identifier(item) or
                 key == "attribution" and isinstance(item, str) and item in {"host", "session", "exact"} or
                 key in {"finished_at", "uncertain_at"} and is_wire_integer(item))
        if valid:
            event[key] = item
        else:
            invalid.append(key)
    for key in ("status", "exit_code", "signal", "group_outlived"):
        if key not in source:
            continue
        item = source[key]
        if item is not None:
            if key == "status":
                if not isinstance(item, str) or item not in {"exited", "signaled", "failed", "unknown"}:
                    raise ValueError("Invalid completion process status")
            elif key == "group_outlived":
                if not isinstance(item, bool):
                    raise ValueError("Invalid completion group state")
            elif not is_wire_integer(item, minimum=1 if key == "signal" else 0):
                raise ValueError("Invalid completion numeric evidence")
        event[key] = item
    process_status, exit_code, signum = event.get("status"), event.get("exit_code"), event.get("signal")
    if not (process_status == "exited" and exit_code == 0 and signum is None or
            process_status == "failed" and is_wire_integer(exit_code, minimum=1) and signum is None or
            process_status == "signaled" and is_wire_integer(signum, minimum=1) and exit_code is None or
            process_status == "unknown" and exit_code is None and signum is None):
        raise ValueError("Completion process status contradicts exit evidence")
    expected = ("failed" if event.get("status") in {"failed", "signaled"} else
                "completed" if event.get("status") == "exited" and event.get("group_outlived") is False else "uncertain")
    if event.get("status") is None or status != expected:
        raise ValueError("Completion outcome contradicts process evidence")
    return {"status": status, "consumer": consumer, "host_id": host_id, "event": event,
            "subscription_id": binding["subscription_id"], "invalid_metadata": invalid}


def _query(host, consumer, timeout, arguments, local, *, cancelled=None, ack=None, connect_timeout=None):
    wrapper = {"name": host.name, "host_id": None, "ok": False}
    try:
        if host.transport == "local":
            raw = json.dumps(local(), separators=(",", ":")).encode()
        else:
            raw = ssh_stdout(host, timeout, arguments, accepted_exit_codes=(0, 1) if ack is None else (0,),
                             connect_timeout=connect_timeout, cancelled=cancelled)
    except SSHQueryError as error:
        wrapper["error"] = {"code": error.code}
        for key in ("exit_code", "errno"):
            if getattr(error, key) is not None:
                wrapper["error"][key] = getattr(error, key)
        if ack is not None and error.code == "exit_failed" and error.exit_code == 1 and error.stdout is not None:
            try:
                if len(error.stdout) > MAX_COMPLETION_BYTES:
                    raise ValueError("Completion refusal response exceeds 16 KiB")
                refusal = json.loads(error.stdout)
                if not isinstance(refusal, dict) or set(refusal) != {"schema_version", "error", *ack} \
                        or type(refusal.get("schema_version")) is not int or refusal["schema_version"] != 1 \
                        or any(refusal.get(key) != value for key, value in ack.items()) \
                        or not isinstance(refusal.get("error"), dict) or set(refusal["error"]) != {"code"} \
                        or refusal["error"]["code"] not in ("database_busy", "acknowledgment_refused"):
                    raise ValueError("Invalid completion refusal response")
            except (ValueError, TypeError, KeyError, RecursionError):
                pass  # The original failed transport remains authoritative.
            else:
                wrapper["error"]["code"] = refusal["error"]["code"]
    except CompletionBusy:
        wrapper["error"] = {"code": "database_busy"}
    except CompletionRefused:
        wrapper["error"] = {"code": "acknowledgment_refused"}
    except TimeoutError:
        wrapper["error"] = {"code": "transport_deadline"}
    except OSError as error:
        wrapper["error"] = {"code": "transport_failed", "errno": error.errno}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        wrapper["error"] = {"code": "unavailable" if isinstance(error.__cause__, OSError) else "request_failed"}
    else:
        try:
            result = _result(raw, consumer, ack)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            wrapper["error"] = {"code": "invalid_response"}
        else:
            wrapper["host_id"] = result.get("host_id")
            if result.get("status") == "unavailable":
                wrapper["error"] = {"code": "unavailable"}
            else:
                invalid = result.pop("invalid_metadata", [])
                wrapper.update(ok=True, result=result)
                if invalid:
                    wrapper["warning"] = {"code": "invalid_metadata", "fields": invalid}
    return wrapper


def query_completions(config: FleetConfig, consumer: str, timeout: float, *, only: str | None = None) -> dict:
    _identifier(consumer, "consumer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
            or not 0 < timeout <= MAX_WAIT_SECONDS:
        raise ValueError("timeout must be finite and greater than zero, at most 3600 seconds")
    hosts = _selected(config, only)
    cancelled = threading.Event()
    arguments = ["wait", f"--consumer={consumer}", f"--timeout={timeout}", "--include-subscription"]
    def query(host):
        return _query(host, consumer, timeout + config.ssh_timeout_seconds, arguments,
                      lambda: wait(consumer, timeout, cancelled=cancelled, include_subscription=True), cancelled=cancelled,
                      connect_timeout=config.ssh_timeout_seconds)
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        try:
            results = list(executor.map(query, hosts))
        except KeyboardInterrupt:
            cancelled.set()
            raise
    identities = [host["host_id"] for host in results if host["host_id"] is not None]
    for host in results:
        if host["host_id"] is not None and identities.count(host["host_id"]) > 1:
            host.update(ok=False, error={"code": "duplicate_host_id"})
            host.pop("result", None)
    return {"schema_version": 1, "consumer": consumer, "hosts": results,
            "partial": any(not host["ok"] or "warning" in host for host in results)}


def acknowledge_completion(config: FleetConfig, only: str, consumer: str, event_id: str,
                           host_id: str, event_host_id: str, subscription_id: str) -> dict:
    _identifier(consumer, "consumer")
    _completion_sequence(event_id)
    _identifier(host_id, "host")
    _identifier(event_host_id, "event host")
    _identifier(subscription_id, "subscription")
    if only is None:
        raise ValueError("Acknowledgment needs one configured host name")
    host = _selected(config, only)[0]
    payload = {"consumer": consumer, "event_id": event_id, "host_id": host_id,
               "event_host_id": event_host_id, "subscription_id": subscription_id}
    arguments = ["ack", f"--consumer={consumer}", f"--host-id={host_id}", f"--event-host-id={event_host_id}",
                 f"--subscription-id={subscription_id}", "--json-errors", event_id]
    result = _query(host, consumer, config.ssh_timeout_seconds, arguments,
                    lambda: request("completion_ack", payload, timeout=config.ssh_timeout_seconds, deadline=True),
                    ack=payload)
    # Any failed reply may follow a committed acknowledgment. Retrying this exact tuple is safe.
    if not result["ok"]:
        result["error"]["acknowledgment"] = "unknown"
    return result
