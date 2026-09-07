# Mabobot 插件规范（Manifest v2）

本目录中的每个可加载插件必须同时提供 `config.json`、`manifest.json` 和 `main.py`。Manifest v2 是插件的运行契约：插件管理器会在加载时校验声明与实际订阅是否完全一致；声明缺失、字段无效或代码额外注册监听器都会导致插件拒绝加载。

## 1. 标准目录

```text
app/plugins/my_plugin/
├── config.json       # 元数据、配置字段与全局值
├── manifest.json     # 事件、触发、范围、传播和任务声明（必需）
├── main.py           # register / unregister 与处理逻辑
├── README.md         # 面向维护者的插件说明（推荐）
└── requirements.txt  # 插件额外依赖（可选）
```

`priority`、`routing_overrides` 和 `block_after_handling` 已废弃，不得出现在插件配置中。消息顺序只有一个事实来源：`app/plugins/routing_order.json`。用户在“插件 → 执行顺序”拖动并应用后，系统直接更新该中央顺序表；新安装的监听器第一次加载时追加到对应事件末尾。

当前触发设置和执行顺序均为全局级，不支持聊天级覆盖。聊天页通过统一插件分配管理普通插件权限、推送权限，以及群聊是否要求 @；推送能力的声明与使用规则见第 2.2 节。

## 2. 最小 config.json

```json
{
  "name": "my_plugin",
  "display_name": "示例插件",
  "version": "2.0.0",
  "description": "收到指定关键词后回复",
  "author": "Team",
  "category": "utility",
  "config_schema": {
    "trigger_keywords": {
      "type": "array",
      "title": "触发关键词",
      "description": "消息包含任一关键词时触发",
      "group": "trigger",
      "default": ["示例"]
    }
  },
  "config": {
    "trigger_keywords": ["示例"]
  }
}
```

凡是允许用户从页面修改的触发条件，必须先在 `config_schema` 声明，并使用 `group: "trigger"`。处理代码必须通过 `get_config()` 读取同一个字段；页面展示值、保存值和运行判断因此来自同一份配置。

固定在代码里的正则、消息结构或业务前置条件不应伪装成可编辑字段，应写入 Manifest 的 `conditions`，在执行顺序页只读展示。

### 2.1 声明模型调用任务

插件只要通过统一 LLM 路由调用模型，就应在 `config.json` 的 `ui.llm_tasks` 中声明每个调用类型的用户可读信息。任务路由页面以这里为唯一说明来源，不维护插件内部名的前端翻译表：

```json
{
  "ui": {
    "icon": "bi-stars",
    "llm_tasks": {
      "summary": {
        "label": "内容摘要",
        "description": "将链接、文章或长文整理为结构化摘要",
        "category": "摘要",
        "order": 10
      }
    }
  }
}
```

`llm_tasks` 的键必须与调用统一 LLM 服务时使用的 `call_type` 完全一致。每个任务支持以下展示字段：

| 字段 | 必需 | 约束与用途 |
|---|---|---|
| `label` | 是 | 面向使用者的任务名称，建议不超过 20 个汉字 |
| `description` | 推荐 | 一句话说明模型在这个任务中产出什么，不写实现细节 |
| `category` | 推荐 | 同一插件内的短分类，如“对话”“记忆”“图片理解” |
| `order` | 推荐 | 整数，数值越小越靠前；建议按 10 递增，方便以后插入 |

能力接口只会下发上述字段，并会限制文本长度和排序范围。未声明 `llm_tasks` 的旧插件仍可加载；如果运行映射中存在未声明的调用类型，页面会以“自定义任务”兜底并保留内部标识，便于逐步迁移。新增或修改模型调用时，应同时完成以下工作：

- 在插件 `ui.llm_tasks` 中新增或更新任务说明。
- 在统一模型映射中为同一个 `call_type` 配置主模型及可选备用模型。
- 在任务路由页确认展示名称、说明和内部标识正确；不要把密钥、提示词或运行参数写进任务元数据。

### 2.2 声明推送能力与使用统一权限

会主动向群聊或私聊发送定时内容、订阅更新的插件，必须在 `config.json` **顶层**声明 `features: ["push"]`；已有其他能力时将 `"push"` 加入同一个数组。仅响应用户消息、向触发聊天返回结果，不因此要求声明推送能力。

以下为推送相关配置片段，应合并到插件的完整 `config.json` 中：

```json
{
  "features": ["push"],
  "config_schema": {
    "PUSH_TIME": {
      "type": "string",
      "title": "推送时间",
      "description": "北京时间 HH:MM",
      "default": "09:00"
    }
  },
  "config": {
    "PUSH_TIME": "09:00"
  }
}
```

声明后，统一插件分配界面会提供该插件的推送授权选项。接收资格以统一权限表中的记录为准：

| 权限记录 | 对主动推送的含义 |
|---|---|
| `my_plugin` | 普通插件授权，不能据此发送主动推送 |
| `my_plugin#push` | 允许向该群聊或私聊主动推送 |
| `some/path/my_plugin#push` | 多级目录插件权限，查询时兼容完整键或末级插件名 |

新增推送插件必须遵守以下规则：

- 不在插件配置中另设 `ENABLE_PUSH`、`monthly_enabled` 等推送总开关，也不另设 `publish_chats`、`enabled_chats`、目标群列表等接收名单。插件生命周期由统一插件管理控制，每个聊天是否接收由其 `#push` 权限控制。
- 时间、频率、内容范围等业务参数可以保留。配置里的管理员故障通知接收人属于独立用途，不能当作业务推送名单；不要因为参考 Weekly 而照搬其管理员能力。
- 调度到期时读取 `#push` 权限，没有接收者就跳过本轮推送生成。不得将普通权限自动升级为推送权限，也不得把旧目标名单自动转换为授权。
- 生成任务可能耗时较长，发送前必须重新读取推送权限；已撤销权限的聊天应跳过。权限读取失败时记录失败并停止发送，不能退回旧名单或向全部聊天发送。
- 主动推送没有入站消息，不能依赖 EventBus 的消息权限过滤代为授权。代码必须主动查询接收者；`require_mention` 是消息触发条件，不是推送开关。
- 推送任务同时在 `manifest.json` 的 `jobs` 中声明时间配置及授权条件。`features` 用于声明能力、显示授权入口；`jobs` 用于描述任务；两者都不会代替代码中的权限检查和调度执行。

查询示例，与 Weekly 使用相同的 `WeChatUser` / `UserPermission` 统一权限记录。通过 EventBus 的会话工厂访问数据库，兼容带目录的权限键，并确保关闭会话：

```python
from app.models.user_permission import UserPermission, WeChatUser

PLUGIN_NAME = "my_plugin"


def get_push_enabled_chats(event_bus):
    db = event_bus.db_session_factory()
    try:
        grants = db.query(WeChatUser.chat_name, UserPermission.plugin_name).join(
            UserPermission, UserPermission.user_id == WeChatUser.id,
        ).all()
        expected = f"{PLUGIN_NAME.rsplit('/', 1)[-1]}#push"
        return sorted({
            chat_name for chat_name, permission_key in grants
            if chat_name and str(permission_key or "").rsplit("/", 1)[-1] == expected
        })
    finally:
        db.close()
```

上例的数据库异常交由托管任务或调度循环记录，不应吞掉后继续发送。调度循环使用 `context.workers.start()`，生成任务使用 `context.tasks.submit()`；发送前再次调用查询函数，并使用持久化发送记录避免重启或重试造成重复推送。

推送插件验收应覆盖：声明能显示推送授权入口、普通权限不会收到推送、`#push` 权限可触发推送、权限撤销后停止发送、无授权或查询失败时不发送，以及同一计划任务不会重复推送。

可参考 [Weekly](Weekly/main.py) 的 `Weekly#push` 权限查询和 [游戏发售日历](game_release_calendar/main.py) 的每月调度、发送前权限查询与发送记录；新插件应按本节规范实现，不以旧插件中的兼容逻辑代替显式声明。

## 3. manifest.json

```json
{
  "schema_version": 2,
  "plugin_api_version": 2,
  "storage": {
    "cache_retention_days": 7,
    "cache_limit_mb": 500
  },
  "backup": {
    "schema_version": 1,
    "include_persistent_storage": true,
    "include_generated_files": false,
    "supports_restore_migration": true
  },
  "health": {
    "critical": false,
    "timeout_seconds": 5
  },
  "listeners": [
    {
      "event": "text_message_received",
      "handler": "handle_text",
      "title": "处理示例命令",
      "trigger": {
        "kind": "keyword",
        "summary": "消息包含任一配置关键词",
        "config_keys": ["trigger_keywords"],
        "conditions": ["空消息不会触发"]
      },
      "scope": {
        "level": "global",
        "chat_types": ["group", "user"]
      },
      "propagation": "stop_on_consumed"
    }
  ],
  "jobs": []
}
```

### listeners 核心字段

| 字段 | 说明 |
|---|---|
| `event` | `EventType` 的字符串值，如 `text_message_received` |
| `handler` | 注册给 `subscribe()` 的函数名；同一插件内 `(event, handler)` 必须唯一 |
| `title` | 面向用户的监听器名称 |
| `trigger.kind` | `always`、`keyword`、`link`、`mention_or_judge`、`session`、`internal` 或 `dynamic` |
| `trigger.summary` | 一句话说明何时进入处理逻辑 |
| `trigger.config_keys` | 可直接编辑的全局配置键；必须存在于 `config_schema` |
| `trigger.conditions` | 代码固定条件，只读展示 |
| `scope.level` | 当前固定为 `global` |
| `scope.chat_types` | `group`、`user` 或两者 |
| `scope.chat_name_config_key` | 可选；仅允许配置指定的单一聊天，例如管理员私聊 |
| `propagation` | `observe`、`continue` 或 `stop_on_consumed` |

传播语义：

- `observe`：旁路观察者，返回值不会截断链路，例如聊天日志。
- `continue`：处理后继续后续监听器。
- `stop_on_consumed`：仅当处理器实际返回 `True`、返回 `{"consumed": true}` 或显式消费事件时结束链路；未命中或失败返回 `False`，继续下一个插件。

因此，“能够截断”不代表前面的插件会无条件挡住后续插件。执行顺序页会同时展示触发条件和上述分支语义。

### 定时任务

定时或推送能力放在 `jobs`，不要伪装成消息监听器：

```json
{
  "id": "daily_push",
  "title": "每日推送",
  "trigger": {
    "kind": "schedule",
    "summary": "每天在配置时间执行",
    "config_keys": ["DAILY_PUSH_TIME"],
    "conditions": ["仅向拥有 my_plugin#push 权限的聊天发送", "只有数据变化时发送"]
  },
  "scope": {"level": "global", "chat_types": ["group", "user"]}
}
```

`jobs` 用于能力说明和设置导航，不进入消息执行顺序，也不会自动启动调度器。推送插件还必须按第 2.2 节声明 `features: ["push"]` 并查询统一推送权限。上例的 `DAILY_PUSH_TIME` 必须在该插件的 `config_schema` 中声明。

## 4. 统一插件管理接口（Plugin Runtime API v2）

所有插件必须声明 `plugin_api_version: 2`，并让 `register()` 接收第三个 `PluginContext`。平台不再提供 Runtime API v1 的兼容加载路径；缺少版本声明或仍使用双参数注册的插件会被直接拒绝加载。

```python
def register(event_bus, subscribe, context):
    path = context.storage.persistent_path("state.json")
    cache = context.storage.cache_path("lookup.json")
    browser_profile = context.storage.machine_bound_path("browser/Default")
    temporary = context.storage.temp_path("download.bin")

    context.health.register(lambda: {"status": "healthy", "message": "连接正常"})
    context.register_cleanup(close_browser_and_http_clients)

    context.tasks.submit(
        "refresh_index",
        "刷新索引",
        lambda operation: rebuild_index(operation, path),
    )
    context.workers.start("scheduler", run_scheduler_loop)
```

运行时约束：

- 正式数据只能写入 `context.storage.persistent_path()`；它会自动进入状态备份和完整迁移。
- 浏览器配置、硬件索引等机器绑定数据写入 `context.storage.machine_bound_path()`；默认不进入备份，只有管理员显式勾选“包含机器绑定数据”时才打包。
- 可重建缓存写入 `cache_path()`，临时下载写入 `temp_path()`；默认不备份，卸载时清理临时目录。
- 后台工作通过 `context.tasks.submit()` 提交，自动获得进度、取消、历史和插件所有权。
- 长任务通过 `context.tasks.submit()` 运行；长期调度循环通过 `context.workers.start()` 启动，并在循环中检查 `context.workers.stop_event`。
- 浏览器、HTTP session、线程池等其他资源必须通过 `register_cleanup()` 登记。
- 插件应注册轻量健康检查；异常会统一出现在“系统 → 运行状态”。
- 需要恢复数据结构升级的插件声明 `supports_restore_migration`，后续格式版本通过插件恢复钩子迁移。

插件不得直接创建 `threading.Thread`、`threading.Timer`、长期 `ClientSession`，也不得随意写入公共 `data/`/`tmp/`。Runtime API v2 不提供旧实现的兼容加载路径。

`summary_plus` 是 Runtime API v2 的完整参考实现：`runtime_support.py` 展示有界任务准入、URL 去重、分池并发与托管产物；`browser_runtime.py` 展示单例浏览器生命周期；`media_pipeline.py` 和 `xhs_service.py` 展示如何把平台能力从入口类拆开。新插件应复用这些运行模式，不要复制具体平台业务代码。

## 5. main.py

```python
import logging

from app.core.event_bus import Event, EventType
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)
plugin = None


class MyPlugin:
    def __init__(self):
        self.trigger_keywords = get_config(
            "trigger_keywords", ["示例"], plugin_name="my_plugin"
        ) or []

    def handle_text(self, event: Event) -> bool:
        message = str(event.data.get("message") or "")
        if not any(word in message for word in self.trigger_keywords):
            return False
        wx = event.context.get("wx")
        chat_name = event.data.get("chat_name")
        if not wx or not chat_name:
            return False
        return bool(wx.send_message(chat_name, "已处理"))


def handle_text(event: Event) -> bool:
    return plugin.handle_text(event) if plugin else False


def register(event_bus, subscribe, context):
    global plugin
    plugin = MyPlugin()
    context.health.register(lambda: {"status": "healthy", "message": "插件已就绪"})
    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_text)


def unregister():
    global plugin
    plugin = None
```

不要向 `subscribe()` 传顺序数值或阻断参数。它只接收事件、处理器，以及少数确需消除同名处理器歧义时使用的 `listener_key`；顺序和传播行为分别由中央顺序表与 Manifest 决定。

## 6. 会话型插件

多轮图片收集、向导等插件应为“开始命令”和“会话续接”分别声明监听器触发语义。若需要在会话期临时豁免群聊 @ 条件，可使用：

```python
event_bus.request_session_permission(chat_name, "my_plugin", duration=300)
event_bus.release_session_permission(chat_name, "my_plugin")
```

必须设置有限时长，并在完成、取消和异常清理时主动释放。插件仍需维护自己的会话状态；临时权限不等于自动消费消息。

## 7. 质量要求与升级清单

- 处理器未命中时返回 `False`，成功消费时才返回 `True`。
- 网络、浏览器和模型异常不得错误返回已消费。
- 耗时工作不得长期阻塞同一聊天的串行消息队列。
- `logging.getLogger(__name__)`，日志中包含插件名、聊天与失败阶段，但不得泄露密钥。
- 配置项使用明确标题、说明、类型和默认值；触发字段归入 `trigger` 组。
- 使用统一 LLM 路由的每个 `call_type` 都在 `ui.llm_tasks` 声明用户可读名称和用途。
- Manifest 中每个监听器与 `register()` 逐一对应，定时任务放入 `jobs`。
- 新插件声明 `plugin_api_version: 2`，并通过 `PluginContext` 管理任务、存储、健康和清理。
- 插件停止或重载后不得残留线程、HTTP session、浏览器进程、计划任务或临时文件。
- 不写 `priority`、`routing_overrides`、`block_after_handling`，也不假设目录扫描顺序。
- 修改后至少运行插件管理、路由服务、Web 契约和插件自身测试；再重启 Web 进程确认所有插件加载成功。

### 用量与调用来源名称

用量页和调用记录左侧的调用来源复用 `ui.llm_tasks`：第一行展示 `label`，第二行保留 `组件.call_type`。无需维护前端翻译表。未声明的历史调用显示“自定义任务”。

只记录用量、不可通过辅助任务路由配置的调用，可在声明中设置 `"routable": false`（例如核心助手的 `chat`）。目录接口的 `usage_tasks` 包含全部声明，`tasks` 仅包含可配置路由。

消息事件处理器以及通过 `context.operations` / 托管 worker 启动的后台任务自动继承聊天归属。脱离事件上下文的调用应传入 `_mabobot_chat_name`；可额外提供 `_mabobot_user_id`（系统聊天对象 ID）、`_mabobot_chat_type`（`group` / `user`）。这些参数不发送给模型。无聊天归属的系统任务可显式传入 `_mabobot_usage_scope="system"`；未提供归属的调用归入“未归属”。
