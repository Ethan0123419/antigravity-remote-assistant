from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from .agy_session import SessionManager
from .config import BridgeConfig
from .web_pwa import WebPwaServer, ensure_web_access_token, get_lan_ipv4


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_preflight(config: BridgeConfig) -> bool:
    logger = logging.getLogger("antigravity_assistant")
    ok = True

    if not config.web_pwa_enabled:
        logger.error("WEB_PWA_ENABLED 当前为关闭状态，请在 .env 中设为 true。")
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
    logger = logging.getLogger("antigravity_assistant")
    logger.info("=== 反重力助手 (Antigravity Assistant) 核心启动 ===")
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
    web_token = ensure_web_access_token(env_file, config.web_access_token)
    lan_ip = get_lan_ipv4()
    logger.info(
        "专属移动端控制台 : 已就绪 -> 局域网 http://%s:%d （访问密钥保护已开启）",
        lan_ip,
        config.web_pwa_port,
    )
    pwa = WebPwaServer(config, session_manager=sessions, access_token=web_token)

    try:
        await pwa.run()
    except asyncio.CancelledError:
        pass
    finally:
        await sessions.shutdown_all()


def main() -> None:
    env_file = Path(".env")
    if not env_file.is_file():
        from .setup_wizard import ensure_env_configured
        ensure_env_configured(Path.cwd())

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
        logging.getLogger("antigravity_assistant").info("收到退出信号，已安全关闭所有会话。")


if __name__ == "__main__":
    main()
