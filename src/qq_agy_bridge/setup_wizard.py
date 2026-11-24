"""Automated cross-machine installer, configurator, and health doctor for Antigravity Remote Assistant.

Designed for BOTH human users and autonomous AI coding agents (Antigravity, Claude Code,
Cursor, Codex, Windsurf, Cline) deploying this repository on a fresh computer.

Examples:
  python -m qq_agy_bridge.setup_wizard --non-interactive --with-tunnel --create-shortcut --doctor
  python -m qq_agy_bridge.setup_wizard --doctor --json
  python -m qq_agy_bridge.setup_wizard --non-interactive --qq-users 12345678 --bot-qq 87654321
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import BridgeConfig, resolve_agy_executable
from .web_pwa import get_lan_ipv4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _update_env_key(lines: list[str], key: str, value: str, *, only_if_empty: bool = False) -> list[str]:
    prefix = f"{key}="
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix):
            cur_val = stripped[len(prefix) :].strip()
            if only_if_empty and cur_val and cur_val != "change-me":
                return lines
            lines[idx] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def ensure_env_configured(
    project_dir: Path = PROJECT_ROOT,
    *,
    force_overwrite: bool = False,
    mode: str = "",
    qq_users: str = "",
    bot_qq: str = "",
    napcat_dir: str = "",
    workspace: str = "",
    pwa_port: int = 0,
) -> Path:
    """Create or update .env safely with strong cryptographic tokens."""
    env_path = project_dir / ".env"
    example_path = project_dir / ".env.example"

    if not env_path.is_file() or force_overwrite:
        if example_path.is_file():
            base_text = example_path.read_text(encoding="utf-8")
        else:
            base_text = "WEB_PWA_ENABLED=true\nWEB_PWA_PORT=8765\nONEBOT_ENABLED=auto\n"
        env_path.write_text(base_text, encoding="utf-8")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    lines = _update_env_key(lines, "WEB_ACCESS_TOKEN", secrets.token_urlsafe(18), only_if_empty=True)
    lines = _update_env_key(lines, "ONEBOT_ACCESS_TOKEN", secrets.token_urlsafe(18), only_if_empty=True)

    if mode in {"pwa", "dual", "auto"}:
        onebot_val = "false" if mode == "pwa" else ("true" if mode == "dual" else "auto")
        lines = _update_env_key(lines, "ONEBOT_ENABLED", onebot_val)
    if qq_users.strip():
        lines = _update_env_key(lines, "ALLOWED_QQ_USERS", qq_users.strip())
    if bot_qq.strip():
        lines = _update_env_key(lines, "BOT_QQ", bot_qq.strip())
    if napcat_dir.strip():
        lines = _update_env_key(lines, "NAPCAT_DIR", napcat_dir.strip())
    if workspace.strip():
        lines = _update_env_key(lines, "AGY_WORKSPACE", str(Path(workspace.strip()).expanduser().resolve()))
    if pwa_port > 0:
        lines = _update_env_key(lines, "WEB_PWA_PORT", str(pwa_port))

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Initialize runtime memory file from example template if missing
    mem_target = project_dir / "memory" / "user_profile.md"
    mem_example = project_dir / "memory" / "user_profile.example.md"
    if not mem_target.is_file() and mem_example.is_file():
        mem_target.parent.mkdir(parents=True, exist_ok=True)
        mem_target.write_text(mem_example.read_text(encoding="utf-8"), encoding="utf-8")

    return env_path


def ensure_cloudflared(project_dir: Path = PROJECT_ROOT) -> tuple[bool, str]:
    """Locate or download official cloudflared binary for encrypted 4G/5G tunnel access."""
    bin_dir = project_dir / "bin"
    exe_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local_bin = bin_dir / exe_name
    if local_bin.is_file():
        return True, str(local_bin)

    sys_found = shutil.which("cloudflared")
    if sys_found:
        return True, sys_found

    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine
    if sys.platform == "win32":
        asset = "cloudflared-windows-arm64.exe" if is_arm else "cloudflared-windows-amd64.exe"
    elif sys.platform == "linux":
        asset = "cloudflared-linux-arm64" if is_arm else "cloudflared-linux-amd64"
    else:
        return False, "macOS 请运行 `brew install cloudflared` 安装外网隧道组件（局域网扫码无需此组件）"

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        tmp_dest = bin_dir / f"{exe_name}.part"
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-Remote-Setup/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp_dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
        tmp_dest.replace(local_bin)
        if sys.platform != "win32":
            local_bin.chmod(0o755)
        return True, str(local_bin)
    except Exception as exc:
        return False, f"自动下载 cloudflared 失败（不影响同 Wi-Fi 局域网使用）：{exc}"


def create_windows_desktop_shortcut(
    project_dir: Path = PROJECT_ROOT,
    shortcut_name: str = "Antigravity 远程助理.lnk",
) -> tuple[bool, str]:
    """Create a Windows Desktop .lnk pointing to the glassmorphic launcher."""
    if sys.platform != "win32":
        return False, "桌面 .lnk 快捷方式仅适用于 Windows"

    venv_pyw = project_dir / ".venv" / "Scripts" / "pythonw.exe"
    target_exe = venv_pyw if venv_pyw.is_file() else Path(sys.executable)
    icon_path = project_dir / "assets" / "app.ico"

    # Resolve real Desktop folder (handles OneDrive redirected Desktop cleanly)
    ps_script = (
        "$desk = [Environment]::GetFolderPath('Desktop'); "
        f"$lnkPath = Join-Path $desk '{shortcut_name}'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut($lnkPath); "
        f"$s.TargetPath = '{target_exe}'; "
        "$s.Arguments = '-X utf8 -m qq_agy_bridge.launcher'; "
        f"$s.WorkingDirectory = '{project_dir}'; "
        f"if (Test-Path '{icon_path}') {{ $s.IconLocation = '{icon_path},0' }}; "
        "$s.Description = 'Antigravity 私人远程助理（手机端专属 App + QQ 控制台）'; "
        "$s.Save(); "
        "Write-Output $lnkPath"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[-1]
        return False, (res.stderr or "创建快捷方式失败").strip()
    except Exception as exc:
        return False, str(exc)


def run_doctor(project_dir: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Run comprehensive environment diagnostics for human or AI agent verification."""
    env_path = project_dir / ".env"
    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        {
            "name": "python_version",
            "ok": py_ok,
            "detail": f"Python {platform.python_version()} ({sys.executable})",
        }
    )

    env_vals = dotenv_values(env_path) if env_path.is_file() else {}
    agy_cfg = str(env_vals.get("AGY_EXECUTABLE") or "agy")
    agy_resolved = resolve_agy_executable(agy_cfg)
    agy_exists = bool(shutil.which(agy_resolved) or Path(agy_resolved).is_file())
    agy_ver = ""
    if agy_exists:
        try:
            extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            r = subprocess.run(
                [agy_resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=8,
                **extra,
            )
            agy_ver = (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else "ok"
        except Exception as exc:
            agy_ver = f"warning: {exc}"
    checks.append(
        {
            "name": "antigravity_cli",
            "ok": agy_exists,
            "detail": f"{agy_resolved} ({agy_ver})" if agy_exists else "未找到 agy，请先安装并登录 Antigravity IDE 或 CLI",
        }
    )

    cfg_ok = False
    pwa_lan_url = ""
    mode_summary = "unconfigured"
    if env_path.is_file():
        try:
            cfg = BridgeConfig.from_env(env_path)
            cfg_ok = (cfg.web_pwa_enabled or cfg.onebot_enabled) and (
                not cfg.onebot_enabled or len(cfg.allowed_qq_users) > 0
            )
            lan_ip = get_lan_ipv4()
            if cfg.web_pwa_enabled and cfg.web_access_token:
                pwa_lan_url = f"http://{lan_ip}:{cfg.web_pwa_port}/?token={cfg.web_access_token}"
            if cfg.web_pwa_enabled and cfg.onebot_enabled:
                mode_summary = "dual (手机端 Web App + QQ 私聊双通道)"
            elif cfg.web_pwa_enabled:
                mode_summary = "pwa_only (纯手机端专属 Web App，免 NapCat)"
            elif cfg.onebot_enabled:
                mode_summary = "qq_only (仅 QQ 私聊)"
        except Exception as exc:
            mode_summary = f"error: {exc}"

    checks.append(
        {
            "name": "env_config",
            "ok": cfg_ok,
            "detail": f".env 就绪 · 运行模式：{mode_summary}" if cfg_ok else f".env 未就绪 ({mode_summary})",
        }
    )

    cf_bin = project_dir / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    cf_path = str(cf_bin) if cf_bin.is_file() else (shutil.which("cloudflared") or "")
    checks.append(
        {
            "name": "cloudflare_tunnel_optional",
            "ok": True,
            "installed": bool(cf_path),
            "detail": f"已安装 ({cf_path})" if cf_path else "未安装（同 Wi-Fi 局域网可直接使用；如需 4G/5G 外网直连请加 --with-tunnel）",
        }
    )

    overall_ok = all(bool(c["ok"]) for c in checks)
    return {
        "ok": overall_ok,
        "project_dir": str(project_dir),
        "mode": mode_summary,
        "lan_pwa_url": pwa_lan_url,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Antigravity Remote Assistant 一键初始化与自检工具（支持人类交互与 AI 智能体静默部署）"
    )
    parser.add_argument("--non-interactive", action="store_true", help="无交互静默配置（推荐 AI 智能体使用）")
    parser.add_argument("--doctor", action="store_true", help="执行完整部署环境自检")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出自检结果")
    parser.add_argument("--force-env", action="store_true", help="强制基于模板重建 .env")
    parser.add_argument("--mode", choices=["auto", "pwa", "dual"], default="", help="通道模式：auto | pwa | dual")
    parser.add_argument("--qq-users", default="", help="允许私聊的 QQ 白名单（逗号分隔）")
    parser.add_argument("--bot-qq", default="", help="NapCat 登录的机器人 QQ 号（可选）")
    parser.add_argument("--napcat-dir", default="", help="NapCatNode 目录路径（可选）")
    parser.add_argument("--workspace", default="", help="默认工作目录路径（可选）")
    parser.add_argument("--pwa-port", type=int, default=0, help="手机端 Web App 端口（默认 8765）")
    parser.add_argument("--with-tunnel", action="store_true", help="自动检测/下载 cloudflared 外网加密隧道组件")
    parser.add_argument("--create-shortcut", action="store_true", help="在 Windows 桌面生成毛玻璃控制台快捷方式")
    args = parser.parse_args(argv)

    # Pure doctor check if only --doctor (--json) was requested and .env already exists
    only_doctor = args.doctor and not (
        args.non_interactive
        or args.force_env
        or args.mode
        or args.qq_users
        or args.with_tunnel
        or args.create_shortcut
    )

    if not only_doctor:
        ensure_env_configured(
            PROJECT_ROOT,
            force_overwrite=args.force_env,
            mode=args.mode,
            qq_users=args.qq_users,
            bot_qq=args.bot_qq,
            napcat_dir=args.napcat_dir,
            workspace=args.workspace,
            pwa_port=args.pwa_port,
        )
        if args.with_tunnel:
            ok_cf, msg_cf = ensure_cloudflared(PROJECT_ROOT)
            if not args.json:
                prefix = "✓" if ok_cf else "!"
                print(f"[{prefix}] 外网加密隧道组件: {msg_cf}")
        if args.create_shortcut and sys.platform == "win32":
            ok_lnk, msg_lnk = create_windows_desktop_shortcut(PROJECT_ROOT)
            if not args.json:
                prefix = "✓" if ok_lnk else "!"
                print(f"[{prefix}] 桌面快捷方式: {msg_lnk}")

    report = run_doctor(PROJECT_ROOT)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n=== Antigravity 远程助理 · 环境自检报告 ===")
        for item in report["checks"]:
            mark = "PASS" if item["ok"] else "FAIL"
            print(f"  [{mark}] {item['name']}: {item['detail']}")
        if report["lan_pwa_url"]:
            masked = report["lan_pwa_url"].split("?token=")[0] + "?token=<已写入 .env>"
            print(f"\n  手机端局域网入口: {masked}")
            print("  提示: 启动桌面控制台点击『📱 手机端专属 App』即可直接扫码免密连接！")
        print("===========================================\n")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
