"""Make the configured Android SDK command-line tools available to child processes."""

from __future__ import annotations

import os
from pathlib import Path


def configure_android_sdk_path() -> Path | None:
    """Prepend SDK tool directories from ANDROID_SDK_ROOT/ANDROID_HOME to PATH.

    Long-running launchers can retain an old user PATH after the SDK is
    installed. Resolving it at app/MCP startup keeps ADB calls independent of
    which shell or service manager launched Android Agent.
    """
    candidates = [os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")]
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(str(Path(local_app_data) / "Android" / "Sdk"))
    candidates.extend((str(Path.home() / "Android" / "Sdk"), str(Path.home() / "AppData/Local/Android/Sdk")))

    root: Path | None = None
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "platform-tools").is_dir():
            root = path
            break
    if root is None:
        return None

    os.environ["ANDROID_SDK_ROOT"] = str(root)
    os.environ["ANDROID_HOME"] = str(root)
    tool_dirs = [root / "platform-tools", root / "emulator", root / "cmdline-tools" / "latest" / "bin"]
    current = os.environ.get("PATH", "")
    present = {item.rstrip("\\/").casefold() for item in current.split(os.pathsep) if item}
    missing = [str(path) for path in tool_dirs if path.is_dir() and str(path).rstrip("\\/").casefold() not in present]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, current] if current else missing)
    return root
