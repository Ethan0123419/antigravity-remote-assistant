"""Dedicated Mobile-First Progressive Web App (PWA) server for 反重力助手 (Antigravity Assistant).

Key Engineering Guarantees:
- Per-conversation isolated `AgySession` slots: viewing or switching to another conversation NEVER kills
  an in-flight command in the previous conversation, and switching back restores live streaming or completed turns.
- Persistent PC -> Mobile continuation mapping (`.continuation_map.json`): follow-up commands sent on mobile inside
  an Antigravity IDE or Codex Desktop chat stay permanently merged into that conversation's view.
- Universal AI Screenshot Extraction: captures `[SEND_IMAGE: ...]`, Markdown `![alt](<C:/...png>)` / `file:///...`,
  tool-generated screenshots in `transcript.jsonl` (`GENERIC` media), AND live CLI `logs/media/` tool outputs.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import mimetypes
import re
import secrets
import socket
import sys
import time
import uuid
from collections import OrderedDict
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .agy_session import AgySession, SessionManager
from .config import BridgeConfig
from .conversations import (
    ConversationDetail,
    _default_cli_dir,
    build_ide_handoff_context,
    get_transcript_signature,
    list_local_conversations,
    list_recent_workspaces,
    load_conversation_detail,
)
from .desktop_tools import (
    ALLOWED_IMAGE_EXTS,
    MODEL_ALIASES,
    PROJECT_INBOX_DIR,
    SEND_FILE_RE,
    SEND_IMAGE_RE,
    THOUGHT_TAG_RE,
    _resolve_inside_workspace,
    capture_desktop_fast_jpeg_sync,
    get_workspace_file_diff,
    inspect_workspace_git,
    is_safe_outbound_file,
    list_workspace_directory,
    prune_inbox,
    read_workspace_file_preview,
    sanitize_filename,
)
from .maintenance import discover_default_cache_targets, run_safe_cache_cleanup

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
ASSETS_DIR = PROJECT_ROOT / "assets"
CONTINUATION_MAP_FILE = PROJECT_ROOT / ".continuation_map.json"

DEFAULT_SLOT_KEY = "default-chat"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Match Markdown images pointing to local paths or file:// URIs (including Codex `<F:/path/img.png>` syntax)
MD_IMAGE_LOCAL_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*<?((?:file:///)?[A-Za-z]:[\\/][^)\r\n>]+)>?\s*\)",
    re.IGNORECASE,
)
# Match Markdown links pointing directly to local image files `[截图](<F:/.../shot.png>)` or `file:///...png`
MD_LINK_LOCAL_IMG_RE = re.compile(
    r"(?<!!)\[([^\]]+)\]\(\s*<?((?:file:///)?[A-Za-z]:[\\/][^)\r\n>]+?\.(?:png|jpg|jpeg|webp|gif|bmp))>?\s*\)",
    re.IGNORECASE,
)

SERVICE_WORKER_JS = """\
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""


def _local_uri_or_path_to_path(raw: str) -> Path:
    s = (raw or "").strip().strip("<>\"'")
    if s.startswith("file://"):
        p = unquote(urlparse(s).path)
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return Path(p)
    return Path(s).expanduser()


def get_lan_ipv4() -> str:
    """Detect primary LAN IPv4 address for instant Wi-Fi access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith(("127.", "169.254.", "198.18.")):
                return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.", "198.18.")):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def ensure_web_access_token(env_path: Path, current_token: str) -> str:
    """Ensure a strong cryptographic WEB_ACCESS_TOKEN exists, persisting to .env if missing."""
    tok = current_token.strip()
    if len(tok) >= 16 and tok != "change-me":
        return tok
    new_tok = secrets.token_urlsafe(18)
    try:
        if env_path.is_file():
            content = env_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            replaced = False
            for i, line in enumerate(lines):
                if line.strip().startswith("WEB_ACCESS_TOKEN="):
                    lines[i] = f"WEB_ACCESS_TOKEN={new_tok}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"WEB_ACCESS_TOKEN={new_tok}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist generated WEB_ACCESS_TOKEN to %s: %s", env_path, exc)
    return new_tok


def extract_pwa_media_and_text(text: str) -> tuple[str, list[Path], list[Path]]:
    """Strip <thought> tags and extract [SEND_IMAGE], [SEND_FILE], and local Markdown image/file tags."""
    cleaned = THOUGHT_TAG_RE.sub("", text or "")
    images: list[Path] = []
    files: list[Path] = []

    def _add_img(p: Path) -> bool:
        if is_safe_outbound_file(p, image_only=True):
            rp = p.resolve()
            if rp not in images:
                images.append(rp)
            return True
        return False

    def _on_send_img(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        if raw:
            _add_img(_local_uri_or_path_to_path(raw))
        return ""

    def _on_send_file(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        if raw:
            p = _local_uri_or_path_to_path(raw)
            if is_safe_outbound_file(p, image_only=False):
                rp = p.resolve()
                if rp not in files:
                    files.append(rp)
        return ""

    def _on_md_img(m: re.Match[str]) -> str:
        raw_target = m.group(2).strip()
        p = _local_uri_or_path_to_path(raw_target)
        if _add_img(p):
            return ""
        return m.group(0)

    def _on_md_link_img(m: re.Match[str]) -> str:
        label, raw_target = m.group(1).strip(), m.group(2).strip()
        p = _local_uri_or_path_to_path(raw_target)
        if _add_img(p):
            return label
        return m.group(0)

    cleaned = SEND_IMAGE_RE.sub(_on_send_img, cleaned)
    cleaned = SEND_FILE_RE.sub(_on_send_file, cleaned)
    cleaned = MD_IMAGE_LOCAL_RE.sub(_on_md_img, cleaned)
    cleaned = MD_LINK_LOCAL_IMG_RE.sub(_on_md_link_img, cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), images, files


def harvest_recent_cli_turn_images(
    cli_store_root: Path,
    conversation_id: str,
    since_epoch_sec: float,
) -> list[Path]:
    """Find screenshots saved by MCP / browser tools in `~/.gemini/antigravity-cli/brain/<cid>/.../media/`."""
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    media_dir = cli_store_root / "brain" / cid / ".system_generated" / "logs" / "media"
    if not media_dir.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        for f in media_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in ALLOWED_IMAGE_EXTS:
                continue
            st = f.stat()
            if st.st_mtime >= (since_epoch_sec - 1.5) and is_safe_outbound_file(f, image_only=True):
                found.append((st.st_mtime, f.resolve()))
    except OSError:
        return []
    found.sort(key=lambda t: t[0])
    return [p for _, p in found[-4:]]


class _UploadState:
    def __init__(self, path: Path, name: str, expected_size: int) -> None:
        self.path = path
        self.name = name
        self.expected_size = expected_size
        self.written = 0
        self.fh = path.open("wb")

    def write(self, data: bytes) -> None:
        if self.written + len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("上传文件超过 100MB 上限")
        self.fh.write(data)
        self.written += len(data)

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


class WebPwaServer:
    """Single-port HTTP + WebSocket server for 反重力助手."""

    def __init__(
        self,
        config: BridgeConfig,
        session_manager: SessionManager,
        access_token: str,
        *,
        ide_store_dir: Path | None = None,
        cli_store_dir: Path | None = None,
        codex_store_dir: Path | None = None,
        continuation_map_path: Path | None = None,
    ) -> None:
        self.config = config
        self.sessions = session_manager
        self.access_token = access_token
        self.ide_store_dir = ide_store_dir
        self.cli_store_dir = cli_store_dir
        self.codex_store_dir = codex_store_dir
        self._cont_map_path = continuation_map_path or (
            (cli_store_dir / "continuation_map.json") if cli_store_dir is not None else CONTINUATION_MAP_FILE
        )
        self._continuation_map: dict[str, str] = self._load_continuation_map()

        self._clients: set[ServerConnection] = set()
        self._media_registry: OrderedDict[str, Path] = OrderedDict()
        self._failed_auth: dict[str, list[float]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

        # Active slot currently displayed on mobile clients + per-slot isolated state
        self._active_slot: str = DEFAULT_SLOT_KEY
        self._slot_history: dict[str, list[dict[str, Any]]] = {DEFAULT_SLOT_KEY: []}
        self._slot_local_turns: dict[str, list[dict[str, Any]]] = {}
        self._slot_inflight: dict[str, dict[str, Any]] = {}
        self._slot_meta: dict[str, dict[str, Any]] = {
            DEFAULT_SLOT_KEY: {"conversation_id": "", "title": "", "store": ""}
        }
        self._cancelled_reply_ids: set[str] = set()
        self._ide_watch_sig: tuple[str, str, int, int] = ("", "", 0, 0)

    def _load_continuation_map(self) -> dict[str, str]:
        if self._cont_map_path.is_file():
            try:
                raw = json.loads(self._cont_map_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {str(k): str(v) for k, v in raw.items() if k and v}
            except Exception:
                pass
        return {}

    def _save_continuation_map(self) -> None:
        try:
            self._cont_map_path.parent.mkdir(parents=True, exist_ok=True)
            self._cont_map_path.write_text(
                json.dumps(self._continuation_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _session_for_slot(self, slot_key: str) -> AgySession:
        s = self.sessions.get(f"pwa:{slot_key}")
        s._ide_continuation_map = self._continuation_map
        return s

    @property
    def _history(self) -> list[dict[str, Any]]:
        return self._slot_history.setdefault(self._active_slot, [])

    def _append_slot_history(self, slot_key: str, item: dict[str, Any]) -> None:
        arr = self._slot_history.setdefault(slot_key, [])
        arr.append(item)
        if len(arr) > 300:
            self._slot_history[slot_key] = arr[-300:]
        loc = self._slot_local_turns.setdefault(slot_key, [])
        loc.append(item)
        if len(loc) > 100:
            self._slot_local_turns[slot_key] = loc[-100:]

    def _client_ip(self, connection: ServerConnection, headers: Headers | None = None) -> str:
        if headers is not None:
            cf_ip = headers.get("CF-Connecting-IP", "").strip()
            if cf_ip:
                return cf_ip
        peer = connection.remote_address
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        return "unknown"

    def _is_ip_locked(self, ip: str) -> bool:
        now = time.monotonic()
        attempts = [t for t in self._failed_auth.get(ip, []) if now - t < 60.0]
        if attempts:
            self._failed_auth[ip] = attempts
        else:
            self._failed_auth.pop(ip, None)
        return len(attempts) >= 8

    def _record_auth_failure(self, ip: str) -> None:
        now = time.monotonic()
        arr = [t for t in self._failed_auth.get(ip, []) if now - t < 60.0]
        arr.append(now)
        self._failed_auth[ip] = arr

    def _extract_token(self, query: str, headers: Headers | None) -> str:
        qs = parse_qs(query or "")
        tok = (qs.get("token") or [""])[0].strip()
        if tok:
            return tok
        if headers is not None:
            raw_cookie = headers.get("Cookie", "")
            if raw_cookie:
                try:
                    jar = SimpleCookie()
                    jar.load(raw_cookie)
                    morsel = jar.get("agy_pwa_token")
                    if morsel and morsel.value:
                        return morsel.value.strip()
                except Exception:
                    pass
        return ""

    def _verify_token(self, token: str, ip: str, *, record_failure: bool = True) -> bool:
        if self._is_ip_locked(ip):
            return False
        if not token or not self.access_token:
            if record_failure:
                self._record_auth_failure(ip)
            return False
        ok = hmac.compare_digest(token.strip(), self.access_token)
        if not ok and record_failure:
            self._record_auth_failure(ip)
        return ok

    def register_media(self, path: Path) -> str:
        resolved = path.resolve()
        for existing_id, existing_path in self._media_registry.items():
            if existing_path == resolved:
                self._media_registry.move_to_end(existing_id)
                return existing_id
        media_id = uuid.uuid4().hex
        self._media_registry[media_id] = resolved
        while len(self._media_registry) > 400:
            self._media_registry.popitem(last=False)
        return media_id

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        raw = json.dumps(payload, ensure_ascii=False)
        stale: list[ServerConnection] = []
        for client in list(self._clients):
            try:
                await client.send(raw)
            except Exception:
                stale.append(client)
        for s in stale:
            self._clients.discard(s)

    def _http_response(
        self,
        status: int,
        reason: str,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        hdrs = Headers(
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("X-Content-Type-Options", "nosniff"),
            ]
        )
        if extra_headers:
            for k, v in extra_headers.items():
                hdrs[k] = v
        return Response(status, reason, hdrs, body)

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        parsed = urlsplit(request.path)
        path = parsed.path

        if path == "/ws":
            return None

        if request.method not in {"GET", "HEAD"}:
            return self._http_response(405, "Method Not Allowed", b"Method Not Allowed", "text/plain")

        if path in {"/", "/index.html"}:
            html_file = STATIC_DIR / "index.html"
            if not html_file.is_file():
                return self._http_response(404, "Not Found", b"Missing index.html", "text/plain")
            body = await asyncio.to_thread(html_file.read_bytes)
            extra_hdrs = {"Cache-Control": "no-cache"}
            ip = self._client_ip(connection, request.headers)
            cand_tok = (parse_qs(parsed.query).get("token") or [""])[0].strip()
            if cand_tok and self._verify_token(cand_tok, ip, record_failure=False):
                extra_hdrs["Set-Cookie"] = (
                    f"agy_pwa_token={cand_tok}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                )
            return self._http_response(
                200,
                "OK",
                body,
                "text/html; charset=utf-8",
                extra_hdrs,
            )

        if path == "/manifest.json":
            ip = self._client_ip(connection, request.headers)
            cand_tok = self._extract_token(parsed.query, request.headers)
            start_url = "/"
            if cand_tok and self._verify_token(cand_tok, ip, record_failure=False):
                start_url = f"/?token={quote(cand_tok)}"
            manifest = {
                "name": "反重力助手",
                "short_name": "反重力助手",
                "description": "专属远程电脑操控与全会话 AI 编程中枢",
                "start_url": start_url,
                "display": "standalone",
                "background_color": "#212121",
                "theme_color": "#212121",
                "orientation": "portrait-primary",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            }
            body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            return self._http_response(
                200,
                "OK",
                body,
                "application/manifest+json",
                {"Cache-Control": "private, no-cache"},
            )

        if path == "/sw.js":
            return self._http_response(
                200,
                "OK",
                SERVICE_WORKER_JS.encode("utf-8"),
                "application/javascript; charset=utf-8",
                {"Cache-Control": "no-cache"},
            )

        if path in {"/icon-192.png", "/icon-512.png"}:
            icon_file = ASSETS_DIR / path.lstrip("/")
            if icon_file.is_file():
                body = await asyncio.to_thread(icon_file.read_bytes)
                return self._http_response(200, "OK", body, "image/png", {"Cache-Control": "max-age=86400"})

        if path == "/favicon.ico":
            ico_file = ASSETS_DIR / "app.ico"
            if ico_file.is_file():
                body = await asyncio.to_thread(ico_file.read_bytes)
                return self._http_response(200, "OK", body, "image/x-icon", {"Cache-Control": "max-age=86400"})

        if path.startswith("/dl/"):
            ip = self._client_ip(connection, request.headers)
            tok = self._extract_token(parsed.query, request.headers)
            if not self._verify_token(tok, ip):
                return self._http_response(401, "Unauthorized", b"Unauthorized", "text/plain")

            media_id = path.removeprefix("/dl/").strip("/")
            target = self._media_registry.get(media_id)
            if target is None or not is_safe_outbound_file(target, image_only=False):
                return self._http_response(404, "Not Found", b"File Not Found", "text/plain")

            body = await asyncio.to_thread(target.read_bytes)
            mime, _ = mimetypes.guess_type(str(target))
            mime = mime or "application/octet-stream"
            disp_type = "inline" if mime.startswith("image/") else "attachment"
            encoded_name = quote(target.name)
            return self._http_response(
                200,
                "OK",
                body,
                mime,
                {
                    "Content-Disposition": f"{disp_type}; filename*=UTF-8''{encoded_name}",
                    "Cache-Control": "private, max-age=3600",
                },
            )

        return self._http_response(404, "Not Found", b"Not Found", "text/plain")

    async def _handle_ws(self, ws: ServerConnection) -> None:
        req = ws.request
        ip = self._client_ip(ws, req.headers if req else None)
        parsed = urlsplit(req.path if req else "")
        tok = self._extract_token(parsed.query, req.headers if req else None)

        if not self._verify_token(tok, ip):
            logger.warning("Rejected unauthorized Web PWA WS connection from %s", ip)
            await ws.close(code=4001, reason="Unauthorized")
            return

        self._clients.add(ws)
        logger.info("反重力助手客户端已连接 (%s)", ip)
        uploads: dict[str, _UploadState] = {}
        live_screen_task: asyncio.Task[None] | None = None

        slot = self._active_slot
        session = self._session_for_slot(slot)
        meta = self._slot_meta.get(slot, {})
        active_cid = meta.get("conversation_id") or session.linked_ide_id or session.conversation_id
        active_store = meta.get("store") or (
            session.linked_store if session.linked_ide_id else ("cli" if session.conversation_id else "")
        )
        inflight = self._slot_inflight.get(slot)
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "slot": slot,
                    "model": session.model or "default",
                    "workspace": str(session.workspace),
                    "active_conversation_id": active_cid,
                    "active_title": meta.get("title") or session.active_title or "",
                    "active_store": active_store,
                    "busy": inflight is not None,
                    "inflight": inflight,
                    "cooldowns": session.failover_pool.cooldown_summary(),
                    "history": self._slot_history.get(slot, []),
                },
                ensure_ascii=False,
            )
        )

        def _stop_live_screen() -> None:
            nonlocal live_screen_task
            if live_screen_task is not None:
                live_screen_task.cancel()
                live_screen_task = None

        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                mtype = msg.get("type")

                if mtype == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": msg.get("ts")}))

                elif mtype == "desktop_frame":
                    try:
                        shot = await asyncio.to_thread(capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR)
                        mid = self.register_media(shot)
                        await ws.send(
                            json.dumps(
                                {"type": "desktop_frame", "media_id": mid, "ts": int(time.time() * 1000)},
                                ensure_ascii=False,
                            )
                        )
                    except Exception as exc:
                        await ws.send(json.dumps({"type": "toast", "level": "err", "text": f"截屏失败：{exc}"}))

                elif mtype == "live_screen":
                    enable = bool(msg.get("enable"))
                    _stop_live_screen()
                    if enable:
                        interval = max(1.0, min(5.0, float(msg.get("interval_ms") or 1500) / 1000.0))

                        async def _stream_screen() -> None:
                            while True:
                                try:
                                    shot = await asyncio.to_thread(
                                        capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR, 1366, 74
                                    )
                                    mid = self.register_media(shot)
                                    await ws.send(
                                        json.dumps(
                                            {
                                                "type": "desktop_frame",
                                                "media_id": mid,
                                                "ts": int(time.time() * 1000),
                                            }
                                        )
                                    )
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    break
                                await asyncio.sleep(interval)

                        live_screen_task = asyncio.create_task(_stream_screen())

                elif mtype == "upload_init":
                    uid = str(msg.get("upload_id") or uuid.uuid4().hex)
                    safe_name = sanitize_filename(str(msg.get("name") or "upload.bin"))
                    size = int(msg.get("size") or 0)
                    if size > MAX_UPLOAD_BYTES:
                        await ws.send(json.dumps({"type": "toast", "level": "err", "text": "文件超过 100MB 上限"}))
                        continue
                    PROJECT_INBOX_DIR.mkdir(parents=True, exist_ok=True)
                    dest = PROJECT_INBOX_DIR / f"pwa_{int(time.time())}_{uuid.uuid4().hex[:5]}_{safe_name}"
                    uploads[uid] = _UploadState(dest, safe_name, size)
                    await ws.send(json.dumps({"type": "upload_ack", "upload_id": uid}))

                elif mtype == "upload_chunk":
                    uid = str(msg.get("upload_id") or "")
                    state = uploads.get(uid)
                    if state is None:
                        continue
                    try:
                        chunk_bytes = base64.b64decode(str(msg.get("data") or ""))
                        await asyncio.to_thread(state.write, chunk_bytes)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "upload_progress",
                                    "upload_id": uid,
                                    "written": state.written,
                                    "total": state.expected_size,
                                }
                            )
                        )
                    except Exception as exc:
                        state.close()
                        uploads.pop(uid, None)
                        await ws.send(json.dumps({"type": "toast", "level": "err", "text": f"上传出错：{exc}"}))

                elif mtype == "upload_complete":
                    uid = str(msg.get("upload_id") or "")
                    state = uploads.pop(uid, None)
                    if state is None:
                        continue
                    state.close()
                    prune_inbox(PROJECT_INBOX_DIR, keep=40)
                    mid = self.register_media(state.path)
                    is_img = state.path.suffix.lower() in ALLOWED_IMAGE_EXTS
                    await ws.send(
                        json.dumps(
                            {
                                "type": "upload_done",
                                "upload_id": uid,
                                "media_id": mid,
                                "name": state.name,
                                "path": str(state.path),
                                "is_image": is_img,
                            },
                            ensure_ascii=False,
                        )
                    )

                elif mtype == "cancel":
                    cur_slot = self._active_slot
                    inf = self._slot_inflight.get(cur_slot)
                    if inf and inf.get("msg_id"):
                        self._cancelled_reply_ids.add(str(inf["msg_id"]))
                    killed = await self._session_for_slot(cur_slot).cancel_current()
                    note = "已停止当前生成。" if killed else "当前没有运行中的任务。"
                    await ws.send(json.dumps({"type": "toast", "level": "info", "text": note}, ensure_ascii=False))

                elif mtype == "list_conversations":
                    await self._handle_list_conversations(ws)

                elif mtype == "select_conversation":
                    max_m = int(msg.get("max_messages") or 200)
                    t = asyncio.create_task(
                        self._handle_select_conversation(
                            ws,
                            str(msg.get("conversation_id") or ""),
                            str(msg.get("store") or "auto"),
                            only_refresh=bool(msg.get("only_refresh", False)),
                            max_messages=max(50, min(3000, max_m)),
                        )
                    )
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

                elif mtype == "refresh_conversation":
                    cur_slot = self._active_slot
                    # Never clobber an actively streaming reply on mobile wakeup!
                    if cur_slot in self._slot_inflight:
                        continue
                    meta_cur = self._slot_meta.get(cur_slot, {})
                    target_cid = str(msg.get("conversation_id") or meta_cur.get("conversation_id") or "")
                    target_store = str(msg.get("store") or meta_cur.get("store") or "auto")
                    if target_cid:
                        t = asyncio.create_task(
                            self._handle_select_conversation(
                                ws, target_cid, target_store, only_refresh=True, max_messages=200
                            )
                        )
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)

                elif mtype == "new_conversation":
                    t = asyncio.create_task(
                        self._handle_new_conversation(ws, str(msg.get("workspace") or ""))
                    )
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

                elif mtype == "workspace_inspect":
                    await self._handle_workspace_inspect(ws, str(msg.get("rel_dir") or ""))

                elif mtype == "workspace_diff":
                    await self._handle_workspace_diff(ws, str(msg.get("path") or ""))

                elif mtype == "workspace_file":
                    await self._handle_workspace_file(ws, str(msg.get("path") or ""))

                elif mtype == "chat":
                    t = asyncio.create_task(self._handle_chat(msg))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
        finally:
            _stop_live_screen()
            for st in uploads.values():
                st.close()
            self._clients.discard(ws)
            logger.info("反重力助手客户端已断开 (%s)", ip)

    def _safe_file_size(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    async def _handle_workspace_inspect(self, ws: ServerConnection, rel_dir: str) -> None:
        session = self._session_for_slot(self._active_slot)
        git_info, dir_info = await asyncio.gather(
            asyncio.to_thread(inspect_workspace_git, session.workspace),
            asyncio.to_thread(list_workspace_directory, session.workspace, rel_dir),
        )
        await ws.send(
            json.dumps(
                {
                    "type": "workspace_state",
                    "workspace": str(session.workspace),
                    "git": git_info,
                    "dir": dir_info,
                },
                ensure_ascii=False,
            )
        )

    async def _handle_workspace_diff(self, ws: ServerConnection, rel_path: str) -> None:
        session = self._session_for_slot(self._active_slot)
        diff_res = await asyncio.to_thread(get_workspace_file_diff, session.workspace, rel_path)
        await ws.send(json.dumps({"type": "workspace_diff", **diff_res}, ensure_ascii=False))

    async def _handle_workspace_file(self, ws: ServerConnection, rel_path: str) -> None:
        session = self._session_for_slot(self._active_slot)
        prev = await asyncio.to_thread(read_workspace_file_preview, session.workspace, rel_path)
        if prev.get("ok"):
            resolved = _resolve_inside_workspace(session.workspace, rel_path)
            if resolved is not None and is_safe_outbound_file(resolved, image_only=False):
                prev["media_id"] = self.register_media(resolved)
        await ws.send(json.dumps({"type": "workspace_file", **prev}, ensure_ascii=False))

    def _convert_detail_to_pwa_history(self, detail: ConversationDetail, model_label: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        origin_tag = (
            "Antigravity 电脑端"
            if detail.store == "ide"
            else ("Codex 电脑端" if detail.store == "codex" else model_label)
        )
        for idx, m in enumerate(detail.messages):
            if m.role == "user":
                atts = []
                for p in m.image_paths:
                    if is_safe_outbound_file(p, image_only=True):
                        atts.append({"media_id": self.register_media(p), "name": p.name, "is_image": True})
                out.append(
                    {
                        "role": "user",
                        "id": f"hist-u-{detail.conversation_id[:8]}-{m.step_index}-{idx}",
                        "text": m.text,
                        "attachments": atts,
                        "ts": m.ts_ms,
                    }
                )
            else:
                clean_txt, extra_imgs, extra_files = extract_pwa_media_and_text(m.text)
                imgs = []
                seen_paths: set[Path] = set()
                for p in [*m.image_paths, *extra_imgs]:
                    if is_safe_outbound_file(p, image_only=True):
                        rp = p.resolve()
                        if rp not in seen_paths:
                            seen_paths.add(rp)
                            imgs.append({"media_id": self.register_media(rp), "name": rp.name})
                fls = [
                    {"media_id": self.register_media(p), "name": p.name, "size": self._safe_file_size(p)}
                    for p in extra_files
                ]
                out.append(
                    {
                        "role": "assistant",
                        "id": f"hist-a-{detail.conversation_id[:8]}-{m.step_index}-{idx}",
                        "text": clean_txt,
                        "images": imgs,
                        "files": fls,
                        "model": origin_tag,
                        "ts": m.ts_ms,
                    }
                )
        return out

    async def _handle_list_conversations(self, ws: ServerConnection) -> None:
        session = self._session_for_slot(self._active_slot)
        meta = self._slot_meta.get(self._active_slot, {})
        items = await asyncio.to_thread(
            list_local_conversations,
            500,
            self.ide_store_dir,
            self.cli_store_dir,
            self.codex_store_dir,
        )
        # Hide internal CLI continuation child conversations that are already merged into their parent PC chat
        hidden_cont_ids = set(self._continuation_map.values())
        visible_items = []
        for it in items:
            if it.store == "cli" and it.conversation_id in hidden_cont_ids:
                continue
            slot_candidate = f"{it.store}:{it.conversation_id}"
            d = it.to_dict()
            if slot_candidate in self._slot_inflight:
                d["running"] = True
            visible_items.append(d)

        workspaces = await asyncio.to_thread(
            list_recent_workspaces,
            session.workspace,
            self.ide_store_dir,
            self.cli_store_dir,
            self.codex_store_dir,
        )
        await ws.send(
            json.dumps(
                {
                    "type": "conversations_list",
                    "active_id": meta.get("conversation_id") or session.linked_ide_id or session.conversation_id,
                    "current_workspace": str(session.workspace),
                    "items": visible_items,
                    "workspaces": workspaces,
                },
                ensure_ascii=False,
            )
        )

    async def _handle_select_conversation(
        self,
        ws: ServerConnection,
        conversation_id: str,
        store: str,
        *,
        only_refresh: bool = False,
        max_messages: int = 200,
    ) -> None:
        detail = await asyncio.to_thread(
            load_conversation_detail,
            conversation_id,
            store,
            max_messages,
            self.ide_store_dir,
            self.cli_store_dir,
            self.codex_store_dir,
        )
        if detail is None:
            await ws.send(json.dumps({"type": "toast", "level": "err", "text": "未找到该对话记录"}, ensure_ascii=False))
            return

        slot_key = f"{detail.store}:{detail.conversation_id}"
        session = self._session_for_slot(slot_key)
        slot_is_running = slot_key in self._slot_inflight

        if not only_refresh:
            self._active_slot = slot_key
            self._slot_meta[slot_key] = {
                "conversation_id": detail.conversation_id,
                "title": detail.title,
                "store": detail.store,
            }
            # Only initialize/attach the slot's AgySession if it is NOT currently executing a turn!
            if not slot_is_running:
                if detail.store == "cli":
                    if session.conversation_id != detail.conversation_id:
                        await session.attach_cli_conversation(
                            detail.conversation_id, workspace=detail.workspace, title=detail.title
                        )
                    self._ide_watch_sig = ("", "", 0, 0)
                else:
                    handoff = build_ide_handoff_context(detail)
                    if session.linked_ide_id != detail.conversation_id:
                        await session.attach_ide_conversation(
                            detail.conversation_id,
                            handoff_context=handoff,
                            workspace=detail.workspace,
                            title=detail.title,
                            store=detail.store,
                        )
                    elif not session.conversation_id:
                        session._pending_handoff_context = handoff
                    sig = await asyncio.to_thread(
                        get_transcript_signature,
                        detail.conversation_id,
                        detail.store,
                        self.ide_store_dir,
                        self.cli_store_dir,
                        self.codex_store_dir,
                    )
                    self._ide_watch_sig = (detail.store, detail.conversation_id, sig[0], sig[1])

        # If this slot already has an in-memory turn running, keep its live history intact!
        if slot_is_running and self._slot_history.get(slot_key):
            loaded_history = self._slot_history[slot_key]
        else:
            loaded_history = self._convert_detail_to_pwa_history(detail, session.model or "default")
            # Merge any linked mobile continuation turns on top of the PC conversation!
            if detail.store in {"ide", "codex"}:
                cont_cid = (
                    self._continuation_map.get(f"{detail.store}:{detail.conversation_id}")
                    or self._continuation_map.get(detail.conversation_id)
                    or session.conversation_id
                )
                if cont_cid:
                    cont_detail = await asyncio.to_thread(
                        load_conversation_detail,
                        cont_cid,
                        "cli",
                        100,
                        self.ide_store_dir,
                        self.cli_store_dir,
                        self.codex_store_dir,
                    )
                    if cont_detail and cont_detail.messages:
                        loaded_history.extend(
                            self._convert_detail_to_pwa_history(cont_detail, session.model or "default")
                        )
                    elif self._slot_local_turns.get(slot_key):
                        loaded_history.extend(self._slot_local_turns[slot_key])
                elif self._slot_local_turns.get(slot_key):
                    loaded_history.extend(self._slot_local_turns[slot_key])
            elif self._slot_local_turns.get(slot_key) and not loaded_history:
                loaded_history.extend(self._slot_local_turns[slot_key])
            self._slot_history[slot_key] = loaded_history

        await self._broadcast(
            {
                "type": "conversation_loaded",
                "slot": slot_key,
                "conversation_id": detail.conversation_id,
                "title": detail.title,
                "store": detail.store,
                "running": detail.running or slot_is_running,
                "busy": slot_is_running,
                "inflight": self._slot_inflight.get(slot_key),
                "workspace": str(session.workspace),
                "silent": only_refresh,
                "has_more": detail.has_more,
                "total_messages": detail.total_messages,
                "history": loaded_history,
            }
        )

    async def _handle_new_conversation(self, ws: ServerConnection, workspace: str) -> None:
        new_slot = f"new:{uuid.uuid4().hex[:8]}"
        session = self._session_for_slot(new_slot)
        try:
            await session.start_new_conversation(workspace=workspace)
        except FileNotFoundError as exc:
            await ws.send(json.dumps({"type": "toast", "level": "err", "text": str(exc)}, ensure_ascii=False))
            return

        self._active_slot = new_slot
        self._slot_history[new_slot] = []
        self._slot_meta[new_slot] = {"conversation_id": "", "title": "新对话", "store": ""}
        self._ide_watch_sig = ("", "", 0, 0)

        await self._broadcast({"type": "clear_history", "slot": new_slot})
        await self._broadcast(
            {
                "type": "meta_update",
                "slot": new_slot,
                "model": session.model or "default",
                "workspace": str(session.workspace),
                "active_conversation_id": "",
                "active_title": "反重力助手",
                "active_store": "",
                "busy": False,
            }
        )

    async def _handle_chat(self, msg: dict[str, Any]) -> None:
        raw_text = str(msg.get("text") or "").strip()
        client_id = str(msg.get("client_id") or f"u-{uuid.uuid4().hex[:10]}")
        attachments = msg.get("attachments") or []
        if not raw_text and not attachments:
            return

        chat_slot = self._active_slot
        session = self._session_for_slot(chat_slot)

        user_entry = {
            "role": "user",
            "id": client_id,
            "text": raw_text,
            "attachments": [
                {
                    "media_id": a.get("media_id"),
                    "name": a.get("name"),
                    "is_image": bool(a.get("is_image")),
                }
                for a in attachments
                if isinstance(a, dict)
            ],
            "ts": int(time.time() * 1000),
        }
        self._append_slot_history(chat_slot, user_entry)
        if self._active_slot == chat_slot:
            await self._broadcast({"type": "user_msg", "slot": chat_slot, "item": user_entry})

        if raw_text.startswith("/") and not attachments:
            handled = await self._handle_slash_command(chat_slot, session, raw_text)
            if handled:
                return

        prompt_parts: list[str] = []
        if raw_text:
            prompt_parts.append(raw_text)
        for att in attachments:
            if not isinstance(att, dict):
                continue
            mid = str(att.get("media_id") or "")
            local_p = self._media_registry.get(mid)
            if local_p and local_p.is_file():
                kind = "图片" if att.get("is_image") else "文件"
                prompt_parts.append(f"[用户上传了{kind} {local_p.name}，已保存在本地，可直接读取或处理: {local_p}]")
        prompt_text = "\n".join(prompt_parts).strip()
        if not prompt_text:
            return

        reply_id = f"a-{uuid.uuid4().hex[:10]}"
        inflight_state: dict[str, Any] = {
            "msg_id": reply_id,
            "slot": chat_slot,
            "model": session.model or "default",
            "text": "",
            "status": "正在思考...",
        }
        self._slot_inflight[chat_slot] = inflight_state
        if self._active_slot == chat_slot:
            await self._broadcast(
                {
                    "type": "stream_start",
                    "slot": chat_slot,
                    "msg_id": reply_id,
                    "model": session.model or "default",
                }
            )

        async def _on_delta(delta: str) -> None:
            inflight_state["text"] = str(inflight_state.get("text") or "") + delta
            inflight_state["status"] = ""
            if self._active_slot == chat_slot:
                await self._broadcast(
                    {"type": "stream_delta", "slot": chat_slot, "msg_id": reply_id, "delta": delta}
                )

        async def _on_reset() -> None:
            inflight_state["text"] = ""
            if self._active_slot == chat_slot:
                await self._broadcast({"type": "stream_reset", "slot": chat_slot, "msg_id": reply_id})

        async def _on_tool(tool_name: str) -> None:
            tlow = tool_name.lower()
            if "screenshot" in tlow or "snapshot" in tlow:
                hint = "正在截取电脑画面..."
            elif "mcp" in tlow or "command" in tlow:
                hint = "正在操作电脑..."
            else:
                hint = "正在检索与处理..."
            inflight_state["status"] = hint
            if self._active_slot == chat_slot:
                await self._broadcast(
                    {"type": "stream_status", "slot": chat_slot, "msg_id": reply_id, "status": hint}
                )

        turn_start_sec = time.time()
        partial_before_finish = ""
        try:
            result = await session.ask(
                prompt_text,
                on_delay_notice=None,
                on_stream_delta=_on_delta,
                on_stream_reset=_on_reset,
                on_tool_start=_on_tool,
            )
        finally:
            partial_before_finish = str(inflight_state.get("text") or "")
            if self._slot_inflight.get(chat_slot) is inflight_state:
                self._slot_inflight.pop(chat_slot, None)
            if session.linked_ide_id and session.conversation_id:
                map_key = f"{session.linked_store}:{session.linked_ide_id}" if session.linked_store else session.linked_ide_id
                self._continuation_map[map_key] = session.conversation_id
                self._continuation_map[session.linked_ide_id] = session.conversation_id
                await asyncio.to_thread(self._save_continuation_map)

        was_user_cancelled = reply_id in self._cancelled_reply_ids
        self._cancelled_reply_ids.discard(reply_id)

        if result.status == "SUCCESS":
            clean_text, img_paths, file_paths = extract_pwa_media_and_text(result.response or "")
            if result.switched_from_model and result.active_model:
                note = f"（注：{result.switched_from_model} 额度已满，已自动切到 {result.active_model}）"
                clean_text = f"{clean_text}\n\n{note}" if clean_text else note
        elif was_user_cancelled:
            clean_resp, img_paths, file_paths = extract_pwa_media_and_text(result.response or partial_before_finish)
            clean_text = f"{clean_resp}\n\n⏹ 已停止生成。".strip() if clean_resp else "⏹ 已停止生成。"
        else:
            clean_resp, img_paths, file_paths = extract_pwa_media_and_text(result.response or partial_before_finish)
            err = result.error or f"执行未成功 ({result.status})"
            clean_text = f"{clean_resp}\n\n刚才这步出了点状况：\n{err}".strip()

        # Also harvest any screenshots captured by MCP / browser tools during this turn
        cli_root = self.cli_store_dir if self.cli_store_dir is not None else _default_cli_dir()
        harvested = await asyncio.to_thread(
            harvest_recent_cli_turn_images,
            cli_root,
            result.conversation_id or session.conversation_id,
            turn_start_sec,
        )
        for hp in harvested:
            if hp not in img_paths:
                img_paths.append(hp)

        if result.status == "SUCCESS" and not clean_text and not img_paths and not file_paths:
            clean_text = "已完成。"

        # Update slot metadata if this was a brand-new conversation that just received a CLI ID
        if chat_slot.startswith("new:") or chat_slot == DEFAULT_SLOT_KEY:
            if session.conversation_id:
                meta_cur = self._slot_meta.setdefault(chat_slot, {})
                if not meta_cur.get("conversation_id"):
                    meta_cur["conversation_id"] = session.conversation_id
                    meta_cur["store"] = "cli"
                    meta_cur["title"] = raw_text[:36] or "新对话"

        img_items = [{"media_id": self.register_media(p), "name": p.name} for p in img_paths]
        file_items = [
            {"media_id": self.register_media(p), "name": p.name, "size": self._safe_file_size(p)}
            for p in file_paths
        ]

        assistant_entry = {
            "role": "assistant",
            "id": reply_id,
            "text": clean_text,
            "images": img_items,
            "files": file_items,
            "model": result.active_model or session.model or "default",
            "ts": int(time.time() * 1000),
        }
        self._append_slot_history(chat_slot, assistant_entry)
        if self._active_slot == chat_slot:
            await self._broadcast({"type": "stream_end", "slot": chat_slot, "item": assistant_entry})
        else:
            meta_done = self._slot_meta.get(chat_slot, {})
            done_title = meta_done.get("title") or "后台会话"
            await self._broadcast(
                {
                    "type": "toast",
                    "level": "info",
                    "text": f"✓ 「{done_title[:18]}」已完成回复",
                }
            )

    async def _send_sys_reply(
        self, slot_key: str, text: str, model: str = "", images: list[dict] | None = None
    ) -> None:
        entry = {
            "role": "assistant",
            "id": f"s-{uuid.uuid4().hex[:10]}",
            "text": text,
            "images": images or [],
            "files": [],
            "model": model,
            "ts": int(time.time() * 1000),
        }
        self._append_slot_history(slot_key, entry)
        if self._active_slot == slot_key:
            await self._broadcast({"type": "stream_end", "slot": slot_key, "item": entry})

    async def _handle_slash_command(self, slot_key: str, session: AgySession, raw_text: str) -> bool:
        parts = raw_text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"/screen", "/shot"}:
            shot = await asyncio.to_thread(capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR)
            mid = self.register_media(shot)
            await self._send_sys_reply(
                slot_key, "当前电脑桌面截图：", session.model, images=[{"media_id": mid, "name": shot.name}]
            )
            return True

        if cmd in {"/new", "/reset"}:
            new_slot = f"new:{uuid.uuid4().hex[:8]}"
            new_sess = self._session_for_slot(new_slot)
            await new_sess.start_new_conversation(workspace=str(session.workspace))
            self._active_slot = new_slot
            self._slot_history[new_slot] = []
            self._slot_meta[new_slot] = {"conversation_id": "", "title": "新对话", "store": ""}
            self._ide_watch_sig = ("", "", 0, 0)
            await self._broadcast({"type": "clear_history", "slot": new_slot})
            await self._broadcast(
                {
                    "type": "meta_update",
                    "slot": new_slot,
                    "model": new_sess.model or "default",
                    "workspace": str(new_sess.workspace),
                    "active_conversation_id": "",
                    "active_title": "反重力助手",
                    "active_store": "",
                    "busy": False,
                }
            )
            return True

        if cmd == "/model":
            if not arg:
                chain = " -> ".join(session.failover_pool.models[:5])
                await self._send_sys_reply(
                    slot_key,
                    f"当前模型：`{session.model or '默认'}`\n容灾链：{chain}\n\n"
                    "可点击顶栏标题切换模型，或输入：`/model fast | balanced | smart | gemini`",
                    session.model,
                )
                return True
            target = MODEL_ALIASES.get(arg.lower(), arg)
            await session.set_model(target)
            await self._broadcast(
                {"type": "meta_update", "slot": slot_key, "model": session.model, "workspace": str(session.workspace)}
            )
            await self._send_sys_reply(slot_key, f"已切换到 `{target}`，上下文继续保留。", session.model)
            return True

        if cmd == "/clean":
            targets = discover_default_cache_targets(PROJECT_ROOT)
            rep = await asyncio.to_thread(
                run_safe_cache_cleanup, targets, force_all_expired_zero=(arg.lower() == "all")
            )
            await self._send_sys_reply(slot_key, rep.format_human(), session.model)
            return True

        if cmd == "/status":
            cooling = session.failover_pool.cooldown_summary()
            cool_str = ", ".join(f"{m}({mins}m)" for m, mins in cooling) if cooling else "全部正常可用"
            await self._send_sys_reply(
                slot_key,
                f"**反重力助手状态**\n"
                f"- 当前模型：`{session.model or '默认'}`\n"
                f"- 额度池：{cool_str}\n"
                f"- 会话轮次：{session.num_turns}\n"
                f"- 工作目录：`{session.workspace}`",
                session.model,
            )
            return True

        if cmd == "/cd" and arg:
            target_dir = Path(arg.strip("\"'"))
            if not target_dir.is_absolute():
                target_dir = session.workspace / target_dir
            try:
                await session.set_workspace(target_dir)
                await self._broadcast(
                    {
                        "type": "meta_update",
                        "slot": slot_key,
                        "model": session.model,
                        "workspace": str(session.workspace),
                    }
                )
                await self._send_sys_reply(slot_key, f"已切换工作目录：\n`{session.workspace}`", session.model)
            except FileNotFoundError as exc:
                await self._send_sys_reply(slot_key, str(exc), session.model)
            return True

        return False

    async def _ide_watch_loop(self) -> None:
        """Auto-push live PC Antigravity / Codex transcript updates when viewing a PC chat."""
        while True:
            interval = getattr(self, "ide_watch_interval", 2.5)
            await asyncio.sleep(interval)
            try:
                if not self._clients:
                    continue
                cur_slot = self._active_slot
                if cur_slot in self._slot_inflight:
                    continue
                session = self._session_for_slot(cur_slot)
                ide_cid = session.linked_ide_id
                watch_store = session.linked_store or "ide"
                if not ide_cid or session.is_busy:
                    continue

                sig = await asyncio.to_thread(
                    get_transcript_signature,
                    ide_cid,
                    watch_store,
                    self.ide_store_dir,
                    self.cli_store_dir,
                    self.codex_store_dir,
                )
                if sig == (0, 0) or (watch_store, ide_cid, sig[0], sig[1]) == self._ide_watch_sig:
                    continue

                self._ide_watch_sig = (watch_store, ide_cid, sig[0], sig[1])
                detail = await asyncio.to_thread(
                    load_conversation_detail,
                    ide_cid,
                    watch_store,
                    200,
                    self.ide_store_dir,
                    self.cli_store_dir,
                    self.codex_store_dir,
                )
                if detail is None or self._active_slot != cur_slot or cur_slot in self._slot_inflight:
                    continue

                if not session.conversation_id:
                    session._pending_handoff_context = build_ide_handoff_context(detail)

                loaded_history = self._convert_detail_to_pwa_history(detail, session.model or "default")
                cont_cid = (
                    self._continuation_map.get(f"{detail.store}:{detail.conversation_id}")
                    or self._continuation_map.get(detail.conversation_id)
                    or session.conversation_id
                )
                if cont_cid:
                    cont_detail = await asyncio.to_thread(
                        load_conversation_detail,
                        cont_cid,
                        "cli",
                        100,
                        self.ide_store_dir,
                        self.cli_store_dir,
                        self.codex_store_dir,
                    )
                    if cont_detail and cont_detail.messages:
                        loaded_history.extend(
                            self._convert_detail_to_pwa_history(cont_detail, session.model or "default")
                        )

                self._slot_history[cur_slot] = loaded_history
                await self._broadcast(
                    {
                        "type": "conversation_loaded",
                        "slot": cur_slot,
                        "conversation_id": detail.conversation_id,
                        "title": detail.title,
                        "store": detail.store,
                        "running": detail.running,
                        "busy": False,
                        "workspace": str(session.workspace),
                        "silent": True,
                        "has_more": detail.has_more,
                        "total_messages": detail.total_messages,
                        "history": loaded_history,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("PC transcript watch loop error: %s", exc)

    async def _cache_clean_loop(self) -> None:
        if self.config.cache_clean_interval_hours <= 0.0:
            return
        interval_sec = max(300.0, self.config.cache_clean_interval_hours * 3600.0)
        await asyncio.sleep(20.0)
        while True:
            try:
                targets = discover_default_cache_targets(PROJECT_ROOT)
                rep = await asyncio.to_thread(run_safe_cache_cleanup, targets)
                if rep.deleted_files > 0:
                    logger.info(
                        "Periodic cache cleanup removed %d expired files (%.2f MB)",
                        rep.deleted_files,
                        rep.freed_mb,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Cache cleaner warning: %s", exc)
            await asyncio.sleep(interval_sec)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        logger.info(
            "Starting 反重力助手 server at http://%s:%d (LAN: http://%s:%d)",
            self.config.web_pwa_host,
            self.config.web_pwa_port,
            get_lan_ipv4(),
            self.config.web_pwa_port,
        )
        if self.config.agy_prewarm:
            pw_task = asyncio.create_task(self._session_for_slot(DEFAULT_SLOT_KEY).prewarm())
            self._tasks.add(pw_task)
            pw_task.add_done_callback(self._tasks.discard)

        for coro in (self._ide_watch_loop(), self._cache_clean_loop()):
            bg_task = asyncio.create_task(coro)
            self._tasks.add(bg_task)
            bg_task.add_done_callback(self._tasks.discard)

        serve_kwargs: dict[str, Any] = {
            "process_request": self._process_request,
            "compression": None,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 16 * 1024 * 1024,
        }
        if sys.platform == "win32":
            serve_kwargs["reuse_address"] = False

        async with websockets.serve(
            self._handle_ws,
            self.config.web_pwa_host,
            self.config.web_pwa_port,
            **serve_kwargs,
        ):
            try:
                if stop_event is not None:
                    await stop_event.wait()
                else:
                    await asyncio.Future()
            finally:
                for task in list(self._tasks):
                    task.cancel()
                if self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
