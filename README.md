# ✦ 反重力助手 (Antigravity Assistant)

> **让手机化身你的本地 AI 编程中枢** —— 零第三方聊天软件依赖、免 API Key，完整同步电脑端 **Google Antigravity IDE** 与 **OpenAI Codex Desktop** 全部聊天记录，支持 **1ms 局域网直连 + 全球 4G/5G 加密隧道**、实时桌面画面、工作区文件浏览与 **Git Diff 代码审阅**。

---

## 核心亮点

- **💻 电脑真实聊天窗口直连驱动 + 手机一键新建电脑会话**
  - 手机端选中任意 **Antigravity 电脑端** 对话发送消息时，通过 Windows UI Automation **直接驱动电脑上打开的 Antigravity 窗口现场执行**（自动切换对应会话、规避中文输入法干扰、实时回传工具状态、流式回复与 AI 生成截图），并支持在手机上一键**新建 Antigravity / Codex 电脑端对话**。
  - 自动发现并合并本机 **Antigravity IDE**（SQLite 索引 + 未入库 `brain/` 会话）与 **OpenAI Codex Desktop**（`state_*.sqlite` + `session_index.jsonl` + `codex exec` 原生续写）的全部对话。
- **📲 iPhone 永久独立全屏 App（零域名成本 · 电脑重启免重扫 · 永不掉签）**
  - 内置 **零信任加密云端信标（`PBKDF2-HMAC-SHA256` 10 万次迭代 + 流加密与 `HMAC` 认证）**：电脑每次自启或外网隧道重连后，自动将加密后的最新地址同步至 `gh-pages` 静态外壳（公开仓库不暴露任何明文 IP 或隧道网址）。
  - iPhone 扫码一次「添加到主屏幕」即生成 OLED 曜石黑全屏独立 App，无需开发者证书签名，重启电脑或切换 Wi-Fi / 5G 均自动寻址秒连。
- **📂 工作区源码浏览与 Git Diff 审阅（灵感源自 GitHub 高星远程开发套件）**
  - 手机端内置工作区抽屉：一键查看当前 Git 分支、未提交文件状态（`M` / `A` / `D`）与彩色高亮 Unified Diff，支持一键点选让助手直接审阅改动或下载项目文件到手机。
- **🖥 实时桌面画面 + 双向大文件互传 + 语音输入**
  - 毫秒级高清桌面截屏与动态画面跟随、100MB 分片上传下载、代码块与整条回复一键复制（兼容局域网 HTTP 与外网 HTTPS）、原生语音听写输入。
- **🪟 Windows 11 凝霜毛玻璃桌面中枢**
  - 打开电脑端「反重力助手」即在主面板内嵌展示双通道免密二维码（无需二次弹窗），支持 4 档窗口通透度调节、防休眠保活（允许息屏省电）、开机静默自启与局域网防火墙一键放行。

---

## 一键自动部署（支持 AI 智能体全自动安装）

你可以把下面这段指令直接发给任何电脑上的 **Antigravity / Claude Code / Codex / Cursor**，智能体会自动完成拉取、虚拟环境安装、外网隧道组件下载、桌面快捷方式创建与健康自检：

```text
请帮我部署「反重力助手」：克隆 https://github.com/wwd177/antigravity-remote-assistant ，阅读仓库根目录的 INSTALL_FOR_AI.md 并按步骤完成依赖安装、生成随机安全密钥、配置外网加密隧道、创建桌面快捷方式并运行 --doctor 自检。
```

### 手动快速启动（Windows PowerShell）

```powershell
git clone https://github.com/wwd177/antigravity-remote-assistant.git
cd antigravity-remote-assistant
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

启动桌面毛玻璃控制台：
```powershell
.\.venv\Scripts\pythonw.exe -X utf8 -m qq_agy_bridge.launcher
```

---

## 安全架构说明

- **高熵加密令牌 + Cookie 免密持久化**：首次运行自动生成高强度 `WEB_ACCESS_TOKEN` 写入本地 `.env`（已被 `.gitignore` 严格排除），扫码一次即可添加到 iOS / Android 主屏幕长期免密直连。
- **防暴力枚举与敏感文件沙箱**：内置单 IP 鉴权失败速率限制；工作区文件浏览与文件下载严格限制在工作目录内，自动拦截 `.env`、SSH 私钥与凭据文件。

---

## 开源协议

[MIT License](LICENSE)
