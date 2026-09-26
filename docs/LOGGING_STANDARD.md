# 日志标准

状态：**2026-09-24 起实施**。实施范围和运行核验见 [日志系统升级方案](LOGGING_UPGRADE_PLAN.md)。本规范适用于 Web、微信 Bot、mabowx、桌面启动器、自动登录助手及插件。聊天档案、模型用量账本、操作审计属于业务数据，不能因日志轮转而删除。

## 1. 职责与文件

| 数据 | 责任 | 目标文件 | 用途 |
| --- | --- | --- | --- |
| 运行日志 | 各进程自己的日志配置 | `logs/app.jsonl`、`logs/wx_bot.jsonl`、`logs/launcher.jsonl`、`logs/wechat_auto_login.jsonl` | 排障、运行中心、最近问题 |
| mabowx 诊断日志 | mabowx logger | `mabowx_logs/mabowx.jsonl`，目录仍可配置 | 微信 UI 自动化诊断 |
| 仪表盘事件 | `dashboard_events` 独立事件写入器 | `logs/dashboard_events.jsonl` | 最近 Judge、搜索等界面事件 |

每个文件同一时刻只能有一个写入进程。`--workers > 1` 时 Web 工作进程使用 `app.<pid>.jsonl`，读取端按时间合并；不得让多个进程共享 `RotatingFileHandler`。桌面启动器把子进程标准输出保存在界面的有限内存缓冲区，子进程的常规运行日志由子进程写盘。子进程输出 `logging.ready` 启动标记；在此之前退出时，启动器保存最近 40 行启动输出。初始化后异常退出则保存最近 20 行输出作为故障证据。

## 2. 记录格式

磁盘文件使用 UTF-8 JSON Lines：每条记录占一行，异常堆栈、消息内换行由 JSON 转义。控制台继续使用便于阅读的文本格式。字段名称、类型和含义保持稳定；新增字段不改变现有字段的含义。

| 字段 | 要求 | 含义 |
| --- | --- | --- |
| `ts` | 必填 | 带时区偏移的 ISO 8601 时间，毫秒精度 |
| `level`、`service`、`logger` | 必填 | 标准级别、进程服务名、Python logger 名 |
| `event`、`message` | 必填 | 稳定事件名、可供人阅读的说明；尚未分类的调用写 `unclassified` |
| `pid`、`thread`、`module`、`function`、`line` | 必填 | 进程、线程和代码位置 |
| `trace_id` | 业务操作时必填 | 一次入口操作在进程间保持不变的关联 ID |
| `delivery_id`、`message_id`、`request_id` | 存在时填写 | 微信投递、应用消息、单次 API 请求各自的原有身份；不可混用 |
| `chat`、`attempt`、`duration_ms`、`outcome` | 有意义时填写 | 目标、重试次数、耗时与结果 |
| `error_code`、`exception` | 故障时填写 | 稳定的失败类别及完整异常堆栈 |

示例（字段顺序不构成接口承诺）：

```json
{"ts":"2026-09-24T01:04:22.087+08:00","level":"ERROR","service":"wx_bot","logger":"wx_bot","event":"wechat.link.resolve.failed","message":"链接卡片打开后未找到浏览器窗口","pid":1234,"thread":5678,"trace_id":"d_123","delivery_id":"d_123","message_id":"link_d_123","request_id":"r_456","chat":"示例会话","attempt":1,"duration_ms":6020,"outcome":"failed","error_code":"browser_window_missing"}
```

`event` 使用小写点分层名称，例如 `wechat.link.resolve.failed`。未改造的第三方库和插件记录由统一格式化器填入 `event="unclassified"`，不能因缺字段丢日志；新增关键链路必须有具体事件名。`error_code` 描述可操作的失败类别，不能用异常文案充当代码。普通消息用参数化 logger 调用；在异常处理处用 `logger.exception` 或 `exc_info=True` 留堆栈，不用 `print` 代替运行日志。插件只取得命名 logger，不自行调用 `basicConfig` 或创建根 logger 的 handler。

## 3. 级别与关联规则

| 级别 | 使用条件 |
| --- | --- |
| DEBUG | 高频轮询、窗口快照、重试细节等诊断信息 |
| INFO | 启停、关键操作开始或结束、状态变化及明确的业务结果 |
| WARNING | 可恢复失败、降级、超时后重试、轮转延迟 |
| ERROR | 本次操作失败，已有稳定错误码和足够定位信息 |
| CRITICAL | 服务无法继续运行或关键数据无法安全写入 |

同一故障在捕获并返回错误的一层记录一次完整异常；上层若增加日志，只补充状态和关联 ID，避免逐层重复堆栈。正常轮询、重复权限查询等不得持续写入 INFO。异步任务和线程池显式传递关联字段；跨 Bot/Web HTTP 请求传递 `trace_id`，保留现有 `delivery_id`、`message_id`、`request_id` 的语义。没有入口关联 ID 的后台任务在开始时生成一个。关联字段在操作结束后清理，避免线程复用时串到下一条消息。

本轮升级**不改变聊天正文的记录策略，也不把正文脱敏作为验收条件**。已有正文记录仍须能被旧日志读取器读取；新增诊断记录不重复复制正文。认证密钥、Cookie 等凭据明文不得写入运行日志。

## 4. 配置、轮转和读取

默认值集中在根目录的 `mabobot_logging.py`，覆盖顺序为明确命令行参数、环境变量、默认值。`.env` 的 `LOG_LEVEL` 控制 Web、Bot 和自动登录助手的根日志级别；mabowx 文件默认 DEBUG，可用 `MABOWX_FILE_LOG_LEVEL` 覆盖。配置校验失败时启动报错。

| 日志流 | 单文件上限 | 备份数 | 最长保留 |
| --- | ---: | ---: | ---: |
| Web、微信 Bot | 20 MiB | 各 5 份 | 30 天 |
| mabowx | 10 MiB | 6 份 | 14 天 |
| 启动器 | 3 MiB | 2 份 | 14 天 |
| 自动登录助手 | 2 MiB | 2 份 | 14 天 |
| 仪表盘事件 | 10 MiB | 4 份 | 30 天 |

大小上限和最长保留同时生效：先按大小分段，再只清理已关闭、已超过份数或期限的分段。不得删除活动文件。Windows 文件占用导致轮转失败时继续写入并稍后重试，同时在控制台或健康状态中报告；文件写入不可用时保留标准错误输出和启动器故障缓冲，不因此覆盖业务原始异常。这些上限不适用于升级前的历史文件；历史证据按迁移方案单独处理。

日志接口和运行中心使用统一读取器：默认反向尾读，文件检索流式扫描轮转范围，不能 `readlines()` 加载整份文件。Web 日志页只读取新 JSONL；升级前的 `.log` 文件不参与界面检索。最近问题从结构化 `level`、`event`、`error_code` 聚合。仪表盘事件仍是独立事件流，轮转后跨分段查找最近事件。页面提供来源、级别、事件、`trace_id`、时间范围筛选及占用/保留状态。

## 5. 验收条件

- 单个入口操作可通过 `trace_id` 查到 mabowx、Bot、Web 中实际参与的记录；原有三个 ID 语义不变。
- 一条 JSONL 记录可以独立解析，异常和正文换行不拆成多条；用户可在运行中心查看和搜索新日志。
- Web “最近问题”、仪表盘最新 Judge/搜索以及启动器实时日志在切换后仍正常。
- 默认大小、份数和期限生效；Windows 轮转被占用、日志目录不可写、进程意外退出、多工作进程分别有可观察的处理结果。
- 日常界面请求不因日志文件增长而线性增长内存占用；聊天档案和其他业务账本不受日志清理影响。
