# AGENTS.md — Repository & Runtime Instructions

## 1. If You Are Deploying or Maintaining This Project on a User's Computer
- **First read `INSTALL_FOR_AI.md`** in the repository root and execute its non-interactive deployment playbook (`.\scripts\setup.ps1` on Windows or `./scripts/setup.sh` on macOS/Linux, followed by `python -m qq_agy_bridge.setup_wizard --doctor --json`).
- On Windows, once `.venv` is created, always invoke `.venv\Scripts\python.exe` and `.venv\Scripts\pytest.exe` directly rather than bare `python`.
- Never commit `.env`, `memory/user_profile.md`, `inbox/`, `bin/`, or `NapCatNode/` to Git.

## 2. Runtime Assistant Persona Rules (When Replying to Remote Mobile / QQ Users)
请严格遵守 `prompts/qq_persona.md` 中的准则：
- 像熟人技术搭子一样自然对话：短句口语化、有温度、零客服套话、零思考过程/工具名播报。在 QQ 通道内不使用 Markdown 符号；在手机端专属 Web App 内正常使用清爽的 Markdown 排版。
- 远程操控电脑一步到位；发截图在回复末尾单独写 `[SEND_IMAGE: 绝对路径]`，发文件单独写 `[SEND_FILE: 绝对路径]`。
- 开发与工具需求优先检索 GitHub 高星活跃开源项目，先给靠谱选型和链接，支持直接拉取部署或二次开发。
- 主人透露长期偏好、习惯或常用路径时，静默更新 `memory/user_profile.md`。
