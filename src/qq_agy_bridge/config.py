from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from dotenv import dotenv_values, load_dotenv


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value.strip())


def _parse_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value.strip())


def _parse_qq_set(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    items = [part.strip() for part in value.split(",") if part.strip()]
    return frozenset(items)


def resolve_agy_executable(configured: str) -> str:
    """Resolve agy binary path, checking common install locations on Windows."""
    configured = configured.strip() or "agy"
    found = shutil.which(configured)
    if found:
        return found

    direct = Path(configured)
    if direct.is_file():
        return str(direct)

    home = Path.home()
    local_app_data = os.getenv("LOCALAPPDATA")
    candidates = [
        home / ".gemini" / "bin" / "agy.exe",
        home / ".gemini" / "bin" / "agy",
        home / ".local" / "bin" / "agy.exe",
        home / ".local" / "bin" / "agy",
        Path("/Applications/Antigravity.app/Contents/Resources/app/bin/agy"),
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ]
    if local_app_data:
        candidates.insert(0, Path(local_app_data) / "Programs" / "Antigravity" / "bin" / "agy.exe")
        candidates.insert(0, Path(local_app_data) / "agy" / "bin" / "agy.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return configured


@dataclass(frozen=True)
class BridgeConfig:
    onebot_mode: str  # "forward_ws" or "reverse_ws"
    onebot_ws_url: str
    onebot_reverse_host: str
    onebot_reverse_port: int
    onebot_reverse_path: str
    onebot_access_token: str
    allowed_qq_users: frozenset[str]
    agy_executable: str
    agy_workspace: Path
    agy_model: str
    agy_effort: str
    agy_dangerously_skip_permissions: bool
    agy_timeout_seconds: int
    progress_notice_delay_seconds: int
    max_message_length: int
    log_level: str
    agy_system_prompt: str = ""
    user_memory_file: Path | None = None
    clean_qq_formatting: bool = True
    agy_prewarm: bool = False
    prewarm_user_ids: tuple[str, ...] = ()
    message_debounce_seconds: float = 0.0
    agy_fallback_models: tuple[str, ...] = ()
    quota_cooldown_minutes: int = 60
    cache_clean_interval_hours: float = 0.0
    onebot_enabled: bool = True
    web_pwa_enabled: bool = False
    web_pwa_host: str = "0.0.0.0"
    web_pwa_port: int = 8765
    web_access_token: str = ""

    def build_session_instructions(self) -> str:
        """Combine persona prompt and latest persistent user memory (re-read fresh per new session)."""
        parts: list[str] = []
        if self.agy_system_prompt.strip():
            parts.append(self.agy_system_prompt.strip())
        if self.user_memory_file is not None:
            mem_path = self.user_memory_file
            mem_text = ""
            if mem_path.is_file():
                try:
                    mem_text = mem_path.read_text(encoding="utf-8").strip()
                except OSError:
                    mem_text = ""
            parts.append(
                f"【跨会话长期记忆文件：{mem_path}】\n"
                f"{mem_text or '（暂无记录）'}\n"
                f"提示：如果主人在聊天中透露了希望长期记住的个人偏好、常用目录、身份信息或习惯，你可以顺手静默更新这个 Markdown 文件，下次新会话会自动生效。"
            )
        return "\n\n".join(parts).strip()

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "BridgeConfig":
        file_vals: dict[str, str] = {}
        if env_path is not None and env_path.is_file():
            raw_vals = dotenv_values(env_path)
            file_vals = {k: v for k, v in raw_vals.items() if v is not None}
            load_dotenv(dotenv_path=env_path, override=True)
        else:
            load_dotenv(override=False)

        def _get(key: str, default: str = "") -> str:
            if key in file_vals:
                return file_vals[key]
            return os.getenv(key, default)

        mode = (_get("ONEBOT_MODE") or "forward_ws").strip().lower()
        if mode not in {"forward_ws", "reverse_ws"}:
            raise ValueError(
                f"Invalid ONEBOT_MODE={mode!r}. Expected 'forward_ws' or 'reverse_ws'."
            )

        workspace_raw = (_get("AGY_WORKSPACE") or "").strip()
        workspace = Path(workspace_raw).expanduser().resolve() if workspace_raw else Path.cwd().resolve()

        reverse_path = (_get("ONEBOT_REVERSE_PATH") or "/onebot/v11/ws").strip()
        if not reverse_path.startswith("/"):
            reverse_path = "/" + reverse_path

        project_root = Path(__file__).resolve().parents[2]
        prompt_file_raw = (_get("AGY_SYSTEM_PROMPT_FILE") or "").strip()
        system_prompt = ""
        if prompt_file_raw.lower() not in {"none", "off", "false"}:
            if prompt_file_raw:
                pf = Path(prompt_file_raw).expanduser()
                if not pf.is_absolute():
                    pf = project_root / pf
            else:
                pf = project_root / "prompts" / "qq_persona.md"
            if pf.is_file():
                system_prompt = pf.read_text(encoding="utf-8").strip()

        memory_file_raw = (_get("USER_MEMORY_FILE") or "").strip()
        memory_file: Path | None = None
        if memory_file_raw.lower() not in {"none", "off", "false"}:
            if memory_file_raw:
                mf = Path(memory_file_raw).expanduser()
                memory_file = mf if mf.is_absolute() else (project_root / mf)
            else:
                default_mf = project_root / "memory" / "user_profile.md"
                example_mf = project_root / "memory" / "user_profile.example.md"
                if not default_mf.is_file() and example_mf.is_file():
                    try:
                        default_mf.parent.mkdir(parents=True, exist_ok=True)
                        default_mf.write_text(example_mf.read_text(encoding="utf-8"), encoding="utf-8")
                    except OSError:
                        pass
                if default_mf.is_file():
                    memory_file = default_mf

        allowed_ordered = tuple(
            part.strip()
            for part in (_get("ALLOWED_QQ_USERS") or "").split(",")
            if part.strip()
        )
        raw_onebot_enabled = (_get("ONEBOT_ENABLED") or "auto").strip().lower()
        if raw_onebot_enabled == "auto":
            onebot_enabled = len(allowed_ordered) > 0
        else:
            onebot_enabled = _parse_bool(raw_onebot_enabled, True)
        prewarm_enabled = _parse_bool(_get("AGY_PREWARM"), True)
        prewarm_raw = (_get("AGY_PREWARM_USERS") or "").strip()
        if prewarm_raw:
            prewarm_users = tuple(
                part.strip() for part in prewarm_raw.split(",") if part.strip()
            )
        else:
            prewarm_users = allowed_ordered[:2]

        from .maintenance import DEFAULT_FALLBACK_MODELS

        fallback_raw = (_get("AGY_FALLBACK_MODELS") or "").strip()
        if fallback_raw.lower() in {"none", "off", "false"}:
            fallback_models: tuple[str, ...] = ()
        elif fallback_raw:
            fallback_models = tuple(
                part.strip() for part in fallback_raw.split(",") if part.strip()
            )
        else:
            fallback_models = DEFAULT_FALLBACK_MODELS

        return cls(
            onebot_mode=mode,
            onebot_ws_url=(_get("ONEBOT_WS_URL") or "ws://127.0.0.1:3001").strip(),
            onebot_reverse_host=(_get("ONEBOT_REVERSE_HOST") or "127.0.0.1").strip(),
            onebot_reverse_port=_parse_int(_get("ONEBOT_REVERSE_PORT"), 8080),
            onebot_reverse_path=reverse_path,
            onebot_access_token=(_get("ONEBOT_ACCESS_TOKEN") or "").strip(),
            allowed_qq_users=frozenset(allowed_ordered),
            agy_executable=resolve_agy_executable(_get("AGY_EXECUTABLE") or "agy"),
            agy_workspace=workspace,
            agy_model=(_get("AGY_MODEL") or "").strip(),
            agy_effort=(_get("AGY_EFFORT") or "").strip(),
            agy_dangerously_skip_permissions=_parse_bool(
                _get("AGY_DANGEROUSLY_SKIP_PERMISSIONS"), True
            ),
            agy_timeout_seconds=max(10, _parse_int(_get("AGY_TIMEOUT_SECONDS"), 300)),
            progress_notice_delay_seconds=max(
                0, _parse_int(_get("PROGRESS_NOTICE_DELAY_SECONDS"), 0)
            ),
            max_message_length=max(200, _parse_int(_get("MAX_MESSAGE_LENGTH"), 1800)),
            log_level=(_get("LOG_LEVEL") or "INFO").strip().upper(),
            agy_system_prompt=system_prompt,
            user_memory_file=memory_file,
            clean_qq_formatting=_parse_bool(_get("CLEAN_QQ_FORMATTING"), True),
            agy_prewarm=prewarm_enabled,
            prewarm_user_ids=prewarm_users,
            message_debounce_seconds=max(
                0.0, min(5.0, _parse_float(_get("MESSAGE_DEBOUNCE_SECONDS"), 1.2))
            ),
            agy_fallback_models=fallback_models,
            quota_cooldown_minutes=max(1, _parse_int(_get("QUOTA_COOLDOWN_MINUTES"), 60)),
            cache_clean_interval_hours=max(
                0.0, _parse_float(_get("CACHE_CLEAN_INTERVAL_HOURS"), 6.0)
            ),
            onebot_enabled=onebot_enabled,
            web_pwa_enabled=_parse_bool(_get("WEB_PWA_ENABLED"), True),
            web_pwa_host=(_get("WEB_PWA_HOST") or "0.0.0.0").strip(),
            web_pwa_port=_parse_int(_get("WEB_PWA_PORT"), 8765),
            web_access_token=(_get("WEB_ACCESS_TOKEN") or "").strip(),
        )
