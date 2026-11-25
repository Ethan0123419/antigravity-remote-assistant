# ✦ 反重力助手 (Antigravity Assistant)

> **让手机化身你的本地 AI 编程中枢** —— 零第三方聊天软件依赖、免 API Key，完整同步电脑端 **Google Antigravity IDE** 与 **OpenAI Codex Desktop** 全部聊天记录，支持 **1ms 局域网直连 + 全球 4G/5G 加密隧道**、实时桌面画面、工作区文件浏览与 **Git Diff 代码审阅**。

---

## 核心亮点

- **💻 电脑全会话零遗漏同步与无缝续聊**
  - 自动发现并合并本机 **Antigravity IDE**（SQLite 索引 + 未入库 `brain/` 会话）与 **OpenAI Codex Desktop**（`state_*.sqlite` + `session_index.jsonl`）的全部对话。
  - 智能剔除跨存储残影快照、内部子智能体（Subagents）与预热探针，支持分类计数筛选（`全部` / `Antigravity` / `Codex` / `远程`）、全文实时检索、完整早期消息分页加载与电脑端对话实时自动同步。
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
