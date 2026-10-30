"""Adapter for AndroidBuild Core's shared physical-device lease files.

Core and Control Center use ``DeviceTests/locks/<serial>.json`` to prevent a
physical validation, console operation, REST action, or MCP tool call from
using the same Android device concurrently. This module implements that
on-disk contract so Android Agent can lease device work consistently.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


_DEFAULT_ROOT = Path(r"V:\AndroidBuild")
_LEASE_TTL_SECONDS = 900
_HEARTBEAT_SECONDS = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _lease_path(root: Path, serial: str) -> Path:
    safe_serial = re.sub(r"[^A-Za-z0-9_.-]", "_", serial)
    if not safe_serial:
        raise ValueError("DEVICE_LEASE=FAIL empty_serial")
    return root / "DeviceTests" / "locks" / f"{safe_serial}.json"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _is_stale(lease: dict) -> bool:
    try:
        heartbeat = datetime.fromisoformat(str(lease["heartbeat_utc"]).replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        ttl = max(1, int(lease["ttl_seconds"]))
        return (_utc_now() - heartbeat.astimezone(timezone.utc)).total_seconds() > ttl
    except (KeyError, TypeError, ValueError):
        return True


def _write_exclusive(path: Path, lease: dict) -> bool:
    payload = json.dumps(lease, ensure_ascii=False, separators=(",", ":"))
    try:
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except FileExistsError:
        return False


def _heartbeat(path: Path, lease_id: str) -> None:
    lease = _load(path)
    if not lease or lease.get("lease_id") != lease_id:
        return
    lease["heartbeat_utc"] = _stamp()
    temporary = path.with_name(f"{path.name}.{lease_id}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            json.dump(lease, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def androidbuild_device_lease(
    serial: str,
    *,
    repository: str = "android-agent/mcp",
    run_id: str = "mcp-tool-call",
) -> Iterator[dict]:
    """Hold Core's exclusive device lease while Android Agent touches Android.

    Core's contract uses a create-new file (rather than a check-then-write), a
    900-second TTL, matching JSON fields, and owner-checked release. Active
    leases are never stolen; stale or malformed ones are recovered the same way
    as Core's ``Acquire-AndroidBuildDeviceLease`` implementation.
    """
    serial = serial.strip()
    if not serial:
        raise ValueError("DEVICE_LEASE=FAIL empty_serial")
    if serial.lower().startswith("ios:"):
        raise RuntimeError(
            "DEVICE_LEASE=BLOCKED iOS is not covered by AndroidBuild's ADB device lease; "
            "configure a separate iOS lease bridge before enabling iOS MCP actions."
        )

    root = Path(os.environ.get("ANDROIDBUILD_ROOT", str(_DEFAULT_ROOT))).expanduser()
    path = _lease_path(root, serial)
    path.parent.mkdir(parents=True, exist_ok=True)

    lease_id = uuid.uuid4().hex
    lease = {
        "schema": "androidbuild-device-lease/v1",
        "lease_id": lease_id,
        "serial": serial,
        "repository": repository,
        "run_id": run_id,
        "job_id": uuid.uuid4().hex,
        "runner": os.environ.get("COMPUTERNAME") or socket.gethostname(),
        "tested_sha": "",
        "pid": os.getpid(),
        "acquired_utc": _stamp(),
        "heartbeat_utc": _stamp(),
        "ttl_seconds": _LEASE_TTL_SECONDS,
    }

    if not _write_exclusive(path, lease):
        existing = _load(path)
        if existing is None or _is_stale(existing):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise RuntimeError(f"DEVICE_BUSY serial={serial}") from error
            if not _write_exclusive(path, lease):
                raise RuntimeError(f"DEVICE_BUSY serial={serial}")
        else:
            raise RuntimeError(f"DEVICE_BUSY serial={serial}")

    stop_heartbeat = threading.Event()

    def keep_alive() -> None:
        while not stop_heartbeat.wait(_HEARTBEAT_SECONDS):
            _heartbeat(path, lease_id)

    heartbeat_thread = threading.Thread(target=keep_alive, name="androidbuild-device-lease", daemon=True)
    heartbeat_thread.start()
    try:
        yield lease
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)
        current = _load(path)
        if current is not None and current.get("lease_id") == lease_id:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
