# ✦ 反重力助手 (Antigravity Assistant)

> **让手机化身你的本地 AI 编程与远程电脑操控中枢** —— 零第三方聊天软件依赖、免 API Key，完整同步并直驱电脑端 **Google Antigravity IDE** 与 **OpenAI Codex Desktop** 全部会话，支持 **1ms 局域网直连 + 全球 4G/5G 加密隧道**、**远程触控桌面**、**电脑端 IDE 确认按钮一键放行**、**双向剪贴板与开发端口雷达**以及 **Hunk 级 Git Diff 精准引用追问**。

---

## 核心亮点

- **💻 电脑真实聊天窗口直连驱动 + 一次性令牌安全审批放行**
  - 手机端选中任意 **Antigravity 电脑端** 对话发送消息时，通过 Windows UI Automation **直接驱动电脑上打开的 Antigravity 窗口现场执行**（自动切换对应会话、规避中文输入法干扰、焦点与剪贴板序列号校验、实时回传工具状态、流式回复与 AI 截图），并支持在手机上一键**新建 Antigravity / Codex 电脑端对话**。
  - **电脑端确认弹窗一键放行**：当电脑端停在 `Proceed` / `Accept` / `Allow Once` / `Run Command` 等单次确认按钮时，手机端自动浮现绿色放行胶囊；采用绑定窗口句柄、会话 ID 与 UIA `RuntimeId` 的**一次性短效令牌**及无焦点 `InvokePattern` 安全触发，请求变更立即自动失效。
  - 自动发现并合并本机 **Antigravity IDE**（SQLite 索引 + 未入库 `brain/` 会话）与 **OpenAI Codex Desktop**（`state_*.sqlite` + `session_index.jsonl` + `codex exec` 原生续写）的全部对话。
- **📲 iPhone 永久独立全屏 App（零域名成本 · 电脑重启免重扫 · 永不掉签）**
  - 内置 **零信任加密云端信标（`PBKDF2-HMAC-SHA256` 10 万次迭代 + 流加密与 `HMAC` 认证）**：电脑每次自启或外网隧道重连后，自动将加密后的最新地址同步至 `gh-pages` 静态外壳（公开仓库不暴露任何明文 IP 或隧道网址）。
  - iPhone 扫码一次「添加到主屏幕」即生成 OLED 曜石黑全屏独立 App，无需开发者证书签名，重启电脑或切换 Wi-Fi / 5G 均自动寻址秒连。
- **👆 交互式触控远程桌面 + 电脑 ↔ 手机双向剪贴板 & 端口雷达**
  - **触控点按模式**：实时远程桌面支持在「缩放查看」与「触控点按」间一键切换（自动扣除黑边进行 DPI 级精确坐标映射 + 脉冲点击动效 + 自动回传新帧），内置 `Enter` / `Esc` / `Space` / 上下滚轮快捷按键栏。
  - **电脑状态与双向剪贴板**：内置「电脑与剪贴板」中枢面板，实时查看 CPU / 内存 / 电量或磁盘空间、本机活跃开发监听端口（如 `:3000` / `:5173` / `:8765` 及所属进程），一键读取电脑剪贴板到手机或推送文本写入电脑剪贴板。
- **📂 工作区源码浏览与 Hunk 级 Git Diff 精准引用**
  - 手机端内置工作区抽屉：一键查看当前 Git 分支、未提交文件状态（`M` / `A` / `D`）与按 `@@ ... @@` 自动切分的彩色高亮 Diff 卡片，支持**按单个代码块（Hunk）一键引用到当前对话追问**或下载项目文件到手机。
- **✨ 丝滑单手移动交互体验**
  - **锁屏唤醒无缝排队发送 + 幂等防重投回执**：弱网或切回手机重连瞬间点发送自动入队，握手恢复后幂等投递并实时推进状态回执（`✓ 网关已接收` → `✓ 已投递至电脑窗口`）。
  - **断点续读 + 长代码智能折叠**：按会话独立保存草稿与阅读锚点，向上翻阅时流式输出不乱跳并提示「↓ 有新内容」；超 24 行长代码块自动折叠防刷屏；支持顶栏后台运行角标、会话 ★ 置顶、单手快捷指令栏与 `/` 斜杠命令菜单。
- **🪟 Windows 11 凝霜毛玻璃桌面中枢**
  - 打开电脑端「反重力助手」即在主面板内嵌展示双通道免密二维码，具备网关专属身份探针（防止端口冲突误映射其他本地程序到公网）、隧道断线自动自愈重启、窗口通透度调节与开机静默自启。

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

- **高熵加密令牌 + CSWSH 跨站劫持防护**：首次运行自动生成高强度 `WEB_ACCESS_TOKEN` 写入本地 `.env`（已被 `.gitignore` 严格排除）；Cookie 握手强制同源 `Origin` 校验，页面启用 `no-referrer` 策略防止令牌外泄。
- **防伪造速率限制与端口归属校验**：仅信任来自本机回环（`cloudflared`）的代理头，杜绝局域网伪造 IP 绕过鉴权锁定；启动外网隧道前强制校验 `/ping` 服务身份标识。
- **敏感文件沙箱与一次性动作绑定**：工作区文件浏览与下载严格限制在工作目录内，自动拦截 `.env`、SSH 私钥与凭据文件；桌面确认按钮采用绑定 `RuntimeId` 的一次性令牌，远程按键严格执行白名单校验。

---

## 开源协议

[MIT License](LICENSE)
