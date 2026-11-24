# Antigravity Remote Assistant ✦ 专属手机端 Web App + QQ 远程私人助理

把你电脑上的 **Google Antigravity (`agy` CLI)** 变成随身携带的私人远程助理 —— **无需任何 API Key**，直接复用本机已登录的 Antigravity 多模型额度池，在手机上随时随地流式对话、查看并接着聊电脑端 IDE 里的现有任务、实时预览电脑桌面、互传任意大文件。

---

## 🤖 方式一：发给其他电脑上的 AI 智能体一键自动部署（推荐 · 复制即用）

无论另一台电脑上用的是 **Antigravity、Claude Code、Cursor、Codex、Windsurf 还是 Cline**，直接把下面这段话发给它，AI 会自动读取仓库内置的 [`INSTALL_FOR_AI.md`](./INSTALL_FOR_AI.md) 协议完成全套部署、自检并弹窗给你扫码：

### 方案 A：纯手机端专属 Web App 模式（零第三方账号 · 30 秒搞定）
```text
请帮我部署这个开源项目：https://github.com/wwd177/antigravity-remote-assistant.git
1. 先克隆仓库到本地合适目录，并严格阅读根目录下的 INSTALL_FOR_AI.md；
2. 按无交互模式执行自动安装（安装虚拟环境、生成 .env 随机强密钥、下载 cloudflared 外网加密隧道组件、创建桌面快捷方式）；
3. 运行 `python -m qq_agy_bridge.setup_wizard --doctor --json` 确认所有检查项全部通过；
4. 直接帮我启动桌面毛玻璃控制台，并告诉我如何在手机上扫码连接和添加到主屏幕。
```

### 方案 B：手机端专属 Web App + QQ 私聊双通道模式
```text
请帮我部署这个开源项目：https://github.com/wwd177/antigravity-remote-assistant.git
1. 先克隆仓库并阅读根目录下的 INSTALL_FOR_AI.md；
2. 以双通道模式（-Mode dual）完成自动安装，将允许私聊的 QQ 白名单设为：<填你的个人QQ号>；
3. 运行 `--doctor --json` 确认环境自检通过，协助我对好本机 NapCatQQ (OneBot 11 正向 WS 端口 3001) 配置后启动桌面控制台。
```

---

## ⚡ 方式二：手动一行命令安装

> **前置条件**：电脑已安装并登录 **Google Antigravity**（确保终端可调用 `agy`），以及 Python 3.11+（推荐安装 `uv`）。

### Windows（含 Win11 半透明毛玻璃桌面控制台 + 桌面快捷方式）
```powershell
git clone https://github.com/wwd177/antigravity-remote-assistant.git
cd antigravity-remote-assistant
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -LaunchNow
```

### macOS / Linux
```bash
git clone https://github.com/wwd177/antigravity-remote-assistant.git
cd antigravity-remote-assistant
chmod +x ./scripts/setup.sh && ./scripts/setup.sh
./.venv/bin/python -X utf8 -u -m qq_agy_bridge.main
```

---

## ✨ 核心特性

- **📱 100% 属于你自己的手机端毛玻璃 Web App (PWA)**
  - **双通道极速直连**：在家连同一 Wi-Fi 扫「局域网 1ms 极速码」零延迟秒回；出门用流量扫「4G/5G 外网加密隧道码」（自动通过 Cloudflare HTTP/2 TLS 加密穿透，无需公网 IP 或云服务器）。
  - **原生级移动端体验**：支持添加到手机主屏幕免密全屏独立运行、断流切网毫秒级原位续接、`visualViewport` 防虚拟键盘遮挡、锁屏亮屏自动探活重连、完整 Markdown 表格/代码块一键复制。
- **💻 电脑端 IDE 聊天无缝查看与续聊**
  - 在手机左上角点击 **「☰ 会话」**，可直接列出电脑端 Antigravity IDE 与手机端的所有历史聊天框（支持搜索标题/项目目录），实时热同步查看电脑上正在运行的完整对话内容，或随时选定任意项目目录新建对话。
- **🖥 实时电脑屏幕预览 & 双向大文件收发**
  - 手机一点即可查看电脑实时高清桌面（支持连续跟随模式与双指缩放），支持手机相册/文件分片上传到电脑及电脑文件安全下载到手机。
- **🔄 多模型额度耗尽自动无感切换 & 冷却自愈**
  - 默认采用响应速度与代码质量均衡的 `claude-sonnet-5-5-medium`，遇到单模型配额用尽（`429 / RESOURCE_EXHAUSTED`）自动按容灾链（`Sonnet Low -> Gemini 4 Neon -> Carbon -> Flash -> GPT-OSS`）切换并保留上下文，冷却期满自动切回主力模型。
- **🧹 定期安全缓存清理 & 防休眠看门狗**
  - 每 6 小时自动清理过期临时截图与媒体缓存（严格隔离保护登录态、`.env` 与跨会话长期记忆），桌面端内置防系统睡眠与进程崩溃自愈守护。
- **🔒 多重安全边界**
  - 高熵随机密钥 + 常量时间比对（`hmac.compare_digest`）+ IP 暴力破解封禁；内网 SSRF 拦截；敏感文件（`.env`、SSH 私钥、凭证配置）外发硬拦截；QQ 通道强制私聊白名单并彻底忽略群聊。

---

## ⌨️ 常用快捷指令（手机端 App 与 QQ 通用）

| 指令 | 说明 |
| --- | --- |
| `/chats`（或点左上角 `☰ 会话`） | 查看电脑端 IDE 与手机端已有对话列表 |
| `/chat <序号>` | 在 QQ 中切换并查看指定编号的电脑端/远程对话 |
| `/screen` | `<0.5s` 极速截取当前电脑高清桌面 |
| `/model [fast\|balanced\|smart\|gemini]` | 查看或秒切 AI 模型（保留当前对话上下文） |
| `/memory [内容]` | 查看或追加跨会话长期记忆 |
| `/clean [all]` | 清理过期临时缓存（加 `all` 立即全清） |
| `/new` 或 `/reset` | 开启全新对话 |
| `/cd <目录路径>` | 切换当前工作目录 |
| `/status` | 查看当前模型、额度冷却池与会话状态 |

---

## 🛠 环境自检与自动化测试

```powershell
# 运行环境健康自检（支持 --json 供 AI 智能体解析）
.\.venv\Scripts\python.exe -X utf8 -m qq_agy_bridge.setup_wizard --doctor

# 运行全套单元与端到端回归测试
.\.venv\Scripts\pytest.exe -v
```

## 📄 License

[MIT License](./LICENSE)
