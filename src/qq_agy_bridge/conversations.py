"""Discover, inspect, and load chat histories from local Antigravity IDE and CLI stores.

Supports both:
- PC Antigravity IDE store (`~/.gemini/antigravity`)
- Antigravity CLI / Remote store (`~/.gemini/antigravity-cli`)

Uses read-only SQLite connections (`mode=ro`) and fast streaming JSONL parsing
so inspecting live IDE conversations never locks or interferes with the desktop IDE.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

SAFE_CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
SYS_BLOCK_RE = re.compile(r"\[系统设定[^\]]*\].*?\[/系统设定\]\s*", re.DOTALL)
HANDOFF_BLOCK_RE = re.compile(r"\[承接电脑端对话上下文\].*?\[/承接电脑端对话上下文\]\s*", re.DOTALL)

PREWARM_PROMPT_MARKER = "连接预热完成，只需回复两个字"


def _default_ide_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity"


def _default_cli_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def _parse_iso_to_ms(raw: str) -> int:
    s = (raw or "").strip()
    if not s or s.startswith("0001-01-01"):
        return 0
    try:
        # Handle 7-digit fractional seconds from Go time.Time (Python 3.11+ handles arbitrary, normalize space->T)
        norm = s.replace(" ", "T")
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _format_relative_time(ts_ms: int) -> str:
    if ts_ms <= 0:
        return ""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    diff_sec = max(0, (now_ms - ts_ms) // 1000)
    if diff_sec < 60:
        return "刚刚"
    if diff_sec < 3600:
        return f"{diff_sec // 60} 分钟前"
    dt_local = datetime.fromtimestamp(ts_ms / 1000.0).astimezone()
    now_local = datetime.now().astimezone()
    if dt_local.date() == now_local.date():
        return dt_local.strftime("今天 %H:%M")
    return dt_local.strftime("%m-%d %H:%M")


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _uri_to_local_path(uri_or_path: str) -> Path:
    s = (uri_or_path or "").strip()
    if s.startswith("file://"):
        p = unquote(urlparse(s).path)
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return Path(p)
    return Path(s)


def _parse_workspace_uris(raw_uris: str) -> str:
    s = (raw_uris or "").strip()
    if not s:
        return ""
    try:
        arr = json.loads(s) if s.startswith("[") else [s]
        for item in arr:
            if not isinstance(item, str) or not item:
                continue
            if item.startswith("file://"):
                return str(_uri_to_local_path(item))
            return item
    except Exception:
        pass
    return ""


def clean_user_input_text(raw_content: str) -> str:
    """Extract clean human prompt from transcript USER_INPUT content."""
    text = raw_content or ""
    m = USER_REQ_RE.search(text)
    if m:
        text = m.group(1)
    else:
        # Strip trailing <ADDITIONAL_METADATA> if <USER_REQUEST> was absent
        idx = text.find("<ADDITIONAL_METADATA>")
        if idx >= 0:
            text = text[:idx]
    text = SYS_BLOCK_RE.sub("", text)
    text = HANDOFF_BLOCK_RE.sub("", text)
    return text.strip()


@dataclass
class ConversationItem:
    conversation_id: str
    title: str
    preview: str
    store: str  # "ide" or "cli"
    source_label: str  # "电脑端 IDE" or "远程 / CLI"
    step_count: int
    updated_ms: int
    updated_human: str
    workspace: str
    running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "preview": self.preview,
            "store": self.store,
            "source_label": self.source_label,
            "step_count": self.step_count,
            "updated_ms": self.updated_ms,
            "updated_human": self.updated_human,
            "workspace": self.workspace,
            "running": self.running,
        }


@dataclass
class ChatMessage:
    role: str  # "user" or "assistant"
    text: str
    step_index: int = 0
    ts_ms: int = 0
    image_paths: list[Path] = field(default_factory=list)


@dataclass
class ConversationDetail:
    conversation_id: str
    title: str
    store: str  # "ide" or "cli"
    workspace: str
    running: bool
    messages: list[ChatMessage] = field(default_factory=list)


def _peek_first_user_prompt(brain_dir: Path, conv_id: str) -> tuple[str, bool]:
    """Return (first_real_user_prompt, is_prewarm_only) by scanning the first few lines of transcript.jsonl."""
    tfile = brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not tfile.is_file():
        return ("", False)
    saw_prewarm = False
    try:
        with tfile.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "USER_INPUT":
                    continue
                cleaned = clean_user_input_text(str(obj.get("content") or ""))
                if not cleaned:
                    continue
                if PREWARM_PROMPT_MARKER in cleaned:
                    saw_prewarm = True
                    continue
                first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), cleaned)
                return (first_line[:48], False)
    except OSError:
        pass
    return ("", saw_prewarm)


def _query_store_summaries(
    base_dir: Path,
    store_key: str,
    source_label: str,
    limit: int,
) -> list[ConversationItem]:
    db_path = base_dir / "conversation_summaries.db"
    brain_dir = base_dir / "brain"
    items: list[ConversationItem] = []

    if db_path.is_file():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = con.execute(
                    "SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status "
                    "FROM conversation_summaries "
                    "WHERE nesting_depth = 0 AND killed = 0 "
                    "ORDER BY last_modified_time DESC LIMIT ?",
                    (limit * 2,),
                ).fetchall()
            finally:
                con.close()

            for cid, title, preview, steps, mod_time, ws_uris, status in rows:
                cid_str = str(cid or "").strip()
                if not SAFE_CONV_ID_RE.match(cid_str):
                    continue
                step_cnt = int(steps or 0)
                clean_title = str(title or "").strip()
                clean_preview = str(preview or "").strip()

                first_prompt, prewarm_only = _peek_first_user_prompt(brain_dir, cid_str)
                if prewarm_only and not first_prompt:
                    # Hide unused background prewarm sessions
                    continue

                if not clean_title:
                    clean_title = first_prompt or f"对话 {cid_str[:8]}"
                if not clean_preview:
                    clean_preview = first_prompt or clean_title

                ts_ms = _parse_iso_to_ms(str(mod_time or ""))
                if ts_ms == 0:
                    tfile = brain_dir / cid_str / ".system_generated" / "logs" / "transcript.jsonl"
                    if tfile.is_file():
                        ts_ms = int(_safe_mtime(tfile) * 1000)

                ws_path = _parse_workspace_uris(str(ws_uris or ""))
                is_running = "RUNNING" in str(status or "").upper()
                items.append(
                    ConversationItem(
                        conversation_id=cid_str,
                        title=clean_title,
                        preview=clean_preview[:80],
                        store=store_key,
                        source_label=source_label,
                        step_count=step_cnt,
                        updated_ms=ts_ms,
                        updated_human=_format_relative_time(ts_ms),
                        workspace=ws_path,
                        running=is_running,
                    )
                )
                if len(items) >= limit:
                    break
            return items
        except Exception as exc:
            logger.warning("Failed reading %s summaries db: %s", store_key, exc)

    # Fallback: scan brain/<id> directories if SQLite DB isn't present
    if brain_dir.is_dir():
        dirs = sorted(
            (d for d in brain_dir.iterdir() if d.is_dir() and SAFE_CONV_ID_RE.match(d.name)),
            key=_safe_mtime,
            reverse=True,
        )
        for d in dirs[: limit * 2]:
            first_prompt, prewarm_only = _peek_first_user_prompt(brain_dir, d.name)
            if prewarm_only and not first_prompt:
                continue
            ts_ms = int(_safe_mtime(d) * 1000)
            items.append(
                ConversationItem(
                    conversation_id=d.name,
                    title=first_prompt or f"对话 {d.name[:8]}",
                    preview=first_prompt or "",
                    store=store_key,
                    source_label=source_label,
                    step_count=0,
                    updated_ms=ts_ms,
                    updated_human=_format_relative_time(ts_ms),
                    workspace="",
                    running=False,
                )
            )
            if len(items) >= limit:
                break

    return items


def list_local_conversations(
    limit: int = 30,
    ide_dir: Path | None = None,
    cli_dir: Path | None = None,
) -> list[ConversationItem]:
    """Return merged, recency-sorted top-level conversations across PC IDE and CLI stores."""
    ide_root = ide_dir if ide_dir is not None else _default_ide_dir()
    cli_root = cli_dir if cli_dir is not None else _default_cli_dir()

    merged: dict[str, ConversationItem] = {}
    for it in _query_store_summaries(ide_root, "ide", "电脑端 IDE", limit):
        merged[it.conversation_id] = it
    for it in _query_store_summaries(cli_root, "cli", "远程 / 手机端", limit):
        # Prefer CLI entry if same ID exists in both so native --conversation resume works
        merged[it.conversation_id] = it

    ordered = sorted(merged.values(), key=lambda x: x.updated_ms, reverse=True)
    return ordered[:limit]


def load_conversation_detail(
    conversation_id: str,
    store: str = "auto",
    max_messages: int = 80,
    ide_dir: Path | None = None,
    cli_dir: Path | None = None,
) -> ConversationDetail | None:
    """Load clean human-readable chat history (user + assistant turns & images) for a conversation."""
    cid = (conversation_id or "").strip()
    if not SAFE_CONV_ID_RE.match(cid):
        return None

    ide_root = ide_dir if ide_dir is not None else _default_ide_dir()
    cli_root = cli_dir if cli_dir is not None else _default_cli_dir()

    candidates: list[tuple[str, Path]] = []
    if store == "cli":
        candidates.append(("cli", cli_root))
    elif store == "ide":
        candidates.append(("ide", ide_root))
    else:
        candidates.extend([("cli", cli_root), ("ide", ide_root)])

    chosen_store = ""
    chosen_base: Path | None = None
    tfile: Path | None = None
    for sk, base in candidates:
        tf = base / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if tf.is_file():
            chosen_store = sk
            chosen_base = base
            tfile = tf
            break

    if chosen_base is None:
        # Check if it exists in DB even without transcript.jsonl yet
        for sk, base in candidates:
            db = base / "conversation_summaries.db"
            if db.is_file():
                try:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
                    row = con.execute(
                        "SELECT title, workspace_uris, status FROM conversation_summaries WHERE conversation_id = ?",
                        (cid,),
                    ).fetchone()
                    con.close()
                    if row:
                        return ConversationDetail(
                            conversation_id=cid,
                            title=str(row[0] or f"对话 {cid[:8]}"),
                            store=sk,
                            workspace=_parse_workspace_uris(str(row[1] or "")),
                            running="RUNNING" in str(row[2] or "").upper(),
                            messages=[],
                        )
                except Exception:
                    pass
        return None

    # Fetch metadata from summaries DB if available
    title = ""
    workspace = ""
    running = False
    db_path = chosen_base / "conversation_summaries.db"
    if db_path.is_file():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            row = con.execute(
                "SELECT title, workspace_uris, status FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            con.close()
            if row:
                title = str(row[0] or "").strip()
                workspace = _parse_workspace_uris(str(row[1] or ""))
                running = "RUNNING" in str(row[2] or "").upper()
        except Exception:
            pass

    assert tfile is not None
    full_tfile = tfile.with_name("transcript_full.jsonl")
    raw_entries: list[tuple[int, dict[str, Any]]] = []
    truncated_line_nums: set[int] = set()

    try:
        with tfile.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw_line in enumerate(fh):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                etype = obj.get("type")
                if etype not in {"USER_INPUT", "PLANNER_RESPONSE"}:
                    continue
                raw_entries.append((line_no, obj))
                trunc = obj.get("truncated_fields") or []
                if isinstance(trunc, list) and "content" in trunc:
                    truncated_line_nums.add(line_no)
    except OSError as exc:
        logger.warning("Error reading transcript %s: %s", tfile, exc)
        return None

    # Upgrade only the tail entries that had truncated content
    tail_entries = raw_entries[-(max_messages * 3) :]
    needed_full = {ln for ln, _ in tail_entries if ln in truncated_line_nums}
    full_Lookup: dict[int, dict[str, Any]] = {}
    if needed_full and full_tfile.is_file():
        max_ln = max(needed_full)
        try:
            with full_tfile.open("r", encoding="utf-8", errors="replace") as ffull:
                for line_no, raw_line in enumerate(ffull):
                    if line_no in needed_full:
                        try:
                            full_Lookup[line_no] = json.loads(raw_line)
                        except json.JSONDecodeError:
                            pass
                    if line_no >= max_ln:
                        break
        except OSError:
            pass

    parsed_msgs: list[ChatMessage] = []
    skip_next_prewarm_reply = False

    for line_no, obj in tail_entries:
        if line_no in full_Lookup:
            obj = full_Lookup[line_no]

        etype = obj.get("type")
        step_idx = int(obj.get("step_index") or 0)
        ts_ms = _parse_iso_to_ms(str(obj.get("created_at") or ""))

        images: list[Path] = []
        for m_item in obj.get("media") or []:
            if isinstance(m_item, dict):
                uri = str(m_item.get("uri") or "").strip()
                mime = str(m_item.get("mime_type") or "").lower()
                if uri and (mime.startswith("image/") or Path(uri).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}):
                    p = _uri_to_local_path(uri)
                    if p.is_file():
                        images.append(p)

        if etype == "USER_INPUT":
            if obj.get("source") not in {"USER_EXPLICIT", None, ""}:
                continue
            cleaned_u = clean_user_input_text(str(obj.get("content") or ""))
            if PREWARM_PROMPT_MARKER in cleaned_u:
                skip_next_prewarm_reply = True
                continue
            skip_next_prewarm_reply = False
            if not cleaned_u and not images:
                continue
            if not title:
                first_ln = next((ln.strip() for ln in cleaned_u.splitlines() if ln.strip()), "")
                title = first_ln[:48]
            parsed_msgs.append(
                ChatMessage(
                    role="user",
                    text=cleaned_u,
                    step_index=step_idx,
                    ts_ms=ts_ms,
                    image_paths=images,
                )
            )
        elif etype == "PLANNER_RESPONSE":
            content = str(obj.get("content") or "").strip()
            if skip_next_prewarm_reply and content in {"就绪", "就绪。", "收到"}:
                skip_next_prewarm_reply = False
                continue
            if not content and not images:
                continue
            # Coalesce consecutive assistant steps within the same user turn so multi-tool IDE turns stay clean
            if parsed_msgs and parsed_msgs[-1].role == "assistant":
                prev = parsed_msgs[-1]
                if content and content != prev.text:
                    prev.text = f"{prev.text}\n\n{content}".strip() if prev.text else content
                for im in images:
                    if im not in prev.image_paths:
                        prev.image_paths.append(im)
                if ts_ms > prev.ts_ms:
                    prev.ts_ms = ts_ms
            else:
                parsed_msgs.append(
                    ChatMessage(
                        role="assistant",
                        text=content,
                        step_index=step_idx,
                        ts_ms=ts_ms,
                        image_paths=images,
                    )
                )

    return ConversationDetail(
        conversation_id=cid,
        title=title or f"对话 {cid[:8]}",
        store=chosen_store,
        workspace=workspace,
        running=running,
        messages=parsed_msgs[-max_messages:],
    )


def build_ide_handoff_context(detail: ConversationDetail, max_chars: int = 5500) -> str:
    """Build a compact context block so continuing a PC IDE conversation on mobile preserves history."""
    lines: list[str] = [
        f"对话标题：{detail.title}",
        f"原对话 ID：{detail.conversation_id}",
    ]
    if detail.workspace:
        lines.append(f"原工作目录：{detail.workspace}")
    lines.append("以下是该电脑端对话最近的聊天记录（请无缝接着聊，无需重复总结前文）：")

    recent_snippets: list[str] = []
    used = 0
    for msg in reversed(detail.messages[-16:]):
        role_tag = "主人" if msg.role == "user" else "助理"
        snippet = msg.text.strip()
        if len(snippet) > 800:
            snippet = snippet[:800] + "..."
        entry = f"{role_tag}：{snippet}"
        if used + len(entry) > max_chars:
            break
        recent_snippets.append(entry)
        used += len(entry)

    recent_snippets.reverse()
    lines.extend(recent_snippets)
    return "\n".join(lines)


def list_recent_workspaces(
    default_workspace: Path,
    ide_dir: Path | None = None,
    cli_dir: Path | None = None,
    limit: int = 10,
) -> list[str]:
    """Collect recent existing workspace directories for quick new-chat project selection."""
    seen: list[str] = []

    def _add(p_str: str) -> None:
        if not p_str:
            return
        try:
            rp = str(Path(p_str).resolve())
            if Path(rp).is_dir() and rp not in seen:
                seen.append(rp)
        except OSError:
            pass

    _add(str(default_workspace))
    for item in list_local_conversations(limit=25, ide_dir=ide_dir, cli_dir=cli_dir):
        if item.workspace:
            _add(item.workspace)

    scratch_dir = (ide_dir if ide_dir is not None else _default_ide_dir()) / "scratch"
    if scratch_dir.is_dir():
        for child in sorted(scratch_dir.iterdir(), key=_safe_mtime, reverse=True)[:8]:
            if child.is_dir():
                _add(str(child))

    return seen[:limit]


def get_transcript_signature(
    conversation_id: str,
    store: str = "ide",
    ide_dir: Path | None = None,
    cli_dir: Path | None = None,
) -> tuple[int, int]:
    """Return lightweight (mtime_ns, size_bytes) signature for detecting live IDE transcript updates."""
    cid = (conversation_id or "").strip()
    if not SAFE_CONV_ID_RE.match(cid):
        return (0, 0)
    base = (cli_dir if cli_dir is not None else _default_cli_dir()) if store == "cli" else (
        ide_dir if ide_dir is not None else _default_ide_dir()
    )
    tfile = base / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        st = tfile.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)
