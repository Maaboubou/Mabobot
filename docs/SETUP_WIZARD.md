# 配置向导与首次安装

完整解压项目后，双击 `START.bat`。基础安装会准备 Python 3.11/3.12（64 位）、WebView2、项目虚拟环境及 Python 依赖，生成 `.env` 和运行目录。缺少系统组件时会先提示确认。请确保能访问 Python、Microsoft、PyPI、GitHub 及所选模型服务的下载源。

基础环境完成后，启动器按原有流程启动 Web 和微信 Bot，并打开四步配置向导。向导不会自动登录微信；使用微信功能前，请自行打开并登录微信。

## 跳过、继续与重新打开

- “稍后配置”或 Esc 会保存当前步骤并关闭向导。以后启动不再自动弹出。
- 在“设置 → 配置向导”可以继续；完成后的向导重新打开时从第一步开始，不会清除已有账号、API 配置或卸载组件。
- 上一步、下一步与顶部步骤导航只保存进度，不执行安装。
- 安装仅在点击对应安装按钮后执行。关闭向导不取消正在进行的安装；Linux 安装可以在其终端按 Ctrl+C 中止。
- 重启电脑后再次双击 `START.bat`，可回到尚未完成的步骤。安装中断后先重新检测，再重试缺失项目。

## 连接 AI

Windows 默认使用 WSL 中的 Codex。向导依次检查 WSL、默认发行版的用户与内核、Codex CLI，以及默认 Profile 的凭据配置。

1. 没有 WSL 或发行版时，点击“安装 WSL 与 Ubuntu”。Windows 可能请求管理员权限。安装要求重启时，请自行保存工作并重启；向导不会自动重启电脑。
2. 点击“打开 Linux 完成初始化”，按终端提示创建 Linux 用户名和密码。密码输入时不显示字符；输入后按回车。完成后输入 `exit` 回到向导并重新检测。
3. 点击“准备 Codex”，在同一个默认 WSL 用户下准备 Linux 依赖并运行官方 Codex 安装器。Ubuntu / Debian 可自动准备；其他发行版可参考安装帮助自行准备。已有可用 CLI 不会被向导替换。
4. 前往 Codex 配置页，创建 Profile，选择 ChatGPT 登录或第三方 Responses API，完成配置并设为默认。返回向导重新检测。

若显式配置为本机 Codex，向导会沿用该选择并提供官方安装帮助，不会擅自切换到 WSL。模型检查仅确认默认 Profile 和凭据已配置，不会自动调用付费模型或向微信发送消息。实际连通性仍需在模型配置页测试或首次使用时验证。

安装依据：[OpenAI WSL 指南](https://learn.chatgpt.com/docs/windows/wsl)、[Microsoft WSL 安装指南](https://learn.microsoft.com/zh-cn/windows/wsl/install)。

## 功能组件

| 组件 | 用途 | 安装位置 |
| --- | --- | --- |
| 浏览器 | 网页阅读、摘要、脑图渲染 | 当前 Windows 用户的 Playwright Chromium 缓存 |
| 媒体 | 视频与音频处理 | 当前项目虚拟环境内的 FFmpeg / FFprobe |
| 文件处理 | 常用 Office、PDF、OCR | 默认 WSL 用户环境；系统依赖需 sudo |

共享浏览器优先使用已有 Chrome；默认 Chrome 不存在时会查找用户安装和已下载的 Chromium。手动指定的自定义浏览器路径会被保留，路径无效时应在链接摘要设置中更正。

文件工具安装可能较大，Ubuntu / Debian 上会安装常用系统工具、`requirements-file-tools.txt` 中的 Python 包和项目工具启动脚本。特定 Skill、外部服务及插件的额外依赖仍以各自配置为准。

## 故障恢复

- 基础安装未完成：再次运行 `START.bat`。`.env`、运行目录或依赖缺失时会重新进入基础准备。
- 浏览器、媒体或文件工具失败：在向导中查看提示并重试该组件。隐藏下载进程的日志位于 `logs/launcher_setup.log`；交互式安装的错误保留在独立终端中。
- WSL 不可用：新安装后先重启 Windows，再初始化 Linux 用户。已有发行版异常、虚拟化未开启或 WSL 1 的情况，请按向导中的官方帮助处理。
- 网络失败：检查系统网络或代理是否能访问相应下载源；Windows 和 WSL 的网络设置可能不同。调整后重新检测、重试。

向导进度保存在 `data/launcher_setup.json`，不包含账号密钥。安装与检测在后台运行，同一时间只执行一个向导安装操作。

开发者直接运行 `scripts/install.ps1` 时，默认只准备基础环境；显式传入 `-IncludeOptionalComponents` 可保留集中安装 Chromium 和 FFmpeg 的流程。普通用户建议使用向导分别安装。
