"""
Emulator Service — Docker+KVM lifecycle management (create/start/stop/delete/snapshots).

Replaces the native Android SDK AVD backend with budtmo/docker-android containers.
Discovery helpers live in _emulator_helpers; pool logic in _emulator_pool.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import gitd.services._emulator_helpers as _eh
from gitd.services._emulator_helpers import (
    ADB_BIN,
    DOCKER_IMAGE,
    EmulatorConfig,  # noqa: F401 — re-exported for routers
    _adb,
    _docker,
    _has_docker,
    _has_kvm,
    _run,
    AVDMANAGER,
    AVD_HOME,
    EMULATOR_BIN,
    next_available_port,
)
from gitd.services._emulator_pool import EmulatorPool

logger = logging.getLogger(__name__)

# ── EmulatorManager ───────────────────────────────────────────────────────────


class EmulatorManager:
    """Manage Android emulator lifecycle via Docker+KVM."""

    def __init__(self):
        # _procs kept for API compatibility — unused in Docker mode
        self._procs: dict = {}
        self._lock = threading.Lock()

    # ── Discovery ─────────────────────────────────────────────────────────

    def check_prerequisites(self) -> dict:
        """Check the selected backend and its platform-specific acceleration."""
        return _eh.check_prerequisites()

    def list_system_images(self) -> list[dict]:
        """Return the installed native SDK images or configured Docker image."""
        return _eh.list_system_images()

    def install_system_image(self, api_level: int, target: str = "google_apis_playstore", arch: str = "") -> dict:
        """Install a native SDK image or pull the configured Docker image."""
        return _eh.install_system_image(api_level, target, arch)

    def list_avds(self) -> list[dict]:
        """List native AVDs or Docker-managed emulators."""
        return _eh.list_avds(self.list_running())

    def list_running(self) -> list[dict]:
        """List emulators reachable through ADB on the selected backend."""
        return _eh.list_running(self._procs, self._lock)

    def _next_native_port(self) -> int:
        """Choose an unused even emulator console port (ADB requires even ports)."""
        used = set()
        for info in self.list_running():
            match = re.fullmatch(r"emulator-(\d+)", info.get("serial", ""))
            if match:
                used.add(int(match.group(1)))
        for port in range(5554, 5682, 2):
            if port not in used:
                return port
        raise RuntimeError("No free Android Emulator console ports remain (5554–5680).")

    def _native_create(self, config: EmulatorConfig) -> dict:
        """Create a stopped SDK AVD; use installed images and device profiles."""
        if not _eh._has_emulator() or not _eh._has_cmdline_tools():
            return {"ok": False, "error": "Native Android SDK tools are incomplete; configure ANDROID_SDK_ROOT."}
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", config.name):
            return {"ok": False, "error": "AVD name may contain only letters, numbers, dot, underscore, and hyphen."}
        existing = {item["name"] for item in self.list_avds()}
        if config.name in existing:
            return {"ok": False, "error": f'Emulator "{config.name}" already exists.'}
        avd_dir = AVD_HOME / f"{config.name}.avd"
        if avd_dir.exists():
            return {"ok": False, "error": f"AVD data folder already exists without registered metadata: {avd_dir}. Review or remove that orphan before recreating it."}

        images = [item for item in _eh._native_system_images() if int(item["api_level"]) == config.api_level]
        image = next((item for item in images if item["target"] == config.target), None)
        if image is None and images:
            # The form exposes API level; use an image actually installed for it
            # rather than pretending the Docker-only Play Store image exists.
            image = next((item for item in images if item["target"] == "google_apis_playstore"), images[0])
        if image is None:
            return {"ok": False, "error": f"No Android {config.api_level} system image is installed. Install one from System Images first."}

        args = ["create", "avd", "-n", config.name, "-k", image["package"]]
        if config.device_profile:
            args += ["-d", config.device_profile]
        try:
            result = _eh._run_sdk_tool(AVDMANAGER, *args, timeout=180, check=False)
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if result.returncode != 0:
                self._remove_native_avd_artifacts(config.name)
                return {"ok": False, "error": output[-1500:] or f"avdmanager exited {result.returncode}"}
            config_path = AVD_HOME / f"{config.name}.avd" / "config.ini"
            self._write_native_config(config_path, config)
            return {
                "ok": True,
                "name": config.name,
                "status": "stopped",
                "backend": "native",
                "system_image": image["package"],
                "config": asdict(config),
                "output": output[-1000:],
            }
        except Exception as exc:
            try:
                self._remove_native_avd_artifacts(config.name)
            except Exception:
                logger.exception("Could not clean partial AVD after failed creation: %s", config.name)
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _remove_native_avd_artifacts(name: str) -> None:
        """Remove only this validated AVD's files, after its process is stopped."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("Invalid AVD name")
        root = AVD_HOME.resolve()
        for candidate in (AVD_HOME / f"{name}.ini", AVD_HOME / f"{name}.avd"):
            resolved = candidate.resolve()
            if resolved.parent != root:
                raise ValueError(f"Refusing to remove a path outside the AVD directory: {resolved}")
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.is_file():
                candidate.unlink()

    @staticmethod
    def _write_native_config(path: Path, config: EmulatorConfig) -> None:
        """Apply requested hardware dimensions without replacing other AVD settings."""
        if not path.is_file():
            raise FileNotFoundError(f"avdmanager did not create {path}")
        width, height = (config.resolution.lower().split("x", 1) + [""])[:2]
        updates = {
            "hw.ramSize": str(max(1024, config.ram_mb)),
            "hw.cpu.ncore": str(max(1, config.cores)),
            "hw.lcd.width": width if width.isdigit() else "1080",
            "hw.lcd.height": height if height.isdigit() else "2400",
            "hw.lcd.density": str(max(120, config.dpi)),
            "disk.dataPartition.size": f"{max(2048, config.disk_mb)}M",
        }
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        seen = set()
        updated = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in updates:
                updated.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                updated.append(line)
        updated.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
        temp_path = path.with_suffix(".ini.tmp")
        temp_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
        temp_path.replace(path)

    def _native_start(self, name: str, headless: bool = False, gpu: str = "auto", cold_boot: bool = False) -> dict:
        """Launch a native AVD accelerated by the host platform (WHPX on Windows)."""
        if name not in _eh._native_avd_names():
            return {"ok": False, "error": f'AVD "{name}" not found. Create it first.'}
        for running in self.list_running():
            if running["name"] == name and running["adb_state"] in {"device", "offline"}:
                return {"ok": True, "already_running": True, "name": name, "serial": running["serial"], "status": running["adb_state"]}

        try:
            port = self._next_native_port()
            serial = f"emulator-{port}"
            args = [str(EMULATOR_BIN), "-avd", name, "-port", str(port), "-accel", "on", "-gpu", gpu or "auto"]
            if headless:
                args.extend(["-no-window", "-no-audio", "-no-boot-anim"])
            if cold_boot:
                args.append("-no-snapshot-load")
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(AVD_HOME.parent)}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(args, **kwargs)
            self._procs[serial] = {"process": process, "name": name}
            watcher = threading.Thread(target=self._wait_for_boot_and_setup, args=(serial, name), daemon=True)
            watcher.start()
            return {"ok": True, "name": name, "serial": serial, "pid": process.pid, "status": "booting", "backend": "native"}
        except Exception as exc:
            return {"ok": False, "error": f"Could not start native emulator: {exc}"}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def create(self, config: EmulatorConfig) -> dict:
        """Create a native AVD or a Docker Android emulator container.

        Native mode creates a stopped AVD, matching the Android SDK workflow.
        Docker mode starts the container immediately because it has no separate
        AVD creation step.
        """
        if _eh.emulator_backend() == "native":
            return self._native_create(config)
        if not _has_docker():
            return {
                "ok": False,
                "error": "Docker daemon not reachable. Make sure Docker is running.",
            }
        if not _has_kvm():
            return {
                "ok": False,
                "error": "/dev/kvm not found. KVM must be enabled for hardware-accelerated emulation.",
            }

        # Check for name collision
        existing = [a["name"] for a in self.list_avds()]
        if config.name in existing:
            return {"ok": False, "error": f'Emulator "{config.name}" already exists.'}

        with self._lock:
            used_ports = _eh._used_host_ports()
        host_port = next_available_port(used_ports)

        try:
            client = _docker()
            container = client.containers.run(
                DOCKER_IMAGE,
                detach=True,
                name=config.name,
                devices=["/dev/kvm:/dev/kvm"],
                environment={
                    "EMULATOR_DEVICE": config.device_profile,
                    "WEB_VNC": "false",
                },
                ports={f"{_eh.CONTAINER_PORT}/tcp": host_port},
                labels={
                    _eh.CONTAINER_LABEL: _eh.CONTAINER_LABEL_VALUE,
                    "ghost.name": config.name,
                    "ghost.device_profile": config.device_profile,
                    "ghost.api_level": str(config.api_level),
                },
            )
        except Exception as e:
            return {"ok": False, "error": f"Docker run failed: {e}"}

        serial = f"localhost:{host_port}"

        # Boot watcher in background
        boot_thread = threading.Thread(
            target=self._wait_for_boot_and_setup,
            args=(serial, config.name),
            daemon=True,
        )
        boot_thread.start()

        return {
            "ok": True,
            "name": config.name,
            "container_id": container.short_id,
            "serial": serial,
            "host_port": host_port,
            "status": "booting",
            "config": asdict(config),
        }

    def _wait_for_boot_and_setup(self, serial: str, name: str):
        """Connect ADB and wait for boot, then run automation setup."""
        # Give the container a moment to initialise its QEMU process
        time.sleep(10)
        try:
            # Connect ADB to the TCP endpoint
            _run([ADB_BIN, "connect", serial], timeout=15, check=False)
            self._wait_for_boot(serial, timeout=240)
            self.setup_for_automation(serial)
            logger.info("Emulator %s (%s) is ready", name, serial)
        except Exception as e:
            logger.error("Emulator %s (%s) boot/setup failed: %s", name, serial, e)

    def delete(self, name: str) -> dict:
        """Stop and remove a native AVD or Docker emulator container."""
        if _eh.emulator_backend() == "native":
            running = next((item for item in self.list_running() if item["name"] == name), None)
            if running:
                stopped = self.stop(running["serial"])
                if not stopped.get("ok"):
                    return stopped
            if name not in _eh._native_avd_names():
                orphan = AVD_HOME / f"{name}.avd"
                if orphan.exists():
                    return {"ok": False, "error": f"AVD metadata is missing but data remains at {orphan}; inspect it before deleting."}
                return {"ok": False, "error": f'AVD "{name}" not found.'}
            try:
                result = _eh._run_sdk_tool(AVDMANAGER, "delete", "avd", "-n", name, timeout=120, check=False)
                output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
                if result.returncode != 0 and ((AVD_HOME / f"{name}.ini").exists() or (AVD_HOME / f"{name}.avd").exists()):
                    return {"ok": False, "error": output[-1000:] or f"avdmanager exited {result.returncode}"}
                self._remove_native_avd_artifacts(name)
                return {"ok": True, "name": name}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        # Disconnect ADB first if it was connected
        for info in self.list_running():
            if info["name"] == name and info.get("serial"):
                try:
                    _run([ADB_BIN, "disconnect", info["serial"]], timeout=5, check=False)
                except Exception:
                    pass

        try:
            client = _docker()
            container = client.containers.get(name)
            container.remove(force=True)
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def start(
        self,
        name: str,
        headless: bool = False,
        gpu: str = "auto",
        cold_boot: bool = False,
        extra_args: list[str] | None = None,
    ) -> dict:
        """Start a stopped native AVD or Docker emulator container.

        If the AVD/container does not exist yet, returns an error directing the
        caller to use create() first. Docker mode accepts headless/gpu/extra_args
        for API compatibility; native mode passes the supported GPU/boot flags.
        """
        if _eh.emulator_backend() == "native":
            return self._native_start(name, headless=headless, gpu=gpu, cold_boot=cold_boot)
        if not _has_docker():
            return {"ok": False, "error": "Docker daemon not reachable."}

        try:
            client = _docker()
            container = client.containers.get(name)
        except Exception:
            return {
                "ok": False,
                "error": (f'Container "{name}" not found. Use POST /api/emulators to create it first.'),
            }

        if container.status == "running":
            info = _eh._container_to_info(container)
            return {"ok": True, "already_running": True, **info}

        # Re-start a stopped container
        try:
            container.start()
            container.reload()
        except Exception as e:
            return {"ok": False, "error": f"Failed to start container: {e}"}

        info = _eh._container_to_info(container)
        serial = info.get("serial")
        if serial:
            boot_thread = threading.Thread(
                target=self._wait_for_boot_and_setup,
                args=(serial, name),
                daemon=True,
            )
            boot_thread.start()

        return {
            "ok": True,
            "name": name,
            "serial": serial,
            "host_port": info.get("host_port"),
            "status": "booting",
        }

    def stop(self, serial: str) -> dict:
        """Stop a running emulator by its ADB serial.

        Stops (but does not remove) the native AVD/container so it can restart.
        """
        if _eh.emulator_backend() == "native":
            if not re.fullmatch(r"emulator-\d+", serial):
                return {"ok": False, "error": f"Invalid native emulator serial: {serial}"}
            result = _run([ADB_BIN, "-s", serial, "emu", "kill"], timeout=20, check=False)
            process_entry = self._procs.pop(serial, None)
            proc = process_entry.get("process") if isinstance(process_entry, dict) else process_entry
            try:
                if proc:
                    proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.terminate()
            except Exception:
                pass
            if result.returncode == 0 or "OK" in (result.stdout + result.stderr):
                return {"ok": True, "serial": serial, "backend": "native"}
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                    return {"ok": True, "serial": serial, "backend": "native"}
                except subprocess.TimeoutExpired:
                    proc.kill()
                    return {"ok": True, "serial": serial, "backend": "native", "forced": True}
            return {"ok": False, "serial": serial, "error": (result.stderr or result.stdout or "Emulator did not accept the stop command.")[-500:]}
        try:
            _run([ADB_BIN, "disconnect", serial], timeout=5, check=False)
        except Exception:
            pass

        # Find the container by matching host port
        try:
            host_port = int(serial.split(":")[-1])
        except ValueError:
            return {"ok": False, "error": f"Cannot parse port from serial: {serial}"}

        try:
            client = _docker()
            containers = client.containers.list(filters={"label": f"{_eh.CONTAINER_LABEL}={_eh.CONTAINER_LABEL_VALUE}"})
            for c in containers:
                for bindings in (c.ports or {}).values():
                    if bindings and any(int(b.get("HostPort", -1)) == host_port for b in bindings):
                        c.stop(timeout=15)
                        return {"ok": True, "serial": serial, "container": c.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {"ok": False, "error": f"No running container found for serial {serial}"}

    def get_boot_status(self, serial: str) -> dict:
        """Check if an emulator has finished booting."""
        try:
            r = _run([ADB_BIN, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=5, check=False)
            booted = r.stdout.strip() == "1"
            return {"serial": serial, "booted": booted}
        except (subprocess.SubprocessError, OSError):
            return {"serial": serial, "booted": False, "error": "not reachable"}

    # ── Setup ──────────────────────────────────────────────────────────────

    def setup_for_automation(self, serial: str) -> dict:
        """Configure emulator for automation after boot."""
        commands = [
            # Disable animations
            ("shell", "settings", "put", "global", "window_animation_scale", "0"),
            ("shell", "settings", "put", "global", "transition_animation_scale", "0"),
            ("shell", "settings", "put", "global", "animator_duration_scale", "0"),
            # Lock portrait
            ("shell", "settings", "put", "system", "accelerometer_rotation", "0"),
            # Max screen timeout
            ("shell", "settings", "put", "system", "screen_off_timeout", "2147483647"),
            # Stay awake while charging (container is always "charging")
            ("shell", "settings", "put", "global", "stay_on_while_plugged_in", "3"),
        ]
        results = []
        for cmd in commands:
            r = _adb(serial, *cmd)
            results.append(r)
        return {"ok": True, "serial": serial, "applied": len(commands)}

    def install_apk(self, serial: str, apk_path: str) -> dict:
        """Install an APK on emulator."""
        if not Path(apk_path).exists():
            return {"ok": False, "error": f"APK not found: {apk_path}"}
        try:
            r = _run([ADB_BIN, "-s", serial, "install", "-r", "-g", apk_path], timeout=120)
            return {"ok": True, "output": r.stdout[-200:]}
        except subprocess.CalledProcessError as e:
            return {"ok": False, "error": e.stderr[-300:]}

    # ── Snapshots ──────────────────────────────────────────────────────────
    # budtmo/docker-android doesn't support AVD snapshots; these return
    # informative messages rather than hard errors so the API surface is intact.

    def snapshot_save(self, serial: str, name: str = "automation_ready") -> dict:
        """Save an emulator snapshot when using native AVDs."""
        if _eh.emulator_backend() == "native":
            return self._native_snapshot(serial, "save", name)
        return {
            "ok": False,
            "serial": serial,
            "snapshot": name,
            "error": "Snapshots are not supported in Docker emulator mode.",
        }

    def snapshot_load(self, serial: str, name: str = "automation_ready") -> dict:
        """Load an emulator snapshot when using native AVDs."""
        if _eh.emulator_backend() == "native":
            return self._native_snapshot(serial, "load", name)
        return {
            "ok": False,
            "serial": serial,
            "snapshot": name,
            "error": "Snapshots are not supported in Docker emulator mode.",
        }

    def list_snapshots(self, serial: str) -> dict:
        """List snapshots from a native AVD or explain Docker's limitation."""
        if _eh.emulator_backend() == "native":
            result = _run([ADB_BIN, "-s", serial, "emu", "avd", "snapshot", "list"], timeout=20, check=False)
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            return {"ok": result.returncode == 0, "serial": serial, "output": output[-2000:], **({} if result.returncode == 0 else {"error": output[-500:]})}
        return {
            "ok": False,
            "serial": serial,
            "output": "",
            "error": "Snapshots are not supported in Docker emulator mode.",
        }

    def _native_snapshot(self, serial: str, action: str, name: str) -> dict:
        """Save or load a snapshot through the Android emulator console."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            return {"ok": False, "serial": serial, "error": "Snapshot name contains unsupported characters."}
        result = _run([ADB_BIN, "-s", serial, "emu", "avd", "snapshot", action, name], timeout=60, check=False)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        ok = result.returncode == 0 and "KO:" not in output
        return {"ok": ok, "serial": serial, "snapshot": name, "output": output[-1500:], **({} if ok else {"error": output[-500:] or f"Snapshot {action} failed."})}

    # ── Internals ──────────────────────────────────────────────────────────

    def _next_available_port(self) -> int:
        """Find next free host port for a new container."""
        with self._lock:
            used = _eh._used_host_ports()
        return next_available_port(used)

    def _wait_for_boot(self, serial: str, timeout: int = 240):
        """Poll until device reports boot complete."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = _run([ADB_BIN, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=5, check=False)
                if r.stdout.strip() == "1":
                    return True
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"{serial} did not boot within {timeout}s")


# ── Singletons ────────────────────────────────────────────────────────────────
_manager: Optional[EmulatorManager] = None
_pool: Optional[EmulatorPool] = None


def get_manager() -> EmulatorManager:
    global _manager
    if _manager is None:
        _manager = EmulatorManager()
    return _manager


def get_pool() -> EmulatorPool:
    global _pool
    if _pool is None:
        _pool = EmulatorPool(get_manager())
    return _pool
