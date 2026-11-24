from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import websockets

from qq_agy_bridge.agy_session import SessionManager
from qq_agy_bridge.config import BridgeConfig
from qq_agy_bridge.maintenance import ModelFailoverPool, run_safe_cache_cleanup
from qq_agy_bridge.onebot import (
    OneBotBridge,
    extract_media_and_clean_for_qq,
    extract_plain_text,
    format_for_qq,
    is_pure_screenshot_request,
    is_safe_public_http_url,
    split_message,
)

MOCK_AGY = Path(__file__).parent / "mock_agy.py"


def _make_config(
    tmp_path: Path,
    *,
    mode: str = "forward_ws",
    ws_url: str = "ws://127.0.0.1:0",
    reverse_port: int = 0,
    token: str = "secret-token",
    max_length: int = 300,
) -> BridgeConfig:
    return BridgeConfig(
        onebot_mode=mode,
        onebot_ws_url=ws_url,
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=reverse_port,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token=token,
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=max_length,
        log_level="DEBUG",
    )


def test_extract_plain_text() -> None:
    event_array = {
        "message": [
            {"type": "text", "data": {"text": "你好 "}},
            {"type": "image", "data": {"file": "abc.png"}},
            {"type": "text", "data": {"text": " 世界"}},
        ]
    }
    assert extract_plain_text(event_array) == "你好 [图片] 世界"

    event_cq = {
        "raw_message": "帮我看下 &amp; [CQ:image,file=1.png] 文件",
    }
    assert extract_plain_text(event_cq) == "帮我看下 & [图片] 文件"


def test_is_pure_screenshot_request() -> None:
    assert is_pure_screenshot_request("截个图")
    assert is_pure_screenshot_request("截个屏给我看看")
    assert is_pure_screenshot_request("截个图给我看一下目前电脑的界面")
    assert is_pure_screenshot_request("看看我电脑桌面")
    assert is_pure_screenshot_request("帮我截屏")

    assert not is_pure_screenshot_request("帮我打开微信再截个图")
    assert not is_pure_screenshot_request("截个图帮我分析一下报错")
    assert not is_pure_screenshot_request("帮我写一个截图工具")
    assert not is_pure_screenshot_request("今天天气怎么样")


def test_split_message() -> None:
    text = "A" * 120 + "\n\n" + "B" * 120 + "\n\n" + "C" * 120
    chunks = split_message(text, max_length=200)
    assert len(chunks) == 3
    assert chunks[0] == "A" * 120
    assert chunks[1] == "B" * 120
    assert chunks[2] == "C" * 120


def test_format_for_qq_strips_thoughts_markdown_and_extracts_images(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    raw = (
        "<thought>先分析一下内部步骤...</thought>\n"
        "### 找到一个不错的开源项目\n"
        "推荐 **NapCatQQ**，直接跑 `git clone` 就行。\n"
        "地址：[项目主页](https://github.com/NapNeko/NapCatQQ)\n"
        "本地参考：[main.py](file:///C:/tmp/main.py)\n"
        "---\n"
        "```bash\n"
        "git clone https://github.com/example/demo.git\n"
        "```\n"
        f"[SEND_IMAGE: {img}]\n"
    )
    cleaned, images = format_for_qq(raw, clean_markdown=True)
    assert "内部步骤" not in cleaned
    assert "<thought>" not in cleaned
    assert "**" not in cleaned
    assert "###" not in cleaned
    assert "`git clone`" not in cleaned
    assert "NapCatQQ" in cleaned
    assert "项目主页 https://github.com/NapNeko/NapCatQQ" in cleaned
    assert "file:///" not in cleaned
    assert "git clone https://github.com/example/demo.git" in cleaned
    assert "[SEND_IMAGE:" not in cleaned
    assert images == [img.resolve()]


def test_security_ssrf_and_sensitive_file_guards(tmp_path: Path) -> None:
    assert not is_safe_public_http_url("http://127.0.0.1:6099/webui")
    assert not is_safe_public_http_url("http://localhost:3001")
    assert not is_safe_public_http_url("http://169.254.169.254/latest/meta-data")
    assert not is_safe_public_http_url("http://192.168.1.1/admin")
    assert not is_safe_public_http_url("file:///C:/Windows/win.ini")

    env_secret = tmp_path / ".env"
    env_secret.write_text("SECRET=123", encoding="utf-8")
    onebot_cfg = tmp_path / "onebot11_10000001.json"
    onebot_cfg.write_text("{}", encoding="utf-8")
    safe_doc = tmp_path / "summary.txt"
    safe_doc.write_text("hello", encoding="utf-8")

    raw = (
        f"结果如下\n"
        f"[SEND_IMAGE: {env_secret}]\n"
        f"[SEND_FILE: {env_secret}]\n"
        f"[SEND_FILE: {onebot_cfg}]\n"
        f"[SEND_FILE: {safe_doc}]\n"
    )
    cleaned, imgs, files = extract_media_and_clean_for_qq(raw)
    assert cleaned == "结果如下"
    assert imgs == []
    assert files == [safe_doc.resolve()]


def test_safe_cache_cleanup_protects_config_and_prunes_expired(tmp_path: Path) -> None:
    import os as _os
    import time as _time

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old_file = inbox / "old_screen.png"
    old_file.write_bytes(b"x" * 2048)
    past = _time.time() - 48 * 3600
    _os.utime(old_file, (past, past))

    fresh_file = inbox / "fresh_screen.png"
    fresh_file.write_bytes(b"y" * 1024)

    protected_cfg = tmp_path / "config"
    protected_cfg.mkdir()
    token_json = protected_cfg / "webui.json"
    token_json.write_text('{"token":"keep"}', encoding="utf-8")
    _os.utime(token_json, (past, past))

    rep = run_safe_cache_cleanup([(inbox, 24.0, 10), (protected_cfg, 0.0, 0)])
    assert rep.deleted_files == 1
    assert rep.freed_bytes == 2048
    assert not old_file.exists()
    assert fresh_file.exists()
    assert token_json.exists()


@pytest.mark.asyncio
async def test_automatic_model_quota_failover_and_recovery(tmp_path: Path) -> None:
    cfg = BridgeConfig(
        onebot_mode="forward_ws",
        onebot_ws_url="ws://127.0.0.1:0",
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=0,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token="tok",
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="exhausted-sonnet",
        agy_fallback_models=("exhausted-sonnet", "exhausted-neon", "healthy-flash"),
        quota_cooldown_minutes=60,
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=500,
        log_level="DEBUG",
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    s = sessions.get("10001")
    try:
        res = await s.ask("测试额度自动切换")
        assert res.status == "SUCCESS"
        assert res.switched_from_model == "exhausted-sonnet"
        assert res.active_model == "healthy-flash"
        assert s.model == "healthy-flash"
        assert "测试额度自动切换" in res.response
        # Both exhausted models should be recorded in cooldown summary
        cooling_names = {m for m, _ in s.failover_pool.cooldown_summary()}
        assert cooling_names == {"exhausted-sonnet", "exhausted-neon"}
    finally:
        await sessions.shutdown_all()

    # Verify cooldown expiry auto-restores primary model
    pool = ModelFailoverPool("primary-m", ("primary-m", "backup-m"), cooldown_seconds=0.05)
    pool.cooldown_seconds = 0.05
    pool.mark_exhausted("primary-m")
    assert pool.pick_best_available() == "backup-m"
    await asyncio.sleep(0.08)
    assert pool.pick_best_available() == "primary-m"


@pytest.mark.asyncio
async def test_burst_coalescing_and_memory_command(tmp_path: Path) -> None:
    received_actions: list[dict] = []
    server_ws_ready: asyncio.Queue = asyncio.Queue()
    mem_file = tmp_path / "user_profile.md"

    async def mock_server(ws) -> None:
        await server_ws_ready.put(ws)
        try:
            async for raw in ws:
                received_actions.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async with websockets.serve(mock_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = BridgeConfig(
            onebot_mode="forward_ws",
            onebot_ws_url=f"ws://127.0.0.1:{port}",
            onebot_reverse_host="127.0.0.1",
            onebot_reverse_port=0,
            onebot_reverse_path="/onebot/v11/ws",
            onebot_access_token="secret-token",
            allowed_qq_users=frozenset({"10001"}),
            agy_executable=sys.executable,
            agy_workspace=tmp_path,
            agy_model="",
            agy_effort="",
            agy_dangerously_skip_permissions=True,
            agy_timeout_seconds=30,
            progress_notice_delay_seconds=0,
            max_message_length=500,
            log_level="DEBUG",
            user_memory_file=mem_file,
            message_debounce_seconds=0.3,
        )
        sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
        bridge = OneBotBridge(cfg, session_manager=sessions)
        stop_event = asyncio.Event()
        bridge_task = asyncio.create_task(bridge.run(stop_event=stop_event))
        try:
            ws = await asyncio.wait_for(server_ws_ready.get(), timeout=5.0)

            # 1. /memory executes immediately without debounce
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "/memory 常用项目在 D:\\code",
                    }
                )
            )
            for _ in range(30):
                if received_actions:
                    break
                await asyncio.sleep(0.05)
            assert any("记下来啦" in str(a.get("params", {}).get("message", "")) for a in received_actions)
            assert "D:\\code" in mem_file.read_text(encoding="utf-8")
            received_actions.clear()

            # 2. Rapid two-message burst coalesces into ONE turn + emits typing status
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "前半句内容",
                    }
                )
            )
            await asyncio.sleep(0.08)
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "后半句补充",
                    }
                )
            )
            for _ in range(50):
                msg_Replies = [a for a in received_actions if a.get("action") == "send_private_msg"]
                if msg_Replies:
                    break
                await asyncio.sleep(0.1)

            typing_actions = [a for a in received_actions if a.get("action") == "set_input_status"]
            msg_actions = [a for a in received_actions if a.get("action") == "send_private_msg"]
            assert len(typing_actions) >= 1
            assert len(msg_actions) == 1
            reply_body = msg_actions[0]["params"]["message"]
            assert "turn=1" in reply_body
            assert "前半句内容" in reply_body
            assert "后半句补充" in reply_body
            assert "D:\\code" in reply_body
        finally:
            stop_event.set()
            await asyncio.wait_for(bridge_task, timeout=5.0)


@pytest.mark.asyncio
async def test_persona_injected_only_on_first_turn(tmp_path: Path) -> None:
    cfg = BridgeConfig(
        onebot_mode="forward_ws",
        onebot_ws_url="ws://127.0.0.1:0",
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=0,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token="tok",
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=500,
        log_level="DEBUG",
        agy_system_prompt="优先去GitHub搜索开源项目",
        clean_qq_formatting=True,
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    s = sessions.get("10001")
    try:
        r1 = await s.ask("帮我写个工具")
        assert "优先去GitHub搜索开源项目" in r1.response
        assert "帮我写个工具" in r1.response

        r2 = await s.ask("好的谢谢")
        assert "优先去GitHub搜索开源项目" not in r2.response
        assert "好的谢谢" in r2.response
    finally:
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_forward_ws_end_to_end(tmp_path: Path) -> None:
    received_actions: list[dict] = []
    server_ws_ready: asyncio.Queue = asyncio.Queue()

    async def mock_napcat_server(ws) -> None:
        assert ws.request is not None
        assert ws.request.headers.get("Authorization") == "Bearer secret-token"
        await server_ws_ready.put(ws)
        try:
            async for raw in ws:
                received_actions.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async with websockets.serve(mock_napcat_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = _make_config(tmp_path, mode="forward_ws", ws_url=f"ws://127.0.0.1:{port}")
        sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
        bridge = OneBotBridge(cfg, session_manager=sessions)
        stop_event = asyncio.Event()
        bridge_task = asyncio.create_task(bridge.run(stop_event=stop_event))

        try:
            ws = await asyncio.wait_for(server_ws_ready.get(), timeout=5.0)

            # 1. Unauthorized private message -> must be ignored
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 99999,
                        "message": [{"type": "text", "data": {"text": "hack"}}],
                    }
                )
            )
            # 2. Group message from whitelisted user -> must be ignored
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 55555,
                        "user_id": 10001,
                        "message": [{"type": "text", "data": {"text": "群消息测试"}}],
                    }
                )
            )
            await asyncio.sleep(0.25)
            assert received_actions == []

            # 3. Authorized private message turn 1
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "message": [{"type": "text", "data": {"text": "第一句话"}}],
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= 1:
                    break
                await asyncio.sleep(0.1)
            assert len(received_actions) == 1
            assert received_actions[0]["action"] == "send_private_msg"
            assert received_actions[0]["params"]["user_id"] == 10001
            assert "turn=1" in received_actions[0]["params"]["message"]
            assert "第一句话" in received_actions[0]["params"]["message"]

            # 4. Turn 2 keeps same session & history
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "message": [{"type": "text", "data": {"text": "第二句话"}}],
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= 2:
                    break
                await asyncio.sleep(0.1)
            assert len(received_actions) == 2
            assert "turn=2" in received_actions[1]["params"]["message"]
            assert "历史条数=2" in received_actions[1]["params"]["message"]

            # 5. Built-in /status command
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "/status",
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= 3:
                    break
                await asyncio.sleep(0.1)
            assert "mock-conv-" in received_actions[2]["params"]["message"]
            assert "累计轮次：2" in received_actions[2]["params"]["message"]

            # 6. Built-in /cd command into a subdirectory
            sub_dir = tmp_path / "my_project"
            sub_dir.mkdir()
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": f"/cd {sub_dir}",
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= 4:
                    break
                await asyncio.sleep(0.1)
            assert "已切换工作目录" in received_actions[3]["params"]["message"]

            # 7. New turn runs in new workspace and fresh turn=1
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "切换目录后的消息",
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= 5:
                    break
                await asyncio.sleep(0.1)
            assert "turn=1" in received_actions[4]["params"]["message"]
            assert "cwd=my_project" in received_actions[4]["params"]["message"]

            # 8. Long reply splits into multiple chunks
            before_long = len(received_actions)
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "__LONG_REPLY__",
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) >= before_long + 2:
                    break
                await asyncio.sleep(0.1)
            assert len(received_actions) >= before_long + 2
            assert received_actions[before_long]["params"]["message"].startswith("[1/")

            # 9. Error reporting
            before_err = len(received_actions)
            await ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "__TRIGGER_ERROR__",
                    }
                )
            )
            for _ in range(50):
                if len(received_actions) > before_err:
                    break
                await asyncio.sleep(0.1)
            assert "simulated failure" in received_actions[-1]["params"]["message"]
        finally:
            stop_event.set()
            await asyncio.wait_for(bridge_task, timeout=5.0)


@pytest.mark.asyncio
async def test_reverse_ws_mode(tmp_path: Path) -> None:
    # Find an available ephemeral port
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = _make_config(tmp_path, mode="reverse_ws", reverse_port=port, token="rev-token")
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    bridge = OneBotBridge(cfg, session_manager=sessions)
    stop_event = asyncio.Event()
    bridge_task = asyncio.create_task(bridge.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.3)

        # Wrong token should be closed with 1008
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/onebot/v11/ws",
            additional_headers={"Authorization": "Bearer wrong"},
        ) as bad_ws:
            with pytest.raises(websockets.ConnectionClosed) as exc_info:
                await bad_ws.recv()
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 1008

        # Valid token succeeds and replies to /help
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/onebot/v11/ws",
            additional_headers={"Authorization": "Bearer rev-token"},
        ) as good_ws:
            await good_ws.send(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "raw_message": "/help",
                    }
                )
            )
            raw_reply = await asyncio.wait_for(good_ws.recv(), timeout=5.0)
            reply = json.loads(raw_reply)
            assert reply["action"] == "send_private_msg"
            assert "/new" in reply["params"]["message"]
    finally:
        stop_event.set()
        await asyncio.wait_for(bridge_task, timeout=5.0)


@pytest.mark.asyncio
async def test_web_pwa_streaming_upload_and_auth(tmp_path: Path) -> None:
    import base64 as _b64
    import socket
    import urllib.request as _urlreq
    from qq_agy_bridge.web_pwa import WebPwaServer

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    tok = "super-secret-pwa-token-12345"
    cfg = BridgeConfig(
        onebot_mode="forward_ws",
        onebot_ws_url="ws://127.0.0.1:0",
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=0,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token="tok",
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="balanced-test",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=500,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=port,
        web_access_token=tok,
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(cfg, session_manager=sessions, access_token=tok)
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.3)

        # 1. HTTP GET / and /manifest.json serve PWA shell & manifest cleanly
        html_bytes = await asyncio.to_thread(
            lambda: _urlreq.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
        )
        assert b"Antigravity" in html_bytes
        manifest_bytes = await asyncio.to_thread(
            lambda: _urlreq.urlopen(f"http://127.0.0.1:{port}/manifest.json", timeout=5).read()
        )
        assert b"standalone" in manifest_bytes

        # 2. Invalid WS token is rejected with 4001
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token=wrong-token") as bad_ws:
            with pytest.raises(websockets.ConnectionClosed) as exc_info:
                await bad_ws.recv()
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 4001

        # 3. Valid WS token connects, uploads a file, downloads it via /dl/, and streams a chat reply
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert hello["type"] == "hello"
            assert hello["model"] == "balanced-test"

            # Chunked file upload
            file_payload = b"hello from mobile pwa"
            await ws.send(json.dumps({"type": "upload_init", "upload_id": "u1", "name": "note.txt", "size": len(file_payload)}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert ack["type"] == "upload_ack"

            await ws.send(json.dumps({"type": "upload_chunk", "upload_id": "u1", "data": _b64.b64encode(file_payload).decode("ascii")}))
            prog = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert prog["type"] == "upload_progress"

            await ws.send(json.dumps({"type": "upload_complete", "upload_id": "u1"}))
            done = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert done["type"] == "upload_done"
            media_id = done["media_id"]

            # Authenticated HTTP download works; unauthenticated returns 401
            dl_bytes = await asyncio.to_thread(
                lambda: _urlreq.urlopen(f"http://127.0.0.1:{port}/dl/{media_id}?token={tok}", timeout=5).read()
            )
            assert dl_bytes == file_payload

            # Streaming chat with attachment
            await ws.send(
                json.dumps(
                    {
                        "type": "chat",
                        "text": "手机端流式测试",
                        "attachments": [{"media_id": media_id, "name": "note.txt", "is_image": False}],
                    }
                )
            )

            seen_types: list[str] = []
            deltas: list[str] = []
            final_item = None
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                seen_types.append(ev["type"])
                if ev["type"] == "stream_delta":
                    deltas.append(ev["delta"])
                elif ev["type"] == "stream_end":
                    final_item = ev["item"]
                    break

            assert "user_msg" in seen_types
            assert "stream_start" in seen_types
            assert len(deltas) >= 2
            assert final_item is not None
            assert "手机端流式测试" in final_item["text"]
            assert "note.txt" in final_item["text"]
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_conversation_list_load_switch_and_new_chat(tmp_path: Path) -> None:
    import socket
    import sqlite3
    from qq_agy_bridge.web_pwa import WebPwaServer

    # Create mock IDE store with SQLite summaries + transcript.jsonl + transcript_full.jsonl
    ide_store = tmp_path / "mock_ide"
    cli_store = tmp_path / "mock_cli"
    ide_cid = "ide-conv-12345678"
    log_dir = ide_store / "brain" / ide_cid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)

    proj_dir = tmp_path / "demo_proj"
    proj_dir.mkdir()

    con = sqlite3.connect(ide_store / "conversation_summaries.db")
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT, title TEXT, preview TEXT, step_count INT, "
        "last_modified_time TEXT, workspace_uris TEXT, status TEXT, "
        "nesting_depth INT, killed INT)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ide_cid,
            "前端组件重构任务",
            "讨论按钮样式",
            6,
            "2026-11-24 12:30:00+00:00",
            json.dumps([proj_dir.as_uri()]),
            "CASCADE_RUN_STATUS_IDLE",
            0,
            0,
        ),
    )
    con.commit()
    con.close()

    (log_dir / "transcript.jsonl").write_text(
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "<USER_REQUEST>\n帮我重构登录页组件\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "截断版本...",
                "truncated_fields": ["content"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (log_dir / "transcript_full.jsonl").write_text(
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "<USER_REQUEST>\n帮我重构登录页组件\n</USER_REQUEST>",
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "完整回复：登录页已拆分为独立表单与校验模块。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    tok = "conv-switch-secret-token"
    cfg = BridgeConfig(
        onebot_mode="forward_ws",
        onebot_ws_url="ws://127.0.0.1:0",
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=0,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token="tok",
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="balanced-test",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=500,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=port,
        web_access_token=tok,
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=ide_store,
        cli_store_dir=cli_store,
    )
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.25)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            _hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert _hello["type"] == "hello"

            # 1. List conversations finds the PC IDE conversation
            await ws.send(json.dumps({"type": "list_conversations"}))
            clist = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert clist["type"] == "conversations_list"
            assert len(clist["items"]) == 1
            assert clist["items"][0]["title"] == "前端组件重构任务"
            assert clist["items"][0]["store"] == "ide"

            # 2. Select the PC IDE conversation -> loads FULL non-truncated chat history!
            await ws.send(
                json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"})
            )
            loaded = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert loaded["type"] == "conversation_loaded"
            assert loaded["title"] == "前端组件重构任务"
            assert len(loaded["history"]) == 2
            assert loaded["history"][0]["text"] == "帮我重构登录页组件"
            assert loaded["history"][1]["text"] == "完整回复：登录页已拆分为独立表单与校验模块。"

            # 3. Sending a continuation message carries over handoff context AND switches to demo_proj!
            await ws.send(json.dumps({"type": "chat", "text": "继续加上记住密码勾选框"}))
            end_item = None
            for _ in range(15):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    end_item = ev["item"]
                    break
            assert end_item is not None
            assert "cwd=demo_proj" in end_item["text"]
            assert "前端组件重构任务" in end_item["text"]
            assert "继续加上记住密码勾选框" in end_item["text"]

            # 4. Creating a new conversation clears history cleanly
            new_ws_dir = tmp_path / "fresh_proj"
            new_ws_dir.mkdir()
            await ws.send(json.dumps({"type": "new_conversation", "workspace": str(new_ws_dir)}))
            seen = []
            for _ in range(3):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                seen.append(ev["type"])
                if ev["type"] == "toast":
                    break
            assert "clear_history" in seen
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_pwa_midstream_reconnect_cookie_auth_epoch_and_live_ide_sync(tmp_path: Path) -> None:
    import socket
    import urllib.request as _urlreq
    from qq_agy_bridge.web_pwa import WebPwaServer

    ide_store = tmp_path / "ide_live"
    ide_cid = "ide-live-99887766"
    log_dir = ide_store / "brain" / ide_cid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    tfile = log_dir / "transcript.jsonl"
    tfile.write_text(
        "\n".join(
            [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "第一问"}),
                json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "先检查配置..."}),
                json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "配置检查完毕！"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    tok = "super-strong-pwa-token-2026"
    cfg = BridgeConfig(
        onebot_mode="forward_ws",
        onebot_ws_url="ws://127.0.0.1:0",
        onebot_reverse_host="127.0.0.1",
        onebot_reverse_port=0,
        onebot_reverse_path="/onebot/v11/ws",
        onebot_access_token="tok",
        allowed_qq_users=frozenset({"10001"}),
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="balanced-test",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        max_message_length=500,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=port,
        web_access_token=tok,
    )
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(cfg, session_manager=sessions, access_token=tok, ide_store_dir=ide_store)
    pwa.ide_watch_interval = 0.15
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))

    try:
        await asyncio.sleep(0.2)

        # 1. Scanning QR URL sets HttpOnly cookie & dynamic manifest preserves start_url token
        def _check_http():
            with _urlreq.urlopen(f"http://127.0.0.1:{port}/?token={tok}", timeout=5) as r:
                set_cookie = r.headers.get("Set-Cookie", "")
            with _urlreq.urlopen(f"http://127.0.0.1:{port}/manifest.json?token={tok}", timeout=5) as m:
                manifest = json.loads(m.read().decode("utf-8"))
            return set_cookie, manifest

        cookie_hdr, manifest_obj = await asyncio.to_thread(_check_http)
        assert f"agy_pwa_token={tok}" in cookie_hdr
        assert f"token={tok}" in manifest_obj["start_url"]

        # 2. Mid-stream disconnect + Cookie-only reconnect recovers live partial streaming text!
        ws1 = await websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}")
        await asyncio.wait_for(ws1.recv(), timeout=5.0)  # hello
        await ws1.send(json.dumps({"type": "chat", "text": "__SLOW_STREAM__"}))
        # Wait until first delta arrives
        for _ in range(10):
            ev = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5.0))
            if ev["type"] == "stream_delta":
                assert "前半段流式输出" in ev["delta"]
                break
        await ws1.close()

        # Reconnect immediately using Cookie header WITHOUT ?token= query param
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws",
            additional_headers={"Cookie": f"agy_pwa_token={tok}"},
        ) as ws2:
            hello2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5.0))
            assert hello2["type"] == "hello"
            assert hello2["busy"] is True
            assert hello2["inflight"] is not None
            assert "前半段流式输出" in hello2["inflight"]["text"]

            # Receive final stream_end cleanly
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5.0))
                if ev["type"] == "stream_end":
                    assert "后半段圆满结束" in ev["item"]["text"]
                    break

            # 3. Epoch guard: switching to new_conversation mid-turn cancels immediately & never pollutes history
            await ws2.send(json.dumps({"type": "chat", "text": "__SLOW_STREAM__"}))
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5.0))
                if ev["type"] == "stream_delta":
                    break
            await ws2.send(json.dumps({"type": "new_conversation", "workspace": ""}))
            got_clear = False
            for _ in range(8):
                ev = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5.0))
                if ev["type"] == "clear_history":
                    got_clear = True
                if ev["type"] == "toast" and "已开启新对话" in ev["text"]:
                    break
            assert got_clear
            await asyncio.sleep(0.3)
            assert pwa._history == []

            # 4. Multi-step PC IDE coalescing + Live IDE watcher auto-push!
            await ws2.send(json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"}))
            loaded = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5.0))
            assert loaded["type"] == "conversation_loaded"
            # Two consecutive PLANNER_RESPONSE steps coalesced into 1 clean assistant bubble!
            assert len(loaded["history"]) == 2
            assert "先检查配置..." in loaded["history"][1]["text"]
            assert "配置检查完毕！" in loaded["history"][1]["text"]

            # Simulate PC IDE appending a brand-new reply on disk -> auto-pushed via _ide_watch_loop!
            with tfile.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "电脑上新提问"})
                    + "\n"
                    + json.dumps({"step_index": 4, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "电脑端实时新回复！"})
                    + "\n"
                )
            pushed = json.loads(await asyncio.wait_for(ws2.recv(), timeout=3.0))
            assert pushed["type"] == "conversation_loaded"
            assert pushed["silent"] is True
            assert len(pushed["history"]) == 4
            assert pushed["history"][3]["text"] == "电脑端实时新回复！"
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()


def test_setup_wizard_and_auto_onebot_mode(tmp_path: Path) -> None:
    from qq_agy_bridge.setup_wizard import ensure_env_configured, run_doctor

    # Copy minimal template files into isolated tmp_path
    (tmp_path / ".env.example").write_text(
        "WEB_PWA_ENABLED=true\nWEB_PWA_PORT=8765\nWEB_ACCESS_TOKEN=\n"
        "ONEBOT_ENABLED=auto\nONEBOT_ACCESS_TOKEN=\nALLOWED_QQ_USERS=\n",
        encoding="utf-8",
    )
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "user_profile.example.md").write_text("# 模板记忆\n", encoding="utf-8")

    # 1. With empty ALLOWED_QQ_USERS, ONEBOT_ENABLED=auto cleanly selects pwa_only mode!
    env_p = ensure_env_configured(tmp_path)
    cfg_pwa = BridgeConfig.from_env(env_p)
    assert cfg_pwa.web_pwa_enabled is True
    assert cfg_pwa.onebot_enabled is False
    assert len(cfg_pwa.web_access_token) >= 16
    assert (mem_dir / "user_profile.md").is_file()

    doc = run_doctor(tmp_path)
    assert doc["mode"].startswith("pwa_only")
    assert "token=" in doc["lan_pwa_url"]

    # 2. Providing --qq-users automatically activates dual mode!
    ensure_env_configured(tmp_path, qq_users="10001,10002")
    cfg_dual = BridgeConfig.from_env(env_p)
    assert cfg_dual.onebot_enabled is True
    assert cfg_dual.allowed_qq_users == frozenset({"10001", "10002"})
