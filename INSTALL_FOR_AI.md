# AI Agent Autonomous Deployment Protocol (`INSTALL_FOR_AI.md`)

> **Target Audience**: AI Coding Agents (Antigravity, Claude Code, Cursor, Codex, Windsurf, Cline, OpenHands, etc.) running on a user's local computer.
> **Goal**: Deploy, verify, and launch **Antigravity Remote Assistant** with **zero unnecessary back-and-forth** and **zero interactive hangs**.

---

## 1. Architecture Summary (Read in 10 Seconds)

- **What this repo does**: Turns the user's local **Antigravity CLI (`agy`)** into a 24/7 remote personal assistant accessible from their phone via:
  1. **Dedicated Mobile Web App / PWA** (Port `8765`, Glassmorphic UI, token-by-token streaming, live desktop preview, arbitrary file transfer, PC Antigravity IDE conversation switcher/viewer, LAN 1ms direct + Cloudflare HTTP/2 encrypted 4G/5G tunnel). **Requires ZERO third-party chat accounts.**
  2. **Optional QQ Private Chat Channel** (via OneBot 11 / NapCatQQ on `ws://127.0.0.1:3001`, strict `ALLOWED_QQ_USERS` whitelist).
- **Default Smart Mode (`ONEBOT_ENABLED=auto`)**:
  - If `ALLOWED_QQ_USERS` is empty, it automatically starts in **Pure Mobile Web App Mode** (no NapCatQQ required!).
  - If the user provides their QQ number(s) for `ALLOWED_QQ_USERS`, it enables **Dual Mode (Mobile Web App + QQ)**.

---

## 2. Guardrails for Deploying AI Agents

1. **NEVER block on interactive CLI prompts** — always pass `--non-interactive` to `qq_agy_bridge.setup_wizard`.
2. **Windows Python Rule**: Prefer `uv sync --python 3.12` if `uv` is installed. Once `.venv` exists, ALWAYS invoke `.venv\Scripts\python.exe` directly (avoid bare `python` on Windows, which may hit the `WindowsApps` Store stub).
3. **Never overwrite an existing `.env`** unless the user explicitly asks to reset configuration.
4. **Verify Antigravity CLI (`agy`) first**:
   - Check `agy --version` (or `%LOCALAPPDATA%\agy\bin\agy.exe` / `%LOCALAPPDATA%\Programs\Antigravity\bin\agy.exe` on Windows, `/Applications/Antigravity.app/Contents/Resources/app/bin/agy` on macOS).
   - If `agy` is NOT installed on the machine, pause and tell the user to install & sign in to **Google Antigravity** first (this project reuses local Antigravity OAuth sessions with no API key needed).

---

## 3. Step-by-Step Execution Playbook

### Case A: Windows (Recommended Full Experience + Glassmorphic Desktop Console)

Run the following commands inside the cloned repository root:

```powershell
# 1. One-shot non-interactive install (creates .venv, generates cryptographic .env tokens,
#    downloads cloudflared into bin/, creates Desktop shortcut, and runs preflight check):
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

If the user ALSO provided their QQ number(s) to enable the QQ channel:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Mode dual -QQUsers "<USER_QQ_ID>" -BotQQ "<BOT_QQ_ID_OPTIONAL>"
```

#### Machine-Readable Verification (Run This to Confirm `ok: true`)
```powershell
.\.venv\Scripts\python.exe -X utf8 -m qq_agy_bridge.setup_wizard --doctor --json
```

#### Run Automated Test Suite (Optional Sanity Check)
```powershell
.\.venv\Scripts\pytest.exe -q
```

#### Launch the Desktop Glassmorphic Console for the User
```powershell
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "-X","utf8","-m","qq_agy_bridge.launcher" -WorkingDirectory (Get-Location)
```

---

### Case B: macOS / Linux

```bash
chmod +x ./scripts/setup.sh
./scripts/setup.sh

# Machine-readable health check:
./.venv/bin/python -X utf8 -m qq_agy_bridge.setup_wizard --doctor --json

# Start background/foreground server:
./.venv/bin/python -X utf8 -u -m qq_agy_bridge.main
```

---

## 4. Optional QQ (NapCat OneBot 11) Setup Notes

Only perform this if the user explicitly asks for QQ bot integration in addition to the Mobile Web App:
1. Ensure NapCatQQ (OneBot 11) has **Forward WebSocket Server** enabled on `127.0.0.1:3001` (matching `ONEBOT_WS_URL=ws://127.0.0.1:3001` in `.env`).
2. Set `ALLOWED_QQ_USERS=<user's personal QQ number>` in `.env` (strict private-chat whitelist; group chats are ignored by design for desktop security).
3. If NapCatNode portable is placed at `..\NapCatNode` (or configured via `NAPCAT_DIR=` in `.env`), the Windows Launcher will automatically start and monitor NapCatQQ alongside the bridge.

---

## 5. What to Report Back to the User When Done

Once `--doctor --json` returns `"ok": true` and the launcher/server is started, tell the user concisely:
1. **Desktop Shortcut Ready**: On Windows, `Antigravity 远程助理` is now on their Desktop and already running.
2. **How to Connect Phone in 3 Seconds**:
   - Click **「📱 手机端专属 App」** in the desktop console window to scan either:
     - **同 Wi-Fi 局域网 1ms 极速码** (recommended at home; if blocked by Windows Public Wi-Fi Firewall, click **「🛡 放行局域网防火墙」** right inside the QR dialog).
     - **4G/5G 外网加密隧道码** (works anywhere outside home via Cloudflare TLS tunnel).
   - Tap **"添加到主屏幕" (Add to Home Screen)** in the mobile browser menu to install it as a standalone full-screen App with persistent passwordless login.
