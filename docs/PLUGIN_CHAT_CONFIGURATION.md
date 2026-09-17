# 插件默认配置与聊天配置

这是所有插件的统一平台能力。基础目录、权限与生命周期遵循 [插件开发规范](../app/plugins/README.md)。

## 配置与界面

- 功能插件页的「默认配置」决定没有覆盖的字段值。
- 聊天管理中的「聊天配置」直接显示有效值。编辑立即呈现「已自定义」状态；点「保存」直接生效，不需要再保存聊天页。
- 只覆盖改动的字段，其他字段继续跟随默认配置。界面只有一个「恢复默认」，没有来源下拉框或自定义模式开关。
- 「恢复默认」将编辑器恢复为默认值，保存时清除覆盖；恢复后仍可继续编辑。
- 配置保存独立于插件授权。未启用的插件也可预先配置，配置本身不会授予执行或推送权限。
- 高级设置默认折叠，长内容在模块内滚动。保存期间锁定编辑器；保存失败保留草稿。

有效值按「字段内置默认 → 插件默认配置 → 当前聊天覆盖」计算。数组、对象整体替换，不做深层合并。配置值存放在 `config.json` 的 `config` 中；兼容读取旧的顶层同名键，顶层值优先，新插件不要重复定义。

## 新插件默认支持

`config_schema` 中可编辑的业务字段自动支持两层配置，无需声明 `scope: "global_and_chat"`。该旧声明仍兼容。提供合法的 `default`，在消息处理时用统一的 `get_config()` 读取即可：

```python
from app.core.event_bus import EventType
from app.utils.plugin_config import get_config


def register(event_bus, subscribe, context):
    def handle_text(event):
        keywords = get_config("trigger_keywords", ["示例"], plugin_name=context.plugin_id)
        if not any(word in str(event.data.get("message") or "") for word in keywords):
            return False
        reply = get_config("reply_text", "已处理", plugin_name=context.plugin_id)
        wx = event.context.get("wx")
        return bool(wx and wx.send_message(event.data["chat_name"], reply))

    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_text)
```

EventBus 从数据库取得可信的聊天 ID，建立配置上下文。`get_config()` / `get_plugin_setting()` 自动读当前聊天的有效值；无聊天上下文时读默认配置。不要直接读取 JSON 文件实现业务配置，也不要在 `register()` / `__init__()` 中缓存业务设置。

每次执行首次读取时取得快照，后续读取及托管后台任务沿用它。保存配置后，下一次执行读取新值，已运行的任务不在中途切换参数。返回的可变值是副本。两个聊天同时调用同一个插件实例也不会修改彼此的配置。

既有插件的启动缓存通过 `ScopedConfigAttribute` 适配：上下文内按快照计算属性，上下文外保留启动值。新插件优先在执行时调用 `get_config()`，避免再增加缓存适配。

`context.config.resolve(chat_id=...)` 仍返回当前插件可覆盖字段的有效字典；匹配当前聊天时复用执行快照，否则独立查询。不包含管理元数据或仅限默认配置的字段。它只解析配置，不检查授权。

## 后台任务和定时推送

`context.tasks.submit()`、`context.workers.start()`、`start_timer()` 自动继承配置上下文。不要自行启动裸线程；自行使用线程池时，每次提交要通过独立的 `copy_context().run` 传递上下文。

脱离入站消息的任务必须先选择目标聊天。按 ID 使用 `plugin_config_scope(chat_id=..., session_factory=...)`；重放历史消息、管理员代某聊天重试可使用 `chat_config_scope(chat_name, session_factory)`。处理多个聊天时，每个目标单独建立上下文，禁止用发起人或上一聊天的配置处理所有目标。

日／周／月推送使用 [ChatSchedule](../app/services/plugin_chat_schedule.py)：逐个读取 `#push` 授权聊天的时间设置，按聊天和时间持久化去重，发送任务开始前重新校验授权。回调只处理传入的目标聊天。共享的任务执行锁用于限制资源并发，不合并不同聊天的业务配置。

持久化业务队列在消费时，按消息所属聊天建立上下文。任务中依赖配置的服务对象、报表数据源、待选会话超时等也必须按目标聊天读取；不能仅改界面。

## 字段边界与凭据

- `scope: "global"` 只用于实际共享的进程资源，如浏览器端口、后台线程数量、总存储配额和清理周期。这些字段只出现在默认配置中。不要将提示词、触发词、开关或推送时间声明为全局。
- `level: "hidden"`、`readOnly: true` 和旧目标名单等兼容字段不进入聊天编辑器。
- 密钥等敏感字段也可按聊天设置。管理接口不回显值，只返回 `configured` 和来源；留空保持原值。「恢复默认」可清除聊天凭据覆盖。运行时读取真实有效值。
- 模板只保存全部非敏感、可覆盖字段，不含凭据、授权或进程资源。导入是复制，不建立联动。
- 模型连接和系统共享浏览器仍由各自的系统入口管理，不通过插件配置复制。

Manifest 的监听器 `scope.level: "global"` 描述监听范围，与配置字段是否支持聊天覆盖无关。无需修改 Runtime API v2 版本。

## 接口与并发

| 接口 | 用途 |
|---|---|
| `GET /api/capabilities/settings/{plugin}` | 默认配置描述 |
| `GET /api/capabilities/settings/{plugin}?user_id={id}` | 聊天配置描述与有效值 |
| `PUT /api/capabilities/settings/{plugin}` | 部分更新默认配置 |
| `GET /api/chats/{id}/policy` | 聊天版本及配置状态 |
| `PATCH /api/chats/{id}/policy` | 直接保存聊天覆盖 |

聊天保存示例：

```json
{"expected_version": 16, "plugin_configs": {"my_plugin": {"set": {"reply_text": "收到"}}}}
```

省略字段表示保留。`reset_fields` 清除指定覆盖；`reset_all: true` 清除全部覆盖，不得和设置字段混用。配置与同次提交的策略字段原子保存，共用 `policy_version`；过期版本返回 409，非法配置返回 422。失败不覆盖线上值，界面保留草稿。独立保存成功同步父表单版本与插件状态，保留其他未保存编辑。不要附带未编辑的 `plugin_grants`。

描述包含 `schema_version`、`overrides`、`defaults`、`effective`、`sources`、`configured` 和 `defaults_revision`。来源只有 `chat` 与 `global`。默认值指纹不是并发写入版本。敏感值在上述字典中均为 `null`，不得直接将公开描述作为完整运行时配置。

模板接口 `/api/capabilities/config-templates/{plugin}` 支持 GET、POST、DELETE。模板修改与删除使用自己的 `expected_version`，与聊天版本独立。更新模板不会改动已经导入的聊天。

## 校验、存储与验收

[plugin_chat_config_service.py](../app/services/plugin_chat_config_service.py) 统一处理字段白名单、基础类型、数值范围与枚举。它不是完整 JSON Schema 实现；跨字段、嵌套等业务约束应扩展统一校验并覆盖默认保存、聊天保存、模板和运行时。翻译已有专用语言及提示词校验。保存默认配置时还会验证它与现有聊天覆盖的组合。

插件配置迁移应先备份，保持可重复执行，不自动新增聊天授权；翻译助手旧提示词需由用户在新的全局默认或聊天设置中重新录入，公开版不包含本机专用迁移脚本。

`chat_plugin_configs` 按 `(user_id, plugin_name)` 存储覆盖，独立于可替换的权限行。撤销授权保留配置，删除聊天清理覆盖。新字段自动继承默认值；字段重命名、移除及收回覆盖能力时需要检查旧覆盖与模板并迁移。配置损坏不静默回退到其他聊天。

回归入口：

- `tests/test_plugin_config_context.py`：新插件自动接入、运行时隔离、快照、敏感值、定时去重和授权撤销。
- `tests/test_plugin_chat_config.py`：覆盖、恢复、并发版本、模板及 API。
- `tests/browser/plugin_chat_config.py`：普通插件直接保存、失败草稿、JSON、手机与主题。
- `tests/browser/translation_chat_config.py`：翻译控件、模板、预览和统一保存。

维护工作区的回归用例覆盖平台配置、翻译业务和浏览器交互；公开源码不分发测试目录。新增插件需补充自己的业务约束和处理器测试，不以翻译测试通过代替自身接入验证。

每个插件还应验证自己的处理器和异步路径，不能以通用配置接口通过测试代替业务生效验证。
