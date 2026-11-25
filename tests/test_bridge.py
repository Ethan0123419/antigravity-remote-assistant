from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import websockets

from qq_agy_bridge.agy_session import SessionManager
from qq_agy_bridge.config import BridgeConfig
from qq_agy_bridge.conversations import (
    list_local_conversations,
    load_conversation_detail,
)
from qq_agy_bridge.desktop_tools import (
    get_workspace_file_diff,
    inspect_workspace_git,
    is_safe_outbound_file,
    list_workspace_directory,
    read_workspace_file_preview,
)
from qq_agy_bridge.maintenance import (
    ModelFailoverPool,
    is_quota_or_capacity_error,
    run_safe_cache_cleanup,
)
from qq_agy_bridge.web_pwa import WebPwaServer, extract_pwa_media_and_text

MOCK_AGY = Path(__file__).parent / "mock_agy.py"


def _make_config(tmp_path: Path, **overrides) -> BridgeConfig:
    base = dict(
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="balanced-test",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=0,
        web_access_token="secret-pwa-token-123456",
    )
    base.update(overrides)
    return BridgeConfig(**base)


def test_quota_detection_and_failover_pool() -> None:
    assert is_quota_or_capacity_error("RESOURCE_EXHAUSTED: model quota exceeded (429)")
    assert is_quota_or_capacity_error("Model is overloaded, please try again later")
    assert not is_quota_or_capacity_error("这一步跑了超过 300s，已安全中断")
    assert not is_quota_or_capacity_error("FileNotFoundError: foo.txt")

    pool = ModelFailoverPool(
        primary_model="claude-sonnet-5-5-medium",
        fallback_models=("claude-sonnet-5-5-low", "gemini-4-neon-medium"),
        cooldown_seconds=3600,
    )
    assert pool.pick_best_available() == "claude-sonnet-5-5-medium"
    nxt = pool.next_after("claude-sonnet-5-5-medium", {"claude-sonnet-5-5-medium"})
    assert nxt == "claude-sonnet-5-5-low"
    assert pool.pick_best_available() == "claude-sonnet-5-5-low"
    assert len(pool.cooldown_summary()) == 1


def test_safe_cache_cleanup_protects_configs_and_prunes_old_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    protected = tmp_path / "config"
    protected.mkdir()
    secret = protected / "token.json"
    secret.write_text("keep-me", encoding="utf-8")

    old_file = inbox / "old.png"
    old_file.write_bytes(b"12345")
    old_ts = time.time() - 48 * 3600
    os.utime(old_file, (old_ts, old_ts))

    fresh_file = inbox / "fresh.png"
    fresh_file.write_bytes(b"abcde")

    rep = run_safe_cache_cleanup([(inbox, 24.0, 10), (protected, 0.0, 1)])
    assert rep.deleted_files == 1
    assert not old_file.exists()
    assert fresh_file.exists()
    assert secret.exists()


def test_media_extraction_and_secret_blocking(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_ACCESS_TOKEN=secret", encoding="utf-8")

    assert is_safe_outbound_file(img, image_only=True)
    assert not is_safe_outbound_file(env_file, image_only=False)

    txt, imgs, files = extract_pwa_media_and_text(
        f"<thought>internal</thought>结果如下\n[SEND_IMAGE: {img}]\n[SEND_FILE: {env_file}]"
    )
    assert txt == "结果如下"
    assert imgs == [img.resolve()]
    assert files == []


@pytest.mark.asyncio
async def test_persona_and_memory_injected_only_on_first_turn(tmp_path: Path) -> None:
    mem = tmp_path / "user_profile.md"
    mem.write_text("- 常用目录 F:\\Projects", encoding="utf-8")
    cfg = _make_config(
        tmp_path,
        agy_system_prompt="你是反重力助手，优先搜索GitHub开源方案",
        user_memory_file=mem,
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    s = sessions.get("owner")
    try:
        r1 = await s.ask("帮我写个工具")
        assert "你是反重力助手" in r1.response
        assert "F:\\Projects" in r1.response
        assert "帮我写个工具" in r1.response

        r2 = await s.ask("好的谢谢")
        assert "你是反重力助手" not in r2.response
        assert "好的谢谢" in r2.response
    finally:
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_quota_auto_failover_switches_model_and_preserves_conversation(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path,
        agy_model="exhausted-primary-model",
        agy_fallback_models=("exhausted-primary-model", "healthy-backup-model"),
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    s = sessions.get("owner")
    try:
        r1 = await s.ask("触发自动切换 __QUOTA_ON_MODEL__:exhausted-primary-model")
        assert r1.status == "SUCCESS"
        assert r1.switched_from_model == "exhausted-primary-model"
        assert r1.active_model == "healthy-backup-model"
        assert s.model == "healthy-backup-model"
    finally:
        await sessions.shutdown_all()


def test_complete_pc_conversations_no_ghost_overwrite_and_codex_support(tmp_path: Path) -> None:
    """Verify:
    1. Stale CLI DB copies of PC IDE chats NEVER overwrite or hide real IDE chats.
    2. IDE DB mirrored CLI prewarm rows (`CORTEX_TRAJECTORY_SOURCE_CLI`) are filtered out of IDE store.
    3. Unindexed top-level `brain/<id>` chats are discovered while `nesting_depth > 0` subagents stay hidden.
    4. OpenAI Codex Desktop (`state_5.sqlite` + `session_index.jsonl` + rollout `.jsonl`) chats are discovered & parsed cleanly.
    """
    ide_dir = tmp_path / "antigravity"
    cli_dir = tmp_path / "antigravity-cli"
    codex_dir = tmp_path / "codex"
    proj_dir = tmp_path / "my_app"
    proj_dir.mkdir()

    # 1. Real IDE conversation in IDE store
    ide_cid = "c63c3f37-e9d8-4ea1-a261-cbf3867a7336"
    ide_tf = ide_dir / "brain" / ide_cid / ".system_generated" / "logs" / "transcript.jsonl"
    ide_tf.parent.mkdir(parents=True)
    ide_tf.write_text(
        "\n".join(
            [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>\n第一个问题\n</USER_REQUEST>"}, ensure_ascii=False),
                json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "第一个回答"}, ensure_ascii=False),
                json.dumps({"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>\n第二个问题\n</USER_REQUEST>"}, ensure_ascii=False),
                json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "第二个回答"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 2. Subagent folder inside IDE brain (must NOT appear as top-level chat!)
    sub_cid = "subagent-1111-2222-3333-444444444444"
    sub_tf = ide_dir / "brain" / sub_cid / ".system_generated" / "logs" / "transcript.jsonl"
    sub_tf.parent.mkdir(parents=True)
    sub_tf.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "subagent task"}) + "\n",
        encoding="utf-8",
    )

    # 3. Unindexed top-level brain folder (NOT in SQLite DB, MUST still be discovered!)
    unindexed_cid = "06cd3358642d3a22283cdb848f660c8b"
    un_tf = ide_dir / "brain" / unindexed_cid / ".system_generated" / "logs" / "transcript.jsonl"
    un_tf.parent.mkdir(parents=True)
    un_tf.write_text(
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "<USER_REQUEST>\n独立未入库桌面对话\n</USER_REQUEST>",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # Populate IDE SQLite DB (including a mirrored CLI ghost row!)
    con_ide = sqlite3.connect(ide_dir / "conversation_summaries.db")
    con_ide.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, "
        "source TEXT, app_data_dir TEXT, nesting_depth INT, killed INT)"
    )
    con_ide.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ide_cid, "反重力重构主对话", "最新进度", 120, "2026-11-25 02:00:00+00:00", json.dumps([proj_dir.as_uri()]), "CASCADE_RUN_STATUS_RUNNING", "", str(ide_dir), 0, 0),
    )
    con_ide.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sub_cid, "Browser Subagent", "", 10, "2026-11-25 02:00:00+00:00", "", "IDLE", "", str(ide_dir), 1, 0),
    )
    con_ide.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("cli-prewarm-ghost-999", "", "", 2, "2026-11-25 02:01:00+00:00", "", "IDLE", "CORTEX_TRAJECTORY_SOURCE_CLI", str(ide_dir), 0, 0),
    )
    con_ide.commit()
    con_ide.close()

    # Populate CLI SQLite DB with a STALE ghost snapshot of `ide_cid` + one genuine CLI chat
    cli_dir.mkdir(parents=True)
    real_cli_cid = "real-mobile-chat-888888"
    cli_tf = cli_dir / "brain" / real_cli_cid / ".system_generated" / "logs" / "transcript.jsonl"
    cli_tf.parent.mkdir(parents=True)
    cli_tf.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "手机发起的真实对话"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    con_cli = sqlite3.connect(cli_dir / "conversation_summaries.db")
    con_cli.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, "
        "source TEXT, app_data_dir TEXT, nesting_depth INT, killed INT)"
    )
    # Stale snapshot of IDE conversation inside CLI DB!
    con_cli.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ide_cid, "反重力重构主对话", "昨天旧快照", 12, "2026-11-24 01:00:00+00:00", "", "IDLE", "", str(ide_dir), 0, 0),
    )
    con_cli.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (real_cli_cid, "", "", 6, "2026-11-25 01:30:00+00:00", "", "IDLE", "", str(cli_dir), 0, 0),
    )
    con_cli.commit()
    con_cli.close()

    # 4. Populate Codex Desktop store (`state_5.sqlite` + `session_index.jsonl` + rollout)
    codex_sess_dir = codex_dir / "sessions" / "2026" / "11" / "25"
    codex_sess_dir.mkdir(parents=True)
    codex_cid = "01a20d71-386d-7930-b242-359877d30271"
    rollout = codex_sess_dir / f"rollout-2026-11-25T09-00-00-{codex_cid}.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": codex_cid, "cwd": f"\\\\?\\{proj_dir}"}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for test"}]}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "帮我重构 Codex 插件架构"}]}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "正在检查目录..."}]}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "已完成插件架构重构。"}]}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (codex_dir / "session_index.jsonl").write_text(
        json.dumps({"id": codex_cid, "thread_name": "重构 Codex 插件架构"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    con_cdx = sqlite3.connect(codex_dir / "state_5.sqlite")
    con_cdx.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INT, source TEXT, cwd TEXT, title TEXT, archived INT)"
    )
    con_cdx.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
        (codex_cid, str(rollout), 1795572000, "vscode", f"\\\\?\\{proj_dir}", "raw title", 0),
    )
    con_cdx.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
        ("sub-codex-99999999", str(rollout), 1795572000, '{"subagent":{"other":"guardian"}}', "", "", 0),
    )
    con_cdx.commit()
    con_cdx.close()

    items = list_local_conversations(limit=100, ide_dir=ide_dir, cli_dir=cli_dir, codex_dir=codex_dir)
    by_id = {it.conversation_id: it for it in items}

    # IDE chat stayed `store == "ide"` with latest step_count=120 (NOT overwritten by CLI ghost!)
    assert ide_cid in by_id
    assert by_id[ide_cid].store == "ide"
    assert by_id[ide_cid].step_count == 120
    assert by_id[ide_cid].running is True

    # Unindexed brain chat was found; subagent & CLI prewarm ghost were excluded!
    assert unindexed_cid in by_id
    assert by_id[unindexed_cid].store == "ide"
    assert "独立未入库桌面对话" in by_id[unindexed_cid].title
    assert sub_cid not in by_id
    assert "cli-prewarm-ghost-999" not in by_id

    # Genuine CLI chat is present under `cli`
    assert real_cli_cid in by_id
    assert by_id[real_cli_cid].store == "cli"

    # Codex PC chat is present under `codex` with human index title, and guardian subagent excluded
    assert codex_cid in by_id
    assert by_id[codex_cid].store == "codex"
    assert by_id[codex_cid].title == "重构 Codex 插件架构"
    assert by_id[codex_cid].workspace == str(proj_dir)
    assert "sub-codex-99999999" not in by_id

    # Verify pagination (`has_more`) on IDE chat when max_messages=2
    det_paged = load_conversation_detail(ide_cid, "ide", max_messages=2, ide_dir=ide_dir, cli_dir=cli_dir, codex_dir=codex_dir)
    assert det_paged is not None
    assert len(det_paged.messages) == 2
    assert det_paged.has_more is True
    assert det_paged.total_messages >= 4

    # Verify Codex detail parsing (AGENTS.md stripped, final_answer supersedes intermediate commentary)
    det_cdx = load_conversation_detail(codex_cid, "codex", max_messages=50, ide_dir=ide_dir, cli_dir=cli_dir, codex_dir=codex_dir)
    assert det_cdx is not None
    assert len(det_cdx.messages) == 2
    assert det_cdx.messages[0].role == "user"
    assert det_cdx.messages[0].text == "帮我重构 Codex 插件架构"
    assert det_cdx.messages[1].role == "assistant"
    assert det_cdx.messages[1].text == "已完成插件架构重构。"


def test_workspace_git_diff_and_file_inspector(tmp_path: Path) -> None:
    repo = tmp_path / "demo_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=str(repo), check=True, capture_output=True)

    app_py = repo / "app.py"
    app_py.write_text("def hello():\n    return 'v1'\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=123", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    app_py.write_text("def hello():\n    return 'v2-antigravity'\n", encoding="utf-8")

    git_state = inspect_workspace_git(repo)
    assert git_state["is_git"] is True
    paths = [c["path"] for c in git_state["changes"]]
    assert "app.py" in paths
    assert ".env" not in paths  # Sensitive file hidden!

    diff_res = get_workspace_file_diff(repo, "app.py")
    assert diff_res["ok"] is True
    assert "+    return 'v2-antigravity'" in diff_res["diff"]

    # Path traversal and .env preview must be blocked
    assert read_workspace_file_preview(repo, "../outside.txt")["ok"] is False
    assert read_workspace_file_preview(repo, ".env")["ok"] is False

    dir_res = list_workspace_directory(repo, "")
    assert dir_res["ok"] is True
    names = [e["name"] for e in dir_res["entries"]]
    assert "app.py" in names
    assert ".env" not in names

    prev_res = read_workspace_file_preview(repo, "app.py")
    assert prev_res["ok"] is True
    assert "v2-antigravity" in prev_res["content"]


@pytest.mark.asyncio
async def test_web_pwa_e2e_streaming_upload_inspector_and_pc_sync(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ide_store = tmp_path / "ide_store"
    cli_store = tmp_path / "cli_store"
    codex_store = tmp_path / "codex_store"
    ide_cid = "ide-live-sync-12345"
    tf = ide_store / "brain" / ide_cid / ".system_generated" / "logs" / "transcript.jsonl"
    tf.parent.mkdir(parents=True)
    tf.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "电脑端第一句"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "电脑端第一轮回复"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    tok = "super-secret-antigravity-token-999"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=ide_store,
        cli_store_dir=cli_store,
        codex_store_dir=codex_store,
    )
    pwa.ide_watch_interval = 0.2  # type: ignore[attr-defined]
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.25)

        # 1. Verify HTML & Manifest carry new "反重力助手" brand + Set-Cookie persistence
        def _fetch_root() -> tuple[str, str]:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/?token={tok}", timeout=5)
            return r.read().decode("utf-8"), (r.headers.get("Set-Cookie") or "")

        html_text, cookie_hdr = await asyncio.to_thread(_fetch_root)
        assert "反重力助手" in html_text
        assert "agy_pwa_token=" in cookie_hdr

        manifest_raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/manifest.json?token={tok}", timeout=5).read().decode("utf-8")
        )
        manifest_data = json.loads(manifest_raw)
        assert manifest_data["name"] == "反重力助手"
        assert tok in manifest_data["start_url"]

        # 2. Connect via Cookie (no query token) to verify iOS standalone PWA auth fallback
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws",
            additional_headers={"Cookie": f"agy_pwa_token={tok}"},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert hello["type"] == "hello"

            # Workspace inspector WS call
            (tmp_path / "readme_demo.md").write_text("# Hello Assistant", encoding="utf-8")
            await ws.send(json.dumps({"type": "workspace_inspect", "rel_dir": ""}))
            ws_state = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert ws_state["type"] == "workspace_state"
            assert any(e["name"] == "readme_demo.md" for e in ws_state["dir"]["entries"])

            # Select PC IDE conversation & verify live auto-watch push when transcript grows
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"}))
            loaded = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert loaded["type"] == "conversation_loaded"
            assert len(loaded["history"]) == 2

            with tf.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "电脑端新增第二句"}, ensure_ascii=False) + "\n")
            pushed = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert pushed["type"] == "conversation_loaded"
            assert pushed["silent"] is True
            assert len(pushed["history"]) == 3

            # Chunked upload + streaming continuation reply
            payload = b"test attachment content"
            await ws.send(json.dumps({"type": "upload_init", "upload_id": "u1", "name": "doc.txt", "size": len(payload)}))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))["type"] == "upload_ack"
            await ws.send(json.dumps({"type": "upload_chunk", "upload_id": "u1", "data": base64.b64encode(payload).decode("ascii")}))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))["type"] == "upload_progress"
            await ws.send(json.dumps({"type": "upload_complete", "upload_id": "u1"}))
            up_done = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert up_done["type"] == "upload_done"

            await ws.send(
                json.dumps(
                    {
                        "type": "chat",
                        "text": "在手机上接着电脑对话往下做",
                        "attachments": [{"media_id": up_done["media_id"], "name": "doc.txt", "is_image": False}],
                    }
                )
            )
            final_item = None
            for _ in range(25):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    final_item = ev["item"]
                    break
            assert final_item is not None
            assert "电脑端第一轮回复" in final_item["text"]
            assert "在手机上接着电脑对话往下做" in final_item["text"]
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


def test_markdown_and_generic_tool_screenshot_extraction(tmp_path: Path) -> None:
    from qq_agy_bridge.web_pwa import harvest_recent_cli_turn_images

    shot1 = tmp_path / "Antigravity-窗口.png"
    shot1.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 80)
    shot2 = tmp_path / "step_6_media_0.png"
    shot2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 80)

    # 1. Angle-bracketed Markdown image syntax (`![alt](<C:/path/中文.png>)`) used by Codex & Antigravity
    clean_txt, imgs, _ = extract_pwa_media_and_text(
        f"这是最新工作画面：\n\n![桌面工作截图](<{shot1.as_posix()}>)"
    )
    assert clean_txt == "这是最新工作画面："
    assert imgs == [shot1.resolve()]

    # 2. Antigravity IDE/CLI transcript `GENERIC` step carrying MCP screenshot `media`
    ide_store = tmp_path / "ide"
    cid = "shot-conv-99999999"
    tf = ide_store / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tf.parent.mkdir(parents=True)
    tf.write_text(
        "\n".join(
            [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "帮我截个图"}, ensure_ascii=False),
                json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": ""}, ensure_ascii=False),
                json.dumps({"step_index": 2, "type": "GENERIC", "media": [{"mime_type": "image/png", "uri": shot2.as_uri()}], "content": "Cursor Position"}, ensure_ascii=False),
                json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "截好啦！"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    det = load_conversation_detail(cid, "ide", ide_dir=ide_store, cli_dir=tmp_path / "cli", codex_dir=tmp_path / "cdx")
    assert det is not None
    assert len(det.messages) == 2
    assert det.messages[1].image_paths == [shot2.resolve()]

    # 3. Live CLI turn media auto-harvesting when tool saves screenshot into logs/media/
    cli_store = tmp_path / "cli_live"
    live_cid = "live-cli-shot-111"
    media_dir = cli_store / "brain" / live_cid / ".system_generated" / "logs" / "media"
    media_dir.mkdir(parents=True)
    t_before = time.time()
    live_shot = media_dir / "step_4_media_0.png"
    live_shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"3" * 80)
    harvested = harvest_recent_cli_turn_images(cli_store, live_cid, t_before)
    assert harvested == [live_shot.resolve()]


def test_segno_qr_png_save_works() -> None:
    import io
    import segno

    buf = io.BytesIO()
    segno.make("http://127.0.0.1:8765/?token=demo", error="m").save(
        buf, kind="png", scale=4, border=2, dark="#ECECEC", light="#212121"
    )
    data = buf.getvalue()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_switching_conversations_never_kills_running_command(tmp_path: Path) -> None:
    """Regression test: sending a command in Chat A and switching to view Chat B mid-run must NOT cancel Chat A."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ide_store = tmp_path / "ide_iso"
    for cid, prompt in (("chat-a-111111", "对话A初始内容"), ("chat-b-222222", "对话B初始内容")):
        tf = ide_store / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        tf.parent.mkdir(parents=True)
        tf.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": prompt}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    tok = "iso-secret-token-12345678"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=ide_store,
        cli_store_dir=tmp_path / "cli_iso",
        codex_store_dir=tmp_path / "cdx_iso",
    )
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5.0)  # hello

            # 1. Open Chat A and start a slow command (__SLOW_STREAM__ sleeps 0.65s)
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": "chat-a-111111", "store": "ide"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))["type"] == "conversation_loaded"

            await ws.send(json.dumps({"type": "chat", "text": "执行耗时命令 __SLOW_STREAM__"}))
            # Wait until stream starts in Chat A
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_delta":
                    break

            # 2. Switch to Chat B WHILE Chat A is still running!
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": "chat-b-222222", "store": "ide"}))
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "conversation_loaded" and ev["conversation_id"] == "chat-b-222222":
                    assert ev["busy"] is False
                    break

            # Wait for background completion toast from Chat A
            for _ in range(15):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "toast" and "已完成回复" in ev["text"]:
                    break

            # 3. Switch back to Chat A — command and full reply must be preserved!
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": "chat-a-111111", "store": "ide"}))
            reloaded_a = None
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "conversation_loaded" and ev["conversation_id"] == "chat-a-111111":
                    reloaded_a = ev
                    break
            assert reloaded_a is not None
            texts_a = [m["text"] for m in reloaded_a["history"]]
            assert any("前半段流式输出...后半段圆满结束！" in t for t in texts_a)
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_live_pc_ide_window_delivery_and_transcript_streaming(tmp_path: Path) -> None:
    """Verify sending a prompt inside an open PC Antigravity IDE conversation drives the PC window directly,
    streams live transcript deltas + screenshots back to mobile, and never duplicates turns on refresh.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ide_store = tmp_path / "ide_pc_live"
    ide_cid = "0318a60f-e198-4f1d-8d52-e88a0cae38cc"
    tf = ide_store / "brain" / ide_cid / ".system_generated" / "logs" / "transcript.jsonl"
    tf.parent.mkdir(parents=True)
    tf.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "初始桌面消息"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "初始桌面回复"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    db_path = ide_store / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, "
        "source TEXT, app_data_dir TEXT, nesting_depth INT, killed INT)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ide_cid, "ChatGPT UI Browser Extension", "初始桌面回复", 2, "2026-11-25 04:00:00+00:00", "", "CASCADE_RUN_STATUS_IDLE", "", str(ide_store), 0, 0),
    )
    con.commit()
    con.close()

    shot_file = tmp_path / "pc_live_shot.png"
    shot_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"S" * 120)

    delivered_calls: list[tuple[str, str, str]] = []

    def _fake_pc_window_sender(cid: str, title: str, prompt: str) -> tuple[bool, str]:
        delivered_calls.append((cid, title, prompt))

        def _simulate_pc_ide_execution() -> None:
            time.sleep(0.08)
            c1 = sqlite3.connect(db_path)
            c1.execute("UPDATE conversation_summaries SET status = 'CASCADE_RUN_STATUS_RUNNING' WHERE conversation_id = ?", (cid,))
            c1.commit()
            c1.close()
            with tf.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": f"<USER_REQUEST>{prompt}</USER_REQUEST>"}, ensure_ascii=False) + "\n")
                fh.write(json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "正在检查电脑端页面...", "tool_calls": [{"name": "mcp_windows-mcp_Screenshot"}]}, ensure_ascii=False) + "\n")

            time.sleep(0.18)
            with tf.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step_index": 4, "type": "GENERIC", "media": [{"mime_type": "image/png", "uri": shot_file.as_uri()}], "content": "Captured"}, ensure_ascii=False) + "\n")
                fh.write(json.dumps({"step_index": 5, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "电脑端同步修复已完成！"}, ensure_ascii=False) + "\n")

            c2 = sqlite3.connect(db_path)
            c2.execute("UPDATE conversation_summaries SET status = 'CASCADE_RUN_STATUS_IDLE' WHERE conversation_id = ?", (cid,))
            c2.commit()
            c2.close()

        import threading
        threading.Thread(target=_simulate_pc_ide_execution, daemon=True).start()
        return (True, "已投递至电脑端 Antigravity 窗口")

    tok = "pc-live-win-token-88888888"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    # Deliberately pass a non-existent CLI command so test FAILS if it ever falls back to headless agy.exe!
    sessions = SessionManager(cfg, command_override=[sys.executable, "-c", "import sys; sys.exit(99)"])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=ide_store,
        cli_store_dir=tmp_path / "cli_unused",
        codex_store_dir=tmp_path / "cdx_unused",
        pc_window_sender=_fake_pc_window_sender,
    )
    pwa.pc_ide_poll_interval = 0.08
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5.0)  # hello

            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"}))
            loaded = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert loaded["type"] == "conversation_loaded"
            assert len(loaded["history"]) == 2

            await ws.send(json.dumps({"type": "chat", "text": "进行bug检查与优化"}))

            saw_delta = False
            end_item = None
            for _ in range(30):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_delta":
                    saw_delta = True
                elif ev["type"] == "stream_end":
                    end_item = ev["item"]
                    break

            assert len(delivered_calls) == 1
            assert delivered_calls[0] == (ide_cid, "ChatGPT UI Browser Extension", "进行bug检查与优化")
            assert saw_delta is True
            assert end_item is not None
            assert end_item["model"] == "Antigravity 电脑端"
            assert "正在检查电脑端页面..." in end_item["text"]
            assert "电脑端同步修复已完成！" in end_item["text"]
            assert len(end_item["images"]) == 1
            assert end_item["images"][0]["name"] == "pc_live_shot.png"

            # Refresh conversation and ensure zero duplicate turns!
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"}))
            reloaded = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert reloaded["type"] == "conversation_loaded"
            assert len(reloaded["history"]) == 4
            assert reloaded["history"][-2]["text"] == "进行bug检查与优化"
            assert "电脑端同步修复已完成！" in reloaded["history"][-1]["text"]
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


def test_encrypted_ios_pwa_beacon_roundtrip_and_tamper_rejection(tmp_path: Path) -> None:
    from qq_agy_bridge.beacon import (
        build_static_pwa_bundle,
        decrypt_beacon_payload,
        encrypt_beacon_payload,
    )

    secret = "my-secret-ios-pwa-token-2026"
    orig = {
        "tunnel_url": "https://fast-nebula-orbit.trycloudflare.com",
        "lan_url": "http://192.168.1.88:8765",
        "ts": 1795580000000,
    }
    env = encrypt_beacon_payload(orig, secret)
    assert "fast-nebula-orbit" not in json.dumps(env)
    assert "192.168.1.88" not in json.dumps(env)

    dec = decrypt_beacon_payload(env, secret)
    assert dec == orig

    # Wrong token or tampered ciphertext MUST fail MAC check before decryption!
    with pytest.raises(ValueError, match="MAC"):
        decrypt_beacon_payload(env, "wrong-token-000000")

    tampered = dict(env)
    tampered["ct"] = ("A" if env["ct"][0] != "A" else "B") + env["ct"][1:]
    with pytest.raises(ValueError, match="MAC"):
        decrypt_beacon_payload(tampered, secret)

    # Verify static iOS PWA bundle builder injects repo metadata & omits manifest start_url override
    proj_root = Path(__file__).resolve().parents[1]
    stage = tmp_path / "gh_pages_out"
    build_static_pwa_bundle(
        proj_root,
        stage,
        owner="wwd177",
        repo="antigravity-remote-assistant",
        access_token=secret,
        tunnel_url=orig["tunnel_url"],
        lan_url=orig["lan_url"],
    )
    assert (stage / "index.html").is_file()
    assert 'data-beacon-repo="wwd177/antigravity-remote-assistant"' in (stage / "index.html").read_text(encoding="utf-8")
    assert (stage / "apple-touch-icon.png").is_file()
    assert (stage / "beacon.json").is_file()
    manifest_obj = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    assert "start_url" not in manifest_obj


@pytest.mark.asyncio
async def test_create_new_pc_ide_conversation_from_mobile_and_continue(tmp_path: Path) -> None:
    """Verify tapping `new_conversation` on mobile opens a new PC Antigravity window chat,
    creates a new IDE conversation ID on first prompt, streams live reply, and routes turn 2 to that new PC ID.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ide_store = tmp_path / "ide_new_pc"
    ide_store.mkdir(parents=True)
    db_path = ide_store / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, "
        "source TEXT, app_data_dir TEXT, nesting_depth INT, killed INT)"
    )
    con.commit()
    con.close()

    opened_count = 0
    created_prompts: list[str] = []
    followup_calls: list[tuple[str, str, str]] = []
    new_pc_cid = "new-pc-agent-88889999"

    def _fake_new_opener() -> tuple[bool, str]:
        nonlocal opened_count
        opened_count += 1
        return (True, "已在电脑端打开新对话窗口")

    def _fake_new_sender(prompt: str) -> tuple[bool, str, str]:
        created_prompts.append(prompt)
        tf = ide_store / "brain" / new_pc_cid / ".system_generated" / "logs" / "transcript.jsonl"
        tf.parent.mkdir(parents=True, exist_ok=True)

        def _sim_new() -> None:
            time.sleep(0.06)
            c1 = sqlite3.connect(db_path)
            c1.execute(
                "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_pc_cid, prompt[:24], "", 1, "2026-11-25 08:30:00+00:00", "", "CASCADE_RUN_STATUS_RUNNING", "", str(ide_store), 0, 0),
            )
            c1.commit()
            c1.close()
            tf.write_text(
                json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": prompt}, ensure_ascii=False)
                + "\n"
                + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "已在电脑端成功创建新窗口并完成第一轮回复！"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            time.sleep(0.12)
            c2 = sqlite3.connect(db_path)
            c2.execute("UPDATE conversation_summaries SET status = 'CASCADE_RUN_STATUS_IDLE' WHERE conversation_id = ?", (new_pc_cid,))
            c2.commit()
            c2.close()

        import threading
        threading.Thread(target=_sim_new, daemon=True).start()
        return (True, new_pc_cid, "已在电脑端新建 Antigravity 会话并发送")

    def _fake_followup_sender(cid: str, title: str, prompt: str) -> tuple[bool, str]:
        followup_calls.append((cid, title, prompt))
        tf = ide_store / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"

        def _sim_followup() -> None:
            time.sleep(0.06)
            c1 = sqlite3.connect(db_path)
            c1.execute("UPDATE conversation_summaries SET status = 'CASCADE_RUN_STATUS_RUNNING' WHERE conversation_id = ?", (cid,))
            c1.commit()
            c1.close()
            with tf.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": prompt}, ensure_ascii=False) + "\n")
                fh.write(json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "新窗口第二轮继续执行完毕！"}, ensure_ascii=False) + "\n")
            time.sleep(0.12)
            c2 = sqlite3.connect(db_path)
            c2.execute("UPDATE conversation_summaries SET status = 'CASCADE_RUN_STATUS_IDLE' WHERE conversation_id = ?", (cid,))
            c2.commit()
            c2.close()

        import threading
        threading.Thread(target=_sim_followup, daemon=True).start()
        return (True, "OK")

    tok = "new-pc-chat-token-77777777"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, "-c", "import sys; sys.exit(99)"])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=ide_store,
        cli_store_dir=tmp_path / "cli_unused2",
        codex_store_dir=tmp_path / "cdx_unused2",
        pc_window_new_opener=_fake_new_opener,
        pc_window_new_sender=_fake_new_sender,
        pc_window_sender=_fake_followup_sender,
    )
    pwa.pc_ide_poll_interval = 0.08
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5.0)  # hello

            # 1. Tap "新建电脑端对话" on mobile
            await ws.send(json.dumps({"type": "new_conversation", "workspace": "", "target": "ide"}))
            saw_ide_meta = False
            for _ in range(8):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "meta_update" and ev.get("active_store") == "ide":
                    saw_ide_meta = True
                if ev["type"] == "toast":
                    break
            assert opened_count == 1
            assert saw_ide_meta is True

            # 2. Send first prompt -> creates `new_pc_cid` on PC IDE window!
            await ws.send(json.dumps({"type": "chat", "text": "帮我在电脑新建一个数据分析项目"}))
            end1 = None
            bound_cid = ""
            for _ in range(30):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "meta_update" and ev.get("active_conversation_id"):
                    bound_cid = ev["active_conversation_id"]
                elif ev["type"] == "stream_end":
                    end1 = ev["item"]
                    break
            assert created_prompts == ["帮我在电脑新建一个数据分析项目"]
            assert bound_cid == new_pc_cid
            assert end1 is not None
            assert "已在电脑端成功创建新窗口并完成第一轮回复！" in end1["text"]

            # 3. Send second follow-up prompt -> must route to existing `new_pc_cid` via `pc_window_sender`!
            await ws.send(json.dumps({"type": "chat", "text": "继续添加可视化图表"}))
            end2 = None
            for _ in range(30):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    end2 = ev["item"]
                    break
            assert len(followup_calls) == 1
            assert followup_calls[0][0] == new_pc_cid
            assert followup_calls[0][2] == "继续添加可视化图表"
            assert end2 is not None
            assert "新窗口第二轮继续执行完毕！" in end2["text"]
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_duplicate_inflight_chat_guard_and_new_codex_thread(tmp_path: Path) -> None:
    """Verify:
    1. Sending a duplicate `chat` frame while the same slot is mid-stream emits a clean toast without corrupting the stream.
    2. Creating a brand-new `codex` conversation (`target='codex'`) runs `codex exec`, binds `thread_id`, and extracts title from rollout.
    3. `index.html` scopes `.drawer-tab[data-f]` so `.new-target-tab` onclick handlers are never overwritten.
    """
    html_src = (Path(__file__).resolve().parents[1] / "src" / "qq_agy_bridge" / "static" / "index.html").read_text(encoding="utf-8")
    assert ".drawer-tab[data-f]" in html_src
    assert "querySelectorAll('.drawer-tab')" not in html_src

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # Mock `codex` CLI script that emits `thread.started` + `agent_message` and writes rollout JSONL
    codex_dir = tmp_path / "codex_new"
    sess_dir = codex_dir / "sessions" / "2026" / "11" / "25"
    sess_dir.mkdir(parents=True)
    new_cdx_id = "cdx-brand-new-77889900"
    rollout = sess_dir / f"rollout-2026-11-25T17-00-00-{new_cdx_id}.jsonl"

    mock_codex_py = tmp_path / "mock_codex.py"
    mock_codex_py.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.read().strip()\n"
        f"open({str(rollout)!r}, 'w', encoding='utf-8').write("
        f"json.dumps({{'type':'response_item','payload':{{'type':'message','role':'user','content':[{{'type':'input_text','text':prompt}}]}}}}, ensure_ascii=False) + '\\n')\n"
        f"print(json.dumps({{'type':'thread.started','thread_id':{new_cdx_id!r}}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Codex 新会话创建成功！'}}, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )
    mock_codex_cmd = tmp_path / ("mock_codex.cmd" if sys.platform == "win32" else "mock_codex.sh")
    if sys.platform == "win32":
        mock_codex_cmd.write_text(f'@"{sys.executable}" "{mock_codex_py}" %*\r\n', encoding="utf-8")
    else:
        mock_codex_cmd.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock_codex_py}" "$@"\n', encoding="utf-8")
        mock_codex_cmd.chmod(0o755)

    con_cdx = sqlite3.connect(codex_dir / "state_5.sqlite")
    con_cdx.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INT, source TEXT, cwd TEXT, title TEXT, archived INT)")
    con_cdx.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (new_cdx_id, str(rollout), 1795598000, "exec", str(tmp_path), "", 0))
    con_cdx.commit()
    con_cdx.close()

    tok = "guard-and-codex-token-6666"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=tmp_path / "ide_g",
        cli_store_dir=tmp_path / "cli_g",
        codex_store_dir=codex_dir,
        codex_cli_path=mock_codex_cmd,
    )
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=5.0)  # hello

            # 1. Start slow turn & immediately fire duplicate `chat` on same slot
            await ws.send(json.dumps({"type": "chat", "text": "第一条慢请求 __SLOW_STREAM__"}))
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_start":
                    break
            await ws.send(json.dumps({"type": "chat", "text": "重复连点第二条"}))

            saw_guard_toast = False
            saw_end = False
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "toast" and "正在执行中" in ev["text"]:
                    saw_guard_toast = True
                elif ev["type"] == "stream_end":
                    saw_end = True
                    break
            assert saw_guard_toast is True
            assert saw_end is True

            # 2. Start brand-new Codex PC conversation (`target='codex'`)
            await ws.send(json.dumps({"type": "new_conversation", "workspace": str(tmp_path), "target": "codex"}))
            for _ in range(8):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "toast":
                    break

            await ws.send(json.dumps({"type": "chat", "text": "帮我初始化 Codex 后端服务"}))
            cdx_end = None
            for _ in range(25):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    cdx_end = ev["item"]
                    break
            assert cdx_end is not None
            assert "Codex 新会话创建成功！" in cdx_end["text"]

            # Verify empty-title Codex thread peeks human title from rollout JSONL!
            items = list_local_conversations(limit=20, ide_dir=tmp_path / "ide_g", cli_dir=tmp_path / "cli_g", codex_dir=codex_dir)
            cdx_item = next((x for x in items if x.conversation_id == new_cdx_id), None)
            assert cdx_item is not None
            assert "帮我初始化 Codex 后端服务" in cdx_item.title
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_security_cswsh_port_identity_codex_reconnect_and_no_double_exec(tmp_path: Path) -> None:
    """Regression suite for Neon x GPT-6.2 Astra joint red-team audit:
    1. `probe_agy_gateway_identity` distinguishes genuine Antigravity gateway (`'agy'`) from an unrelated HTTP listener (`'foreign'`).
    2. Cookie-authenticated WebSocket rejects cross-origin `Origin` (CSWSH defense), while same-origin Cookie WS succeeds.
    3. Direct non-loopback peers cannot spoof `CF-Connecting-IP` to bypass rate-limiting, and `manifest.json` does not echo HttpOnly Cookie tokens.
    4. Reconnecting mid-Codex turn cleanly serializes `hello.inflight` (no `Process` JSON crash), and if Codex fails AFTER `thread.started`, it reports `ERROR` WITHOUT double-submitting the prompt to `agy`.
    5. Aborted chunk uploads clean up `.part` files on disconnect, and frontend HTML enforces `no-referrer` + safe DOM creation.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from websockets.datastructures import Headers
    from qq_agy_bridge.beacon import decrypt_beacon_payload, encrypt_beacon_payload
    from qq_agy_bridge.launcher import probe_agy_gateway_identity

    # Static frontend safety assertions
    html_src = (Path(__file__).resolve().parents[1] / "src" / "qq_agy_bridge" / "static" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="referrer" content="no-referrer" />' in html_src
    assert 'id="bub-${id}"' not in html_src
    assert r"/^\s*(?:>|&gt;)/.test(lines[i])" in html_src

    # Beacon inner timestamp freshness check
    stale_env = encrypt_beacon_payload({"tunnel_url": "https://a.trycloudflare.com", "lan_url": "", "ts": 1000}, "tok-123")
    with pytest.raises(ValueError, match="expired"):
        decrypt_beacon_payload(stale_env, "tok-123", max_age_sec=60.0)
    fresh_env = encrypt_beacon_payload(
        {"tunnel_url": "https://a.trycloudflare.com", "lan_url": "", "ts": int(time.time() * 1000)},
        "tok-123",
    )
    assert decrypt_beacon_payload(fresh_env, "tok-123", max_age_sec=60.0)["tunnel_url"] == "https://a.trycloudflare.com"

    # 1. Foreign HTTP listener on random port MUST be detected as `'foreign'`
    class _ForeignHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"hello":"other-app"}')

        def log_message(self, *args) -> None:
            pass

    foreign_httpd = HTTPServer(("127.0.0.1", 0), _ForeignHandler)
    f_port = foreign_httpd.server_address[1]
    f_thread = threading.Thread(target=foreign_httpd.serve_forever, daemon=True)
    f_thread.start()
    try:
        assert probe_agy_gateway_identity("127.0.0.1", f_port) == "foreign"
    finally:
        foreign_httpd.shutdown()
        foreign_httpd.server_close()

    # 2. Spin up WebPwaServer with a mock Codex script that sleeps briefly, emits `thread.started`, then exits 1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    mock_cdx_fail_py = tmp_path / "mock_cdx_midfail.py"
    mock_cdx_fail_py.write_text(
        "import json, sys, time\n"
        "_ = sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'cdx-midfail-999'}), flush=True)\n"
        "sys.stderr.write('simulated mid-turn failure after thread started\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.35)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    mock_cdx_cmd = tmp_path / ("mock_cdx_midfail.cmd" if sys.platform == "win32" else "mock_cdx_midfail.sh")
    if sys.platform == "win32":
        mock_cdx_cmd.write_text(f'@"{sys.executable}" "{mock_cdx_fail_py}" %*\r\n', encoding="utf-8")
    else:
        mock_cdx_cmd.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock_cdx_fail_py}" "$@"\n', encoding="utf-8")
        mock_cdx_cmd.chmod(0o755)

    tok = "astra-redteam-token-999"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    agy_calls: list[str] = []
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=tmp_path / "ide_sec",
        cli_store_dir=tmp_path / "cli_sec",
        codex_store_dir=tmp_path / "cdx_sec",
        codex_cli_path=mock_cdx_cmd,
    )

    # Verify non-loopback peer cannot spoof `CF-Connecting-IP`
    class _FakeLanConn:
        remote_address = ("192.168.1.88", 54321)

    hdr_spoof = Headers([("CF-Connecting-IP", "8.8.8.8")])
    assert pwa._client_ip(_FakeLanConn(), hdr_spoof) == "192.168.1.88"  # type: ignore[arg-type]

    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        await asyncio.sleep(0.2)
        assert (await asyncio.to_thread(probe_agy_gateway_identity, "127.0.0.1", port)) == "agy"

        # Cookie-only GET /manifest.json MUST NOT echo secret token into `start_url`
        def _fetch_manifest_with_cookie() -> dict:
            req_m = urllib.request.Request(
                f"http://127.0.0.1:{port}/manifest.json",
                headers={"Cookie": f"agy_pwa_token={tok}"},
            )
            with urllib.request.urlopen(req_m, timeout=3) as resp_m:
                return json.loads(resp_m.read().decode("utf-8"))

        manifest_obj = await asyncio.to_thread(_fetch_manifest_with_cookie)
        assert manifest_obj["start_url"] == "/"

        # Cookie-authenticated cross-origin WebSocket MUST be rejected with 4003 (CSWSH block!)
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws",
            origin="https://evil.example",
            additional_headers={"Cookie": f"agy_pwa_token={tok}"},
        ) as ws_evil:
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
                await asyncio.wait_for(ws_evil.recv(), timeout=3.0)
            assert exc_info.value.rcvd is not None and exc_info.value.rcvd.code == 4003

        # Aborted upload MUST NOT leave `.part` or partial files behind on disconnect
        from qq_agy_bridge.desktop_tools import PROJECT_INBOX_DIR

        before_parts = set(PROJECT_INBOX_DIR.glob("*.part")) if PROJECT_INBOX_DIR.is_dir() else set()
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws_up:
            await asyncio.wait_for(ws_up.recv(), timeout=3.0)
            await ws_up.send(json.dumps({"type": "upload_init", "upload_id": "abort1", "name": "half.bin", "size": 100}))
            await asyncio.wait_for(ws_up.recv(), timeout=3.0)  # upload_ack
            await ws_up.send(json.dumps({"type": "upload_chunk", "upload_id": "abort1", "data": base64.b64encode(b"12345").decode()}))
            await asyncio.wait_for(ws_up.recv(), timeout=3.0)  # upload_progress
        await asyncio.sleep(0.15)
        after_parts = set(PROJECT_INBOX_DIR.glob("*.part")) if PROJECT_INBOX_DIR.is_dir() else set()
        assert after_parts - before_parts == set()

        # Reconnect while Codex turn is in-flight + verify NO fallback double-exec to `agy` when Codex fails post-accept
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws1:
            await asyncio.wait_for(ws1.recv(), timeout=3.0)  # hello
            await ws1.send(json.dumps({"type": "new_conversation", "workspace": str(tmp_path), "target": "codex"}))
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws1.recv(), timeout=3.0))
                if ev["type"] == "meta_update":
                    cdx_slot = ev["slot"]
                    break
            else:
                cdx_slot = ""
            assert cdx_slot

            orig_ask = pwa._session_for_slot(cdx_slot).ask

            async def _spy_ask(p: str, **kw):  # type: ignore[no-untyped-def]
                agy_calls.append(p)
                return await orig_ask(p, **kw)

            pwa._session_for_slot(cdx_slot).ask = _spy_ask  # type: ignore[method-assign]

            await ws1.send(json.dumps({"type": "chat", "slot": cdx_slot, "text": "只允许执行一次的修改指令"}))
            for _ in range(8):
                ev = json.loads(await asyncio.wait_for(ws1.recv(), timeout=3.0))
                if ev["type"] == "stream_start":
                    break

            # Open a second WS client mid-Codex turn -> `hello` MUST serialize cleanly without `TypeError: Process`!
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws2:
                hello2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=3.0))
                assert hello2["type"] == "hello"
                assert hello2["busy"] is True
                assert isinstance(hello2["inflight"], dict)
                assert "codex_proc" not in hello2["inflight"]

            final_item = None
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    final_item = ev["item"]
                    break
            assert final_item is not None
            assert "simulated mid-turn failure" in final_item["text"]
            # CRITICAL: `agy` fallback MUST NOT have been called!
            assert agy_calls == []
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_ergonomic_and_expert_upgrades_e2e(tmp_path: Path) -> None:
    """Verify user & expert power upgrades:
    1. `client_id` idempotent deduplication + `chat_ack` delivery receipts prevent duplicate execution on mobile reconnect.
    2. Background slot completion populates `slots_overview.unread` and clears upon `select_conversation`.
    3. One-time IDE approval `action_token` cannot be replayed twice, and `perform_desktop_remote_input` rejects non-whitelisted keys.
    4. `pc_system_inspect`, `pc_clipboard_set`, `pc_clipboard_get`, and `desktop_input` round-trip cleanly over WebSocket.
    5. Frontend `index.html` includes hunk diff quoting, collapsible long code cards, touch desktop toggle, and per-slot drafts.
    """
    from qq_agy_bridge.desktop_tools import (
        _IDE_ACTION_LOCK,
        _IDE_ACTION_TOKENS,
        invoke_antigravity_ide_action,
        perform_desktop_remote_input,
        set_pc_clipboard_text,
    )

    html_src = (Path(__file__).resolve().parents[1] / "src" / "qq_agy_bridge" / "static" / "index.html").read_text(encoding="utf-8")
    assert "code-expand-bar" in html_src
    assert "diff-hunk-card" in html_src
    assert "toggleTouchBtn" in html_src
    assert "agy_slot_drafts_v1" in html_src
    assert "slashMenu" in html_src

    # Whitelist, NaN/Inf, & size guards on desktop tools
    ok_bad_key, msg_bad_key = perform_desktop_remote_input("key", key_name="f12_evil")
    assert ok_bad_key is False and "白名单" in msg_bad_key
    ok_nan, msg_nan = perform_desktop_remote_input("click", x_ratio=float("nan"), y_ratio=0.5)
    assert ok_nan is False and "有限" in msg_nan
    ok_oob, _ = perform_desktop_remote_input("click", x_ratio=1.5, y_ratio=0.5)
    assert ok_oob is False
    ok_bad_act, _ = perform_desktop_remote_input("drag_drop")
    assert ok_bad_act is False
    ok_huge_clip, _ = set_pc_clipboard_text("x" * 70000)
    assert ok_huge_clip is False

    # Expired / unknown / replayed one-time IDE action token must be rejected without touching UIA
    with _IDE_ACTION_LOCK:
        _IDE_ACTION_TOKENS["expired-tok-1"] = {
            "hwnd": 12345,
            "cid": "c1",
            "label": "Proceed",
            "expires_mono": time.monotonic() - 5.0,
        }
    ok_exp, note_exp = invoke_antigravity_ide_action("expired-tok-1")
    assert ok_exp is False and "过期" in note_exp
    ok_replay, _ = invoke_antigravity_ide_action("expired-tok-1")
    assert ok_replay is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    clip_box = {"text": "initial-pc-clip"}
    invoked_tokens: list[str] = []
    remote_inputs: list[tuple[str, float, float, str]] = []

    def _mock_inspect_actions(cid: str) -> list[dict]:
        return [{"action_token": "tok-proceed-once", "label": "Proceed", "conversation_id": cid}]

    def _mock_invoke_action(tok: str) -> tuple[bool, str]:
        if tok in invoked_tokens:
            return (False, "确认按钮已过期或已使用")
        invoked_tokens.append(tok)
        return (True, "已在电脑端触发「Proceed」")

    def _mock_desk_input(act: str, xr: float, yr: float, kn: str) -> tuple[bool, str]:
        remote_inputs.append((act, xr, yr, kn))
        return (True, f"已执行 {act}")

    tok = "ergonomic-upgrade-tok-777"
    cfg = _make_config(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=tmp_path / "ide_erg",
        cli_store_dir=tmp_path / "cli_erg",
        codex_store_dir=tmp_path / "cdx_erg",
        ide_actions_inspector=_mock_inspect_actions,
        ide_action_invoker=_mock_invoke_action,
        desktop_input_handler=_mock_desk_input,
        pc_clipboard_getter=lambda: {"ok": True, "text": clip_box["text"], "truncated": False, "length": len(clip_box["text"])},
        pc_clipboard_setter=lambda t: (clip_box.__setitem__("text", t) or True, "已写入电脑剪贴板"),
        pc_system_inspector=lambda _ws: {
            "cpu_pct": 14,
            "ram_used_gb": 12.4,
            "ram_total_gb": 32.0,
            "ram_pct": 39,
            "disk_free_gb": 180.5,
            "disk_total_gb": 512.0,
            "battery_pct": 88,
            "charging": True,
            "ports": [{"port": 5173, "pid": 4321, "process": "node", "addr": "127.0.0.1"}],
        },
    )
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
            assert "slots_overview" in hello

            # 1. Idempotent `client_id` deduplication + `chat_ack`
            await ws.send(json.dumps({"type": "chat", "client_id": "idem-msg-001", "text": "测试幂等回执"}))
            saw_ack = False
            saw_end = False
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "chat_ack" and ev["client_id"] == "idem-msg-001":
                    saw_ack = True
                elif ev["type"] == "stream_end":
                    saw_end = True
                    break
            assert saw_ack and saw_end

            turns_after_first = pwa._session_for_slot(hello["slot"]).num_turns
            assert turns_after_first == 1

            # Re-send exact same `client_id` -> must return `duplicate_ignored` and NOT run `ask` again!
            await ws.send(json.dumps({"type": "chat", "client_id": "idem-msg-001", "text": "测试幂等回执"}))
            dup_ack = None
            for _ in range(8):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "chat_ack" and ev.get("stage") == "duplicate_ignored":
                    dup_ack = ev
                    break
            assert dup_ack is not None
            assert pwa._session_for_slot(hello["slot"]).num_turns == 1

            # 2. IDE approval inspect & one-time invoke
            await ws.send(json.dumps({"type": "ide_actions_inspect", "conversation_id": "cid-demo"}))
            act_ev = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "ide_actions":
                    act_ev = ev
                    break
            assert act_ev is not None and act_ev["actions"][0]["label"] == "Proceed"

            await ws.send(json.dumps({"type": "ide_action_invoke", "action_token": "tok-proceed-once"}))
            res_ev = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "ide_action_result":
                    res_ev = ev
                    break
            assert res_ev is not None and res_ev["ok"] is True

            # Second invoke with same token fails one-time check
            await ws.send(json.dumps({"type": "ide_action_invoke", "action_token": "tok-proceed-once"}))
            res_ev2 = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "ide_action_result":
                    res_ev2 = ev
                    break
            assert res_ev2 is not None and res_ev2["ok"] is False

            # 3. PC System + Bidirectional Clipboard Sync
            await ws.send(json.dumps({"type": "pc_system_inspect"}))
            sys_ev = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "pc_system_state":
                    sys_ev = ev
                    break
            assert sys_ev is not None
            assert sys_ev["system"]["ports"][0]["port"] == 5173
            assert sys_ev["clipboard"]["text"] == "initial-pc-clip"

            await ws.send(json.dumps({"type": "pc_clipboard_set", "text": "from-iphone-clipboard"}))
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "toast" and "剪贴板" in ev["text"]:
                    break
            assert clip_box["text"] == "from-iphone-clipboard"

            # 4. Invalid `desktop_input` coordinates emit toast WITHOUT killing the WS loop!
            await ws.send(json.dumps({"type": "desktop_input", "action": "click", "x_ratio": "bad-num", "y_ratio": 0.5}))
            err_toast = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if ev["type"] == "toast" and "无效" in ev["text"]:
                    err_toast = ev
                    break
            assert err_toast is not None

            await ws.send(json.dumps({"type": "ping", "ts": 12345}))
            pong_ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert pong_ev["type"] == "pong"
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()
