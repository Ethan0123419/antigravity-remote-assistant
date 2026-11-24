from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from .agy_session import SessionManager
from .config import BridgeConfig
from .onebot import OneBotBridge
from .web_pwa import WebPwaServer, ensure_web_access_token, get_lan_ipv4


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_preflight(config: BridgeConfig) -> bool:
    logger = logging.getLogger("qq_agy_bridge")
    ok = True

    if not config.onebot_enabled and not config.web_pwa_enabled:
        logger.error("安全检查失败：ONEBOT_ENABLED 与 WEB_PWA_ENABLED 不能同时关闭。")
        ok = False

    if config.onebot_enabled and not config.allowed_qq_users:
        logger.error(
            "安全检查失败：开启 QQ 通道时 ALLOWED_QQ_USERS 不能为空！请在 .env 中填入允许对话的 QQ 号。"
        )
        ok = False

    if (
        config.onebot_enabled
        and config.onebot_mode == "reverse_ws"
        and config.onebot_reverse_host not in {"127.0.0.1", "localhost", "::1"}
        and not config.onebot_access_token
    ):
        logger.error(
            "安全检查失败：Reverse WS 监听在非本机地址 (%s) 但未设置 ONEBOT_ACCESS_TOKEN，已拒绝启动！",
            config.onebot_reverse_host,
        )
        ok = False

    exe = config.agy_executable
    if not (shutil.which(exe) or Path(exe).is_file()):
        logger.error(
            "未找到 agy 可执行文件（当前配置：%s）。请确认 Antigravity CLI 已安装并登录。",
            exe,
        )
        ok = False

    return ok


async def _run_async(config: BridgeConfig, env_file: Path) -> None:
    logger = logging.getLogger("qq_agy_bridge")
    logger.info("=== Antigravity 私人助理桥接启动 ===")
    logger.info("默认模型         : %s", config.agy_model or "CLI 默认")
    logger.info(
        "额度自动容灾链   : %s (冷却 %dm)",
        " -> ".join(config.agy_fallback_models[:5]) if config.agy_fallback_models else "未启用",
        config.quota_cooldown_minutes,
    )
    logger.info(
        "定期缓存清理     : %s",
        f"每 {config.cache_clean_interval_hours:g} 小时自动清理"
        if config.cache_clean_interval_hours > 0
        else "关闭",
    )

    sessions = SessionManager(config)
    runners = []

    if config.onebot_enabled:
        logger.info("QQ 通道          : 已启用 (%s -> %s)", config.onebot_mode, config.onebot_ws_url)
        logger.info("允许私聊 QQ 白名单: %s", ", ".join(sorted(config.allowed_qq_users)))
        bridge = OneBotBridge(config, session_manager=sessions)
        runners.append(bridge.run())
    else:
        logger.info("QQ 通道          : 已关闭（仅运行专属手机端 Web App）")

    if config.web_pwa_enabled:
        web_token = ensure_web_access_token(env_file, config.web_access_token)
        lan_ip = get_lan_ipv4()
        logger.info(
            "专属手机端 PWA   : 已启动 -> 局域网 http://%s:%d （访问密钥已保护）",
            lan_ip,
            config.web_pwa_port,
        )
        pwa = WebPwaServer(config, session_manager=sessions, access_token=web_token)
        runners.append(pwa.run())

    try:
        await asyncio.gather(*runners)
    except asyncio.CancelledError:
        pass
    finally:
        await sessions.shutdown_all()


def main() -> None:
    env_file = Path(".env")
    if not env_file.is_file():
        print(
            "未找到 .env 配置文件。请先复制 .env.example 为 .env。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        config = BridgeConfig.from_env(env_file)
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(config.log_level)

    if not _check_preflight(config):
        sys.exit(1)

    try:
        asyncio.run(_run_async(config, env_file))
    except KeyboardInterrupt:
        logging.getLogger("qq_agy_bridge").info("收到退出信号，已关闭所有 Antigravity 会话。")


if __name__ == "__main__":
    main()
