# AGENTS.md

When deploying or maintaining **反重力助手 (Antigravity Assistant)**:

1. For fresh installation on a user machine, read and execute `INSTALL_FOR_AI.md`.
2. Key modules under `src/qq_agy_bridge/`:
   - `conversations.py`: Discovers & parses 100% of local PC conversations across Antigravity IDE (`~/.gemini/antigravity`), OpenAI Codex Desktop (`~/.codex`), and Remote CLI (`~/.gemini/antigravity-cli`) using read-only SQLite (`mode=ro`) and streaming JSONL readers.
   - `desktop_tools.py`: Live Windows UIA prompt delivery & new-conversation creation for open Antigravity Desktop windows, Codex CLI discovery, DPI-aware fast JPEG screen capture, workspace file explorer, and `git status` / `git diff` inspector.
   - `beacon.py`: Zero-trust PBKDF2 + HMAC-SHA256 encrypted tunnel beacon & static iOS Standalone PWA publisher (`gh-pages` single-commit orphan sync).
   - `web_pwa.py` + `static/index.html`: Single-port HTTP + WebSocket OLED obsidian Mobile/iOS Standalone PWA server with per-slot isolation and live PC transcript streaming.
   - `launcher.py`: Windows 11 DWM translucent glassmorphic desktop hub with inline live QR pairing card.
   - `agy_session.py`: Persistent `agy --input-format stream-json --output-format stream-json` process manager with automatic model quota failover.
3. Never commit `.env`, `memory/user_profile.md`, `inbox/`, or `bin/` to Git.
