# AndroidBuild integration

`android-agent-workshop` is managed by the AndroidBuild Control Center under **Taller → Dispositivos**. The integration uses Android Agent's own dashboard and FastAPI service; it does not route phone commands through Video Studio.

## Local runtime

- Dashboard: `http://127.0.0.1:6175`
- API and OpenAPI: `http://127.0.0.1:5055/docs`
- Both services bind only to loopback. They are not part of Control Center LAN sharing.
- Python and Node are taken from AndroidBuild's portable runtimes. The API uses the AndroidBuild SDK's `adb` through its local `PATH`.
- SQLite data and npm cache live under `V:\AndroidBuild\State\android-agent`. `.env` stays in this checkout and is ignored by Git; it contains a random admin token and local settings, not provider credentials.
- Android is enabled. The optional iOS path remains disabled until Appium/WebDriverAgent is installed and configured.
- No LLM provider is configured. The dashboard and device tools work without cloud keys; natural-language agent chat needs a provider login or local model configured separately.

## Core validation

The repository declares the AndroidBuild `web-service` / `web-validation` profile in `.androidbuild.json`. Core verifies the exact clean Git SHA, installs Python from `requirements.lock`, installs the Vue dashboard with `npm ci`, compiles it, starts temporary loopback services for health/smoke/functional checks, and records sanitized evidence under `Logs/web-validation`. Those checks only call health and feature-list endpoints; they do not tap, type, install apps on, or otherwise act on a connected device. Physical validation is `NOT_APPLICABLE` for this web service.

## Device-action boundary

Control Center owns installation, preparation, availability, and start/stop/restart. Codex connects to the local stdio MCP from this checkout; MCP tools that target an Android serial acquire the same `androidbuild-device-lease/v1` lock used by Core physical validation and release it after the tool call. This blocks MCP device actions while Core or another Control Center operation owns that device.

The Android Agent dashboard still runs its upstream ADB actions directly and does not yet acquire that lease. Do not run dashboard automation and Core physical validation against the same device at the same time. iOS remains disabled until Appium/WebDriverAgent and a separate iOS lease policy are configured.

Codex configuration lives in the user's private `config.toml`, not in this public repository. All MCP tool calls use Codex's normal tool policy; CRM tools are disabled for this integration. The local launcher injects the portable Android SDK paths, so no API key is required for ADB control.
