"""Desktop capture, secure media validation, and workspace/Git inspection utilities for 反重力助手."""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
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


_CACHED_CODEX_CLI: Path | None = None
_CODEX_CLI_CHECKED: bool = False


def find_codex_cli_executable() -> Path | None:
    """Locate official OpenAI `codex.exe` CLI (PATH or WindowsApps `OpenAI.Codex` bundle)."""
    global _CACHED_CODEX_CLI, _CODEX_CLI_CHECKED
    if _CODEX_CLI_CHECKED:
        return _CACHED_CODEX_CLI
    _CODEX_CLI_CHECKED = True

    direct = shutil.which("codex")
    if direct:
        p = Path(direct)
        if p.is_file():
            _CACHED_CODEX_CLI = p
            return p

    if sys.platform == "win32":
        try:
            res = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                    "$pkg = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue | "
                    "Sort-Object Version -Descending | Select-Object -First 1; "
                    "if ($pkg) { Join-Path $pkg.InstallLocation 'app\\resources\\codex.exe' }",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            cand_raw = (res.stdout or "").strip().splitlines()
            if cand_raw:
                cand = Path(cand_raw[-1].strip())
                if cand.is_file():
                    _CACHED_CODEX_CLI = cand
                    return cand
        except Exception as exc:
            logger.debug("Codex CLI discovery skipped: %s", exc)

    return None


def send_prompt_to_antigravity_ide_window(
    conversation_id: str,
    title: str,
    prompt: str,
) -> tuple[bool, str]:
    """Deliver a user prompt directly into the live PC Antigravity Desktop IDE window via Windows UIA.

    Switches to `conversation_id` in the left Sidebar if another chat is currently shown, verifies the
    active webview Document URL matches `/c/<conversation_id>`, pastes `prompt` via Clipboard (`Ctrl+V`,
    immune to Chinese Pinyin IME composition), and presses `Enter`.
    """
    if sys.platform != "win32":
        return (False, "仅支持 Windows 桌面窗口投递")

    cid = (conversation_id or "").strip()
    if not cid:
        return (False, "会话 ID 为空")

    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AgyWin32 {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    public static void ClickAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(35);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    }

    public static void WheelDownAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(30);
        mouse_event(0x0800, 0, 0, -480, UIntPtr.Zero);
    }

    public static void PasteAndEnter() {
        keybd_event(0x11, 0, 0, UIntPtr.Zero);
        keybd_event(0x56, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        keybd_event(0x56, 0, 0x0002, UIntPtr.Zero);
        keybd_event(0x11, 0, 0x0002, UIntPtr.Zero);
    }

    public static void PressEnter() {
        keybd_event(0x0D, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        keybd_event(0x0D, 0, 0x0002, UIntPtr.Zero);
    }
}
"@

[AgyWin32]::SetProcessDPIAware() | Out-Null

$targetCid = $env:AGY_TARGET_CID
$targetTitle = $env:AGY_TARGET_TITLE
$promptBytes = [Convert]::FromBase64String($env:AGY_PROMPT_B64)
$promptText = [System.Text.Encoding]::UTF8.GetString($promptBytes)
$ideBrainDir = $env:AGY_IDE_BRAIN_DIR

$root = [System.Windows.Automation.AutomationElement]::RootElement
$topChildren = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)

$agyWin = $null
foreach ($el in $topChildren) {
    try {
        $hwndVal = $el.Current.NativeWindowHandle
        if ($hwndVal -eq 0) { continue }
        $proc = Get-Process -Id $el.Current.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $proc -or $proc.ProcessName -ne 'Antigravity') { continue }
        $agyWin = $el
        if ($el.Current.Name -match 'Antigravity') { break }
    } catch {}
}

if ($null -eq $agyWin) {
    Write-Output "ERR:未检测到打开的 Antigravity 桌面端窗口"
    exit 0
}

$hwnd = [IntPtr]$agyWin.Current.NativeWindowHandle
$winPid = $agyWin.Current.ProcessId
if ([AgyWin32]::IsIconic($hwnd)) {
    [AgyWin32]::ShowWindow($hwnd, 9) | Out-Null
    Start-Sleep -Milliseconds 220
}
try {
    $wshell = New-Object -ComObject WScript.Shell
    $wshell.AppActivate($winPid) | Out-Null
} catch {}
[AgyWin32]::BringWindowToTop($hwnd) | Out-Null
[AgyWin32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 140

function Get-ActiveCid($winEl) {
    $docCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Document
    )
    $docs = $winEl.FindAll([System.Windows.Automation.TreeScope]::Descendants, $docCond)
    foreach ($d in $docs) {
        try {
            $vp = $d.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            if ($null -ne $vp) {
                $u = [string]$vp.Current.Value
                if ($u -match '/c/([A-Za-z0-9_-]{6,80})') {
                    return $Matches[1]
                }
            }
        } catch {}
    }
    return ""
}

function Find-ConversationLink($scopeEl, $cidStr, $titleStr) {
    $linkCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Hyperlink
    )
    $links = $scopeEl.FindAll([System.Windows.Automation.TreeScope]::Descendants, $linkCond)
    $bestByTitle = $null
    foreach ($lnk in $links) {
        try {
            $vp = $null
            if ($lnk.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
                $href = [string]$vp.Current.Value
                if ($href -match ("/c/" + [regex]::Escape($cidStr) + "([/?#]|$)")) {
                    return $lnk
                }
            }
            if ($null -eq $bestByTitle -and $titleStr.Length -gt 0) {
                $rawName = [string]$lnk.Current.Name
                $cleanName = ($rawName -replace '\s+\d+\s*(秒|分钟|小时|天|周|个月|年).*$', '').Trim()
                if ($cleanName -eq $titleStr) {
                    $bestByTitle = $lnk
                } elseif ($cleanName.Length -ge 4) {
                    $stem = ($cleanName -replace '(\.\.\.|…)+$', '').Trim()
                    if ($stem.Length -ge 4 -and $titleStr.StartsWith($stem, [System.StringComparison]::OrdinalIgnoreCase)) {
                        $bestByTitle = $lnk
                    } elseif ($cleanName.StartsWith($titleStr, [System.StringComparison]::OrdinalIgnoreCase)) {
                        $bestByTitle = $lnk
                    }
                }
            }
        } catch {}
    }
    return $bestByTitle
}

function Activate-Element($el) {
    $rect = $el.Current.BoundingRectangle
    if (-not $rect.IsEmpty -and $rect.Width -gt 4 -and $rect.Height -gt 4) {
        $x = [int]($rect.Left + [Math]::Min(90, $rect.Width / 2))
        $y = [int]($rect.Top + ($rect.Height / 2))
        [AgyWin32]::ClickAt($x, $y)
        return $true
    }
    $inv = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$inv)) {
        $inv.Invoke()
        return $true
    }
    return $false
}

$activeCid = Get-ActiveCid $agyWin
if ($activeCid -ne $targetCid) {
    $navCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Custom
    )
    $sidebar = $null
    $allDesc = $agyWin.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            "Sidebar"
        ))
    )
    if ($allDesc.Count -gt 0) {
        $sidebar = $allDesc.Item(0)
    } else {
        $toggleBtns = $agyWin.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Button
            ))
        )
        foreach ($b in $toggleBtns) {
            if ($b.Current.Name -match '切换侧边栏|Toggle Sidebar') {
                Activate-Element $b | Out-Null
                Start-Sleep -Milliseconds 280
                break
            }
        }
        $allDesc2 = $agyWin.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                "Sidebar"
            ))
        )
        if ($allDesc2.Count -gt 0) { $sidebar = $allDesc2.Item(0) }
    }

    $searchScope = if ($null -ne $sidebar) { $sidebar } else { $agyWin }
    $targetLink = Find-ConversationLink $searchScope $targetCid $targetTitle

    if ($null -eq $targetLink -and $null -ne $sidebar) {
        $sRect = $sidebar.Current.BoundingRectangle
        if (-not $sRect.IsEmpty -and $sRect.Width -gt 20 -and $sRect.Height -gt 60) {
            $sx = [int]($sRect.Left + ($sRect.Width / 2))
            $sy = [int]($sRect.Top + ($sRect.Height * 0.65))
            for ($sc = 0; $sc -lt 4; $sc++) {
                [AgyWin32]::WheelDownAt($sx, $sy)
                Start-Sleep -Milliseconds 220
                $targetLink = Find-ConversationLink $sidebar $targetCid $targetTitle
                if ($null -ne $targetLink) { break }
            }
        }
    }

    if ($null -ne $targetLink) {
        Activate-Element $targetLink | Out-Null
        for ($w = 0; $w -lt 14; $w++) {
            Start-Sleep -Milliseconds 180
            $activeCid = Get-ActiveCid $agyWin
            if ($activeCid -eq $targetCid) { break }
        }
    }

    $activeCid = Get-ActiveCid $agyWin
    if ($activeCid -ne "" -and $activeCid -ne $targetCid) {
        Write-Output "ERR:桌面端当前未能切换到该会话窗口"
        exit 0
    }
}

$comboCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::ComboBox
)
$msgBox = $null
$combos = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $comboCond)
foreach ($cb in $combos) {
    if ($cb.Current.Name -match 'Message input|输入任何问题') {
        $msgBox = $cb
        break
    }
}
if ($null -eq $msgBox) {
    $editCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    )
    $edits = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
    foreach ($ed in $edits) {
        if ($ed.Current.Name -match 'Message input|输入任何问题') {
            $msgBox = $ed
            break
        }
    }
}

if ($null -eq $msgBox) {
    Write-Output "ERR:未找到桌面端对话输入框 (Message input)"
    exit 0
}

$boxRect = $msgBox.Current.BoundingRectangle
if ($boxRect.IsEmpty -or $boxRect.Width -lt 30 -or $boxRect.Height -lt 10) {
    Write-Output "ERR:桌面端对话输入框当前不可见"
    exit 0
}

$hadText = $false
$oldClip = ""
try {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
        $hadText = $true
        $oldClip = [System.Windows.Forms.Clipboard]::GetText()
    }
} catch {}

[AgyWin32]::SetForegroundWindow($hwnd) | Out-Null
$cx = [int]($boxRect.Left + [Math]::Min(120, $boxRect.Width / 2))
$cy = [int]($boxRect.Top + ($boxRect.Height / 2))
[AgyWin32]::ClickAt($cx, $cy)
Start-Sleep -Milliseconds 140

[System.Windows.Forms.Clipboard]::SetText($promptText)
Start-Sleep -Milliseconds 60
[AgyWin32]::PasteAndEnter()
Start-Sleep -Milliseconds 230
[AgyWin32]::PressEnter()
Start-Sleep -Milliseconds 200

if ($hadText) {
    try { [System.Windows.Forms.Clipboard]::SetText($oldClip) } catch {}
}

Write-Output "OK:已投递至电脑端 Antigravity 窗口"
"""

    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    env["AGY_TARGET_CID"] = cid
    env["AGY_TARGET_TITLE"] = (title or "").strip()
    env["AGY_PROMPT_B64"] = base64.b64encode((prompt or "").encode("utf-8")).decode("ascii")

    try:
        res = subprocess.run(
            [
                "powershell.exe",
                "-Sta",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=16,
            creationflags=CREATE_NO_WINDOW,
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        for ln in reversed(lines):
            if ln.startswith("OK:"):
                return (True, ln.removeprefix("OK:").strip())
            if ln.startswith("ERR:"):
                return (False, ln.removeprefix("ERR:").strip())
        err_tail = (res.stderr or "").strip()[:160]
        return (False, f"桌面窗口投递未返回确认 {err_tail}".strip())
    except Exception as exc:
        return (False, f"桌面窗口投递异常：{exc}")


def cancel_antigravity_ide_window_turn(conversation_id: str) -> bool:
    """Click the Stop/Cancel button in the open PC Antigravity IDE window if matching `conversation_id`."""
    if sys.platform != "win32" or not (conversation_id or "").strip():
        return False

    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AgyStopWin32 {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
    public static void ClickAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(30);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    }
}
"@
[AgyStopWin32]::SetProcessDPIAware() | Out-Null
$cid = $env:AGY_TARGET_CID
$root = [System.Windows.Automation.AutomationElement]::RootElement
foreach ($el in $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try {
        if ($el.Current.NativeWindowHandle -eq 0) { continue }
        $p = Get-Process -Id $el.Current.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $p -or $p.ProcessName -ne 'Antigravity') { continue }
        $docs = $el.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Document)))
        $matched = $false
        foreach ($d in $docs) {
            try {
                $vp = $d.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                if ([string]$vp.Current.Value -match ("/c/" + [regex]::Escape($cid))) { $matched = $true; break }
            } catch {}
        }
        if (-not $matched) { continue }
        $btns = $el.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
        foreach ($b in $btns) {
            if ($b.Current.Name -match '取消|停止|Stop|Cancel') {
                $r = $b.Current.BoundingRectangle
                if (-not $r.IsEmpty -and $r.Width -gt 6) {
                    [AgyStopWin32]::SetForegroundWindow([IntPtr]$el.Current.NativeWindowHandle) | Out-Null
                    [AgyStopWin32]::ClickAt([int]($r.Left + $r.Width / 2), [int]($r.Top + $r.Height / 2))
                    Write-Output "STOPPED"
                    exit 0
                }
            }
        }
    } catch {}
}
"""
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    env["AGY_TARGET_CID"] = conversation_id.strip()
    try:
        res = subprocess.run(
            ["powershell.exe", "-Sta", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return "STOPPED" in (res.stdout or "")
    except Exception:
        return False


def open_new_antigravity_ide_chat_window() -> tuple[bool, str]:
    """Click `+ 新建会话` (`New Conversation`) in the open PC Antigravity Desktop window."""
    if sys.platform != "win32":
        return (False, "仅支持 Windows 桌面窗口操作")

    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AgyNewWin32 {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
    public static void ClickAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(35);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    }
}
"@
[AgyNewWin32]::SetProcessDPIAware() | Out-Null

$root = [System.Windows.Automation.AutomationElement]::RootElement
$agyWin = $null
foreach ($el in $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try {
        if ($el.Current.NativeWindowHandle -eq 0) { continue }
        $p = Get-Process -Id $el.Current.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $p -or $p.ProcessName -ne 'Antigravity') { continue }
        $agyWin = $el
        if ($el.Current.Name -match 'Antigravity') { break }
    } catch {}
}
if ($null -eq $agyWin) {
    Write-Output "ERR:未检测到打开的 Antigravity 桌面窗口"
    exit 0
}

$hwnd = [IntPtr]$agyWin.Current.NativeWindowHandle
if ([AgyNewWin32]::IsIconic($hwnd)) {
    [AgyNewWin32]::ShowWindow($hwnd, 9) | Out-Null
    Start-Sleep -Milliseconds 220
}
try { (New-Object -ComObject WScript.Shell).AppActivate($agyWin.Current.ProcessId) | Out-Null } catch {}
[AgyNewWin32]::BringWindowToTop($hwnd) | Out-Null
[AgyNewWin32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 120

function Activate-El($el) {
    $r = $el.Current.BoundingRectangle
    if (-not $r.IsEmpty -and $r.Width -gt 4 -and $r.Height -gt 4) {
        [AgyNewWin32]::ClickAt([int]($r.Left + [Math]::Min(80, $r.Width / 2)), [int]($r.Top + $r.Height / 2))
        return $true
    }
    $inv = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$inv)) {
        $inv.Invoke()
        return $true
    }
    return $false
}

$sidebarDesc = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "Sidebar")))
if ($sidebarDesc.Count -eq 0) {
    $btns = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
    foreach ($b in $btns) {
        if ($b.Current.Name -match '切换侧边栏|Toggle Sidebar') {
            Activate-El $b | Out-Null
            Start-Sleep -Milliseconds 260
            break
        }
    }
}

$orCond = New-Object System.Windows.Automation.OrCondition(
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Hyperlink))
)
$candidates = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $orCond)
$newBtn = $null
foreach ($c in $candidates) {
    $nm = [string]$c.Current.Name
    if ($nm -match '新建会话|新建对话|New Conversation|New Chat') {
        $newBtn = $c
        break
    }
}

if ($null -eq $newBtn) {
    Write-Output "ERR:未在桌面窗口中找到「新建会话」按钮"
    exit 0
}

Activate-El $newBtn | Out-Null
Start-Sleep -Milliseconds 260
Write-Output "OK:已在电脑端打开新对话窗口"
"""
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    try:
        res = subprocess.run(
            ["powershell.exe", "-Sta", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        for ln in reversed([x.strip() for x in (res.stdout or "").splitlines() if x.strip()]):
            if ln.startswith("OK:"):
                return (True, ln.removeprefix("OK:").strip())
            if ln.startswith("ERR:"):
                return (False, ln.removeprefix("ERR:").strip())
        return (False, "打开电脑端新对话未返回确认")
    except Exception as exc:
        return (False, f"打开电脑端新对话异常：{exc}")


def create_and_send_antigravity_ide_new_chat(
    prompt: str,
    ide_store_dir: Path | None = None,
) -> tuple[bool, str, str]:
    """Create a brand-new conversation in the open PC Antigravity window, submit `prompt`, and return `(ok, new_cid, note)`."""
    if sys.platform != "win32":
        return (False, "", "仅支持 Windows 桌面窗口投递")

    ide_root = ide_store_dir if ide_store_dir is not None else (Path.home() / ".gemini" / "antigravity")
    brain_dir = ide_root / "brain"
    before_ids: set[str] = set()
    if brain_dir.is_dir():
        try:
            before_ids = {d.name for d in brain_dir.iterdir() if d.is_dir()}
        except OSError:
            pass

    ps_script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AgyCreateWin32 {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    public static void ClickAt(int x, int y) {
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(35);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    }
    public static void PasteClip() {
        keybd_event(0x11, 0, 0, UIntPtr.Zero);
        keybd_event(0x56, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        keybd_event(0x56, 0, 0x0002, UIntPtr.Zero);
        keybd_event(0x11, 0, 0x0002, UIntPtr.Zero);
    }
    public static void PressEnter() {
        keybd_event(0x0D, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(25);
        keybd_event(0x0D, 0, 0x0002, UIntPtr.Zero);
    }
}
"@
[AgyCreateWin32]::SetProcessDPIAware() | Out-Null

$promptBytes = [Convert]::FromBase64String($env:AGY_PROMPT_B64)
$promptText = [System.Text.Encoding]::UTF8.GetString($promptBytes)
$ideBrainDir = $env:AGY_IDE_BRAIN_DIR

$root = [System.Windows.Automation.AutomationElement]::RootElement
$agyWin = $null
foreach ($el in $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    try {
        if ($el.Current.NativeWindowHandle -eq 0) { continue }
        $p = Get-Process -Id $el.Current.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $p -or $p.ProcessName -ne 'Antigravity') { continue }
        $agyWin = $el
        if ($el.Current.Name -match 'Antigravity') { break }
    } catch {}
}
if ($null -eq $agyWin) {
    Write-Output "ERR:未检测到打开的 Antigravity 桌面端窗口"
    exit 0
}

$hwnd = [IntPtr]$agyWin.Current.NativeWindowHandle
if ([AgyCreateWin32]::IsIconic($hwnd)) {
    [AgyCreateWin32]::ShowWindow($hwnd, 9) | Out-Null
    Start-Sleep -Milliseconds 220
}
try { (New-Object -ComObject WScript.Shell).AppActivate($agyWin.Current.ProcessId) | Out-Null } catch {}
[AgyCreateWin32]::BringWindowToTop($hwnd) | Out-Null
[AgyCreateWin32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 140

function Get-ActiveCid($winEl) {
    $docCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Document
    )
    foreach ($d in $winEl.FindAll([System.Windows.Automation.TreeScope]::Descendants, $docCond)) {
        try {
            $vp = $d.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            if ($null -ne $vp) {
                $u = [string]$vp.Current.Value
                if ($u -match '/c/([A-Za-z0-9_-]{6,80})') {
                    return $Matches[1]
                }
            }
        } catch {}
    }
    return ""
}

function Activate-El($el) {
    $r = $el.Current.BoundingRectangle
    if (-not $r.IsEmpty -and $r.Width -gt 4 -and $r.Height -gt 4) {
        [AgyCreateWin32]::ClickAt([int]($r.Left + [Math]::Min(80, $r.Width / 2)), [int]($r.Top + $r.Height / 2))
        return $true
    }
    $inv = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$inv)) {
        $inv.Invoke()
        return $true
    }
    return $false
}

$prevCid = Get-ActiveCid $agyWin
$prevHasHistory = $false
if ($prevCid -ne "" -and $ideBrainDir -ne "") {
    $prevTf = Join-Path $ideBrainDir "$prevCid\.system_generated\logs\transcript.jsonl"
    if (Test-Path -LiteralPath $prevTf) {
        try {
            if ((Get-Item -LiteralPath $prevTf).Length -gt 0) { $prevHasHistory = $true }
        } catch { $prevHasHistory = $true }
    }
}

if ($prevHasHistory) {
    $sidebarDesc = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "Sidebar")))
    if ($sidebarDesc.Count -eq 0) {
        $btns = $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
        foreach ($b in $btns) {
            if ($b.Current.Name -match '切换侧边栏|Toggle Sidebar') {
                Activate-El $b | Out-Null
                Start-Sleep -Milliseconds 260
                break
            }
        }
    }
    $orCond = New-Object System.Windows.Automation.OrCondition(
        (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)),
        (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Hyperlink))
    )
    $newBtn = $null
    foreach ($c in $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $orCond)) {
        if ([string]$c.Current.Name -match '新建会话|新建对话|New Conversation|New Chat') {
            $newBtn = $c
            break
        }
    }
    if ($null -eq $newBtn) {
        Write-Output "ERR:未找到桌面窗口「新建会话」按钮"
        exit 0
    }
    Activate-El $newBtn | Out-Null
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Milliseconds 160
        $curCid = Get-ActiveCid $agyWin
        if ($curCid -ne $prevCid) { break }
    }
    $afterClickCid = Get-ActiveCid $agyWin
    if ($afterClickCid -ne "" -and $afterClickCid -eq $prevCid) {
        Write-Output "ERR:桌面窗口未能切换到新建会话页"
        exit 0
    }
}

$msgBox = $null
$comboCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::ComboBox)
foreach ($cb in $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $comboCond)) {
    if ($cb.Current.Name -match 'Message input|输入任何问题') { $msgBox = $cb; break }
}
if ($null -eq $msgBox) {
    $editCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
    foreach ($ed in $agyWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)) {
        if ($ed.Current.Name -match 'Message input|输入任何问题') { $msgBox = $ed; break }
    }
}
if ($null -eq $msgBox) {
    Write-Output "ERR:未找到新建会话输入框"
    exit 0
}
$boxRect = $msgBox.Current.BoundingRectangle
if ($boxRect.IsEmpty -or $boxRect.Width -lt 30 -or $boxRect.Height -lt 10) {
    Write-Output "ERR:新建会话输入框不可见"
    exit 0
}

$hadText = $false
$oldClip = ""
try {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
        $hadText = $true
        $oldClip = [System.Windows.Forms.Clipboard]::GetText()
    }
} catch {}

[AgyCreateWin32]::SetForegroundWindow($hwnd) | Out-Null
[AgyCreateWin32]::ClickAt([int]($boxRect.Left + [Math]::Min(120, $boxRect.Width / 2)), [int]($boxRect.Top + $boxRect.Height / 2))
Start-Sleep -Milliseconds 140

[System.Windows.Forms.Clipboard]::SetText($promptText)
Start-Sleep -Milliseconds 60
[AgyCreateWin32]::PasteClip()
Start-Sleep -Milliseconds 230
[AgyCreateWin32]::PressEnter()

$newCid = ""
for ($w = 0; $w -lt 22; $w++) {
    Start-Sleep -Milliseconds 180
    $cand = Get-ActiveCid $agyWin
    if ($cand -ne "" -and ((-not $prevHasHistory) -or ($cand -ne $prevCid))) {
        $newCid = $cand
        break
    }
}

if ($hadText) {
    try { [System.Windows.Forms.Clipboard]::SetText($oldClip) } catch {}
}

Write-Output "OK:$newCid"
"""
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    env["AGY_PROMPT_B64"] = base64.b64encode((prompt or "").encode("utf-8")).decode("ascii")
    env["AGY_IDE_BRAIN_DIR"] = str(brain_dir)

    try:
        res = subprocess.run(
            ["powershell.exe", "-Sta", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=18,
            creationflags=CREATE_NO_WINDOW,
        )
        ok_matched = False
        new_cid = ""
        for ln in reversed([x.strip() for x in (res.stdout or "").splitlines() if x.strip()]):
            if ln.startswith("OK:"):
                ok_matched = True
                new_cid = ln.removeprefix("OK:").strip()
                break
            if ln.startswith("ERR:"):
                return (False, "", ln.removeprefix("ERR:").strip())

        if not ok_matched:
            return (False, "", "新建桌面会话未返回确认")

        # Fallback: if Document URL hadn't updated yet, inspect newly created brain/<uuid> directory
        if not new_cid and brain_dir.is_dir():
            for _ in range(10):
                time.sleep(0.45)
                try:
                    candidates = [
                        d
                        for d in brain_dir.iterdir()
                        if d.is_dir() and d.name != "tempmediaStorage" and d.name not in before_ids
                    ]
                    if candidates:
                        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                        new_cid = candidates[0].name
                        break
                except OSError:
                    pass

        if not new_cid:
            return (False, "", "未能捕获新建桌面会话 ID")

        return (True, new_cid, "已在电脑端新建 Antigravity 会话并发送")
    except Exception as exc:
        return (False, "", f"新建桌面会话异常：{exc}")
