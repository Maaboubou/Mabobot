# 内置图片与引用视频出处检索

Mabobot App Server 内置 `wx_lens.search(image_refs, purpose)` 和
`wx_lens.read(image_ref, offset)`，不需要启动 sbigg 服务、安装独立插件或配置另一个模型。
这套工具不适用于旧版 Codex CLI exec 运行路径。

## 调用流程

本轮图片先作为正常视觉输入交给 Codex，并登记为 `img1`、`img2` 等图片引用。
引用视频沿用现有精确消息定位、下载、四帧抽取和可选本地 ASR；每帧的时间戳随工具绑定保留。
Lens 使用宿主机已经落地的图片文件，不依赖临时抽帧目录，也不需要模型转换 Windows/WSL 路径。

用户要求查出处或作品信息，或者问题确实需要外部证据时，Codex 决定是否检索。
有明确标题、水印或字幕时优先文字检索；缺少文字线索时调用 Lens。
普通总结、翻译、评论不要求搜索。已知姓名可用于文字检索，公开页面署名只能作为来源信息，
不提供凭照片辨认真人身份的能力。

视频先选 1～2 帧，必要时再补搜。一次调用最多两张，一轮最多四张不同图片。
工具等待固定检索流程完成，并将排队、上传和解析阶段写入现有任务事件。
返回每张图的标题、真实链接、来源及图片地址，每页 20 条、每图最多保留 200 条；
`read` 只读取本轮已保存结果，不重新上传。Codex 按需使用 `wx_browser.open` 核查候选页面。
多帧命中同一转载页面不算多个独立来源，相似匹配不能直接断言原始出处。

实机排查：重启服务后，在“运行与日志”的应用日志中搜索 `Lens`，或打开 Codex 任务详情。
INFO 日志记录 request ID、图片引用、阶段以及每图的 `status`、`results`、`cached` 和耗时，
不记录图片内容和结果网页正文。`status=ok` 才表示拿到了匹配证据；任务里的工具“调用成功”
仅表示工具正常返回，也可能返回 `google_blocked` 等检索状态。
失败时还记录 `failed_stage`、`error_type`；检测到验证页时记录 `block_reason` 和
不含查询参数的 `page_location`。隐藏的 reCAPTCHA iframe 本身不再被判为拦截，
但真实 `/sorry/` 页面或可见验证组件仍会停止检索，不会自动尝试通过验证。

2026-10-03 在同一台 Windows 机器上的对照测试使用了同一张非人物 PNG（按 SHA-256
确认输入一致）：sbigg 返回 60 条真实链接、耗时 11.44 秒；Mabobot 返回 60 条真实链接、
耗时 9.56 秒，其中 28 条链接重合。两次结果均非缓存。网页搜索的排序和结果集合可能变化，
本次验收标准是两边都取得真实匹配，不要求列表完全一致。仅切换 Patchright 时仍返回验证页，
继续对齐社区版浏览器配置后成功；尚未逐个隔离配置字段判断单一原因。

## 图形化管理

打开 **角色管理 → 系统规则 → 图片与视频检索**：

- 图片检索、视频抽帧检索规则独立编辑，复用系统规则的草稿预览、默认值比较、历史恢复。
- 表单管理总开关、单图超时、每轮/每次图片数、全局并发上限和缓存有效期；保存带版本校验，冲突时保留草稿。
- 所有 App Server Profiles 继承同一全局配置；选择聊天可查看在线研究与工作区权限。聊天权限仍可禁止检索。
- 环境状态显示宿主进程的 Patchright 版本、浏览器路径及本次服务启动后的最近结果/错误。此操作不会启动浏览器或联网测试。
- 真实请求快照记录本轮规则版本、参数版本、完整系统指令和工具定义。快照与未保存草稿相互独立。

每轮在启动时读取配置；保存后新请求采用新值，进行中的请求继续使用原值。禁用后新线程不注册 Lens；已有线程在下一次请求时更新工具集合。
首次部署这些代码需要重启服务并刷新管理页面，之后修改界面配置无需重启。
规则只控制检索策略；输入绑定、权限校验、网络范围、SafeSearch 和进程清理由程序固定执行。
图形界面保存值优先于环境默认值。参数“填入默认值”读取当前环境默认值，仍需保存才能生效。

## 部署与配置

默认开启；重启 Mabobot 后，新动态工具签名会参与现有会话配置更新。
宿主 Python 使用 Pillow、psutil 和 `requirements.txt` 中固定版本的 Patchright。
Lens 固定使用与 sbigg 对齐的 Patchright 1.63.0 和浏览器配置，不再静默回退到 Playwright。
旧安装需要在宿主虚拟环境更新项目依赖；缺少驱动会明确返回 `dependency_missing`。
通用 `wx_browser` 仍使用 Playwright，不受 Lens 驱动选择影响。
Windows 优先探测本机 Chrome/Edge，其他平台使用已安装的 Chromium。
也可配置 `CODEX_LENS_BROWSER_PATH`。运行时不会下载浏览器或安装依赖。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `CODEX_LENS_TOOL_ENABLED` | `true` | 管理员总开关 |
| `CODEX_LENS_IMAGE_TIMEOUT` | `100` | 每图墙钟上限，限制在 10～180 秒 |
| `CODEX_LENS_BROWSER_PATH` | 空 | 宿主机浏览器可执行文件路径 |

聊天关闭在线研究时不注册 Lens，并且每次工具执行再次检查权限；只读工作区也拒绝执行。
Lens 只接收本轮图片引用，不接收任意路径或上传网址。使用临时浏览器上下文，没有个人登录态。
图片会上传给 Google：上传前移除元数据，小图放大、大图缩小，并限制尺寸和文件大小。
SafeSearch 固定使用 `active`。Lens 专用网络规则只允许 Google 检索及静态资源，
POST 只开放特定 Google 上传/检索路径；通用网页浏览器的只读规则保持不变。

## 生命周期、缓存与错误

同一宿主进程最多同时执行两个 worker。单张图片使用独立子进程，超时和取消会清理浏览器进程，
回收容量后才删除临时图片。任务结束前可用 `read` 取更多结果，结束后清理临时工作目录。

成功结果按聊天 ID、图片内容哈希和解析版本缓存，当前为进程内缓存，默认 TTL 一小时（界面可调，0 禁用跨请求缓存）、最多 32 项。
不缓存图片字节；不同聊天不共享结果；重启后缓存清空。失败不会进入跨请求缓存。
同一请求重复调用不会重复搜索，以免不断重试 Google 拦截页。

错误会保留 `google_blocked`、`timeout`、`browser_unavailable`、`dependency_missing`、
`parser_changed`、`busy`、`search_failed` 等状态；`no_results` 仅代表本次没取得可用匹配，
不能证明原始出处不存在。Google 页面结构和访问限制仍可能影响成功率。

## 代码与验证

- `app/services/lens/parser.py`：从 sbigg 提取的 `2003` hydration 解析与严格卡片链接兜底。
- `app/services/lens/worker.py`：从 sbigg 上传、等待和 Visual matches 处理流程改写的独立采集器。
- `app/services/lens/browser_profile.py`：sbigg 社区版的浏览器配置；不读取个人会话或 Cookie 文件。
- `app/services/codex_lens_tool.py`：工具协议、图片绑定、缓存、任务取消和 worker 监督。
- `app/services/codex_app_server.py`：注册、权限过滤与当前 turn 的工具分派。
- `app/assistant/handler.py`：引用视频帧元数据和事实性输入绑定。

未迁入 sbigg 的人名/番号启发式打分、站点偏好权重、固定站点核验和额外 LLM 复排。
页面解析仍由固定代码完成，语义判断与回答由当前 Codex 完成。

离线回归：

```sh
python -m pytest tests/services/test_codex_lens_tool.py tests/services/test_codex_browser_tool.py tests/services/test_codex_permission_runtime.py tests/assistant/test_quote_image_binding.py
```
