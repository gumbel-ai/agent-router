"""Low-overhead local host capacity sampling and explicit classification."""

import ctypes
import json
import math
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
MAX_RESERVATION_BYTES = 16 * 1024
RESERVATION_FIELDS = (
    "reserved_by", "reserved_until", "reserved_cpu_slots",
    "reserved_memory_bytes", "reservation_source",
)


@dataclass(frozen=True)
class CapacityThresholds:
    minimum_available_memory_gib: float = 4
    maximum_cpu_busy_for_available_pct: float = 65
    maximum_load_per_logical_cpu_for_available: float = 0.75
    minimum_disk_available_gib: float = 10
    maximum_cpu_busy_for_saturated_pct: float = 90
    maximum_load_per_logical_cpu_for_saturated: float = 1.25
    minimum_available_memory_gib_for_saturated: float = 1
    minimum_disk_available_gib_for_saturated: float = 2


def _linux_memory() -> tuple[int, int, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        fields = value.split()
        if fields:
            values[key] = int(fields[0]) * 1024
    return values["MemTotal"], values["MemAvailable"], values.get("SwapTotal", 0) - values.get("SwapFree", 0)


class _VMStatistics64(ctypes.Structure):
    _fields_ = [
        ("free_count", ctypes.c_uint32), ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32), ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64), ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64), ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64), ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64), ("hits", ctypes.c_uint64), ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32), ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64), ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64), ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32), ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32), ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class _SwapUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("available", ctypes.c_uint64),
                ("used", ctypes.c_uint64), ("page_size", ctypes.c_uint32), ("encrypted", ctypes.c_uint32)]


def _darwin_library():
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib")


def _darwin_memory() -> tuple[int, int, int]:
    library = _darwin_library()
    host = library.mach_host_self()
    page_size = ctypes.c_uint32()
    if library.host_page_size(host, ctypes.byref(page_size)) != 0:
        raise OSError("host_page_size failed")
    statistics = _VMStatistics64()
    count = ctypes.c_uint32(ctypes.sizeof(statistics) // ctypes.sizeof(ctypes.c_uint32))
    if library.host_statistics64(host, 4, ctypes.byref(statistics), ctypes.byref(count)) != 0:
        raise OSError("host_statistics64 failed")
    total = os.sysconf("SC_PHYS_PAGES") * page_size.value
    available = (statistics.free_count + statistics.inactive_count + statistics.speculative_count) * page_size.value
    swap = _SwapUsage()
    size = ctypes.c_size_t(ctypes.sizeof(swap))
    if library.sysctlbyname(b"vm.swapusage", ctypes.byref(swap), ctypes.byref(size), None, 0) != 0:
        raise OSError("sysctl vm.swapusage failed")
    return total, available, swap.used


def _linux_cpu_ticks() -> tuple[int, int]:
    fields = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields[:8]), idle


def _darwin_cpu_ticks() -> tuple[int, int]:
    library = _darwin_library()
    values = (ctypes.c_uint32 * 4)()
    count = ctypes.c_uint32(4)
    if library.host_statistics(library.mach_host_self(), 3, ctypes.byref(values), ctypes.byref(count)) != 0:
        raise OSError("host_statistics failed")
    return sum(values), values[2]


def _raw_cpu_ticks() -> tuple[int, int]:
    if sys.platform.startswith("linux"):
        return _linux_cpu_ticks()
    if sys.platform == "darwin":
        return _darwin_cpu_ticks()
    raise OSError(f"CPU sampling unsupported on {sys.platform}")


def _raw_memory() -> tuple[int, int, int]:
    if sys.platform.startswith("linux"):
        return _linux_memory()
    if sys.platform == "darwin":
        return _darwin_memory()
    raise OSError(f"Memory sampling unsupported on {sys.platform}")


def classify(sample: dict, thresholds: CapacityThresholds, stale_after_ms: float = 15_000) -> tuple[str, list[str]]:
    if sample.get("partial") is True:
        reasons = sample.get("capacity_reasons")
        if (isinstance(reasons, list) and reasons
                and all(isinstance(reason, str) for reason in reasons)):
            return "unknown", reasons
        return "unknown", ["capacity sample is partial"]
    sample_age = sample.get("sample_age_ms")
    if (not isinstance(sample_age, (int, float)) or isinstance(sample_age, bool)
            or not math.isfinite(sample_age) or sample_age < 0):
        return "unknown", ["capacity sample age is unavailable"]
    if sample_age > stale_after_ms:
        return "unknown", [f"capacity sample is stale by {sample['sample_age_ms']:.0f} ms"]
    required = ("logical_cpus", "load_1m", "cpu_busy_pct", "memory_available_bytes", "disk_available_bytes")
    missing = [key for key in required if sample.get(key) is None]
    if missing:
        return "unknown", ["capacity data unavailable: " + ", ".join(missing)]
    reservation_values = [sample.get(field) for field in RESERVATION_FIELDS]
    reservation_active = sample.get("reservation_active")
    if any(value is not None for value in reservation_values):
        if not isinstance(reservation_active, bool) or any(value is None for value in reservation_values):
            return "unknown", ["reservation data is incomplete"]
    elif reservation_active not in (None, False):
        return "unknown", ["reservation data is incomplete"]
    cpus = sample["logical_cpus"]
    load_ratio = sample["load_1m"] / cpus
    reserved_memory = sample.get("reserved_memory_bytes", 0) if sample.get("reservation_active") else 0
    unreserved_memory = max(0, sample["memory_available_bytes"] - reserved_memory)
    memory_gib = unreserved_memory / GIB
    disk_gib = sample["disk_available_bytes"] / GIB
    available_reasons = []
    if sample["cpu_busy_pct"] > thresholds.maximum_cpu_busy_for_available_pct:
        available_reasons.append(f"cpu_busy_pct {sample['cpu_busy_pct']:.1f} exceeds {thresholds.maximum_cpu_busy_for_available_pct:.1f}")
    if load_ratio > thresholds.maximum_load_per_logical_cpu_for_available:
        available_reasons.append(f"load per logical CPU {load_ratio:.2f} exceeds {thresholds.maximum_load_per_logical_cpu_for_available:.2f}")
    if memory_gib < thresholds.minimum_available_memory_gib:
        available_reasons.append(f"available memory {memory_gib:.1f} GiB is below {thresholds.minimum_available_memory_gib:.1f} GiB")
    if disk_gib < thresholds.minimum_disk_available_gib:
        available_reasons.append(f"available disk {disk_gib:.1f} GiB is below {thresholds.minimum_disk_available_gib:.1f} GiB")
    if sample.get("reservation_active"):
        available_reasons.append(
            f"reserved by {sample['reserved_by']} until {sample['reserved_until']}"
        )
    reserved_slots = sample.get("reserved_cpu_slots", 0) if sample.get("reservation_active") else 0
    saturated = (
        sample["cpu_busy_pct"] > thresholds.maximum_cpu_busy_for_saturated_pct
        or load_ratio > thresholds.maximum_load_per_logical_cpu_for_saturated
        or memory_gib < thresholds.minimum_available_memory_gib_for_saturated
        or disk_gib < thresholds.minimum_disk_available_gib_for_saturated
        or reserved_slots >= cpus
    )
    if saturated:
        return "saturated", available_reasons
    if available_reasons:
        return "busy", available_reasons
    return "available", []


class ReservationReader:
    """Read the external coordinator's single-host reservation record."""

    def __init__(self, path: Path):
        self.path = path

    def read(self, at: int) -> dict:
        empty = {field: None for field in RESERVATION_FIELDS}
        empty["reservation_active"] = False
        try:
            with self.path.open("rb") as stream:
                payload = stream.read(MAX_RESERVATION_BYTES + 1)
        except FileNotFoundError:
            return empty
        if len(payload) > MAX_RESERVATION_BYTES:
            raise ValueError("reservation.json exceeds 16 KiB")
        try:
            record = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise ValueError("reservation.json is not valid JSON") from error
        allowed = {"schema_version", *RESERVATION_FIELDS}
        if not isinstance(record, dict) or set(record) != allowed or record.get("schema_version") != 1:
            raise ValueError("reservation.json must use schema_version 1 and the documented fields")
        for field in ("reserved_by", "reservation_source"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 240:
                raise ValueError(f"reservation.json {field} must be a non-empty string up to 240 characters")
            record[field] = value.strip()
        for field in ("reserved_until", "reserved_cpu_slots", "reserved_memory_bytes"):
            value = record.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"reservation.json {field} must be a nonnegative integer")
        return {field: record[field] for field in RESERVATION_FIELDS} | {
            "reservation_active": record["reserved_until"] > at,
        }


class CapacitySampler:
    def __init__(self, disk_path: Path, interval_seconds: float = 5,
                 reservation_path: Path | None = None):
        self.disk_path = disk_path
        self.interval_seconds = interval_seconds
        self.reservations = ReservationReader(reservation_path or disk_path / "reservation.json")
        self._lock = threading.Lock()
        self._previous_cpu: tuple[int, int] | None = None
        self._latest: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sample()

    def sample(self) -> dict:
        observed_at = int(time.time() * 1000)
        try:
            total_ticks, idle_ticks = _raw_cpu_ticks()
            previous = self._previous_cpu
            delta_total = total_ticks - previous[0] if previous else 0
            delta_idle = idle_ticks - previous[1] if previous else 0
            cpu_busy = 100 * (1 - delta_idle / delta_total) if previous and delta_total > 0 else None
            self._previous_cpu = total_ticks, idle_ticks
            memory_total, memory_available, swap_used = _raw_memory()
            disk_available = shutil.disk_usage(self.disk_path).free
            load = os.getloadavg()
            result = {
                "logical_cpus": os.cpu_count(), "load_1m": load[0], "load_5m": load[1],
                "cpu_busy_pct": round(cpu_busy, 1) if cpu_busy is not None else None,
                "memory_total_bytes": memory_total, "memory_available_bytes": memory_available,
                "swap_used_bytes": swap_used, "disk_available_bytes": disk_available,
                "observed_at": observed_at, "partial": False,
            }
        except (OSError, ValueError, KeyError, ZeroDivisionError) as error:
            result = {"observed_at": observed_at, "partial": True, "error": str(error)}
        with self._lock:
            self._latest = result
        return result

    def view(self, thresholds: CapacityThresholds, **counts: int) -> dict:
        with self._lock:
            sample = dict(self._latest)
        at = int(time.time() * 1000)
        sample["sample_age_ms"] = max(0, at - sample.get("observed_at", 0))
        sample.update(counts)
        try:
            sample.update(self.reservations.read(at))
        except (OSError, ValueError) as error:
            sample.update({field: None for field in RESERVATION_FIELDS})
            sample.update(reservation_active=False, partial=True, error=str(error),
                          capacity_state="unknown",
                          capacity_reasons=["reservation data unavailable"])
            return sample
        state, reasons = classify(sample, thresholds, self.interval_seconds * 3000)
        sample.update(capacity_state=state, capacity_reasons=reasons)
        return sample

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="router-capacity", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
