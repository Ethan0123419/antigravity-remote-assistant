"""Desktop capture, secure media validation, and workspace/Git inspection utilities for 反重力助手."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

THOUGHT_TAG_RE = re.compile(
    r"<(?:thought|thinking|analysis)\b[^>]*>.*?</(?:thought|thinking|analysis)>",
    re.IGNORECASE | re.DOTALL,
)
SEND_IMAGE_RE = re.compile(r"\[SEND_IMAGE:\s*[\"']?([^\]\"'\r\n]+?)[\"']?\s*\]", re.IGNORECASE)
SEND_FILE_RE = re.compile(r"\[SEND_FILE:\s*[\"']?([^\]\"'\r\n]+?)[\"']?\s*\]", re.IGNORECASE)

PROJECT_INBOX_DIR = Path(__file__).resolve().parents[2] / "inbox"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_SEND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SEND_FILE_BYTES = 100 * 1024 * 1024
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "auth.json",
    "passkey.json",
    "cookies.sqlite",
    "credentials.json",
}

HIDDEN_DIR_SKIP = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
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


def sanitize_filename(name: str) -> str:
    """Keep a safe basename without path traversal characters."""
    base = Path(name or "file.bin").name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return (cleaned or "file.bin")[:120]


def is_safe_outbound_file(path: Path, *, image_only: bool = False) -> bool:
    """Block sensitive credential files and enforce extension / size limits."""
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            return False
        name_low = resolved.name.lower()
        if name_low in SENSITIVE_FILENAMES or name_low.startswith(".env."):
            logger.warning("Blocked sending sensitive file: %s", resolved)
            return False
        size = resolved.stat().st_size
        if image_only:
            if resolved.suffix.lower() not in ALLOWED_IMAGE_EXTS:
                return False
            return 0 < size <= MAX_SEND_IMAGE_BYTES
        return 0 < size <= MAX_SEND_FILE_BYTES
    except OSError:
        return False


def prune_inbox(inbox_dir: Path, keep: int = 35) -> None:
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


def capture_desktop_fast_jpeg_sync(
    inbox_dir: Path, max_width: int = 1600, quality: int = 80
) -> Path:
    """Capture a scaled, crisp JPEG desktop frame (~100-180 KB) for fast mobile preview."""
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
    prune_inbox(inbox_dir, keep=35)
    return dest


def _resolve_inside_workspace(workspace: Path, rel_path: str) -> Path | None:
    """Resolve a relative path strictly inside `workspace`, preventing path traversal."""
    try:
        ws_root = workspace.resolve()
        clean_rel = (rel_path or "").strip().lstrip("/\\")
        target = (ws_root / clean_rel).resolve() if clean_rel else ws_root
        if target != ws_root and ws_root not in target.parents:
            return None
        return target
    except OSError:
        return None


def inspect_workspace_git(workspace: Path) -> dict[str, Any]:
    """Return branch name and changed/untracked files for `workspace` (like Termote / Claude Code UI)."""
    ws = workspace.resolve()
    if not ws.is_dir():
        return {"is_git": False, "branch": "", "changes": []}

    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            **extra,
        )
        if branch_res.returncode != 0:
            return {"is_git": False, "branch": "", "changes": []}
        branch = branch_res.stdout.strip()

        st_res = subprocess.run(
            ["git", "-c", "core.quotepath=false", "status", "--porcelain=v1", "-uall"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **extra,
        )
        changes: list[dict[str, str]] = []
        for raw in (st_res.stdout or "").splitlines():
            if len(raw) < 4:
                continue
            code = raw[:2].strip() or "M"
            rel = raw[3:].strip()
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1].strip()
            rel = rel.strip('"')
            if Path(rel).name.lower() in SENSITIVE_FILENAMES:
                continue
            changes.append({"status": code, "path": rel})
            if len(changes) >= 120:
                break
        return {"is_git": True, "branch": branch, "changes": changes}
    except Exception:
        return {"is_git": False, "branch": "", "changes": []}


def get_workspace_file_diff(workspace: Path, rel_path: str, max_chars: int = 24000) -> dict[str, Any]:
    """Return unified git diff (or new-file preview for untracked files) inside `workspace`."""
    target = _resolve_inside_workspace(workspace, rel_path)
    if target is None or target.name.lower() in SENSITIVE_FILENAMES:
        return {"ok": False, "path": rel_path, "diff": "无法读取该路径或敏感配置文件已受保护。"}

    ws = workspace.resolve()
    rel_clean = str(target.relative_to(ws)).replace("\\", "/")
    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        res = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff", "HEAD", "--", rel_clean],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **extra,
        )
        diff_txt = (res.stdout or "").strip()
        if not diff_txt:
            # Might be untracked or repo has no HEAD commit yet
            res2 = subprocess.run(
                ["git", "-c", "core.quotepath=false", "diff", "--", rel_clean],
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                **extra,
            )
            diff_txt = (res2.stdout or "").strip()
        if not diff_txt and target.is_file():
            raw_txt = target.read_text(encoding="utf-8", errors="replace")
            added = "\n".join(f"+{ln}" for ln in raw_txt.splitlines()[:300])
            diff_txt = f"--- /dev/null\n+++ b/{rel_clean}\n@@ 新文件预览 @@\n{added}"
        if len(diff_txt) > max_chars:
            diff_txt = diff_txt[:max_chars] + "\n\n... (Diff 较长，已截断显示前 24KB)"
        return {"ok": True, "path": rel_clean, "diff": diff_txt or "（无文本差异或二进制文件）"}
    except Exception as exc:
        return {"ok": False, "path": rel_clean, "diff": f"读取 Diff 失败：{exc}"}


def list_workspace_directory(workspace: Path, rel_dir: str = "") -> dict[str, Any]:
    """List directories and safe files inside `workspace / rel_dir` for mobile file explorer."""
    target = _resolve_inside_workspace(workspace, rel_dir)
    ws = workspace.resolve()
    if target is None or not target.is_dir():
        return {"ok": False, "rel_dir": "", "entries": []}

    rel_norm = "" if target == ws else str(target.relative_to(ws)).replace("\\", "/")
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            name = child.name
            if name.lower() in SENSITIVE_FILENAMES or name.lower().startswith(".env."):
                continue
            child_rel = str(child.relative_to(ws)).replace("\\", "/")
            if child.is_dir():
                if name in HIDDEN_DIR_SKIP:
                    continue
                dirs.append({"name": name, "rel_path": child_rel, "is_dir": True, "size": 0})
            elif child.is_file():
                try:
                    sz = child.stat().st_size
                except OSError:
                    sz = 0
                files.append({"name": name, "rel_path": child_rel, "is_dir": False, "size": sz})
    except OSError:
        pass

    return {
        "ok": True,
        "workspace": str(ws),
        "rel_dir": rel_norm,
        "entries": (dirs + files)[:180],
    }


def read_workspace_file_preview(workspace: Path, rel_path: str, max_chars: int = 24000) -> dict[str, Any]:
    """Read a safe UTF-8 text preview (or image metadata) of a file inside `workspace`."""
    target = _resolve_inside_workspace(workspace, rel_path)
    ws = workspace.resolve()
    if target is None or not target.is_file() or not is_safe_outbound_file(target, image_only=False):
        return {"ok": False, "path": rel_path, "error": "文件不存在或属于受保护的隐私配置。"}

    rel_clean = str(target.relative_to(ws)).replace("\\", "/")
    ext = target.suffix.lower()
    is_img = ext in ALLOWED_IMAGE_EXTS
    size = target.stat().st_size
    if is_img:
        return {"ok": True, "path": rel_clean, "name": target.name, "size": size, "is_image": True, "content": ""}

    try:
        raw_bytes = target.read_bytes()[: max_chars * 3]
        if b"\x00" in raw_bytes[:2048]:
            return {
                "ok": True,
                "path": rel_clean,
                "name": target.name,
                "size": size,
                "is_image": False,
                "is_binary": True,
                "content": "",
            }
        txt = raw_bytes.decode("utf-8", errors="replace")
        truncated = len(txt) > max_chars or size > len(raw_bytes)
        if len(txt) > max_chars:
            txt = txt[:max_chars]
        return {
            "ok": True,
            "path": rel_clean,
            "name": target.name,
            "size": size,
            "is_image": False,
            "is_binary": False,
            "truncated": truncated,
            "content": txt,
        }
    except OSError as exc:
        return {"ok": False, "path": rel_clean, "error": str(exc)}
