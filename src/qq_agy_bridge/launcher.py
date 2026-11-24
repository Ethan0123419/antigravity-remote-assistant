"""Windows 11 translucent glassmorphic desktop launcher for NapCatQQ + Antigravity Bridge.

Features:
- Native Windows 11 DWM immersive dark chrome, rounded corners, seamless caption/border tint
- Smooth layered window translucency (-alpha) with live glass presets (82% / 90% / 95% / 100%)
- Procedurally rendered anti-aliased frosted glass cards (specular top rim + radial glow orbs)
- Custom dark minimal scrollbar (no bright Win32 scrollbar chrome)
- One-click Windows boot auto-start toggle (--minimized support)
- Win64-safe ctypes HANDLE/HWND declarations, auto-heal bridge watchdog, non-blocking shutdown
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import io
import json
import math
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable
from urllib.parse import urlparse

from dotenv import dotenv_values

from .maintenance import discover_default_cache_targets, run_safe_cache_cleanup
from .web_pwa import ensure_web_access_token, get_lan_ipv4

APP_TITLE = "QQ Antigravity 机器人"
MUTEX_NAME = "Local\\QQAgyBridgeLauncherSingleton"

PROJECT_DIR = Path(__file__).resolve().parents[2]
NAPCAT_DIR = Path(os.getenv("NAPCAT_DIR", str(PROJECT_DIR.parent / "NapCatNode")))
ICON_PATH = PROJECT_DIR / "assets" / "app.ico"
UI_PREFS_PATH = PROJECT_DIR / ".launcher_ui.json"
STARTUP_SHORTCUT_NAME = "QQ Antigravity 机器人.lnk"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
WEBUI_URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/webui(?:\?token=[A-Za-z0-9._-]+)?")
TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NOISY_LOG_PATTERNS = (
    "light_quic_transport_session.cc",
    "light_quic_crypto_config.cc",
    "ERROR:light_quic_",
    "light_quic_session_stats_report.cc",
)
QR_KEYWORDS = (
    "二维码",
    "qrcode",
    "快速登录失败",
    "请扫码",
    "扫码登录",
    "login fail",
    "token expired",
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

GLASS_PRESETS: tuple[tuple[str, float], ...] = (
    ("深透 82%", 0.82),
    ("凝霜 90%", 0.90),
    ("微透 95%", 0.95),
    ("纯实 100%", 1.00),
)


# ---------------------------------------------------------------------------
# Pure-stdlib anti-aliased RGBA PNG builders for frosted glass surfaces & orbs
# ---------------------------------------------------------------------------

def _pack_png_rgba(width: int, height: int, raw_rows: bytes) -> bytes:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw_rows, level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _hex_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def render_glass_card_b64(
    width: int,
    height: int,
    *,
    radius: int = 14,
    outer_bg: str = "#0B111B",
    top_fill: str = "#162338",
    bottom_fill: str = "#0F1828",
    rim_top: str = "#67E8F9",
    rim_bottom: str = "#283952",
    rim_top_alpha: float = 0.34,
    rim_bottom_alpha: float = 0.16,
) -> str:
    """Render an anti-aliased frosted glass rounded panel with a 1px specular top rim."""
    width = max(width, 24)
    height = max(height, 24)
    obr, obg, obb = _hex_rgb(outer_bg)
    tfr, tfg, tfb = _hex_rgb(top_fill)
    bfr, bfg, bfb = _hex_rgb(bottom_fill)
    rtr, rtg, rtb = _hex_rgb(rim_top)
    rbr, rbg, rbb = _hex_rgb(rim_bottom)

    hw = width * 0.5
    hh = height * 0.5
    rad = min(float(radius), hw - 1.0, hh - 1.0)
    bx = hw - 0.5 - rad
    by = hh - 0.5 - rad

    # Fast row builder: interior middle rows without corner curvature share identical X profiles
    rows = bytearray()
    inv_h = 1.0 / max(height - 1, 1)

    for y in range(height):
        vy = y * inv_h
        # Vertical frosted fill + subtle top sheen
        sheen = math.exp(-vy * 5.5) * 0.08
        fill_r = tfr + (bfr - tfr) * vy + 255.0 * sheen
        fill_g = tfg + (bfg - tfg) * vy + 255.0 * sheen
        fill_b = tfb + (bfb - tfb) * vy + 255.0 * sheen

        rim_r = rtr + (rbr - rtr) * vy
        rim_g = rtg + (rbg - rtg) * vy
        rim_b = rtb + (rbb - rtb) * vy
        rim_a = rim_top_alpha + (rim_bottom_alpha - rim_top_alpha) * vy

        py = abs(y - (hh - 0.5)) - by
        rows.append(0)  # PNG filter 0

        if py <= -1.5 and 2 <= y <= height - 3:
            # Fast path for straight vertical edges in the middle band!
            # x = 0: border pixel; x = 1..width-2: interior pixel; x = width-1: border pixel
            br_r = int(min(255.0, fill_r * (1.0 - rim_a) + rim_r * rim_a) + 0.5)
            br_g = int(min(255.0, fill_g * (1.0 - rim_a) + rim_g * rim_a) + 0.5)
            br_b = int(min(255.0, fill_b * (1.0 - rim_a) + rim_b * rim_a) + 0.5)

            in_r = int(min(255.0, fill_r) + 0.5)
            in_g = int(min(255.0, fill_g) + 0.5)
            in_b = int(min(255.0, fill_b) + 0.5)

            rows.extend((br_r, br_g, br_b, 255))
            rows.extend(bytes((in_r, in_g, in_b, 255)) * (width - 2))
            rows.extend((br_r, br_g, br_b, 255))
            continue

        for x in range(width):
            px = abs(x - (hw - 0.5)) - bx
            if px <= 0.0 and py <= 0.0:
                d = max(px, py) - rad
            else:
                d = math.hypot(max(px, 0.0), max(py, 0.0)) - rad

            cov = 0.0 if d >= 0.5 else (1.0 if d <= -0.5 else (0.5 - d))
            if cov <= 0.0:
                rows.extend((int(obr), int(obg), int(obb), 255))
                continue

            # 1.1px crisp specular glass border around d in [-1.2, 0.0]
            rim_w = max(0.0, 1.0 - abs(d + 0.55) * 1.15) * rim_a
            cr = min(255.0, fill_r * (1.0 - rim_w) + rim_r * rim_w)
            cg = min(255.0, fill_g * (1.0 - rim_w) + rim_g * rim_w)
            cb = min(255.0, fill_b * (1.0 - rim_w) + rim_b * rim_w)

            out_r = int(obr * (1.0 - cov) + cr * cov + 0.5)
            out_g = int(obg * (1.0 - cov) + cg * cov + 0.5)
            out_b = int(obb * (1.0 - cov) + cb * cov + 0.5)
            rows.extend((out_r, out_g, out_b, 255))

    return base64.b64encode(_pack_png_rgba(width, height, bytes(rows))).decode("ascii")


def render_glow_orb_b64(size: int, core_hex: str, bg_hex: str) -> str:
    """Render an anti-aliased status orb with a soft luminous radial halo."""
    br, bg, bb = _hex_rgb(bg_hex)
    cr, cg, cb = _hex_rgb(core_hex)
    cx = cy = (size - 1) * 0.5
    core_r = size * 0.23
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            dist = math.hypot(x - cx, y - cy)
            halo = math.exp(-((dist / (size * 0.34)) ** 2) * 2.1) * 0.42
            core = max(0.0, min(1.0, 0.5 - (dist - core_r) * 1.25))
            a = min(1.0, core + halo * (1.0 - core))
            # Slight white specular highlight on upper-left of core
            spec = max(0.0, min(1.0, 1.0 - math.hypot(x - (cx - 1.2), y - (cy - 1.2)) / (core_r * 0.9))) * core * 0.35
            r = min(255.0, br * (1.0 - a) + cr * a + 255.0 * spec)
            g = min(255.0, bg * (1.0 - a) + cg * a + 255.0 * spec)
            b = min(255.0, bb * (1.0 - a) + cb * a + 255.0 * spec)
            rows.extend((int(r + 0.5), int(g + 0.5), int(b + 0.5), 255))
    return base64.b64encode(_pack_png_rgba(size, size, bytes(rows))).decode("ascii")


# ---------------------------------------------------------------------------
# Win32 / DWM helpers (64-bit HANDLE/HWND safe)
# ---------------------------------------------------------------------------

def _setup_win32_prototypes() -> None:
    if sys.platform != "win32":
        return
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.GetLastError.argtypes = []
    k32.GetLastError.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    k32.SetThreadExecutionState.restype = wintypes.DWORD

    u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u32.FindWindowW.restype = wintypes.HWND
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    u32.GetParent.argtypes = [wintypes.HWND]
    u32.GetParent.restype = wintypes.HWND


_setup_win32_prototypes()


def acquire_single_instance_mutex() -> int | None:
    """Acquire a named Win32 mutex; if another instance exists, bring it to front and return None."""
    if sys.platform != "win32":
        return 1
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    last_err = k32.GetLastError()
    if last_err == 183:  # ERROR_ALREADY_EXISTS
        hwnd = u32.FindWindowW(None, APP_TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        if handle:
            k32.CloseHandle(handle)
        return None
    return int(handle) if handle else 1


def close_mutex_handle(handle: int | None) -> None:
    if sys.platform == "win32" and handle and handle != 1:
        try:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


def enable_high_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Antigravity.QQBridge.Launcher"
        )
    except Exception:
        pass


def set_prevent_system_sleep(prevent: bool) -> None:
    """Keep Windows CPU/network awake while assistant is running (still lets monitor turn off!)."""
    if sys.platform != "win32":
        return
    try:
        # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
        flags = 0x80000001 if prevent else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(wintypes.DWORD(flags))
    except Exception:
        pass


def apply_win11_glass_dwm(root: tk.Tk) -> None:
    """Apply Windows 11 DWM dark mode, rounded corners, seamless caption & glass rim."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        frame_str = root.wm_frame()
        hwnd = int(frame_str, 16) if frame_str and frame_str != "0x0" else ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            hwnd = root.winfo_id()

        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            wintypes.LPCVOID,
            wintypes.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long

        def _set_int(attr: int, val: int) -> None:
            c_val = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                wintypes.DWORD(attr),
                ctypes.byref(c_val),
                ctypes.sizeof(c_val),
            )

        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE
        _set_int(20, 1)
        # 33 = DWMWA_WINDOW_CORNER_PREFERENCE (2 = DWMWCP_ROUND)
        _set_int(33, 2)
        # 35 = DWMWA_CAPTION_COLOR (COLORREF 0x00BBGGRR for #0B111B -> 0x001B110B)
        _set_int(35, 0x001B110B)
        # 36 = DWMWA_TEXT_COLOR (#E2E8F0 -> 0x00F0E8E2)
        _set_int(36, 0x00F0E8E2)
        # 34 = DWMWA_BORDER_COLOR (subtle ice-cyan slate rim #2C4866 -> 0x0066482C)
        _set_int(34, 0x0066482C)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Settings, Startup Shortcut & Process Helpers
# ---------------------------------------------------------------------------

@dataclass
class LauncherSettings:
    bot_qq: str
    onebot_enabled: bool
    onebot_host: str
    onebot_port: int
    onebot_ws_url: str
    allowed_users: list[str]
    active_model: str
    webui_url: str
    web_pwa_enabled: bool
    web_pwa_port: int
    web_access_token: str
    tunnel_enabled: bool
    napcat_dir: Path
    project_dir: Path


def _bool_env(val: object, default: bool = True) -> bool:
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> LauncherSettings:
    env_file = PROJECT_DIR / ".env"
    env_data = dotenv_values(env_file) if env_file.is_file() else {}
    ws_url = str(env_data.get("ONEBOT_WS_URL") or "ws://127.0.0.1:3001").strip()
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3001

    raw_users = str(env_data.get("ALLOWED_QQ_USERS") or "")
    allowed_users = [u.strip() for u in raw_users.split(",") if u.strip()]
    active_model = str(env_data.get("AGY_MODEL") or "默认模型").strip() or "默认模型"

    raw_onebot = str(env_data.get("ONEBOT_ENABLED") or "auto").strip().lower()
    if raw_onebot == "auto":
        onebot_enabled = len(allowed_users) > 0
    else:
        onebot_enabled = _bool_env(raw_onebot, True)
    web_pwa_enabled = _bool_env(env_data.get("WEB_PWA_ENABLED"), True)
    web_pwa_port = int(str(env_data.get("WEB_PWA_PORT") or "8765").strip() or 8765)
    raw_web_tok = str(env_data.get("WEB_ACCESS_TOKEN") or "").strip()
    web_tok = ensure_web_access_token(env_file, raw_web_tok) if web_pwa_enabled else raw_web_tok
    tunnel_enabled = _bool_env(env_data.get("CLOUDFLARE_TUNNEL_ENABLED"), True)

    custom_napcat = str(env_data.get("NAPCAT_DIR") or "").strip()
    resolved_napcat_dir = Path(custom_napcat).expanduser().resolve() if custom_napcat else NAPCAT_DIR

    bot_qq = str(env_data.get("BOT_QQ") or "").strip()
    if not bot_qq:
        cfg_dir = resolved_napcat_dir / "napcat" / "config"
        if cfg_dir.is_dir():
            for p in sorted(cfg_dir.glob("onebot11_*.json")):
                stem_qq = p.stem.removeprefix("onebot11_")
                if stem_qq.isdigit():
                    bot_qq = stem_qq
                    break

    return LauncherSettings(
        bot_qq=bot_qq,
        onebot_enabled=onebot_enabled,
        onebot_host=host,
        onebot_port=port,
        onebot_ws_url=ws_url,
        allowed_users=allowed_users,
        active_model=active_model,
        webui_url=read_napcat_webui_url(resolved_napcat_dir),
        web_pwa_enabled=web_pwa_enabled,
        web_pwa_port=web_pwa_port,
        web_access_token=web_tok,
        tunnel_enabled=tunnel_enabled,
        napcat_dir=resolved_napcat_dir,
        project_dir=PROJECT_DIR,
    )


def read_napcat_webui_url(napcat_dir: Path) -> str:
    webui_cfg = napcat_dir / "napcat" / "config" / "webui.json"
    port = 6099
    token = ""
    if webui_cfg.is_file():
        try:
            data = json.loads(webui_cfg.read_text(encoding="utf-8"))
            port = int(data.get("port") or 6099)
            token = str(data.get("token") or "").strip()
        except Exception:
            pass
    return f"http://127.0.0.1:{port}/webui?token={token}" if token else f"http://127.0.0.1:{port}/webui"


def load_ui_prefs() -> dict:
    if UI_PREFS_PATH.is_file():
        try:
            return json.loads(UI_PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"alpha": 0.90}


def save_ui_prefs(prefs: dict) -> None:
    try:
        UI_PREFS_PATH.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_SHORTCUT_NAME


def is_autostart_enabled() -> bool:
    return get_startup_shortcut_path().is_file()


def set_autostart_enabled(enable: bool) -> bool:
    sc_path = get_startup_shortcut_path()
    if not enable:
        try:
            sc_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    target = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
    icon = PROJECT_DIR / "assets" / "app.ico"
    ps = f"""
$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut('{str(sc_path).replace("'", "''")}')
$sc.TargetPath = '{str(target).replace("'", "''")}'
$sc.Arguments = '-X utf8 -m qq_agy_bridge.launcher --minimized'
$sc.WorkingDirectory = '{str(PROJECT_DIR).replace("'", "''")}'
$sc.IconLocation = '{str(icon).replace("'", "''")},0'
$sc.Description = '开机静默启动 QQ Antigravity 机器人'
$sc.Save()
"""
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            check=True,
            capture_output=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return sc_path.is_file()
    except Exception:
        return False


def is_tcp_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_external_processes(exclude_pids: set[int]) -> tuple[list[int], list[int]]:
    """Return (napcat_pids, bridge_pids) currently running on Windows."""
    ps_script = r"""
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$napcat = @(
    $procs | Where-Object {
        $_.Name -ieq 'node.exe' -and (
            ($_.ExecutablePath -like '*NapCatNode*') -or
            ($_.CommandLine -like '*NapCatNode*')
        )
    } | ForEach-Object { [int]$_.ProcessId }
)
$bridge = @(
    $procs | Where-Object {
        ($_.Name -ieq 'qq-agy-bridge.exe') -or (
            $_.Name -ieq 'python.exe' -and (
                ($_.CommandLine -like '*qq-agy-bridge*') -or
                ($_.CommandLine -like '*qq_agy_bridge.main*')
            ) -and ($_.CommandLine -notlike '*qq_agy_bridge.launcher*')
        )
    } | ForEach-Object { [int]$_.ProcessId }
)
@{ napcat = $napcat; bridge = $bridge } | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps_script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = result.stdout.strip()
        if not raw:
            return ([], [])
        data = json.loads(raw)
        napcat_raw = data.get("napcat") or []
        bridge_raw = data.get("bridge") or []
        if isinstance(napcat_raw, int):
            napcat_raw = [napcat_raw]
        if isinstance(bridge_raw, int):
            bridge_raw = [bridge_raw]
        napcat_pids = [int(p) for p in napcat_raw if int(p) not in exclude_pids]
        bridge_pids = [int(p) for p in bridge_raw if int(p) not in exclude_pids]
        return (napcat_pids, bridge_pids)
    except Exception:
        return ([], [])


def kill_pid_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


FIREWALL_RULE_NAME = "Antigravity Mobile PWA"


def is_lan_firewall_rule_present(port: int) -> bool:
    """Check whether Windows Firewall already has our inbound TCP allow rule."""
    if sys.platform != "win32":
        return True
    try:
        res = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE_NAME}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0 and str(port) in (res.stdout or "")
    except Exception:
        return False


def request_lan_firewall_allow(port: int) -> bool:
    """Prompt Windows UAC via ShellExecuteW to allow inbound TCP on the PWA LAN port."""
    if sys.platform != "win32":
        return True
    try:
        args = (
            f'advfirewall firewall add rule name="{FIREWALL_RULE_NAME}" '
            f"dir=in action=allow protocol=TCP localport={int(port)}"
        )
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0)
        return int(ret) > 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Glassmorphic Launcher Application
# ---------------------------------------------------------------------------

class LauncherApp:
    # Deep Midnight Aurora Glass Palette
    BG = "#0B111B"
    GLASS_TOP = "#16243A"
    GLASS_BOTTOM = "#0F1929"
    GLASS_SURFACE = "#131E31"
    LOG_BG = "#070C14"

    TEXT_PRIMARY = "#F1F5F9"
    TEXT_SECONDARY = "#94A3B8"
    TEXT_MUTED = "#64748B"

    CYAN = "#38BDF8"
    EMERALD = "#34D399"
    AMBER = "#FBBF24"
    ROSE = "#FB7185"

    PILL_PRIMARY = "#0369A1"
    PILL_PRIMARY_HOVER = "#0284C7"
    PILL_SLATE = "#19283F"
    PILL_SLATE_HOVER = "#233754"
    PILL_DANGER = "#881337"
    PILL_DANGER_HOVER = "#BE123C"

    MAX_LOG_LINES = 2000

    def __init__(self, root: tk.Tk, settings: LauncherSettings, *, start_minimized: bool = False) -> None:
        self.root = root
        self.settings = settings
        self._webui_url = settings.webui_url
        self._prefs = load_ui_prefs()
        self._alpha: float = float(self._prefs.get("alpha", 0.90))

        self._napcat_proc: subprocess.Popen[str] | None = None
        self._bridge_proc: subprocess.Popen[str] | None = None
        self._tunnel_proc: subprocess.Popen[str] | None = None
        self._tunnel_url: str = ""
        self._qr_refresh_cb: Callable[[], None] | None = None
        self._napcat_external = False
        self._bridge_external = False
        self._bridge_connected = False
        self._qr_required = False
        self._desired_running = True
        self._auto_restart_timestamps: list[float] = []
        self._busy = False
        self._closing = False

        self._op_lock = threading.Lock()
        self._log_queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._show_debug = False
        self._show_debug_var = tk.BooleanVar(value=False)
        self._autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self._keep_awake_var = tk.BooleanVar(value=bool(self._prefs.get("keep_awake", True)))

        # PhotoImage caches (prevent GC)
        self._img_cache: dict[tuple, tk.PhotoImage] = {}
        self._orbs: dict[str, tk.PhotoImage] = {}

        self._configure_window(start_minimized=start_minimized)
        self._configure_dark_styles()
        self._prepare_orb_icons()
        self._build_ui()
        apply_win11_glass_dwm(self.root)
        self._apply_window_alpha(self._alpha)
        self._start_ui_loops()

        self.root.after(200, lambda: self._run_async(self._start_all_worker, False))

    def _configure_window(self, *, start_minimized: bool) -> None:
        self.root.title(APP_TITLE)
        self.root.configure(bg=self.BG)
        self.root.minsize(820, 580)
        self.root.geometry("940x670")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_requested)

        if ICON_PATH.is_file():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except Exception:
                pass

        self.root.update_idletasks()
        w, h = 940, 670
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max((sw - w) // 2, 20)
        y = max((sh - h) // 2 - 24, 20)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        if start_minimized:
            self.root.iconify()

    def _configure_dark_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Glass.Vertical.TScrollbar",
            gripcount=0,
            background="#1E2F47",
            darkcolor=self.LOG_BG,
            lightcolor=self.LOG_BG,
            troughcolor=self.LOG_BG,
            bordercolor=self.LOG_BG,
            arrowcolor="#64748B",
            relief="flat",
            borderwidth=0,
            arrowsize=0,
            width=8,
        )
        style.map(
            "Glass.Vertical.TScrollbar",
            background=[("active", "#38BDF8"), ("!active", "#1E2F47")],
        )

    def _prepare_orb_icons(self) -> None:
        for name, hex_col in (
            ("green", self.EMERALD),
            ("amber", self.AMBER),
            ("red", self.ROSE),
        ):
            b64 = render_glow_orb_b64(20, hex_col, self.GLASS_SURFACE)
            self._orbs[name] = tk.PhotoImage(data=b64)

    def _get_glass_image(
        self,
        width: int,
        height: int,
        *,
        radius: int = 14,
        rim_top: str = "#67E8F9",
        top_fill: str = "#16243A",
        bottom_fill: str = "#0F1929",
    ) -> tk.PhotoImage:
        key = (width, height, radius, rim_top, top_fill, bottom_fill)
        cached = self._img_cache.get(key)
        if cached is not None:
            return cached
        if len(self._img_cache) > 24:
            self._img_cache.clear()
        b64 = render_glass_card_b64(
            width,
            height,
            radius=radius,
            outer_bg=self.BG,
            top_fill=top_fill,
            bottom_fill=bottom_fill,
            rim_top=rim_top,
        )
        img = tk.PhotoImage(data=b64)
        self._img_cache[key] = img
        return img

    def _apply_window_alpha(self, alpha: float) -> None:
        self._alpha = max(0.75, min(1.0, alpha))
        try:
            self.root.attributes("-alpha", self._alpha)
        except Exception:
            pass
        if hasattr(self, "_alpha_btns"):
            for btn, val in self._alpha_btns:
                active = abs(val - self._alpha) < 0.015
                bg = "#0284C7" if active else "#132033"
                fg = "#FFFFFF" if active else "#94A3B8"
                btn._base_bg = bg  # type: ignore[attr-defined]
                btn.configure(bg=bg, fg=fg)
        self._prefs["alpha"] = round(self._alpha, 2)
        save_ui_prefs(self._prefs)

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG, padx=22, pady=16)
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Top Header Bar ──
        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill=tk.X, pady=(0, 14))

        left_hdr = tk.Frame(header, bg=self.BG)
        left_hdr.pack(side=tk.LEFT, fill=tk.X, expand=True)

        title_row = tk.Frame(left_hdr, bg=self.BG)
        title_row.pack(fill=tk.X)

        tk.Label(
            title_row,
            text="✦",
            bg=self.BG,
            fg=self.CYAN,
            font=("Segoe UI Symbol", 15, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(
            title_row,
            text="QQ · Antigravity 私人助理",
            bg=self.BG,
            fg=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 16, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT)

        whitelist_str = ", ".join(self.settings.allowed_users) if self.settings.allowed_users else "未配置"
        self.subtitle_lbl = tk.Label(
            left_hdr,
            text=(
                f"机器人 QQ {self.settings.bot_qq}   ·   "
                f"仅响应白名单私聊 ({whitelist_str})   ·   Google OAuth 直连"
            ),
            bg=self.BG,
            fg=self.TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            pady=3,
        )
        self.subtitle_lbl.pack(fill=tk.X)

        # Right Header Controls: Opacity Glass Switcher + Auto-Start Toggle
        right_hdr = tk.Frame(header, bg=self.BG)
        right_hdr.pack(side=tk.RIGHT, anchor="ne")

        glass_bar = tk.Frame(
            right_hdr,
            bg="#101B2B",
            highlightbackground="#233854",
            highlightthickness=1,
            padx=6,
            pady=4,
        )
        glass_bar.pack(side=tk.TOP, anchor="e")

        tk.Label(
            glass_bar,
            text="玻璃透感",
            bg="#101B2B",
            fg=self.TEXT_MUTED,
            font=("Microsoft YaHei UI", 8),
            padx=4,
        ).pack(side=tk.LEFT)

        self._alpha_btns: list[tuple[tk.Button, float]] = []
        for label, val in GLASS_PRESETS:
            b = self._make_button(
                glass_bar,
                text=label,
                command=lambda v=val: self._apply_window_alpha(v),
                bg="#132033",
                hover="#1E3350",
                fg="#94A3B8",
                padx=8,
                pady=2,
                font_size=8,
            )
            b.pack(side=tk.LEFT, padx=2)
            self._alpha_btns.append((b, val))

        chk_row = tk.Frame(right_hdr, bg=self.BG)
        chk_row.pack(side=tk.TOP, anchor="e", pady=(6, 0))

        awake_chk = tk.Checkbutton(
            chk_row,
            text="防自动休眠(允许息屏)",
            variable=self._keep_awake_var,
            command=self._on_toggle_keep_awake,
            bg=self.BG,
            fg=self.TEXT_SECONDARY,
            selectcolor="#132033",
            activebackground=self.BG,
            activeforeground=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 8),
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        awake_chk.pack(side=tk.LEFT, padx=(0, 10))

        autostart_chk = tk.Checkbutton(
            chk_row,
            text="开机静默自启",
            variable=self._autostart_var,
            command=self._on_toggle_autostart,
            bg=self.BG,
            fg=self.TEXT_SECONDARY,
            selectcolor="#132033",
            activebackground=self.BG,
            activeforeground=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 8),
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        autostart_chk.pack(side=tk.LEFT)

        # ── Frosted Glass Status Cards Row ──
        cards_row = tk.Frame(outer, bg=self.BG)
        cards_row.pack(fill=tk.X, pady=(0, 12))
        cards_row.columnconfigure(0, weight=1, uniform="glass_cards")
        cards_row.columnconfigure(1, weight=1, uniform="glass_cards")

        self.napcat_orb_lbl, self.napcat_status_lbl, self.napcat_detail_lbl = self._create_glass_card(
            cards_row,
            col=0,
            padx=(0, 8),
            title="NAPCAT · QQ 协议核心",
            detail=f"OneBot WS  {self.settings.onebot_host}:{self.settings.onebot_port}  ·  本地隔离登录",
            rim_color="#38BDF8",
        )
        self.bridge_orb_lbl, self.bridge_status_lbl, self.bridge_detail_lbl = self._create_glass_card(
            cards_row,
            col=1,
            padx=(8, 0),
            title="ANTIGRAVITY · 智能桥接与热会话",
            detail=f"当前模型：{self.settings.active_model}  ·  支持截图 / 传文件 / 长期记忆",
            rim_color="#34D399",
        )

        # ── Dynamic Glass Banner (hidden unless needed) ──
        self.banner_frame = tk.Frame(
            outer,
            bg="#1E1B10",
            highlightbackground="#D97706",
            highlightthickness=1,
            padx=14,
            pady=9,
        )
        self.banner_lbl = tk.Label(
            self.banner_frame,
            text="",
            bg="#1E1B10",
            fg="#FDE68A",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            justify=tk.LEFT,
        )
        self.banner_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.banner_btn = self._make_button(
            self.banner_frame,
            text="打开扫码后台",
            command=self._open_webui,
            bg="#D97706",
            hover="#B45309",
            fg="#FFFFFF",
            padx=12,
            pady=4,
        )
        self.banner_btn.pack(side=tk.RIGHT, padx=(10, 0))

        # ── Action Pill Bar ──
        self.btn_bar = tk.Frame(outer, bg=self.BG)
        self.btn_bar.pack(fill=tk.X, pady=(0, 12))

        self.btn_start = self._make_button(
            self.btn_bar,
            text="▶  一键启动",
            command=lambda: self._run_async(self._start_all_worker, False),
            bg=self.PILL_PRIMARY,
            hover=self.PILL_PRIMARY_HOVER,
            highlight="#38BDF8",
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_restart = self._make_button(
            self.btn_bar,
            text="↻  重启全部",
            command=lambda: self._run_async(self._start_all_worker, True),
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            highlight="#334E6E",
        )
        self.btn_restart.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = self._make_button(
            self.btn_bar,
            text="■  停止服务",
            command=lambda: self._run_async(self._stop_all_worker, True),
            bg=self.PILL_DANGER,
            hover=self.PILL_DANGER_HOVER,
            highlight="#F43F5E",
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 12))

        self.btn_pwa = self._make_button(
            self.btn_bar,
            text="📱  手机端专属 App",
            command=self._open_pwa_qr_dialog,
            bg="#312E81",
            hover="#4338CA",
            highlight="#818CF8",
        )
        self.btn_pwa.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_webui = self._make_button(
            self.btn_bar,
            text="🌐  NapCat 扫码后台",
            command=self._open_webui,
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            highlight="#2B425E",
        )
        self.btn_webui.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_folder = self._make_button(
            self.btn_bar,
            text="📂  配置与记忆",
            command=self._open_config_folder,
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            highlight="#2B425E",
        )
        self.btn_folder.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_clean = self._make_button(
            self.btn_bar,
            text="🧹  清理缓存",
            command=self._clean_cache_async,
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            highlight="#2B425E",
        )
        self.btn_clean.pack(side=tk.LEFT)

        # ── Log Stream Header ──
        log_hdr = tk.Frame(outer, bg=self.BG)
        log_hdr.pack(fill=tk.X, pady=(2, 6))

        tk.Label(
            log_hdr,
            text="实时运行流",
            bg=self.BG,
            fg=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)

        clear_btn = self._make_button(
            log_hdr,
            text="清空日志",
            command=self._clear_logs,
            bg="#132033",
            hover="#1E3350",
            fg=self.TEXT_SECONDARY,
            padx=10,
            pady=2,
            font_size=8,
        )
        clear_btn.pack(side=tk.RIGHT)

        debug_chk = tk.Checkbutton(
            log_hdr,
            text="显示底层网络帧",
            variable=self._show_debug_var,
            command=lambda: setattr(self, "_show_debug", bool(self._show_debug_var.get())),
            bg=self.BG,
            fg=self.TEXT_MUTED,
            selectcolor="#132033",
            activebackground=self.BG,
            activeforeground=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 8),
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        debug_chk.pack(side=tk.RIGHT, padx=(0, 12))

        # ── Frosted Glass Terminal Panel ──
        log_glass_border = tk.Frame(
            outer,
            bg="#263B57",
            padx=1,
            pady=1,
        )
        log_glass_border.pack(fill=tk.BOTH, expand=True)

        inner_log = tk.Frame(log_glass_border, bg=self.LOG_BG, padx=4, pady=4)
        inner_log.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            inner_log,
            bg=self.LOG_BG,
            fg="#CBD5E1",
            insertbackground="#CBD5E1",
            selectbackground="#1E3A5F",
            font=("Consolas", 9),
            wrap=tk.WORD,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            state=tk.DISABLED,
        )
        scrollbar = ttk.Scrollbar(
            inner_log,
            orient=tk.VERTICAL,
            command=self.log_text.yview,
            style="Glass.Vertical.TScrollbar",
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(2, 0))
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("ts", foreground="#475569")
        self.log_text.tag_configure("sys", foreground="#94A3B8")
        self.log_text.tag_configure("napcat", foreground="#38BDF8")
        self.log_text.tag_configure("bridge", foreground="#34D399")
        self.log_text.tag_configure("warn", foreground="#FBBF24")
        self.log_text.tag_configure("err", foreground="#FB7185")

        # Footer
        tk.Label(
            outer,
            text="最小化窗口后助理会在后台安静值守（异常断线会自动自愈重连）；右上角可随时调节窗口玻璃通透度。",
            bg=self.BG,
            fg="#4E617A",
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            pady=5,
        ).pack(fill=tk.X)

    def _create_glass_card(
        self,
        parent: tk.Frame,
        col: int,
        padx: tuple[int, int],
        title: str,
        detail: str,
        rim_color: str,
    ) -> tuple[tk.Label, tk.Label, tk.Label]:
        """Build an anti-aliased frosted glass card using a Canvas backdrop + crisp interior widgets."""
        card_canvas = tk.Canvas(
            parent,
            bg=self.BG,
            height=96,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
        )
        card_canvas.grid(row=0, column=col, sticky="nsew", padx=padx)

        content = tk.Frame(card_canvas, bg=self.GLASS_SURFACE)

        tk.Label(
            content,
            text=title,
            bg=self.GLASS_SURFACE,
            fg="#7C93B0",
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
        ).pack(fill=tk.X)

        status_row = tk.Frame(content, bg=self.GLASS_SURFACE)
        status_row.pack(fill=tk.X, pady=(5, 4))

        orb_lbl = tk.Label(
            status_row,
            image=self._orbs["amber"],
            bg=self.GLASS_SURFACE,
            bd=0,
        )
        orb_lbl.pack(side=tk.LEFT, padx=(0, 8))

        status_lbl = tk.Label(
            status_row,
            text="正在检测状态...",
            bg=self.GLASS_SURFACE,
            fg=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        detail_lbl = tk.Label(
            content,
            text=detail,
            bg=self.GLASS_SURFACE,
            fg="#64748B",
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        detail_lbl.pack(fill=tk.X)

        bg_item = card_canvas.create_image(0, 0, anchor="nw")
        win_item = card_canvas.create_window(16, 13, anchor="nw", window=content)
        resize_after_id: list[str | None] = [None]

        def _redraw(event: tk.Event) -> None:  # type: ignore[type-arg]
            w, h = max(event.width, 60), max(event.height, 60)
            card_canvas.itemconfigure(win_item, width=max(w - 32, 30))

            def _render() -> None:
                resize_after_id[0] = None
                if self._closing:
                    return
                img = self._get_glass_image(
                    w,
                    h,
                    radius=14,
                    rim_top=rim_color,
                    top_fill=self.GLASS_SURFACE,
                    bottom_fill=self.GLASS_SURFACE,
                )
                card_canvas.itemconfigure(bg_item, image=img)

            if resize_after_id[0] is not None:
                try:
                    self.root.after_cancel(resize_after_id[0])
                except Exception:
                    pass
            resize_after_id[0] = self.root.after(25, _render)

        card_canvas.bind("<Configure>", _redraw)
        return (orb_lbl, status_lbl, detail_lbl)

    def _make_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        bg: str,
        hover: str,
        fg: str = "#F8FAFC",
        highlight: str | None = None,
        padx: int = 15,
        pady: int = 7,
        font_size: int = 9,
    ) -> tk.Button:
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground="#FFFFFF",
            disabledforeground="#475569",
            font=("Microsoft YaHei UI", font_size, "bold"),
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1 if highlight else 0,
            highlightbackground=highlight or bg,
            highlightcolor=highlight or bg,
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        btn._base_bg = bg  # type: ignore[attr-defined]
        btn._hover_bg = hover  # type: ignore[attr-defined]
        btn.bind(
            "<Enter>",
            lambda _e: btn.configure(bg=btn._hover_bg) if btn["state"] == tk.NORMAL else None,  # type: ignore[attr-defined]
        )
        btn.bind("<Leave>", lambda _e: btn.configure(bg=btn._base_bg))  # type: ignore[attr-defined]
        return btn

    def _on_toggle_autostart(self) -> None:
        want = bool(self._autostart_var.get())
        ok = set_autostart_enabled(want)
        if not ok:
            self._autostart_var.set(not want)
            self._enqueue_log("系统", "设置开机自启快捷方式失败。", "err")
            return
        msg = "已开启开机静默自启（开机后自动在任务栏最小化运行）。" if want else "已关闭开机自启。"
        self._enqueue_log("系统", msg, "sys")

    def _on_toggle_keep_awake(self) -> None:
        want = bool(self._keep_awake_var.get())
        self._prefs["keep_awake"] = want
        save_ui_prefs(self._prefs)
        set_prevent_system_sleep(want and self._desired_running)
        msg = "已开启防自动休眠保活（屏幕仍可正常自动关闭省电，网络与助理不断线）。" if want else "已关闭防休眠保活。"
        self._enqueue_log("系统", msg, "sys")

    def _enqueue_log(self, source: str, message: str, tag: str = "sys") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put((ts, source, f"{tag}|{message}"))

    def _post_ui(self, fn: Callable[[], None]) -> None:
        if not self._closing:
            self._ui_queue.put(fn)

    def _start_ui_loops(self) -> None:
        self._drain_ui_and_logs()
        self._schedule_health_poll()

    def _drain_ui_and_logs(self) -> None:
        if self._closing:
            return
        for _ in range(50):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                return

        batch: list[tuple[str, str, str, str]] = []
        for _ in range(80):
            try:
                ts, source, payload = self._log_queue.get_nowait()
            except queue.Empty:
                break
            tag, _, msg = payload.partition("|")
            batch.append((ts, source, tag, msg))

        if batch:
            self.log_text.configure(state=tk.NORMAL)
            for ts, source, tag, msg in batch:
                self.log_text.insert(tk.END, f"{ts}  ", "ts")
                self.log_text.insert(tk.END, f"[{source}] ", tag)
                self.log_text.insert(
                    tk.END,
                    f"{msg}\n",
                    "err" if tag == "err" else ("warn" if tag == "warn" else ""),
                )

            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > self.MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{line_count - self.MAX_LOG_LINES}.0")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        self.root.after(80, self._drain_ui_and_logs)

    def _schedule_health_poll(self) -> None:
        if self._closing:
            return
        threading.Thread(target=self._health_poll_worker, daemon=True).start()
        self.root.after(2500, self._schedule_health_poll)

    def _health_poll_worker(self, force: bool = False) -> None:
        if self._closing or (self._busy and not force):
            return
        port_open = is_tcp_port_open(self.settings.onebot_host, self.settings.onebot_port, timeout=0.35)
        napcat_alive = self._napcat_proc is not None and self._napcat_proc.poll() is None
        bridge_alive = self._bridge_proc is not None and self._bridge_proc.poll() is None

        if port_open and self._qr_required:
            self._qr_required = False
            self._enqueue_log("系统", "检测到 OneBot 端口 3001 已就绪，登录成功！", "sys")

        # Auto-heal watchdog: if Bridge exited unexpectedly while user wanted services running
        if (
            self._desired_running
            and not self._busy
            and self._bridge_proc is not None
            and not bridge_alive
            and not self._bridge_external
            and not self._closing
        ):
            now = time.monotonic()
            self._auto_restart_timestamps = [t for t in self._auto_restart_timestamps if now - t < 120.0]
            if len(self._auto_restart_timestamps) < 3:
                self._auto_restart_timestamps.append(now)
                self._enqueue_log("系统", "检测到桥接服务意外退出，正在自动自愈重启...", "warn")
                self._launch_bridge()
                bridge_alive = self._bridge_proc is not None and self._bridge_proc.poll() is None

        self._post_ui(lambda: self._refresh_status_ui(port_open, napcat_alive, bridge_alive))

    def _refresh_status_ui(self, port_open: bool, napcat_alive: bool, bridge_alive: bool) -> None:
        if self._closing:
            return
        # NapCat card
        if not self.settings.onebot_enabled:
            self._set_card(
                self.napcat_orb_lbl,
                self.napcat_status_lbl,
                "green",
                "免 QQ 纯净模式（仅运行手机端 App）",
            )
        elif self._qr_required and not port_open:
            self._set_card(self.napcat_orb_lbl, self.napcat_status_lbl, "amber", "等待手机 QQ 扫码登录")
            self._show_banner(
                kind="warn",
                text="NapCatQQ 登录态需要验证，请点击右侧按钮在浏览器中扫码登录。",
                btn_text="立即打开扫码页",
                btn_cmd=self._open_webui,
            )
        elif port_open:
            if napcat_alive:
                qq_tag = f" · QQ {self.settings.bot_qq}" if self.settings.bot_qq else ""
                self._set_card(
                    self.napcat_orb_lbl,
                    self.napcat_status_lbl,
                    "green",
                    f"在线值守中{qq_tag}",
                )
            elif self._napcat_external:
                self._set_card(
                    self.napcat_orb_lbl,
                    self.napcat_status_lbl,
                    "green",
                    "在线运行中 · 外部后台进程",
                )
            else:
                self._set_card(
                    self.napcat_orb_lbl,
                    self.napcat_status_lbl,
                    "green",
                    f"端口 {self.settings.onebot_port} 已就绪",
                )
        elif napcat_alive:
            self._set_card(self.napcat_orb_lbl, self.napcat_status_lbl, "amber", "正在启动并快速登录...")
        else:
            self._set_card(self.napcat_orb_lbl, self.napcat_status_lbl, "red", "已停止")

        # Bridge card
        if bridge_alive:
            if not self.settings.onebot_enabled:
                self._set_card(
                    self.bridge_orb_lbl,
                    self.bridge_status_lbl,
                    "green",
                    "手机端专属 App 已就绪 · 点上方扫码",
                )
            elif self._bridge_connected and port_open:
                self._set_card(
                    self.bridge_orb_lbl,
                    self.bridge_status_lbl,
                    "green",
                    "已连通 · 手机 App 与 QQ 双在线",
                )
            else:
                self._set_card(
                    self.bridge_orb_lbl,
                    self.bridge_status_lbl,
                    "amber",
                    "运行中 · 正在握手 OneBot...",
                )
        elif self._bridge_external:
            if port_open:
                self._set_card(
                    self.bridge_orb_lbl,
                    self.bridge_status_lbl,
                    "green",
                    "在线运行中 · 外部后台进程",
                )
            else:
                self._set_card(
                    self.bridge_orb_lbl,
                    self.bridge_status_lbl,
                    "amber",
                    "外部进程运行中（等待 NapCat）",
                )
        else:
            self._set_card(self.bridge_orb_lbl, self.bridge_status_lbl, "red", "已停止")

        if not self._qr_required:
            if self._napcat_external or self._bridge_external:
                self._show_banner(
                    kind="info",
                    text="检测到后台已有运行中的进程，已自动跳过重复启动。点右侧「接管并重启」可切换至本窗口统一管理并加载最新配置。",
                    btn_text="接管并重启",
                    btn_cmd=lambda: self._run_async(self._start_all_worker, True),
                )
            else:
                self._hide_banner()

    def _set_card(self, orb_lbl: tk.Label, lbl: tk.Label, orb_key: str, text: str) -> None:
        orb_img = self._orbs.get(orb_key)
        if orb_img is not None:
            orb_lbl.configure(image=orb_img)
        lbl.configure(text=text)

    def _show_banner(self, kind: str, text: str, btn_text: str, btn_cmd) -> None:
        if kind == "warn":
            bg, border, fg = "#26190B", "#D97706", "#FDE68A"
            btn_bg, btn_hover = "#D97706", "#B45309"
        else:
            bg, border, fg = "#0D253B", "#0284C7", "#E0F2FE"
            btn_bg, btn_hover = "#0284C7", "#0369A1"

        self.banner_frame.configure(bg=bg, highlightbackground=border)
        self.banner_lbl.configure(bg=bg, fg=fg, text=text)
        self.banner_btn._base_bg = btn_bg  # type: ignore[attr-defined]
        self.banner_btn._hover_bg = btn_hover  # type: ignore[attr-defined]
        self.banner_btn.configure(text=btn_text, command=btn_cmd, bg=btn_bg, activebackground=btn_hover)
        if not self.banner_frame.winfo_ismapped():
            self.banner_frame.pack(fill=tk.X, pady=(0, 12), before=self.btn_bar)

    def _hide_banner(self) -> None:
        if self.banner_frame.winfo_ismapped():
            self.banner_frame.pack_forget()

    def _set_buttons_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (self.btn_start, self.btn_restart, self.btn_stop):
            btn.configure(state=state)

    def _run_async(self, fn, *args) -> None:
        def _wrapper() -> None:
            if not self._op_lock.acquire(blocking=False):
                return
            try:
                self._busy = True
                self._post_ui(lambda: self._set_buttons_busy(True))
                fn(*args)
            finally:
                self._busy = False
                self._op_lock.release()
                if not self._closing:
                    self._post_ui(lambda: self._set_buttons_busy(False))
                    self._health_poll_worker(force=True)

        threading.Thread(target=_wrapper, daemon=True).start()

    def _start_all_worker(self, takeover: bool) -> None:
        self._desired_running = True
        if self._keep_awake_var.get():
            set_prevent_system_sleep(True)
        self._auto_restart_timestamps.clear()
        self.settings = load_settings()
        self._webui_url = self.settings.webui_url
        self._qr_required = False
        self._post_ui(
            lambda: self.bridge_detail_lbl.configure(
                text=f"当前模型：{self.settings.active_model}  ·  支持截图 / 传文件 / 长期记忆"
            )
        )

        if takeover:
            self._enqueue_log("系统", "正在平滑停止旧进程并重启全部服务...", "sys")
            self._stop_all_internal(include_external=True)
            time.sleep(0.8)
        else:
            self._enqueue_log("系统", "正在检查并启动 QQ · Antigravity 服务...", "sys")

        managed_pids: set[int] = {os.getpid()}
        if self._napcat_proc and self._napcat_proc.poll() is None:
            managed_pids.add(self._napcat_proc.pid)
        if self._bridge_proc and self._bridge_proc.poll() is None:
            managed_pids.add(self._bridge_proc.pid)

        ext_napcat, ext_bridge = find_external_processes(managed_pids)
        port_open = is_tcp_port_open(self.settings.onebot_host, self.settings.onebot_port)

        # 1. Start or detect NapCatQQ (when ONEBOT_ENABLED=true)
        if not self.settings.onebot_enabled:
            self._napcat_external = False
            self._enqueue_log("系统", "QQ 通道已设为关闭，仅启动专属手机端 Web App。", "sys")
        elif self._napcat_proc and self._napcat_proc.poll() is None:
            self._napcat_external = False
            self._enqueue_log("系统", "NapCatQQ 已在本窗口运行中。", "sys")
        elif port_open or ext_napcat:
            self._napcat_external = True
            self._enqueue_log(
                "系统",
                f"检测到 NapCatQQ 已在后台运行（端口 {self.settings.onebot_port} {'已开放' if port_open else '启动中'}），未重复启动。",
                "sys",
            )
        else:
            self._napcat_external = False
            self._launch_napcat()
            deadline = time.monotonic() + 14.0
            while time.monotonic() < deadline and not self._closing:
                if is_tcp_port_open(self.settings.onebot_host, self.settings.onebot_port):
                    self._enqueue_log("系统", f"NapCatQQ 就绪（端口 {self.settings.onebot_port} 已监听）。", "sys")
                    break
                if self._napcat_proc and self._napcat_proc.poll() is not None:
                    self._enqueue_log("系统", f"NapCatQQ 进程意外退出（退出码 {self._napcat_proc.returncode}）。", "err")
                    break
                if self._qr_required:
                    self._enqueue_log("系统", "NapCatQQ 等待扫码验证，扫码完成后将自动连通桥接。", "warn")
                    break
                time.sleep(0.4)

        # 2. Start or detect Bridge (+ Web PWA)
        if self._bridge_proc and self._bridge_proc.poll() is None:
            self._bridge_external = False
            self._enqueue_log("系统", "Antigravity 桥接服务已在本窗口运行中。", "sys")
        elif ext_bridge:
            self._bridge_external = True
            self._enqueue_log(
                "系统",
                f"检测到桥接服务已在后台运行（PID: {', '.join(map(str, ext_bridge))}），未重复启动。",
                "sys",
            )
        else:
            self._bridge_external = False
            self._launch_bridge()

        # 3. Start encrypted Cloudflare Quick Tunnel for anywhere mobile access
        if self.settings.web_pwa_enabled and self.settings.tunnel_enabled:
            if self._tunnel_proc is None or self._tunnel_proc.poll() is not None:
                self._launch_tunnel()

    def _launch_napcat(self) -> None:
        node_exe = self.settings.napcat_dir / "node.exe"
        index_js = self.settings.napcat_dir / "index.js"
        if not node_exe.is_file() or not index_js.is_file():
            self._enqueue_log("系统", f"未找到 NapCatNode 文件：{node_exe}", "err")
            return

        cmd = [str(node_exe), ".\\index.js"]
        if self.settings.bot_qq:
            cmd.extend(["-q", self.settings.bot_qq])
        qq_note = f" (QQ: {self.settings.bot_qq})" if self.settings.bot_qq else ""
        self._enqueue_log("系统", f"正在启动 NapCatQQ{qq_note}...", "sys")
        self._post_ui(
            lambda: self._set_card(
                self.napcat_orb_lbl, self.napcat_status_lbl, "amber", "正在启动并快速登录..."
            )
        )
        try:
            env = os.environ.copy()
            env["FORCE_COLOR"] = "0"
            self._napcat_proc = subprocess.Popen(
                cmd,
                cwd=str(self.settings.napcat_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            )
            threading.Thread(
                target=self._stream_process_output,
                args=(self._napcat_proc, "NapCat", "napcat"),
                daemon=True,
            ).start()
        except Exception as exc:
            self._enqueue_log("系统", f"启动 NapCatQQ 失败：{exc}", "err")

    def _launch_bridge(self) -> None:
        venv_python = self.settings.project_dir / ".venv" / "Scripts" / "python.exe"
        py_exe = venv_python if venv_python.is_file() else Path(sys.executable)
        if py_exe.name.lower() == "pythonw.exe":
            candidate = py_exe.with_name("python.exe")
            if candidate.is_file():
                py_exe = candidate

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        agy_bin = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin"
        if agy_bin.is_dir():
            env["PATH"] = f"{agy_bin};{env.get('PATH', '')}"

        cmd = [str(py_exe), "-X", "utf8", "-u", "-m", "qq_agy_bridge.main"]
        self._bridge_connected = False
        self._enqueue_log("系统", "正在启动 Antigravity 智能桥接与热会话...", "sys")
        self._post_ui(
            lambda: self._set_card(
                self.bridge_orb_lbl, self.bridge_status_lbl, "amber", "正在连接 OneBot WS..."
            )
        )
        try:
            self._bridge_proc = subprocess.Popen(
                cmd,
                cwd=str(self.settings.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            )
            threading.Thread(
                target=self._stream_process_output,
                args=(self._bridge_proc, "Bridge", "bridge"),
                daemon=True,
            ).start()
        except Exception as exc:
            self._enqueue_log("系统", f"启动桥接服务失败：{exc}", "err")

    def _launch_tunnel(self) -> None:
        cf_exe = self.settings.project_dir / "bin" / "cloudflared.exe"
        if not cf_exe.is_file():
            found = os.popen("where cloudflared 2>nul").read().strip().splitlines()
            if found and Path(found[0]).is_file():
                cf_exe = Path(found[0])
            else:
                self._enqueue_log("系统", "未检测到 cloudflared.exe，当前手机端可通过同 Wi-Fi 局域网扫码访问。", "warn")
                return

        self._tunnel_url = ""
        cmd = [
            str(cf_exe),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{self.settings.web_pwa_port}",
            "--protocol",
            "http2",
            "--edge-ip-version",
            "4",
            "--no-autoupdate",
        ]
        self._enqueue_log("系统", "正在建立手机端 4G/5G 加密隧道 (HTTP/2 直连优化)...", "sys")
        try:
            env = os.environ.copy()
            env["NO_PROXY"] = "127.0.0.1,localhost,::1"
            env["no_proxy"] = "127.0.0.1,localhost,::1"
            self._tunnel_proc = subprocess.Popen(
                cmd,
                cwd=str(self.settings.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            )
            threading.Thread(
                target=self._stream_process_output,
                args=(self._tunnel_proc, "Tunnel", "sys"),
                daemon=True,
            ).start()
        except Exception as exc:
            self._enqueue_log("系统", f"启动加密隧道失败：{exc}", "warn")

    def _stream_process_output(
        self,
        proc: subprocess.Popen[str],
        source: str,
        default_tag: str,
    ) -> None:
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                if self._closing:
                    break
                line = ANSI_RE.sub("", raw_line).rstrip("\r\n")
                if not line.strip():
                    continue

                if source == "Tunnel":
                    m_cf = TRYCLOUDFLARE_RE.search(line)
                    if m_cf and not self._tunnel_url:
                        self._tunnel_url = m_cf.group(0)
                        self._enqueue_log(
                            "Tunnel",
                            f"外网加密隧道已开通：{self._tunnel_url} （点击上方『📱 手机端专属 App』即可扫码直连）",
                            "bridge",
                        )
                        if self._qr_refresh_cb is not None:
                            self._post_ui(self._qr_refresh_cb)
                    if not self._show_debug:
                        continue

                if source == "NapCat":
                    url_match = WEBUI_URL_RE.search(line)
                    if url_match:
                        self._webui_url = url_match.group(0)
                    low = line.lower()
                    if any(k in low for k in QR_KEYWORDS):
                        if not self._qr_required:
                            self._qr_required = True
                            threading.Thread(target=self._health_poll_worker, daemon=True).start()
                    if not self._show_debug and any(p in line for p in NOISY_LOG_PATTERNS):
                        continue

                if source == "Bridge":
                    if "Connected to NapCatQQ OneBot WS" in line:
                        self._bridge_connected = True
                        threading.Thread(target=self._health_poll_worker, daemon=True).start()
                    elif "auto-switching to " in line:
                        new_m = line.split("auto-switching to ", 1)[1].split()[0].strip()
                        if new_m:
                            self._post_ui(
                                lambda m=new_m: self.bridge_detail_lbl.configure(
                                    text=f"当前模型：{m} (额度自动切换)  ·  支持截图 / 传文件 / 长期记忆"
                                )
                            )
                    elif (
                        "Disconnected from OneBot WS" in line
                        or "Failed to connect" in line
                        or "OneBot WS connection error" in line
                    ):
                        self._bridge_connected = False
                        threading.Thread(target=self._health_poll_worker, daemon=True).start()

                tag = default_tag
                if "[ERROR]" in line or "安全检查失败" in line or "未找到 agy" in line:
                    tag = "err"
                elif "[WARNING]" in line or "[warn]" in line.lower():
                    tag = "warn"

                self._enqueue_log(source, line, tag)
        except Exception as exc:
            if not self._closing:
                self._enqueue_log("系统", f"{source} 日志流中断：{exc}", "warn")
        finally:
            code = proc.poll()
            if source == "Bridge":
                self._bridge_connected = False
            if not self._closing and code is not None:
                self._enqueue_log("系统", f"{source} 进程已退出（退出码 {code}）。", "warn")

    def _stop_all_worker(self, include_external: bool) -> None:
        self._desired_running = False
        set_prevent_system_sleep(False)
        self._enqueue_log("系统", "正在停止所有服务...", "sys")
        self._stop_all_internal(include_external=include_external)
        self._enqueue_log("系统", "所有服务已安全停止。", "sys")

    def _stop_all_internal(self, include_external: bool) -> None:
        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            kill_pid_tree(self._tunnel_proc.pid)
        self._tunnel_proc = None
        self._tunnel_url = ""

        if self._bridge_proc and self._bridge_proc.poll() is None:
            kill_pid_tree(self._bridge_proc.pid)
        self._bridge_proc = None
        self._bridge_connected = False
        self._bridge_external = False

        managed_napcat_started = self._napcat_proc is not None
        if self._napcat_proc and self._napcat_proc.poll() is None:
            kill_pid_tree(self._napcat_proc.pid)
        self._napcat_proc = None
        self._napcat_external = False
        self._qr_required = False

        if include_external or managed_napcat_started:
            ext_napcat, ext_bridge = find_external_processes({os.getpid()})
            pids_to_kill = (ext_bridge + ext_napcat) if include_external else ext_napcat
            for pid in pids_to_kill:
                kill_pid_tree(pid)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not is_tcp_port_open(self.settings.onebot_host, self.settings.onebot_port, timeout=0.2):
                break
            time.sleep(0.2)

    def _open_webui(self) -> None:
        url = self._webui_url or read_napcat_webui_url(self.settings.napcat_dir)
        self._enqueue_log("系统", f"正在打开 NapCat 后台：{url}", "sys")
        webbrowser.open(url)

    def _open_pwa_qr_dialog(self) -> None:
        self.settings = load_settings()
        lan_ip = get_lan_ipv4()
        tok = self.settings.web_access_token
        lan_url = f"http://{lan_ip}:{self.settings.web_pwa_port}/?token={tok}"

        dlg = tk.Toplevel(self.root)
        dlg.title("手机端专属 Web App (PWA)")
        dlg.configure(bg=self.BG)
        dlg.geometry("475x575")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        apply_win11_glass_dwm(dlg)
        try:
            dlg.attributes("-alpha", self._alpha)
        except Exception:
            pass

        mode_var = tk.StringVar(value="lan")
        qr_img_ref: list[tk.PhotoImage] = []

        wrap = tk.Frame(dlg, bg=self.BG, padx=22, pady=18)
        wrap.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            wrap,
            text="📱 扫码打开你的专属手机端 App",
            bg=self.BG,
            fg=self.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack()

        tk.Label(
            wrap,
            text="在家连同一 Wi-Fi 请扫「局域网 1ms 极速码」（零绕路秒开）；出门用流量请扫「4G/5G 外网加密隧道」",
            bg=self.BG,
            fg=self.TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            wraplength=400,
            pady=4,
        ).pack()

        tab_row = tk.Frame(wrap, bg=self.BG, pady=8)
        tab_row.pack()

        btn_tab_lan = self._make_button(
            tab_row,
            text="📶 同 Wi-Fi 局域网 1ms 极速 (推荐)",
            command=lambda: _switch("lan"),
            bg=self.PILL_PRIMARY,
            hover=self.PILL_PRIMARY_HOVER,
            padx=12,
            pady=5,
            font_size=8,
        )
        btn_tab_lan.pack(side=tk.LEFT, padx=4)

        btn_tab_tunnel = self._make_button(
            tab_row,
            text="🌐 4G/5G 外网加密隧道",
            command=lambda: _switch("tunnel"),
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            padx=12,
            pady=5,
            font_size=8,
        )
        btn_tab_tunnel.pack(side=tk.LEFT, padx=4)

        qr_box = tk.Frame(wrap, bg="#111C2E", highlightbackground="#294366", highlightthickness=1, padx=14, pady=14)
        qr_box.pack(pady=8)
        qr_lbl = tk.Label(qr_box, bg="#111C2E", fg=self.TEXT_SECONDARY, font=("Microsoft YaHei UI", 9))
        qr_lbl.pack()

        url_status_lbl = tk.Label(
            wrap,
            text="",
            bg=self.BG,
            fg=self.CYAN,
            font=("Consolas", 8),
            wraplength=410,
            pady=4,
        )
        url_status_lbl.pack()

        act_row = tk.Frame(wrap, bg=self.BG, pady=8)
        act_row.pack()

        def _current_url() -> str:
            if mode_var.get() == "tunnel":
                return f"{self._tunnel_url}/?token={tok}" if self._tunnel_url else ""
            return lan_url

        def _render_qr() -> None:
            if not dlg.winfo_exists():
                return
            m = mode_var.get()
            btn_tab_tunnel.configure(bg=self.PILL_PRIMARY if m == "tunnel" else self.PILL_SLATE)
            btn_tab_tunnel._base_bg = self.PILL_PRIMARY if m == "tunnel" else self.PILL_SLATE  # type: ignore[attr-defined]
            btn_tab_lan.configure(bg=self.PILL_PRIMARY if m == "lan" else self.PILL_SLATE)
            btn_tab_lan._base_bg = self.PILL_PRIMARY if m == "lan" else self.PILL_SLATE  # type: ignore[attr-defined]

            target_u = _current_url()
            if not target_u:
                qr_lbl.configure(
                    image="",
                    text="\n⏳ 正在建立 Cloudflare 加密隧道（约 3~6 秒）...\n建好后此处会自动刷出二维码\n\n（若在家里同一 Wi-Fi 下，可先点上方『局域网零延迟』直接扫）\n",
                )
                url_status_lbl.configure(text="等待隧道分配 https://*.trycloudflare.com ...")
                return

            try:
                import segno
                buf = io.BytesIO()
                segno.make(target_u, error="m").write(
                    buf, kind="png", scale=6, border=2, dark="#F1F5F9", light="#111C2E"
                )
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                photo = tk.PhotoImage(data=b64)
                qr_img_ref.clear()
                qr_img_ref.append(photo)
                qr_lbl.configure(image=photo, text="")
                masked = target_u.split("?token=")[0] + "?token=••••••"
                url_status_lbl.configure(text=masked)
            except Exception as exc:
                qr_lbl.configure(image="", text=f"二维码生成失败：{exc}")

        def _switch(new_mode: str) -> None:
            mode_var.set(new_mode)
            _render_qr()

        def _copy_link() -> None:
            u = _current_url() or lan_url
            dlg.clipboard_clear()
            dlg.clipboard_append(u)
            url_status_lbl.configure(text="✓ 已复制完整免密链接到剪贴板！")

        def _open_local() -> None:
            webbrowser.open(f"http://127.0.0.1:{self.settings.web_pwa_port}/?token={tok}")

        self._make_button(
            act_row,
            text="📋 复制免密链接",
            command=_copy_link,
            bg=self.PILL_PRIMARY,
            hover=self.PILL_PRIMARY_HOVER,
            padx=11,
            pady=6,
        ).pack(side=tk.LEFT, padx=4)

        self._make_button(
            act_row,
            text="🖥 本机体验",
            command=_open_local,
            bg=self.PILL_SLATE,
            hover=self.PILL_SLATE_HOVER,
            padx=11,
            pady=6,
        ).pack(side=tk.LEFT, padx=4)

        fw_btn = self._make_button(
            act_row,
            text="🛡 放行局域网防火墙",
            command=lambda: _allow_fw(),
            bg="#1E3A5F",
            hover="#0284C7",
            padx=11,
            pady=6,
        )

        def _allow_fw() -> None:
            if request_lan_firewall_allow(self.settings.web_pwa_port):
                url_status_lbl.configure(text=f"✓ 已请求放行 TCP {self.settings.web_pwa_port} 端口，手机同 Wi-Fi 可直连！")
                fw_btn.pack_forget()

        def _check_fw_bg() -> None:
            ok = is_lan_firewall_rule_present(self.settings.web_pwa_port)
            if not ok:
                self._ui_queue.put(
                    lambda: fw_btn.pack(side=tk.LEFT, padx=4) if dlg.winfo_exists() else None
                )

        threading.Thread(target=_check_fw_bg, daemon=True).start()

        self._qr_refresh_cb = _render_qr
        dlg.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_qr_refresh_cb", None), dlg.destroy()))
        _render_qr()

    def _open_config_folder(self) -> None:
        try:
            os.startfile(str(self.settings.project_dir))  # type: ignore[attr-defined]
        except Exception as exc:
            self._enqueue_log("系统", f"打开目录失败：{exc}", "err")

    def _clean_cache_async(self) -> None:
        def _worker() -> None:
            targets = discover_default_cache_targets(self.settings.project_dir, self.settings.napcat_dir)
            rep = run_safe_cache_cleanup(targets, force_all_expired_zero=True)
            self._enqueue_log("系统", rep.format_human(), "sys")

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_logs(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _has_running_managed_services(self) -> bool:
        napcat_alive = self._napcat_proc is not None and self._napcat_proc.poll() is None
        bridge_alive = self._bridge_proc is not None and self._bridge_proc.poll() is None
        return napcat_alive or bridge_alive

    def _on_close_requested(self) -> None:
        if self._has_running_managed_services():
            choice = messagebox.askyesnocancel(
                "关闭 QQ · Antigravity 私人助理",
                "助理当前正在运行中。\n\n"
                "• 点击「是」：停止所有后台服务并彻底退出\n"
                "• 点击「否」：最小化到任务栏，继续安静值守\n"
                "• 点击「取消」：返回窗口",
                parent=self.root,
            )
            if choice is None:
                return
            if choice is False:
                self.root.iconify()
                return

            # Hide immediately so window never appears frozen while cleaning up process trees
            self._desired_running = False
            self._closing = True
            self.root.withdraw()

            def _shutdown_bg() -> None:
                try:
                    self._stop_all_internal(include_external=False)
                finally:
                    try:
                        self.root.quit()
                    except Exception:
                        pass

            threading.Thread(target=_shutdown_bg, daemon=True).start()
            return

        self._closing = True
        self.root.destroy()


def main() -> None:
    enable_high_dpi()
    set_windows_app_id()

    mutex_handle = acquire_single_instance_mutex()
    if mutex_handle is None:
        return

    start_minimized = "--minimized" in sys.argv
    root: tk.Tk | None = None
    try:
        settings = load_settings()
        root = tk.Tk()
        LauncherApp(root, settings, start_minimized=start_minimized)
        root.mainloop()
    finally:
        set_prevent_system_sleep(False)
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        close_mutex_handle(mutex_handle)


if __name__ == "__main__":
    main()
