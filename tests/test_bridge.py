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
