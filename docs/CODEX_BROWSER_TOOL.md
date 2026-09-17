# Codex 浏览器工具

Mabobot 通过 Codex App Server 的动态工具接口，为每一轮聊天提供临时的
Playwright Chromium。它用于普通搜索看不到的 JavaScript 页面、动态列表，以及把
当前聊天生成的 HTML 渲染为 PDF 或 PNG；它不是带登录状态的个人浏览器。

## 能力

- `wx_browser.open`：打开公共 HTTP(S) 页面，执行页面 JavaScript，保存渲染后的
  HTML、可见文本和受限数量的 JSON 响应，供当前 Codex 轮次继续分析。
- `wx_browser.fetch_json_pages`：发现分页 API 后，在同一个临时 Chromium 会话中批量
  获取最多 25 个同源 JSON 页面，减少分页期间的数据漂移、重复启动和上下文消耗。
- `wx_browser.download`：下载公开 HTTP(S) 文件，通过独立内容审核后保存到本次
  `outputs/`，支持审核清单中的短视频、音频、PDF、图片、文本和 Office 文档。
  返回实际文件路径、字节数和 SHA-256；重定向仍经过公网和来源许可校验。
  下载文件优先使用此工具，避免把 MP4 当网页打开或反复尝试网页代理。
  B 站 `bilivideo.com` CDN 使用宿主固定的站点 Referer；不接受模型提供的任意请求头。
  DASH 分轨视频用 `url` 传视频轨、`audio_url` 传音频轨，`filename` 必须是 `.mp4`。
  两轨在宿主临时目录下载并合并，审核包含音轨的最终 MP4，审核前不向聊天开放分轨。
  不提供完整的 B 站/YouTube 链接解析器；签名过期、需要登录或源站拒绝时仍会失败。
- `wx_browser.render_html`：只读取当前聊天工作区或本次请求目录内的 HTML，在本次
  输出目录生成 PDF/PNG。大型 Microsoft 商城封面目录会自动请求适合目录的缩略图。
- 浏览器临时抓取文件位于本次请求的 `.browser/`，轮次结束后删除；只有 `outputs/`
  中的文件会作为聊天附件收集。

## 权限边界

浏览器由主应用执行，但每次调用都绑定到可信的 `thread_id + turn_id + call_id` 和
当前聊天目录。未绑定、重复、过期或跨轮次调用都会失败关闭。

- 仅允许 `GET`、`HEAD`、`OPTIONS`，仅允许 80/443 端口。
- 拒绝 localhost、局域网、链路本地、保留地址、CGNAT/Tailscale 地址和混合公私
  DNS 结果。
- Chromium 的 HTTP(S) 流量必须经过一次性本机代理；代理把连接固定到已验证的
  公网数字 IP，避免 DNS 重绑定绕过。下载工具使用同一代理及重定向检查。
- 隔离聊天的本地命令仍禁用网络。公开文件通过宿主下载工具获取，避免任意命令
  绕过下载来源和内容审核；聊天文件范围仍由原有隔离策略控制。
- 使用全新的无痕上下文，不读取 Chrome Profile、Cookie、密码、扩展或本机浏览记录；
  网页浏览上下文禁用自动下载和 Service Worker，并阻断 WebSocket；文件下载由
  `wx_browser.download` 单独执行，不读取登录凭据，也不接收任意请求头或上传正文。
- 本地 `file://` 子资源只能位于当前聊天工作区或本次请求目录，符号链接和联接点不能
  用来逃逸目录边界。
- 群聊仍共享该群自己的隔离空间，不会因此得到管理员工作区或 Tailnet 管理权限。

这套工具适合公开网页研究，不适合需要登录、提交表单、访问内网后台或进行交易的任务。

## 安装与运维

Python 依赖已在 `requirements.txt` 中声明。首次安装或 Chromium 损坏时，可在
“系统 → 系统工具 → Playwright Chromium”执行修复；命令行等价操作是：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

主要环境变量：

```dotenv
CODEX_BROWSER_TOOL_ENABLED=true
CODEX_BROWSER_MAX_CONCURRENCY=1
CODEX_BROWSER_NAVIGATION_TIMEOUT_MS=90000
CODEX_BROWSER_RENDER_TIMEOUT_MS=120000
CODEX_BROWSER_MAX_REQUESTS=3000
CODEX_BROWSER_DOWNLOAD_MAX_BYTES=268435456
CODEX_BROWSER_DOWNLOAD_TIMEOUT_SECONDS=120
```

修改开关或工具定义后需重启 Mabobot。工具签名变化会轮换旧 Codex 线程，避免旧线程
在没有新权限声明的情况下继续复用。

下载默认最多 256 MiB，失败、空文件、超限或不完整的响应不会留下可交付文件。
分轨下载的总传输量及合并后的文件均受该大小上限约束。
已有同名输出不会被覆盖。需要登录的文件、站点拒绝访问和源站故障仍可能失败，
应根据错误停止或改用有效的公开链接。内容审核拒绝后不得换链接或工具尝试绕过。
隔离群聊不能用 `curl`、`wget`、Python HTTP、替换 DNS/IP 或 CNAME 主机名进行兜底；
工具明确说明这条边界，避免重复禁网尝试和证书主机名错误。

## 微信附件内容审核

管理员规则位于 `config/wechat_content_policy.json`，不向隔离聊天开放写权限。
默认拦截色情和中国政治敏感内容；具体定义及附加主题由管理员调整，这不是微信
官方敏感词清单。`allowed_download_domains` 非空时只允许清单中的精确域名，
重定向同样受限；文件类型、大小、文字长度、图片数量和视频时长也可调整。

审核使用独立的 `assistant.content_review` 模型路由，在模型路由页面显示为
“微信附件内容审核”。图片、PDF 和视频需要支持图片输入的模型，不能降级成只看
文字的审核。模型返回拒绝、不确定、低置信度、无效 JSON 或发生错误均暂停交付。

实际内容读取包括文本全文、图片、Office 文本与内嵌图片、PDF 每页文本与画面。
媒体默认最多 120 秒，视频抽取 12 帧，音轨必须完成本地 ASR 转写；沿用
`summary_plus` 的 FFmpeg 与 SenseVoice 配置，缺少资源时暂停交付。PDF 使用
`requirements.txt` 中的 PyMuPDF。压缩包、可执行文件、宏、加密或不能读取的
嵌入内容不在默认许可范围。

未审核下载仅存放于宿主临时目录，拒绝后删除。Codex 助手最终附件再次审核；
发送时复制到宿主临时目录并审核该副本，所有附件通过后同步发送并清理副本。
审核缓存按文件内容、类型、名称、来源与管理员规则计算，修改这些信息会触发新审核。

模型分类和视频抽帧不能保证零误判或零漏判，短暂画面尤其可能漏检。要求人工
逐帧确认的素材应禁用自动视频附件。当前入口覆盖 Codex 助手附件，不覆盖普通
文字回复、入站微信文件或其他插件的发送。
