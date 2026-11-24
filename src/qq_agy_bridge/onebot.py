from __future__ import annotations

import asyncio
import base64
import hmac
import html
import ipaddress
import json
import logging
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection

from .agy_session import SessionManager
from .config import BridgeConfig
from .conversations import (
    build_ide_handoff_context,
    list_local_conversations,
    load_conversation_detail,
)
from .maintenance import discover_default_cache_targets, run_safe_cache_cleanup

logger = logging.getLogger(__name__)

CQ_TAG_RE = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)(?:,[^\]]*)?\]")
THOUGHT_TAG_RE = re.compile(
    r"<(?:thought|thinking|analysis)\b[^>]*>.*?</(?:thought|thinking|analysis)>",
    re.IGNORECASE | re.DOTALL,
)
SEND_IMAGE_RE = re.compile(r"\[SEND_IMAGE:\s*[\"']?([^\]\"'\r\n]+?)[\"']?\s*\]", re.IGNORECASE)
SEND_FILE_RE = re.compile(r"\[SEND_FILE:\s*[\"']?([^\]\"'\r\n]+?)[\"']?\s*\]", re.IGNORECASE)
FENCED_CODE_RE = re.compile(r"(```[^\n]*\n.*?```)", re.DOTALL)
MD_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file:///[^)]+\)")
MD_WEB_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
MD_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_HR_RE = re.compile(r"(?m)^[ \t]*[-*_]{3,}[ \t]*$")
MD_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*][ \t]+")

PROJECT_INBOX_DIR = Path(__file__).resolve().parents[2] / "inbox"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_SEND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SEND_FILE_BYTES = 100 * 1024 * 1024
SENSITIVE_FILENAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "webui.json",
    "passkey.json",
    "cookies.sqlite",
}

MODEL_ALIASES: dict[str, str] = {
    "fast": "claude-sonnet-5-5-low",
    "balanced": "claude-sonnet-5-5-medium",
    "smart": "claude-opus-5-5-medium",
    "opus": "claude-opus-5-5-medium",
    "sonnet": "claude-sonnet-5-5-medium",
    "gemini": "gemini-4-neon-medium",
    "flash": "gemini-3.8-flash-medium",
}

PURE_SCREENSHOT_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我|帮忙)?(?:现在|马上|立刻)?(?:再)?"
    r"(?:截(?:个|张|一下)?(?:图|屏|桌面|屏幕)|(?:发?给?我看|看)(?:看|一?下)?(?:我|现在|当前|目前|电脑|的)*(?:屏幕|桌面|界面|画面))"
    r"(?:(?:发)?(?:给)?我?(?:看(?:看|一?下)?)?(?:我|现在|当前|目前|电脑|的)*(?:屏幕|桌面|界面|画面)?)?"
    r"[。！!~～？?]*$"
)
NON_PURE_SCREENSHOT_HINTS = (
    "打开", "关闭", "点击", "输入", "搜索", "分析", "报错", "错误", "修复",
    "然后", "顺便", "并且", "怎么", "为什么", "什么", "帮我改", "帮我做", "写",
)


def is_pure_screenshot_request(text: str) -> bool:
    """Detect simple standalone desktop screenshot requests for <0.5s instant capture."""
    s = text.strip()
    if not s or len(s) > 28:
        return False
    if any(hint in s for hint in NON_PURE_SCREENSHOT_HINTS):
        return False
    return bool(PURE_SCREENSHOT_RE.match(s))


def _is_safe_outbound_file(path: Path, *, image_only: bool = False) -> bool:
    """Block sensitive credential files and enforce extension / size limits."""
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            return False
        name_low = resolved.name.lower()
        if name_low in SENSITIVE_FILENAMES or name_low.startswith("onebot11_"):
            logger.warning("Blocked sending sensitive file to QQ: %s", resolved)
            return False
        size = resolved.stat().st_size
        if image_only:
            if resolved.suffix.lower() not in ALLOWED_IMAGE_EXTS:
                logger.warning("Blocked non-image file in SEND_IMAGE: %s", resolved)
                return False
            return 0 < size <= MAX_SEND_IMAGE_BYTES
        return 0 < size <= MAX_SEND_FILE_BYTES
    except OSError:
        return False


def is_safe_public_http_url(url: str) -> bool:
    """Validate URL is http/https and all resolved IPs are public (SSRF protection)."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.strip().lower()
        if host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
            return False
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not infos:
            return False
        for info in infos:
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_loopback
                or ip_obj.is_private
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False
        return True
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_public_http_url(newurl):
            raise urllib.error.URLError(f"Blocked unsafe redirect target: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _prune_inbox(inbox_dir: Path, keep: int = 30) -> None:
    if not inbox_dir.is_dir():
        return
    files = sorted(
        (p for p in inbox_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _strip_markdown_prose(segment: str) -> str:
    text = MD_FILE_LINK_RE.sub(r"\1", segment)

    def _web_link(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        if not label or label == url:
            return url
        return f"{label} {url}"

    text = MD_WEB_LINK_RE.sub(_web_link, text)
    text = MD_HEADING_RE.sub("", text)
    text = MD_BOLD_RE.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text)
    text = MD_INLINE_CODE_RE.sub(r"\1", text)
    text = MD_HR_RE.sub("", text)
    text = MD_BULLET_RE.sub(r"\1• ", text)
    return text


def extract_media_and_clean_for_qq(
    text: str, clean_markdown: bool = True
) -> tuple[str, list[Path], list[Path]]:
    """Strip thinking tags, extract [SEND_IMAGE] & [SEND_FILE], and clean Markdown."""
    cleaned = THOUGHT_TAG_RE.sub("", text)

    image_paths: list[Path] = []
    file_paths: list[Path] = []

    def _collect_image(match: re.Match[str]) -> str:
        raw_path = match.group(1).strip()
        if raw_path:
            candidate = Path(raw_path).expanduser()
            if _is_safe_outbound_file(candidate, image_only=True):
                image_paths.append(candidate.resolve())
            else:
                logger.warning("Skipped invalid or unsafe SEND_IMAGE path: %s", raw_path)
        return ""

    def _collect_file(match: re.Match[str]) -> str:
        raw_path = match.group(1).strip()
        if raw_path:
            candidate = Path(raw_path).expanduser()
            if _is_safe_outbound_file(candidate, image_only=False):
                file_paths.append(candidate.resolve())
            else:
                logger.warning("Skipped invalid or unsafe SEND_FILE path: %s", raw_path)
        return ""

    cleaned = SEND_IMAGE_RE.sub(_collect_image, cleaned)
    cleaned = SEND_FILE_RE.sub(_collect_file, cleaned)

    if clean_markdown:
        parts = FENCED_CODE_RE.split(cleaned)
        rebuilt: list[str] = []
        for part in parts:
            if part.startswith("```") and part.endswith("```"):
                lines = part.splitlines()
                body = "\n".join(lines[1:-1]).strip("\n")
                if body:
                    rebuilt.append(body)
            else:
                rebuilt.append(_strip_markdown_prose(part))
        cleaned = "".join(rebuilt)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, image_paths, file_paths


def format_for_qq(text: str, clean_markdown: bool = True) -> tuple[str, list[Path]]:
    """Backward-compatible wrapper returning (cleaned_text, image_paths)."""
    cleaned, image_paths, _ = extract_media_and_clean_for_qq(text, clean_markdown=clean_markdown)
    return cleaned, image_paths


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .")
    return cleaned[:80] or "attachment"


def _download_media_sync(url: str, inbox_dir: Path, suggested_name: str = "") -> Path | None:
    if not is_safe_public_http_url(url):
        logger.warning("Blocked non-public URL download (SSRF guard): %s", url[:100])
        return None
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        opener = urllib.request.build_opener(_SafeRedirectHandler)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=15) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(25 * 1024 * 1024)
        if not data:
            return None
        if suggested_name:
            safe_name = _sanitize_filename(suggested_name)
            dest = inbox_dir / f"qq_{int(time.time())}_{uuid.uuid4().hex[:4]}_{safe_name}"
        else:
            ext = ".png" if "png" in content_type else (".gif" if "gif" in content_type else ".jpg")
            dest = inbox_dir / f"qq_in_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
        dest.write_bytes(data)
        _prune_inbox(inbox_dir)
        return dest
    except Exception as exc:
        logger.warning("Failed to download inbound QQ media (%s): %s", url[:80], exc)
        return None


def _capture_desktop_screenshot_sync(inbox_dir: Path) -> Path:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"screen_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    ps_script = f"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class DpiHelper {{
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
}}
"@
[DpiHelper]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('{str(dest).replace("'", "''")}', [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
"""
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps_script,
        ],
        check=True,
        capture_output=True,
        timeout=15,
        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _prune_inbox(inbox_dir)
    return dest


def _capture_desktop_fast_jpeg_sync(
    inbox_dir: Path, max_width: int = 1600, quality: int = 80
) -> Path:
    """Capture a scaled, crisp JPEG desktop frame (~100-180 KB, 20x smaller than raw PNG) for fast mobile preview."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"preview_{int(time.time() * 1000)}_{uuid.uuid4().hex[:5]}.jpg"
    ps_script = f"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class DpiHelper {{
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
}}
"@
[DpiHelper]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$srcBmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$srcGfx = [System.Drawing.Graphics]::FromImage($srcBmp)
$srcGfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$srcGfx.Dispose()

$targetW = [Math]::Min($bounds.Width, {int(max_width)})
$targetH = [int]([Math]::Round($bounds.Height * ($targetW / [double]$bounds.Width)))
if ($targetW -lt $bounds.Width) {{
    $outBmp = New-Object System.Drawing.Bitmap $targetW, $targetH
    $outGfx = [System.Drawing.Graphics]::FromImage($outBmp)
    $outGfx.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $outGfx.DrawImage($srcBmp, 0, 0, $targetW, $targetH)
    $outGfx.Dispose()
    $srcBmp.Dispose()
}} else {{
    $outBmp = $srcBmp
}}

$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object {{ $_.MimeType -eq 'image/jpeg' }} | Select-Object -First 1
$ep = New-Object System.Drawing.Imaging.EncoderParameters 1
$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [long]{int(quality)})
$outBmp.Save('{str(dest).replace("'", "''")}', $codec, $ep)
$ep.Dispose()
$outBmp.Dispose()
"""
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps_script,
        ],
        check=True,
        capture_output=True,
        timeout=15,
        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _prune_inbox(inbox_dir, keep=35)
    return dest


def extract_plain_text(event: dict[str, Any]) -> str:
    """Extract readable plain text from an OneBot 11 message event (array or CQ string)."""
    message = event.get("message")
    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "file":
                name = data.get("name") or data.get("file") or ""
                parts.append(f"[文件: {name}]" if name else "[文件]")
        text = "".join(parts).strip()
        if text:
            return text

    raw = event.get("raw_message")
    if isinstance(raw, str):
        def _replace_cq(match: re.Match[str]) -> str:
            cq_type = match.group(1)
            if cq_type == "image":
                return "[图片]"
            if cq_type == "file":
                return "[文件]"
            return ""

        cleaned = CQ_TAG_RE.sub(_replace_cq, raw)
        return html.unescape(cleaned).strip()

    if isinstance(message, str):
        return html.unescape(CQ_TAG_RE.sub("", message)).strip()

    return ""


def split_message(text: str, max_length: int) -> list[str]:
    """Split a long reply into QQ-friendly chunks along paragraph/line boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        window = remaining[:max_length]
        split_at = window.rfind("\n\n")
        if split_at < max_length // 3:
            split_at = window.rfind("\n")
        if split_at < max_length // 3:
            split_at = window.rfind(" ")
        if split_at < max_length // 3:
            split_at = max_length
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


HELP_TEXT = (
    "常用快捷指令：\n"
    "/chats          查看电脑端与远程已有聊天框列表\n"
    "/chat <序号>    切到指定聊天框（展示最近聊天内容并接着聊）\n"
    "/new 或 /reset  新建空白对话\n"
    "/screen         秒截当前电脑桌面发回 QQ\n"
    "/model [档位]   切换模型（支持额度用尽自动无缝切换）\n"
    "/clean [all]    清理过期缓存\n"
    "/memory [内容]  查看或追加长期记忆\n"
    "/status         查看当前状态与额度池\n"
    "/cd <目录>      切换工作目录\n"
    "/cancel         随时打断正在跑的任务\n\n"
    "平时直接像聊天一样发消息就好，连发几句或先发图再打字都会自动合并理解。"
)


@dataclass
class _PendingBatch:
    prompts: list[str] = field(default_factory=list)
    first_ts: float = 0.0
    timer_task: asyncio.Task[None] | None = None


class OneBotBridge:
    """OneBot 11 private-message-only bridge backed by Antigravity CLI (agy)."""

    def __init__(
        self,
        config: BridgeConfig,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.config = config
        self.sessions = session_manager or SessionManager(config)
        self._tasks: set[asyncio.Task[None]] = set()
        self._prewarm_started = False
        self._cache_cleaner_started = False
        self._active_ws: ClientConnection | ServerConnection | None = None
        self._batches: dict[str, _PendingBatch] = {}
        self._last_listed_chats: dict[str, list[tuple[str, str]]] = {}

    def _schedule_cache_cleaner(self) -> None:
        if self._cache_cleaner_started or self.config.cache_clean_interval_hours <= 0.0:
            return
        self._cache_cleaner_started = True
        project_root = Path(__file__).resolve().parents[2]
        interval_sec = max(300.0, self.config.cache_clean_interval_hours * 3600.0)

        async def _cleaner_loop() -> None:
            # Initial gentle cleanup 15s after startup, then every N hours
            await asyncio.sleep(15.0)
            while True:
                try:
                    targets = discover_default_cache_targets(project_root)
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
                    logger.warning("Periodic cache cleanup error: %s", exc)
                await asyncio.sleep(interval_sec)

        task = asyncio.create_task(_cleaner_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_prewarm(self) -> None:
        if self._prewarm_started or not self.config.agy_prewarm or not self.config.prewarm_user_ids:
            return
        self._prewarm_started = True

        async def _run_prewarm() -> None:
            for uid in self.config.prewarm_user_ids:
                try:
                    await self.sessions.get(uid).prewarm()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Background prewarm error for user=%s: %s", uid, exc)

        task = asyncio.create_task(_run_prewarm())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _is_authorized_private_message(self, event: dict[str, Any]) -> tuple[bool, str]:
        """Enforce private-only + strict QQ whitelist."""
        if event.get("post_type") != "message":
            return False, "not_message"
        if event.get("message_type") != "private":
            return False, "non_private"

        user_id = str(event.get("user_id") or "").strip()
        if not user_id:
            return False, "missing_user_id"
        if user_id not in self.config.allowed_qq_users:
            return False, f"unauthorized:{user_id}"
        return True, user_id

    async def _send_ws_json(
        self,
        preferred_ws: ClientConnection | ServerConnection | None,
        payload: dict[str, Any],
    ) -> None:
        """Send an OneBot action using the latest active WS, surviving mid-turn reconnects."""
        encoded = json.dumps(payload, ensure_ascii=False)
        candidates = []
        if self._active_ws is not None:
            candidates.append(self._active_ws)
        if preferred_ws is not None and preferred_ws is not self._active_ws:
            candidates.append(preferred_ws)

        for ws in candidates:
            try:
                await ws.send(encoded)
                return
            except Exception:
                continue

        # Wait briefly in case reconnect is in progress right now
        for _ in range(12):
            await asyncio.sleep(0.5)
            ws = self._active_ws
            if ws is not None:
                try:
                    await ws.send(encoded)
                    return
                except Exception:
                    continue
        logger.error("Failed to deliver OneBot action %s: WebSocket unavailable", payload.get("action"))

    async def _set_typing_status(
        self,
        ws: ClientConnection | ServerConnection | None,
        user_id: int | str,
    ) -> None:
        """Trigger native QQ '对方正在输入...' indicator (NapCat extension, ignored safely by mocks)."""
        uid: int | str = int(user_id) if str(user_id).isdigit() else str(user_id)
        action = {
            "action": "set_input_status",
            "params": {
                "user_id": uid,
                "event_type": 1,
            },
            "echo": f"typing-{uuid.uuid4().hex[:10]}",
        }
        try:
            target = self._active_ws or ws
            if target is not None:
                await target.send(json.dumps(action, ensure_ascii=False))
        except Exception:
            pass

    async def _send_private_msg(
        self,
        ws: ClientConnection | ServerConnection | None,
        user_id: int | str,
        text: str,
    ) -> None:
        chunks = split_message(text, self.config.max_message_length)
        if not chunks:
            return

        uid: int | str = int(user_id) if str(user_id).isdigit() else str(user_id)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            body = f"[{idx}/{total}]\n{chunk}" if total > 1 else chunk
            action = {
                "action": "send_private_msg",
                "params": {
                    "user_id": uid,
                    "message": body,
                },
                "echo": f"reply-{uuid.uuid4().hex[:12]}",
            }
            await self._send_ws_json(ws, action)
            if idx < total:
                await asyncio.sleep(0.35)

    async def _send_private_image(
        self,
        ws: ClientConnection | ServerConnection | None,
        user_id: int | str,
        image_path: Path,
    ) -> None:
        try:
            raw = await asyncio.to_thread(image_path.read_bytes)
            b64 = base64.b64encode(raw).decode("ascii")
        except OSError as exc:
            logger.error("Failed to read image for QQ sending (%s): %s", image_path, exc)
            return

        uid: int | str = int(user_id) if str(user_id).isdigit() else str(user_id)
        action = {
            "action": "send_private_msg",
            "params": {
                "user_id": uid,
                "message": [
                    {
                        "type": "image",
                        "data": {"file": f"base64://{b64}"},
                    }
                ],
            },
            "echo": f"img-{uuid.uuid4().hex[:12]}",
        }
        await self._send_ws_json(ws, action)

    async def _send_private_file(
        self,
        ws: ClientConnection | ServerConnection | None,
        user_id: int | str,
        file_path: Path,
    ) -> None:
        uid: int | str = int(user_id) if str(user_id).isdigit() else str(user_id)
        action = {
            "action": "upload_private_file",
            "params": {
                "user_id": uid,
                "file": str(file_path.resolve()),
                "name": file_path.name,
            },
            "echo": f"file-{uuid.uuid4().hex[:12]}",
        }
        await self._send_ws_json(ws, action)

    async def _enrich_inbound_media(self, event: dict[str, Any], text: str) -> str:
        """Download or resolve incoming QQ images and files so agy can inspect them locally."""
        message = event.get("message")
        if not isinstance(message, list):
            return text

        notes: list[str] = []
        media_count = 0
        for seg in message:
            if not isinstance(seg, dict) or media_count >= 4:
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "image":
                url = str(data.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    media_count += 1
                    saved = await asyncio.to_thread(_download_media_sync, url, PROJECT_INBOX_DIR)
                    if saved is not None:
                        notes.append(f"[用户发来图片，已保存到本地，可用 read_file 查看: {saved}]")
            elif seg_type == "file":
                local_path = str(data.get("path") or data.get("file") or "").strip()
                file_name = str(data.get("name") or "").strip()
                url = str(data.get("url") or "").strip()
                if local_path and Path(local_path).is_file():
                    media_count += 1
                    notes.append(f"[用户发来文件 {file_name or Path(local_path).name}，本地路径: {local_path}]")
                elif url.startswith(("http://", "https://")):
                    media_count += 1
                    saved = await asyncio.to_thread(
                        _download_media_sync, url, PROJECT_INBOX_DIR, file_name
                    )
                    if saved is not None:
                        notes.append(f"[用户发来文件 {file_name or saved.name}，已保存到: {saved}]")

        if not notes:
            return text
        joined = "\n".join(notes)
        return f"{text}\n{joined}" if text else joined

    async def _handle_command(
        self,
        ws: ClientConnection | ServerConnection,
        user_id: str,
        text: str,
    ) -> bool:
        """Handle built-in bridge slash commands immediately (0ms debounce)."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False

        parts = stripped.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"/screen", "/shot"}:
            try:
                shot = await asyncio.to_thread(_capture_desktop_screenshot_sync, PROJECT_INBOX_DIR)
                await self._send_private_image(ws, user_id, shot)
            except Exception as exc:
                await self._send_private_msg(ws, user_id, f"截屏没成功：{exc}")
            return True

        if cmd == "/clean":
            project_root = Path(__file__).resolve().parents[2]
            targets = discover_default_cache_targets(project_root)
            force_all = arg.lower() in {"all", "force", "全部"}
            rep = await asyncio.to_thread(
                run_safe_cache_cleanup, targets, force_all_expired_zero=force_all
            )
            await self._send_private_msg(ws, user_id, rep.format_human())
            return True

        if cmd in {"/chats", "/list"}:
            items = await asyncio.to_thread(list_local_conversations, 12)
            if not items:
                await self._send_private_msg(ws, user_id, "目前还没有找到已有的聊天记录。")
                return True
            self._last_listed_chats[user_id] = [(it.conversation_id, it.store) for it in items]
            lines = ["电脑与远程最近对话（发 /chat 序号 即可查看聊天内容并接着聊）："]
            for idx, it in enumerate(items, start=1):
                tag = "💻电脑" if it.store == "ide" else "📱远程"
                run_mark = " · 运行中" if it.running else ""
                lines.append(f"{idx}. [{tag}] {it.title} ({it.updated_human}{run_mark})")
            lines.append("\n也可以发 /new 开启全新对话。")
            await self._send_private_msg(ws, user_id, "\n".join(lines))
            return True

        if cmd == "/chat":
            if not arg:
                await self._send_private_msg(ws, user_id, "先发 /chats 查看编号，再发 /chat 1 切过去查看聊天内容。")
                return True
            target_cid = arg
            target_store = "auto"
            if arg.isdigit():
                cached = self._last_listed_chats.get(user_id)
                if not cached:
                    items = await asyncio.to_thread(list_local_conversations, 12)
                    cached = [(it.conversation_id, it.store) for it in items]
                    self._last_listed_chats[user_id] = cached
                pos = int(arg) - 1
                if 0 <= pos < len(cached):
                    target_cid, target_store = cached[pos]
                else:
                    await self._send_private_msg(ws, user_id, f"序号超出范围（1~{len(cached)}），请先发 /chats 查看。")
                    return True

            detail = await asyncio.to_thread(load_conversation_detail, target_cid, target_store, 30)
            if detail is None:
                await self._send_private_msg(ws, user_id, f"未找到对话：{target_cid}")
                return True

            session = self.sessions.get(user_id)
            if detail.store == "cli":
                await session.attach_cli_conversation(
                    detail.conversation_id, workspace=detail.workspace, title=detail.title
                )
            else:
                handoff = build_ide_handoff_context(detail)
                await session.attach_ide_conversation(
                    detail.conversation_id, handoff_context=handoff, workspace=detail.workspace, title=detail.title
                )

            preview_lines = [
                f"已切换到{'电脑端' if detail.store == 'ide' else '远程'}对话：{detail.title}",
                "—— 最近聊天内容 ——",
            ]
            for m in detail.messages[-4:]:
                who = "你" if m.role == "user" else "助理"
                clean_m, _, _ = extract_media_and_clean_for_qq(m.text, clean_markdown=True)
                snippet = clean_m[:240] + ("..." if len(clean_m) > 240 else "")
                preview_lines.append(f"【{who}】{snippet}")
            if not detail.messages:
                preview_lines.append("（该对话暂无消息记录）")
            preview_lines.append("\n现在直接发消息就能接着这个窗口往下聊。")
            await self._send_private_msg(ws, user_id, "\n".join(preview_lines))
            return True

        if cmd == "/help":
            await self._send_private_msg(ws, user_id, HELP_TEXT)
            return True

        if cmd == "/memory":
            mem_file = self.config.user_memory_file
            if mem_file is None:
                mem_file = Path(__file__).resolve().parents[2] / "memory" / "user_profile.md"
            mem_file.parent.mkdir(parents=True, exist_ok=True)
            if not arg:
                content = mem_file.read_text(encoding="utf-8").strip() if mem_file.is_file() else "（目前还没有记录长期记忆）"
                await self._send_private_msg(
                    ws,
                    user_id,
                    f"{content}\n\n想补充可以发：/memory 记住我常用目录是 D:\\work",
                )
                return True
            existing = mem_file.read_text(encoding="utf-8").rstrip() if mem_file.is_file() else "# 主人偏好与常用信息（跨会话持久记忆）"
            mem_file.write_text(f"{existing}\n- {arg}\n", encoding="utf-8")
            await self._send_private_msg(ws, user_id, f"记下来啦：{arg}\n（以后每次新对话都会自动带上）")
            return True

        if cmd in {"/new", "/reset"}:
            # Clear any pending debounced lines too
            pending = self._batches.pop(user_id, None)
            if pending and pending.timer_task:
                pending.timer_task.cancel()

            session = self.sessions.get(user_id)
            await session.reset()
            await self._send_private_msg(
                ws,
                user_id,
                f"好勒，已经清空上下文重新开始。\n当前模型：{session.model or '默认'}\n工作目录：{session.workspace}",
            )
            if self.config.agy_prewarm:
                t = asyncio.create_task(session.prewarm())
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            return True

        if cmd == "/model":
            session = self.sessions.get(user_id)
            if not arg:
                cooling = session.failover_pool.cooldown_summary()
                cool_note = ""
                if cooling:
                    items = ", ".join(f"{m}(约{mins}分钟后恢复)" for m, mins in cooling)
                    cool_note = f"\n额度冷却中：{items}"
                chain = " -> ".join(session.failover_pool.models[:5]) or "未配置"
                await self._send_private_msg(
                    ws,
                    user_id,
                    f"当前模型：{session.model or '默认'}\n"
                    f"自动切换链：{chain}{cool_note}\n\n"
                    "直接发下面指令就能秒切（保留聊天上下文）：\n"
                    "/model fast     -> 极速日常聊天 (claude-sonnet-5-5-low)\n"
                    "/model balanced -> 速度质量兼顾 (claude-sonnet-5-5-medium)\n"
                    "/model smart    -> 深度思考最强 (claude-opus-5-5-medium)\n"
                    "/model gemini   -> Gemini 4 均衡 (gemini-4-neon-medium)",
                )
                return True
            target_model = MODEL_ALIASES.get(arg.lower(), arg)
            await session.set_model(target_model)
            await self._send_private_msg(
                ws,
                user_id,
                f"已切到 {target_model}，咱们刚才聊的上下文还留着。",
            )
            return True

        if cmd == "/status":
            session = self.sessions.get(user_id)
            cooling = session.failover_pool.cooldown_summary()
            cool_str = (
                ", ".join(f"{m}({mins}m)" for m, mins in cooling)
                if cooling
                else "全部正常可用"
            )
            status_lines = [
                f"当前模型：{session.model or '默认'}",
                f"额度池状态：{cool_str}",
                f"进程状态：{'热待命中' if session.is_running else '待机（发消息即启）'}",
                f"正在忙碌：{'是' if session.is_busy else '否'}",
                f"会话 ID：{session.conversation_id or '（尚未建立）'}",
                f"累计轮次：{session.num_turns}",
                f"工作目录：{session.workspace}",
            ]
            await self._send_private_msg(ws, user_id, "\n".join(status_lines))
            return True

        if cmd == "/cd":
            if not arg:
                await self._send_private_msg(
                    ws,
                    user_id,
                    "用法：/cd <绝对路径或相对路径>\n比如：/cd D:\\projects\\my-app",
                )
                return True
            session = self.sessions.get(user_id)
            target = Path(arg.strip("\"'"))
            if not target.is_absolute():
                target = session.workspace / target
            try:
                await session.set_workspace(target)
            except FileNotFoundError as exc:
                await self._send_private_msg(ws, user_id, str(exc))
                return True
            await self._send_private_msg(
                ws,
                user_id,
                f"已切换工作目录：\n{session.workspace}",
            )
            return True

        if cmd == "/cancel":
            pending = self._batches.pop(user_id, None)
            if pending and pending.timer_task:
                pending.timer_task.cancel()
            session = self.sessions.get(user_id)
            killed = await session.cancel_current()
            msg = "已经帮你停掉刚才的任务了，随时可以接着聊。" if killed else "当前没有正在跑的任务哈。"
            await self._send_private_msg(ws, user_id, msg)
            return True

        return False

    async def handle_event(
        self,
        ws: ClientConnection | ServerConnection,
        event: dict[str, Any],
    ) -> None:
        ok, info = self._is_authorized_private_message(event)
        if not ok:
            if info.startswith("unauthorized:"):
                logger.warning("Ignored private message from non-whitelisted QQ: %s", info.split(":", 1)[1])
            elif info == "non_private":
                logger.debug("Ignored non-private message (group_id=%s)", event.get("group_id"))
            return

        user_id = info
        text = extract_plain_text(event)
        if not text:
            logger.debug("Ignored empty private message from user=%s", user_id)
            return

        logger.info("Received private message from QQ %s: %s", user_id, text[:120])

        if await self._handle_command(ws, user_id, text):
            return

        if is_pure_screenshot_request(text):
            try:
                shot = await asyncio.to_thread(_capture_desktop_screenshot_sync, PROJECT_INBOX_DIR)
                await self._send_private_image(ws, user_id, shot)
                return
            except Exception as exc:
                logger.warning("Fast-path screenshot failed, falling back to agy: %s", exc)

        prompt_text = await self._enrich_inbound_media(event, text)

        # Smart message burst coalescing (merges rapid consecutive lines / image + question)
        debounce = self.config.message_debounce_seconds
        if debounce <= 0.0:
            await self._dispatch_prompt(ws, user_id, prompt_text, send_typing=False)
            return

        now = time.monotonic()
        batch = self._batches.get(user_id)
        if batch is None:
            batch = _PendingBatch(prompts=[prompt_text], first_ts=now)
            self._batches[user_id] = batch
        else:
            batch.prompts.append(prompt_text)
            if batch.timer_task is not None:
                batch.timer_task.cancel()

        # Cap total wait since first message at 3.2s so rapid typing never feels delayed
        elapsed = now - batch.first_ts
        wait_s = max(0.05, min(debounce, 3.2 - elapsed))

        async def _flush_after() -> None:
            await asyncio.sleep(wait_s)
            popped = self._batches.pop(user_id, None)
            if not popped or not popped.prompts:
                return
            combined = "\n".join(popped.prompts)
            await self._dispatch_prompt(ws, user_id, combined, send_typing=True)

        t = asyncio.create_task(_flush_after())
        batch.timer_task = t
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _dispatch_prompt(
        self,
        ws: ClientConnection | ServerConnection,
        user_id: str,
        prompt_text: str,
        *,
        send_typing: bool = True,
    ) -> None:
        session = self.sessions.get(user_id)

        typing_task: asyncio.Task[None] | None = None
        if send_typing:
            async def _keep_typing() -> None:
                while True:
                    await self._set_typing_status(ws, user_id)
                    await asyncio.sleep(6.0)

            typing_task = asyncio.create_task(_keep_typing())

        async def _send_notice() -> None:
            await self._send_private_msg(ws, user_id, "稍等哈，这步稍微有点慢，弄好马上跟你说~")

        on_delay = _send_notice if self.config.progress_notice_delay_seconds > 0 else None
        try:
            result = await session.ask(prompt_text, on_delay_notice=on_delay)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

        if result.status == "SUCCESS":
            raw_reply = result.response or ""
            clean_reply, image_paths, file_paths = extract_media_and_clean_for_qq(
                raw_reply, clean_markdown=self.config.clean_qq_formatting
            )
            if result.switched_from_model and result.active_model:
                switch_note = (
                    f"（顺带说一声：{result.switched_from_model} 额度刚用满，"
                    f"已自动切到备用模型 {result.active_model} 接着聊）"
                )
                clean_reply = f"{clean_reply}\n\n{switch_note}" if clean_reply else switch_note
            if clean_reply:
                await self._send_private_msg(ws, user_id, clean_reply)
            for img_path in image_paths:
                await self._send_private_image(ws, user_id, img_path)
            for file_path in file_paths:
                await self._send_private_file(ws, user_id, file_path)
            if not clean_reply and not image_paths and not file_paths:
                await self._send_private_msg(ws, user_id, "搞定啦。")
        else:
            err = result.error or f"刚才卡了一下（{result.status}）"
            clean_resp, image_paths, file_paths = extract_media_and_clean_for_qq(
                result.response or "", clean_markdown=self.config.clean_qq_formatting
            )
            reply = f"刚才这步出了点小状况：\n{err}"
            if clean_resp:
                reply = f"{clean_resp}\n\n{reply}"
            await self._send_private_msg(ws, user_id, reply)
            for img_path in image_paths:
                await self._send_private_image(ws, user_id, img_path)
            for file_path in file_paths:
                await self._send_private_file(ws, user_id, file_path)

    def _spawn_event_task(
        self,
        ws: ClientConnection | ServerConnection,
        event: dict[str, Any],
    ) -> None:
        async def _runner() -> None:
            try:
                await self.handle_event(ws, event)
            except Exception:
                logger.exception("Unhandled error while processing OneBot event")

        task = asyncio.create_task(_runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume_connection(self, ws: ClientConnection | ServerConnection) -> None:
        self._active_ws = ws
        try:
            async for raw_msg in ws:
                if not isinstance(raw_msg, str):
                    continue
                try:
                    payload = json.loads(raw_msg)
                except json.JSONDecodeError:
                    logger.debug("Skipped non-JSON WebSocket frame")
                    continue

                if not isinstance(payload, dict):
                    continue
                if "echo" in payload and "post_type" not in payload:
                    echo_id = str(payload.get("echo") or "")
                    if payload.get("status") == "failed" and not echo_id.startswith("typing-"):
                        logger.error("OneBot action failed: %s", payload)
                    continue

                if payload.get("post_type") == "meta_event":
                    continue

                self._spawn_event_task(ws, payload)
        finally:
            if self._active_ws is ws:
                self._active_ws = None

    async def run_forward_ws(self, stop_event: asyncio.Event | None = None) -> None:
        """Connect to NapCatQQ's WebSocket Server with automatic reconnect."""
        headers: dict[str, str] = {}
        if self.config.onebot_access_token:
            headers["Authorization"] = f"Bearer {self.config.onebot_access_token}"

        backoff = 2.0
        while stop_event is None or not stop_event.is_set():
            try:
                logger.info("Connecting to OneBot Forward WS: %s", self.config.onebot_ws_url)
                async with websockets.connect(
                    self.config.onebot_ws_url,
                    additional_headers=headers or None,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    logger.info("Connected to NapCatQQ OneBot WS: %s", self.config.onebot_ws_url)
                    self._active_ws = ws
                    self._schedule_prewarm()
                    self._schedule_cache_cleaner()
                    backoff = 2.0
                    if stop_event is not None:
                        consume_task = asyncio.create_task(self._consume_connection(ws))
                        wait_stop = asyncio.create_task(stop_event.wait())
                        done, pending = await asyncio.wait(
                            {consume_task, wait_stop},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for p in pending:
                            p.cancel()
                        if consume_task in done:
                            consume_task.result()
                    else:
                        await self._consume_connection(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop_event is not None and stop_event.is_set():
                    break
                logger.warning(
                    "OneBot WS connection error (%s). Reconnecting in %.0fs...",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)

    def _validate_reverse_ws_token(self, ws: ServerConnection) -> bool:
        expected = self.config.onebot_access_token
        if not expected:
            return True

        req = ws.request
        if req is None:
            return False

        auth_header = req.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {expected}") or hmac.compare_digest(
            auth_header, f"Token {expected}"
        ):
            return True

        parsed = urlsplit(req.path)
        query_tokens = parse_qs(parsed.query).get("access_token", [])
        return any(hmac.compare_digest(tok, expected) for tok in query_tokens)

    async def _handle_reverse_ws_client(self, ws: ServerConnection) -> None:
        req_path = urlsplit(ws.request.path).path if ws.request else ""
        if req_path != self.config.onebot_reverse_path:
            logger.warning("Rejected Reverse WS connection on unexpected path: %s", req_path)
            await ws.close(code=1008, reason="Invalid path")
            return

        if not self._validate_reverse_ws_token(ws):
            logger.warning("Rejected Reverse WS connection due to invalid access token")
            await ws.close(code=1008, reason="Unauthorized")
            return

        logger.info("NapCatQQ connected via Reverse WS (%s)", ws.remote_address)
        self._active_ws = ws
        self._schedule_prewarm()
        self._schedule_cache_cleaner()
        try:
            await self._consume_connection(ws)
        finally:
            logger.info("NapCatQQ disconnected from Reverse WS (%s)", ws.remote_address)

    async def run_reverse_ws(self, stop_event: asyncio.Event | None = None) -> None:
        """Listen as a Reverse WebSocket Server for NapCatQQ to connect."""
        logger.info(
            "Starting OneBot Reverse WS server at ws://%s:%d%s",
            self.config.onebot_reverse_host,
            self.config.onebot_reverse_port,
            self.config.onebot_reverse_path,
        )
        async with websockets.serve(
            self._handle_reverse_ws_client,
            self.config.onebot_reverse_host,
            self.config.onebot_reverse_port,
            ping_interval=20,
            ping_timeout=20,
            max_size=16 * 1024 * 1024,
        ):
            if stop_event is not None:
                await stop_event.wait()
            else:
                await asyncio.Future()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        try:
            if self.config.onebot_mode == "forward_ws":
                await self.run_forward_ws(stop_event=stop_event)
            else:
                await self.run_reverse_ws(stop_event=stop_event)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for batch in self._batches.values():
            if batch.timer_task is not None:
                batch.timer_task.cancel()
        self._batches.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.sessions.shutdown_all()
