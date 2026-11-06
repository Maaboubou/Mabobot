# 终端

入口：管理页面侧栏 **终端**（位于“用量统计”下方，路径 `/terminal`）。选择一个已就绪的 Codex Profile，点击新建会话，即在项目目录中启动该 Profile 的真实 Codex CLI。

## 会话与权限

终端由 [ttyd](https://github.com/tsl0922/ttyd) 提供网页界面，使用 tmux 保持本机进程。Profile 的登录状态、模型和配置沿用现有独立启动脚本，不改动全局默认 Profile，也不经过聊天助手的工具转发。

“完整访问”开启时，Codex 可直接执行命令、读写本机文件及操作 Git，实际系统权限与启动 Mabobot／WSL 的账户一致；关闭时沿用 Profile 自身的 Codex 权限设置。每个新终端独立创建会话，最多同时保留 4 个。

页面切换、刷新或关闭浏览器只断开终端显示，会话继续运行。重新打开终端页可选择已有会话继续操作。“结束会话”会停止对应 Codex、tmux 会话和 ttyd 进程。Mabobot 正常退出时会清理其管理的终端会话。终端中的命令、批准提示、快捷键及 `/` 命令由 Codex CLI 自身处理。

## 运行环境

主路径使用 Linux／WSL；Windows 部署复用项目现有的 WSL Codex 环境。当前不直接启动 Windows 原生 PowerShell／ConPTY 终端。

目标 Linux／WSL 环境需要 **ttyd 1.7.0 或更新版本**和 `tmux`。页面显示依赖状态，缺失或版本过旧时不会创建模拟终端。Debian／Ubuntu 可在相同环境中安装：

```bash
sudo apt install ttyd tmux
```

如果系统软件源提供的 ttyd 版本过旧，可从 [ttyd 官方 Releases](https://github.com/tsl0922/ttyd/releases) 获取对应架构的版本。将经过验证的可执行文件放在项目的 `data/terminal-tools/ttyd` 并赋予执行权限；该文件属于本机运行依赖，不提交到仓库。Profile 启动脚本需在该环境中可执行；项目目录会转换为对应的 WSL 路径。

## 访问边界

终端支持本机页面和项目现有的 **Tailscale Serve** 远程入口。已完成 [Tailscale Serve 配置](TAILSCALE_SERVE.md) 后，可以从 Tailnet 内其他设备打开 `https://<机器名>.<tailnet>.ts.net/terminal` 或 `http://<机器名>:8888/terminal`，直接创建和操作终端会话。访问范围继续沿用现有 Tailnet 策略，不额外增加用户或设备黑白名单；带标签设备也可使用。

后端仍只监听本机回环地址。终端从本机 Tailscale 状态读取当前节点地址和 Serve 配置，只认可实际代理到当前 Mabobot Web 端口的入口，并校验 Serve 转发信息。新建、结束会话和 WebSocket 连接按外部入口检查同源 Origin；跨站请求、伪造的其他节点域名和 Funnel 公网请求不会获得终端访问权限。修改 Serve 配置后，等待短暂缓存刷新再重试。

项目启动器会禁用 Uvicorn 的通用代理头重写，由终端入口单独核对 Tailscale Serve 请求。如果自行启动 Uvicorn，请使用 `--no-proxy-headers`，例如 `uvicorn app.main:app --host 127.0.0.1 --port 8888 --no-proxy-headers`。无需修改 `WEB_HOST`、开放普通局域网端口或启用 Funnel；ttyd 与 Mabobot 之间的内部代理仍由项目管理。

每个 ttyd 实例只监听回环地址，并使用独立的内部连接凭据。浏览器通过 Mabobot 的同源代理访问终端，不接收 Profile 密钥或 ttyd 的内部凭据。代理只连接服务端登记的会话端口，不接受浏览器指定命令、工作目录、上游地址或 ttyd URL 参数。终端内容不写入额外的应用调用日志。

## 接口

- `GET /api/terminal`：运行环境、依赖状态和会话列表。
- `GET /api/terminal/sessions`：当前会话列表。
- `POST /api/terminal/sessions`：使用 `profile_id` 和 `full_access` 创建会话。
- `DELETE /api/terminal/sessions/{id}`：结束指定会话。
- `GET /api/terminal/sessions/{id}/view/`：同源网页终端。
- `WS /api/terminal/sessions/{id}/view/ws`：终端输入、输出和窗口尺寸传输。

ttyd 的内部 token 接口和首次 WebSocket 鉴权由代理处理，不把内部连接口令传到网页。
