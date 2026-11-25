# INSTALL_FOR_AI.md — AI 智能体全自动部署指南（反重力助手）

如果你是运行在用户电脑上的 AI 编程智能体（Antigravity、Codex、Claude Code、Cursor、Windsurf 等），请按以下确定性步骤完成 **反重力助手 (Antigravity Assistant)** 的无人值守部署与验证。

---

## Step 1：前置检查

确认本机已安装并登录 Google Antigravity（桌面客户端或 `agy` CLI）：
```bash
agy --version
```
（Windows 常见路径：`%LOCALAPPDATA%\agy\bin\agy.exe` 或 `%LOCALAPPDATA%\Programs\Antigravity\bin\agy.exe`，程序会自动探测。）

## Step 2：创建隔离虚拟环境并安装依赖

**Windows (PowerShell):**
```powershell
uv venv --python 3.12 .venv
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
```
*(若无 `uv`，可使用 `python -m venv .venv` 搭配 `.\.venv\Scripts\pip install -e ".[dev]"`)*

**macOS / Linux:**
```bash
uv venv --python 3.12 .venv
uv pip install --python ./.venv/bin/python -e ".[dev]"
```

## Step 3：全自动初始化 `.env`、外网隧道与桌面快捷方式

**Windows:**
```powershell
.\.venv\Scripts\python.exe -X utf8 -m qq_agy_bridge.setup_wizard --non-interactive --with-tunnel --create-shortcut --doctor --json
```

**macOS / Linux:**
```bash
./.venv/bin/python -X utf8 -m qq_agy_bridge.setup_wizard --non-interactive --with-tunnel --doctor --json
```

确认返回的 JSON 中 `"ok": true`。

## Step 4：运行自动化单测验证

```powershell
.\.venv\Scripts\pytest.exe -q
```

## Step 5：拉起「反重力助手」桌面控制台

- **Windows 图形控制台（推荐）**：
  ```powershell
  Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "-X utf8 -m qq_agy_bridge.launcher" -WorkingDirectory (Get-Location)
  ```
- **命令行常驻模式（跨平台）**：
  ```bash
  ./.venv/bin/python -X utf8 -u -m qq_agy_bridge.main
  ```

部署完成后，提醒用户直接在桌面「反重力助手」窗口右侧用手机扫码即可秒进专属移动端 App。
