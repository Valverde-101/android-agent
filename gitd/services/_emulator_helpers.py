"""
Shared helpers for emulator_service — Docker-based Android emulators,
ADB utilities, EmulatorConfig dataclass, and discovery/query functions.

Replaces the AVD/SDK-based backend with Docker+KVM via budtmo/docker-android.
"""

import logging
import os
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

try:
    import docker as docker_sdk
except ImportError:
    docker_sdk = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:
    psutil = None  # Not available on Android (C extension)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

DOCKER_IMAGE = "budtmo/docker-android:emulator_11.0"
CONTAINER_LABEL = "ghost.type"
CONTAINER_LABEL_VALUE = "emulator"
CONTAINER_PORT = 5555  # internal ADB port inside every budtmo container
BASE_HOST_PORT = 5555  # first mapped host port; increments per instance

def _sdk_root() -> Path | None:
    """Find the local Android SDK without assuming a machine-specific drive."""
    candidates = [
        os.environ.get("ANDROID_SDK_ROOT"),
        os.environ.get("ANDROID_HOME"),
        str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"),
        str(Path.home() / "Android" / "Sdk"),
    ]
    for candidate in candidates:
        if candidate:
            root = Path(candidate).expanduser()
            if (root / "platform-tools").is_dir() or (root / "emulator").is_dir():
                return root
    return None


SDK_ROOT = _sdk_root()
AVD_HOME = Path(os.environ.get("ANDROID_AVD_HOME", Path.home() / ".android" / "avd")).expanduser()
_windows = platform.system() == "Windows"
_exe = ".exe" if _windows else ""
_bat = ".bat" if _windows else ""
EMULATOR_BIN = (SDK_ROOT / "emulator" / f"emulator{_exe}") if SDK_ROOT else Path("emulator")
AVDMANAGER = (SDK_ROOT / "cmdline-tools" / "latest" / "bin" / f"avdmanager{_bat}") if SDK_ROOT else Path("avdmanager")
SDKMANAGER = (SDK_ROOT / "cmdline-tools" / "latest" / "bin" / f"sdkmanager{_bat}") if SDK_ROOT else Path("sdkmanager")

# Prefer the SDK paired with the configured emulator over another adb on PATH.
_adb_candidate = (SDK_ROOT / "platform-tools" / f"adb{_exe}") if SDK_ROOT else None
ADB_BIN: str = str(_adb_candidate) if _adb_candidate and _adb_candidate.exists() else (shutil.which("adb") or "adb")


# ── Docker client ─────────────────────────────────────────────────────────────


def _docker() -> "docker_sdk.DockerClient":
    """Return a connected Docker client (raises RuntimeError if unavailable)."""
    if docker_sdk is None:
        raise RuntimeError("docker Python SDK not installed. Run: pip install docker")
    return docker_sdk.from_env()


# ── Utility functions ─────────────────────────────────────────────────────────


def _has_cmdline_tools() -> bool:
    """Whether AVD and SDK manager command-line tools are installed."""
    return AVDMANAGER.exists() and SDKMANAGER.exists()


def _has_emulator() -> bool:
    """Whether the native Android Emulator binary is installed."""
    return EMULATOR_BIN.exists()


def _has_docker() -> bool:
    """Check if Docker daemon is reachable."""
    try:
        _docker().ping()
        return True
    except Exception:
        return False


def _has_kvm() -> bool:
    return Path("/dev/kvm").exists()


def _has_native_avd() -> bool:
    """Native AVDs use WHPX on Windows, KVM on Linux, and HVF on macOS."""
    return _has_emulator() and _has_cmdline_tools()


def emulator_backend() -> str:
    """Choose a supported backend; Windows prefers its native WHPX emulator."""
    requested = os.environ.get("ANDROID_AGENT_EMULATOR_BACKEND", "auto").strip().lower()
    if requested in {"native", "docker"}:
        return requested
    if platform.system() == "Windows" and _has_emulator():
        return "native"
    if _has_kvm() and _has_docker():
        return "docker"
    if _has_native_avd():
        return "native"
    return "docker"


def _run(cmd: list[str], timeout: int = 60, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check, **kw)


def _run_sdk_tool(tool: Path, *args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    """Run Android SDK tools, including .bat launchers on Windows."""
    command = [str(tool), *map(str, args)]
    if _windows and tool.suffix.lower() in {".bat", ".cmd"}:
        # SDK arguments below are validated at their API boundaries; shell=True
        # is only needed for Windows batch files, not native executables.
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=check, shell=True)
    return _run(command, timeout=timeout, check=check)


def _native_accel() -> tuple[bool, str, str]:
    """Read the emulator's own acceleration check (WHPX on this Windows host)."""
    if not _has_emulator():
        return False, "Unavailable", "Android Emulator binary is missing."
    try:
        result = _run([str(EMULATOR_BIN), "-accel-check"], timeout=15, check=False)
        detail = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        match = re.search(r"(WHPX|AEHD|HAXM|KVM|HVF)", detail, re.IGNORECASE)
        accel_type = match.group(1).upper() if match else "Software"
        usable = result.returncode == 0 and re.search(r"installed and usable|is installed and usable|usable", detail, re.I)
        return bool(usable), accel_type, detail or f"emulator -accel-check exited {result.returncode}"
    except Exception as exc:
        return False, "Unavailable", str(exc)


def _avd_config(name: str) -> dict[str, str]:
    """Read a local AVD's config.ini as key/value pairs."""
    path = AVD_HOME / f"{name}.avd" / "config.ini"
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def _native_system_images() -> list[dict]:
    """Enumerate installed system images from the configured SDK."""
    if not SDK_ROOT:
        return []
    result = []
    image_root = SDK_ROOT / "system-images"
    if not image_root.is_dir():
        return result
    for api_dir in sorted(image_root.glob("android-*"), reverse=True):
        api_level = api_dir.name.removeprefix("android-")
        if not api_level.isdigit():
            continue
        for target_dir in sorted(api_dir.iterdir()):
            if not target_dir.is_dir():
                continue
            for arch_dir in sorted(target_dir.iterdir()):
                if arch_dir.is_dir():
                    package = f"system-images;android-{api_level};{target_dir.name};{arch_dir.name}"
                    result.append({
                        "api_level": api_level,
                        "target": target_dir.name,
                        "arch": arch_dir.name,
                        "package": package,
                        "path": str(arch_dir),
                    })
    return result


def _native_avd_names() -> list[str]:
    """List AVDs from their metadata files, without requiring a running ADB."""
    try:
        return sorted(path.stem for path in AVD_HOME.glob("*.ini") if (AVD_HOME / f"{path.stem}.avd").is_dir())
    except OSError:
        return []


def _native_avd_name(serial: str) -> str:
    """Resolve an emulator serial to its AVD name using the emulator console."""
    result = _run([ADB_BIN, "-s", serial, "emu", "avd", "name"], timeout=8, check=False)
    for line in result.stdout.splitlines():
        value = line.strip()
        if value and value != "OK" and value in _native_avd_names():
            return value
    result = _run([ADB_BIN, "-s", serial, "shell", "getprop", "ro.boot.qemu.avd_name"], timeout=8, check=False)
    value = result.stdout.strip()
    return value if value in _native_avd_names() else serial


def _adb(serial: str, *args, timeout: int = 30) -> str:
    r = _run([ADB_BIN, "-s", serial, *args], timeout=timeout, check=False)
    return r.stdout.strip()


def _safe_int(val: str, default: int = 0) -> int:
    """Parse int from string, stripping size suffixes like 'M', 'G'."""
    val = val.strip()
    if not val:
        return default
    for suffix in ("MB", "GB", "KB", "M", "G", "K"):
        if val.upper().endswith(suffix):
            val = val[: -len(suffix)]
            break
    try:
        return int(val)
    except ValueError:
        return default


def is_emulator(serial: str) -> bool:
    return serial.startswith("emulator-") or "localhost:" in serial


def default_arch() -> str:
    if platform.machine().lower() in ("arm64", "aarch64"):
        return "arm64-v8a"
    return "x86_64"


# ── Config dataclass ──────────────────────────────────────────────────────────


@dataclass
class EmulatorConfig:
    name: str
    api_level: int = 30  # Android 11 (emulator_11.0 image)
    target: str = "google_apis_playstore"
    arch: str = ""  # auto-detected if empty
    device_profile: str = "Samsung Galaxy S10"  # passed as EMULATOR_DEVICE env var
    ram_mb: int = 2048
    disk_mb: int = 6144
    resolution: str = "1080x2400"
    dpi: int = 420
    gpu: str = "auto"  # kept for API compat; Docker uses swiftshader internally
    cores: int = 2
    headless: bool = False  # Docker is always headless
    snapshot: bool = True

    def __post_init__(self):
        if not self.arch:
            self.arch = default_arch()

    def system_image_pkg(self) -> str:
        """Legacy compat — not used in Docker mode."""
        api = f"android-{self.api_level}"
        return f"system-images;{api};{self.target};{self.arch}"

    def system_image_dir(self) -> Path:
        """Legacy compat — not used in Docker mode."""
        api = f"android-{self.api_level}"
        return Path("/dev/null") / "system-images" / api / self.target / self.arch


# ── Port allocation ───────────────────────────────────────────────────────────


def _used_host_ports() -> set[int]:
    """Collect host ports already in use by our emulator containers."""
    used: set[int] = set()
    try:
        client = _docker()
        for c in client.containers.list(filters={"label": f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}"}):
            ports = c.ports or {}
            # ports dict: {"5555/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5556"}]}
            for bindings in ports.values():
                if bindings:
                    for b in bindings:
                        try:
                            used.add(int(b["HostPort"]))
                        except (KeyError, ValueError, TypeError):
                            pass
    except Exception:
        pass
    return used


def next_available_port(used_ports: set[int] | None = None) -> int:
    """Find the next free host port starting from BASE_HOST_PORT."""
    if used_ports is None:
        used_ports = _used_host_ports()
    port = BASE_HOST_PORT
    while port in used_ports:
        port += 1
    return port


# ── Container helpers ─────────────────────────────────────────────────────────


def _container_to_info(c) -> dict:
    """Convert a Docker container object to the canonical emulator info dict."""
    labels = c.labels or {}
    ports = c.ports or {}

    host_port = None
    for bindings in ports.values():
        if bindings:
            for b in bindings:
                try:
                    host_port = int(b["HostPort"])
                    break
                except (KeyError, ValueError, TypeError):
                    pass
        if host_port:
            break

    serial = f"localhost:{host_port}" if host_port else None
    status = c.status  # running / exited / created / ...

    return {
        "name": labels.get("ghost.name", c.name),
        "container_id": c.short_id,
        "container_name": c.name,
        "host_port": host_port,
        "serial": serial,
        "status": "running" if status == "running" else status,
        "device_profile": labels.get("ghost.device_profile", ""),
        "api_level": int(labels.get("ghost.api_level", 30)),
    }


# ── Discovery / query functions ───────────────────────────────────────────────


def check_prerequisites() -> dict:
    """Report the active emulator backend and the host capabilities it needs."""
    docker_ok = _has_docker()
    kvm_ok = _has_kvm()
    adb_ok = Path(ADB_BIN).exists() or bool(shutil.which(ADB_BIN))
    backend = emulator_backend()
    if backend == "native":
        hw_accel, accel_type, accel_detail = _native_accel()
        sdk_root = str(SDK_ROOT) if SDK_ROOT else "Not found"
        return {
            "backend": "native",
            "backend_label": "Android Emulator (WHPX/native)",
            "docker_available": docker_ok,
            "kvm_available": kvm_ok,
            "adb_binary": adb_ok,
            "hw_accel": hw_accel,
            "hw_accel_type": accel_type,
            "hw_accel_detail": accel_detail,
            "platform": platform.system(),
            "arch": default_arch(),
            "image": None,
            "sdk_root": sdk_root,
            "sdk_exists": bool(SDK_ROOT),
            "emulator_binary": _has_emulator(),
            "cmdline_tools": _has_cmdline_tools(),
            "avd_home": str(AVD_HOME),
        }
    return {
        "backend": "docker",
        "backend_label": "Docker Android emulator (KVM)",
        "docker_available": docker_ok,
        "kvm_available": kvm_ok,
        "adb_binary": adb_ok,
        "hw_accel": kvm_ok,
        "hw_accel_type": "KVM",
        "hw_accel_detail": "KVM (/dev/kvm) is required for this Docker emulator backend.",
        "platform": platform.system(),
        "arch": default_arch(),
        "image": DOCKER_IMAGE,
        # Legacy fields (kept so existing frontend/clients don't break)
        "sdk_root": "N/A (docker mode)",
        "sdk_exists": False,
        "emulator_binary": False,
        "cmdline_tools": False,
        "avd_home": "N/A (docker mode)",
    }


def list_system_images() -> list[dict]:
    """List installed SDK images in native mode, or the configured Docker image."""
    if emulator_backend() == "native":
        return _native_system_images()
    return [
        {
            "api_level": "30",
            "target": "google_apis_playstore",
            "arch": default_arch(),
            "package": DOCKER_IMAGE,
            "path": f"docker://{DOCKER_IMAGE}",
            "note": "Docker image — no SDK system images needed",
        }
    ]


def install_system_image(api_level: int, target: str = "google_apis_playstore", arch: str = "") -> dict:
    """Install an SDK image natively or pull the configured Docker image."""
    if emulator_backend() == "native":
        if not SDK_ROOT or not SDKMANAGER.exists():
            return {"ok": False, "error": "Android SDK Manager was not found. Set ANDROID_SDK_ROOT."}
        arch = arch or default_arch()
        package = f"system-images;android-{api_level};{target};{arch}"
        try:
            result = _run_sdk_tool(SDKMANAGER, "--install", package, timeout=1800, check=False)
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if result.returncode != 0:
                return {"ok": False, "package": package, "error": output[-1500:] or f"sdkmanager exited {result.returncode}"}
            return {"ok": True, "package": package, "output": output[-1500:]}
        except Exception as exc:
            return {"ok": False, "package": package, "error": str(exc)}
    try:
        client = _docker()
        client.images.pull(DOCKER_IMAGE)
        return {"ok": True, "package": DOCKER_IMAGE, "output": f"Pulled {DOCKER_IMAGE}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_avds(running_list: list[dict]) -> list[dict]:
    """List native AVDs or Docker-managed emulators with running status."""
    if emulator_backend() == "native":
        running_by_name = {item["name"]: item for item in running_list}
        avds = []
        for name in _native_avd_names():
            cfg = _avd_config(name)
            running = running_by_name.get(name)
            image = cfg.get("image.sysdir.1", "")
            match = re.search(r"android-(\d+)[/\\]([^/\\]+)[/\\]([^/\\]+)", image)
            api_level, target, abi = match.groups() if match else ("", "", "")
            try:
                disk = cfg.get("disk.dataPartition.size", "")
                ram_mb = int(cfg.get("hw.ramSize", "0")) or None
                dpi = int(cfg.get("hw.lcd.density", "0")) or None
            except ValueError:
                ram_mb = dpi = None
                disk = cfg.get("disk.dataPartition.size", "")
            width = cfg.get("hw.lcd.width", "")
            height = cfg.get("hw.lcd.height", "")
            avds.append({
                "name": name,
                "display_name": name,
                "api_level": api_level,
                "target_flavor": target,
                "abi": abi,
                "resolution": f"{width}x{height}" if width and height else None,
                "dpi": dpi,
                "ram_mb": ram_mb,
                "disk": disk,
                "gpu_mode": cfg.get("hw.gpu.mode", "auto"),
                "cores": int(cfg.get("hw.cpu.ncore", "0") or 0) or None,
                "playstore": cfg.get("PlayStore.enabled", "no").lower() == "true",
                "status": "running" if running and running.get("adb_state") == "device" else ("booting" if running else "stopped"),
                "serial": running.get("serial") if running else None,
                "pid": running.get("pid") if running else None,
            })
        return avds

    try:
        client = _docker()
        containers = client.containers.list(
            all=True,
            filters={"label": f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}"},
        )
    except Exception as e:
        logger.warning("list_avds: Docker error: %s", e)
        return []

    running_by_name = {r["name"]: r for r in running_list}
    avds = []
    for c in containers:
        info = _container_to_info(c)
        run_info = running_by_name.get(info["name"])
        if run_info:
            info["status"] = "running" if run_info.get("adb_state") == "device" else "booting"
            info["serial"] = run_info["serial"]
        avds.append(info)
    return avds


def list_running(procs: dict, lock: threading.Lock) -> list[dict]:
    """List running native AVDs or Docker emulator containers reachable via ADB."""
    if emulator_backend() == "native":
        try:
            result = _run([ADB_BIN, "devices"], timeout=10, check=False)
        except Exception as exc:
            logger.warning("list_running: adb devices failed: %s", exc)
            return []
        running = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or not parts[0].startswith("emulator-"):
                continue
            serial, adb_state = parts[0], parts[1]
            process_entry = procs.get(serial)
            process = process_entry.get("process") if isinstance(process_entry, dict) else process_entry
            name = (process_entry.get("name") if isinstance(process_entry, dict) else None) or _native_avd_name(serial)
            running.append({
                "serial": serial,
                "name": name,
                "host_port": None,
                "pid": getattr(process, "pid", None),
                "adb_state": "device" if adb_state == "device" else adb_state,
                "container_id": None,
            })
        # A newly launched QEMU process can take several seconds to register
        # with adb. Keep it visible as booting so repeat clicks cannot launch a
        # second copy and name-based stop/delete continue to work.
        known_serials = {item["serial"] for item in running}
        for serial, process_entry in list(procs.items()):
            process = process_entry.get("process") if isinstance(process_entry, dict) else process_entry
            if serial in known_serials or process is None or process.poll() is not None:
                continue
            name = process_entry.get("name") if isinstance(process_entry, dict) else serial
            running.append({
                "serial": serial,
                "name": name or serial,
                "host_port": None,
                "pid": getattr(process, "pid", None),
                "adb_state": "offline",
                "container_id": None,
            })
        return running

    try:
        client = _docker()
        containers = client.containers.list(
            filters={"label": f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}", "status": "running"}
        )
    except Exception as e:
        logger.warning("list_running: Docker error: %s", e)
        return []

    running = []
    for c in containers:
        info = _container_to_info(c)
        serial = info.get("serial")
        if not serial:
            continue
        # Check ADB state
        try:
            r = _run([ADB_BIN, "-s", serial, "get-state"], timeout=5, check=False)
            adb_state = "device" if r.stdout.strip() == "device" else "offline"
        except Exception:
            adb_state = "offline"

        running.append(
            {
                "serial": serial,
                "name": info["name"],
                "host_port": info.get("host_port"),
                "pid": None,  # no native PID — it's a container
                "adb_state": adb_state,
                "container_id": info.get("container_id"),
            }
        )
    return running
