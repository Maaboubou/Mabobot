# Mabobot

Mabobot 是运行在 Windows 上的本地微信自动化与 AI 助手。它使用内置 `mabowx` 连接微信，并提供 Web 控制台、AI 助手、插件、模型路由、聊天档案与文件处理能力。

当前版本：`3.4.0`

## 启动

运行前请准备：

- Windows 10/11 与可交互的桌面会话；
- 已安装并登录的微信 4.1 客户端；
- 可访问 Python、Microsoft 与 PyPI 官方下载源的网络；
- 如需 Codex 助手，请在 WSL 中安装 Codex CLI 运行框架；模型既可使用 ChatGPT 账号认证，也可在 Profile 中配置第三方 Responses 兼容接口，并非必须登录官方账号。

使用 Git 获取项目：

```powershell
cd C:\Users\你的用户名
git clone https://github.com/Maaboubou/Mabobot.git
cd Mabobot
.\START.bat
```

也可以从 GitHub 下载 ZIP，完整解压后双击根目录的 **`START.bat`**。

无需提前安装 Python。首次启动发现缺少 64 位 Python 3.11/3.12 或 Microsoft WebView2 Runtime 时，会先列出组件并征得确认，再从官方源下载、验证数字签名并安装到当前用户。随后会自动创建 `.venv`、安装项目依赖与浏览器组件，并生成本机 `.env`。准备完成后会打开 Mabobot 桌面面板，微信 Bot 与 Web 服务会自动启动。

日常使用只需要这一个入口。面板中可以：

- 统一启动、停止或重启微信 Bot 与 Web 服务；
- 打开 Web 控制台并查看实时日志；
- 检查或修复本地运行环境；
- 开启“随 Windows 登录启动”；
- 按需开启“启动前自动确认微信登录”。

首次关闭桌面窗口时，可选择“最小化到系统托盘”或“停止服务并退出”，并记住选择。之后可在“设置 → 窗口行为”中修改关闭方式。

默认地址：

- Web 控制台：<http://127.0.0.1:8888/>
- Web 健康检查：<http://127.0.0.1:8888/health>
- 微信 Bot 健康检查：<http://127.0.0.1:5555/health>

> Web 控制台默认没有独立登录，只应在可信本机或 Tailnet 中使用，不要把端口直接暴露到公网。

## 开始使用

1. 保持微信在线，在 Web 控制台的“聊天”页面同步会话；
2. 开启需要监听的聊天，并配置 AI 助手或插件；
3. 在“Codex”页面确认 CLI 框架可用，并创建或选择模型 Profile；
4. 按需配置回复策略、聊天档案与辅助模型；
5. 回到“运行与日志”确认服务状态正常。

大部分设置都在 Web 控制台中完成。`.env` 用于端口、运行参数和外部服务凭据；未使用的项目可以留空。

公开版包含聊天记录、翻译助手、Magnet Check、菜单翻译、Summary Plus、Weekly、中国银行汇率、电子书下载、图像编辑和游戏发售日历 10 个插件。翻译助手支持每聊天双语／三语、提示词与模板，启用前请先在模型配置中分配翻译模型。

真实密钥、Cookie、数据库、聊天记录和下载文件不要提交到 Git。运行数据主要位于 `data/`、`logs/` 和 `tmp/`。

## 开发与排障

`START.bat` 是唯一的用户启动入口。开发时仍可分别运行两个内部服务：

```powershell
.\.venv\Scripts\python.exe wx_bot.py
.\.venv\Scripts\python.exe start.py --host 127.0.0.1 --port 8888
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

需要远程访问时，请参考 [Tailscale Serve](docs/TAILSCALE_SERVE.md)。

## 架构

```text
START.bat
   ↓ 环境检查与首次安装
mabobot_launcher/                 独立桌面窗口、进程监管、托盘与登录启动
   ├── wx_bot.py + mabowx         微信监听、读取与发送（:5555）
   └── start.py + app/ + web/     应用服务与 Web 控制台（:8888）
```

主要目录：

```text
mabobot_launcher/  桌面启动器及本地界面
app/               应用核心、AI 助手、服务与插件
mabowx/            微信 UI 自动化、选择器与消息模型
web/               Web 控制台
scripts/           安装、运维与文件工具
```

进一步说明：

- [插件开发规范（Manifest / Runtime API v2）](app/plugins/README.md)
- [每聊天插件配置与模板开发指南](docs/PLUGIN_CHAT_CONFIGURATION.md)
- [翻译助手配置说明](docs/TRANSLATION_CHAT_CONFIG_UPGRADE.md)
- [mabowx 与主程序边界](docs/MABOWX_BOUNDARY.md)
- [Web 控制台架构](docs/WEB_CONSOLE_ARCHITECTURE.md)
- [Codex Profile 配置与权限架构](docs/CODEX_PROFILE_ARCHITECTURE.md)
- [聊天档案与线程上下文](docs/CHAT_ARCHIVE_OPERATIONS.md)
- [模型用量与计价](docs/LLM_USAGE.md)
- [安全策略](SECURITY.md)

项目主体使用 [MIT License](LICENSE)，内置第三方代码许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
