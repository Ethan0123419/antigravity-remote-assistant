"""Dedicated Mobile-First Progressive Web App (PWA) server for Antigravity remote control.

Runs alongside OneBotBridge on a single HTTP + WebSocket port (default 8765),
compatible with both local Wi-Fi LAN and Cloudflare Encrypted Quick Tunnels.
Provides:
- True token-by-token live streaming responses
- Live desktop screen preview & instant HD screenshots
- Chunked arbitrary file/photo upload & download with path-traversal/secret protection
- High-entropy token authentication + per-IP brute-force rate limiting
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import mimetypes
import secrets
import socket
import time
import uuid
from collections import OrderedDict
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .agy_session import SessionManager
from .config import BridgeConfig
from .conversations import (
    ConversationDetail,
    build_ide_handoff_context,
    get_transcript_signature,
    list_local_conversations,
    list_recent_workspaces,
    load_conversation_detail,
)
from .maintenance import discover_default_cache_targets, run_safe_cache_cleanup
from .onebot import (
    MODEL_ALIASES,
    PROJECT_INBOX_DIR,
    SEND_FILE_RE,
    SEND_IMAGE_RE,
    THOUGHT_TAG_RE,
    _capture_desktop_fast_jpeg_sync,
    _is_safe_outbound_file,
    _prune_inbox,
    _sanitize_filename,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
ASSETS_DIR = PROJECT_ROOT / "assets"

PWA_SESSION_USER_ID = "web-pwa-owner"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

SERVICE_WORKER_JS = """\
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""


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
    """Strip <thought> tags and extract [SEND_IMAGE] / [SEND_FILE], keeping clean Markdown for rich PWA rendering."""
    cleaned = THOUGHT_TAG_RE.sub("", text)
    images: list[Path] = []
    files: list[Path] = []

    def _on_img(m) -> str:
        raw = m.group(1).strip()
        if raw:
            p = Path(raw).expanduser()
            if _is_safe_outbound_file(p, image_only=True):
                images.append(p.resolve())
        return ""

    def _on_file(m) -> str:
        raw = m.group(1).strip()
        if raw:
            p = Path(raw).expanduser()
            if _is_safe_outbound_file(p, image_only=False):
                files.append(p.resolve())
        return ""

    cleaned = SEND_IMAGE_RE.sub(_on_img, cleaned)
    cleaned = SEND_FILE_RE.sub(_on_file, cleaned)
    return cleaned.strip(), images, files


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
    """Single-port HTTP + WebSocket server for the mobile/desktop PWA client."""

    def __init__(
        self,
        config: BridgeConfig,
        session_manager: SessionManager,
        access_token: str,
        *,
        ide_store_dir: Path | None = None,
        cli_store_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.sessions = session_manager
        self.access_token = access_token
        self.ide_store_dir = ide_store_dir
        self.cli_store_dir = cli_store_dir
        self._clients: set[ServerConnection] = set()
        self._media_registry: OrderedDict[str, Path] = OrderedDict()
        self._history: list[dict[str, Any]] = []
        self._failed_auth: dict[str, list[float]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._session_epoch: int = 0
        self._inflight_reply: dict[str, Any] | None = None
        self._cancelled_reply_ids: set[str] = set()
        self._ide_watch_sig: tuple[str, int, int] = ("", 0, 0)

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
        while len(self._media_registry) > 200:
            self._media_registry.popitem(last=False)
        return media_id

    def _append_history(self, item: dict[str, Any]) -> None:
        self._history.append(item)
        if len(self._history) > 60:
            self._history = self._history[-60:]

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

        # Allow WebSocket upgrade on /ws
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
                "name": "Antigravity 私人助理",
                "short_name": "Antigravity",
                "description": "专属远程电脑控制与 AI 私人助理",
                "start_url": start_url,
                "display": "standalone",
                "background_color": "#060A13",
                "theme_color": "#060A13",
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
            if target is None or not _is_safe_outbound_file(target, image_only=False):
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
        logger.info("Web PWA client connected (%s)", ip)
        uploads: dict[str, _UploadState] = {}
        live_screen_task: asyncio.Task[None] | None = None

        session = self.sessions.get(PWA_SESSION_USER_ID)
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "model": session.model or "default",
                    "workspace": str(session.workspace),
                    "active_conversation_id": session.linked_ide_id or session.conversation_id,
                    "active_title": session.active_title or "",
                    "active_store": "ide" if session.linked_ide_id else ("cli" if session.conversation_id else ""),
                    "busy": session.is_busy or (self._inflight_reply is not None),
                    "inflight": self._inflight_reply,
                    "cooldowns": session.failover_pool.cooldown_summary(),
                    "history": self._history,
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
                        shot = await asyncio.to_thread(_capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR)
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
                                        _capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR, 1366, 74
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
                    safe_name = _sanitize_filename(str(msg.get("name") or "upload.bin"))
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
                    _prune_inbox(PROJECT_INBOX_DIR, keep=40)
                    mid = self.register_media(state.path)
                    is_img = state.path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
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
                    if self._inflight_reply and self._inflight_reply.get("msg_id"):
                        self._cancelled_reply_ids.add(str(self._inflight_reply["msg_id"]))
                    killed = await self.sessions.get(PWA_SESSION_USER_ID).cancel_current()
                    note = "已停止当前生成。" if killed else "当前没有运行中的任务。"
                    await ws.send(json.dumps({"type": "toast", "level": "info", "text": note}, ensure_ascii=False))

                elif mtype == "list_conversations":
                    await self._handle_list_conversations(ws)

                elif mtype == "select_conversation":
                    t = asyncio.create_task(
                        self._handle_select_conversation(
                            ws,
                            str(msg.get("conversation_id") or ""),
                            str(msg.get("store") or "auto"),
                            only_refresh=False,
                        )
                    )
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

                elif mtype == "refresh_conversation":
                    s = self.sessions.get(PWA_SESSION_USER_ID)
                    target_cid = str(msg.get("conversation_id") or s.linked_ide_id or s.conversation_id or "")
                    target_store = "ide" if s.linked_ide_id else "auto"
                    if target_cid:
                        t = asyncio.create_task(
                            self._handle_select_conversation(ws, target_cid, target_store, only_refresh=True)
                        )
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)

                elif mtype == "new_conversation":
                    t = asyncio.create_task(
                        self._handle_new_conversation(ws, str(msg.get("workspace") or ""))
                    )
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

                elif mtype == "chat":
                    t = asyncio.create_task(self._handle_chat(msg))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
        finally:
            _stop_live_screen()
            for st in uploads.values():
                st.close()
            self._clients.discard(ws)
            logger.info("Web PWA client disconnected (%s)", ip)

    def _safe_file_size(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _convert_detail_to_pwa_history(self, detail: ConversationDetail, model_label: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for idx, m in enumerate(detail.messages):
            if m.role == "user":
                atts = []
                for p in m.image_paths:
                    if _is_safe_outbound_file(p, image_only=True):
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
                for p in [*m.image_paths, *extra_imgs]:
                    if _is_safe_outbound_file(p, image_only=True):
                        imgs.append({"media_id": self.register_media(p), "name": p.name})
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
                        "model": "电脑端 IDE" if detail.store == "ide" else model_label,
                        "ts": m.ts_ms,
                    }
                )
        return out

    async def _handle_list_conversations(self, ws: ServerConnection) -> None:
        session = self.sessions.get(PWA_SESSION_USER_ID)
        items = await asyncio.to_thread(
            list_local_conversations, 40, self.ide_store_dir, self.cli_store_dir
        )
        workspaces = await asyncio.to_thread(
            list_recent_workspaces, session.workspace, self.ide_store_dir, self.cli_store_dir
        )
        await ws.send(
            json.dumps(
                {
                    "type": "conversations_list",
                    "active_id": session.linked_ide_id or session.conversation_id,
                    "current_workspace": str(session.workspace),
                    "items": [it.to_dict() for it in items],
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
    ) -> None:
        detail = await asyncio.to_thread(
            load_conversation_detail,
            conversation_id,
            store,
            100,
            self.ide_store_dir,
            self.cli_store_dir,
        )
        if detail is None:
            await ws.send(json.dumps({"type": "toast", "level": "err", "text": "未找到该对话记录"}, ensure_ascii=False))
            return

        session = self.sessions.get(PWA_SESSION_USER_ID)
        if not only_refresh:
            self._session_epoch += 1
            self._inflight_reply = None
            if detail.store == "cli":
                await session.attach_cli_conversation(
                    detail.conversation_id, workspace=detail.workspace, title=detail.title
                )
            else:
                handoff = build_ide_handoff_context(detail)
                await session.attach_ide_conversation(
                    detail.conversation_id,
                    handoff_context=handoff,
                    workspace=detail.workspace,
                    title=detail.title,
                )
                sig = await asyncio.to_thread(
                    get_transcript_signature,
                    detail.conversation_id,
                    "ide",
                    self.ide_store_dir,
                    self.cli_store_dir,
                )
                self._ide_watch_sig = (detail.conversation_id, sig[0], sig[1])

        loaded_history = self._convert_detail_to_pwa_history(detail, session.model or "default")
        self._history = loaded_history
        await self._broadcast(
            {
                "type": "conversation_loaded",
                "conversation_id": detail.conversation_id,
                "title": detail.title,
                "store": detail.store,
                "running": detail.running,
                "workspace": str(session.workspace),
                "silent": only_refresh,
                "history": loaded_history,
            }
        )

    async def _handle_new_conversation(self, ws: ServerConnection, workspace: str) -> None:
        session = self.sessions.get(PWA_SESSION_USER_ID)
        self._session_epoch += 1
        self._inflight_reply = None
        try:
            await session.start_new_conversation(workspace=workspace)
        except FileNotFoundError as exc:
            await ws.send(json.dumps({"type": "toast", "level": "err", "text": str(exc)}, ensure_ascii=False))
            return

        self._ide_watch_sig = ("", 0, 0)
        self._history.clear()
        await self._broadcast({"type": "clear_history"})
        await self._broadcast(
            {
                "type": "meta_update",
                "model": session.model or "default",
                "workspace": str(session.workspace),
                "active_conversation_id": "",
                "active_title": "新对话",
                "active_store": "",
            }
        )
        await ws.send(
            json.dumps(
                {"type": "toast", "level": "info", "text": f"已开启新对话（目录：{session.workspace.name}）"},
                ensure_ascii=False,
            )
        )
        if self.config.agy_prewarm:
            t = asyncio.create_task(session.prewarm())
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _handle_chat(self, msg: dict[str, Any]) -> None:
        raw_text = str(msg.get("text") or "").strip()
        client_id = str(msg.get("client_id") or f"u-{uuid.uuid4().hex[:10]}")
        attachments = msg.get("attachments") or []
        if not raw_text and not attachments:
            return

        session = self.sessions.get(PWA_SESSION_USER_ID)
        turn_epoch = self._session_epoch

        # Record user message in history & broadcast to any other open tab
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
        self._append_history(user_entry)
        await self._broadcast({"type": "user_msg", "item": user_entry})

        # Handle built-in slash commands cleanly
        if raw_text.startswith("/") and not attachments:
            handled = await self._handle_slash_command(session, raw_text)
            if handled:
                return

        # Build prompt with attached local file paths
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
            "model": session.model or "default",
            "text": "",
            "status": "",
        }
        self._inflight_reply = inflight_state
        await self._broadcast({"type": "stream_start", "msg_id": reply_id, "model": session.model or "default"})

        async def _on_delta(delta: str) -> None:
            if turn_epoch != self._session_epoch:
                return
            inflight_state["text"] = str(inflight_state.get("text") or "") + delta
            inflight_state["status"] = ""
            await self._broadcast({"type": "stream_delta", "msg_id": reply_id, "delta": delta})

        async def _on_reset() -> None:
            if turn_epoch != self._session_epoch:
                return
            inflight_state["text"] = ""
            await self._broadcast({"type": "stream_reset", "msg_id": reply_id})

        async def _on_tool(tool_name: str) -> None:
            if turn_epoch != self._session_epoch:
                return
            hint = "正在操作电脑..." if ("mcp" in tool_name.lower() or "command" in tool_name.lower()) else "正在检索处理..."
            inflight_state["status"] = hint
            await self._broadcast({"type": "stream_status", "msg_id": reply_id, "status": hint})

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
            if self._inflight_reply is inflight_state:
                self._inflight_reply = None

        # Discard stale reply if user already switched conversations or started a new chat
        if turn_epoch != self._session_epoch:
            self._cancelled_reply_ids.discard(reply_id)
            return

        was_user_cancelled = reply_id in self._cancelled_reply_ids
        self._cancelled_reply_ids.discard(reply_id)

        if result.status == "SUCCESS":
            clean_text, img_paths, file_paths = extract_pwa_media_and_text(result.response or "")
            if result.switched_from_model and result.active_model:
                note = f"（注：{result.switched_from_model} 额度刚满，已自动切到 {result.active_model} 继续）"
                clean_text = f"{clean_text}\n\n{note}" if clean_text else note
            if not clean_text and not img_paths and not file_paths:
                clean_text = "搞定啦。"
        elif was_user_cancelled:
            clean_resp, img_paths, file_paths = extract_pwa_media_and_text(result.response or partial_before_finish)
            clean_text = f"{clean_resp}\n\n⏹ 已停止生成。".strip() if clean_resp else "⏹ 已停止生成。"
        else:
            clean_resp, img_paths, file_paths = extract_pwa_media_and_text(result.response or partial_before_finish)
            err = result.error or f"执行未成功 ({result.status})"
            clean_text = f"{clean_resp}\n\n刚才这步出了点状况：\n{err}".strip()

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
        self._append_history(assistant_entry)
        await self._broadcast({"type": "stream_end", "item": assistant_entry})

    async def _send_sys_reply(self, text: str, model: str = "", images: list[dict] | None = None) -> None:
        entry = {
            "role": "assistant",
            "id": f"s-{uuid.uuid4().hex[:10]}",
            "text": text,
            "images": images or [],
            "files": [],
            "model": model,
            "ts": int(time.time() * 1000),
        }
        self._append_history(entry)
        await self._broadcast({"type": "stream_end", "item": entry})

    async def _handle_slash_command(self, session, raw_text: str) -> bool:
        parts = raw_text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"/screen", "/shot"}:
            shot = await asyncio.to_thread(_capture_desktop_fast_jpeg_sync, PROJECT_INBOX_DIR)
            mid = self.register_media(shot)
            await self._send_sys_reply("当前电脑实时画面：", session.model, images=[{"media_id": mid, "name": shot.name}])
            return True

        if cmd in {"/new", "/reset"}:
            self._session_epoch += 1
            self._inflight_reply = None
            self._ide_watch_sig = ("", 0, 0)
            await session.reset()
            self._history.clear()
            await self._broadcast({"type": "clear_history"})
            await self._broadcast(
                {
                    "type": "meta_update",
                    "model": session.model or "default",
                    "workspace": str(session.workspace),
                    "active_conversation_id": "",
                    "active_title": "新对话",
                    "active_store": "",
                }
            )
            await self._send_sys_reply(f"好勒，已开启全新对话。\n当前模型：{session.model or '默认'}", session.model)
            if self.config.agy_prewarm:
                t = asyncio.create_task(session.prewarm())
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            return True

        if cmd == "/model":
            if not arg:
                chain = " -> ".join(session.failover_pool.models[:5])
                await self._send_sys_reply(
                    f"当前模型：{session.model or '默认'}\n自动容灾链：{chain}\n\n"
                    "可点顶部模型胶囊或输入：/model fast | balanced | smart | gemini",
                    session.model,
                )
                return True
            target = MODEL_ALIASES.get(arg.lower(), arg)
            await session.set_model(target)
            await self._broadcast({"type": "meta_update", "model": session.model, "workspace": str(session.workspace)})
            await self._send_sys_reply(f"已切换到 {target}，上下文继续保留。", session.model)
            return True

        if cmd == "/clean":
            targets = discover_default_cache_targets(PROJECT_ROOT)
            rep = await asyncio.to_thread(
                run_safe_cache_cleanup, targets, force_all_expired_zero=(arg.lower() == "all")
            )
            await self._send_sys_reply(rep.format_human(), session.model)
            return True

        if cmd == "/status":
            cooling = session.failover_pool.cooldown_summary()
            cool_str = ", ".join(f"{m}({mins}m)" for m, mins in cooling) if cooling else "全部正常可用"
            await self._send_sys_reply(
                f"当前模型：{session.model or '默认'}\n"
                f"额度池：{cool_str}\n"
                f"会话轮次：{session.num_turns}\n"
                f"工作目录：{session.workspace}",
                session.model,
            )
            return True

        if cmd == "/cd" and arg:
            target_dir = Path(arg.strip("\"'"))
            if not target_dir.is_absolute():
                target_dir = session.workspace / target_dir
            try:
                self._session_epoch += 1
                self._inflight_reply = None
                self._ide_watch_sig = ("", 0, 0)
                await session.set_workspace(target_dir)
                await self._broadcast(
                    {
                        "type": "meta_update",
                        "model": session.model,
                        "workspace": str(session.workspace),
                        "active_conversation_id": "",
                        "active_store": "",
                    }
                )
                await self._send_sys_reply(f"已切换工作目录：\n{session.workspace}", session.model)
            except FileNotFoundError as exc:
                await self._send_sys_reply(str(exc), session.model)
            return True

        return False

    async def _ide_watch_loop(self) -> None:
        """Auto-push live PC IDE transcript updates when a mobile client is viewing an IDE chat."""
        while True:
            interval = getattr(self, "ide_watch_interval", 2.5)
            await asyncio.sleep(interval)
            try:
                if not self._clients:
                    continue
                session = self.sessions.get(PWA_SESSION_USER_ID)
                ide_cid = session.linked_ide_id
                if not ide_cid or session.is_busy or self._inflight_reply is not None:
                    continue
                # Only auto-replace view while viewing the unbranched PC IDE chat
                if session.conversation_id:
                    continue

                sig = await asyncio.to_thread(
                    get_transcript_signature,
                    ide_cid,
                    "ide",
                    self.ide_store_dir,
                    self.cli_store_dir,
                )
                if sig == (0, 0) or (ide_cid, sig[0], sig[1]) == self._ide_watch_sig:
                    continue

                self._ide_watch_sig = (ide_cid, sig[0], sig[1])
                detail = await asyncio.to_thread(
                    load_conversation_detail,
                    ide_cid,
                    "ide",
                    100,
                    self.ide_store_dir,
                    self.cli_store_dir,
                )
                if detail is None or session.linked_ide_id != ide_cid or session.conversation_id:
                    continue

                session._pending_handoff_context = build_ide_handoff_context(detail)
                loaded_history = self._convert_detail_to_pwa_history(detail, session.model or "default")
                self._history = loaded_history
                await self._broadcast(
                    {
                        "type": "conversation_loaded",
                        "conversation_id": detail.conversation_id,
                        "title": detail.title,
                        "store": detail.store,
                        "running": detail.running,
                        "workspace": str(session.workspace),
                        "silent": True,
                        "history": loaded_history,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("IDE watch loop error: %s", exc)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        logger.info(
            "Starting Web PWA server at http://%s:%d (LAN: http://%s:%d)",
            self.config.web_pwa_host,
            self.config.web_pwa_port,
            get_lan_ipv4(),
            self.config.web_pwa_port,
        )
        if self.config.agy_prewarm:
            pw_task = asyncio.create_task(self.sessions.get(PWA_SESSION_USER_ID).prewarm())
            self._tasks.add(pw_task)
            pw_task.add_done_callback(self._tasks.discard)

        watch_task = asyncio.create_task(self._ide_watch_loop())
        self._tasks.add(watch_task)
        watch_task.add_done_callback(self._tasks.discard)

        async with websockets.serve(
            self._handle_ws,
            self.config.web_pwa_host,
            self.config.web_pwa_port,
            process_request=self._process_request,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            max_size=16 * 1024 * 1024,
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
