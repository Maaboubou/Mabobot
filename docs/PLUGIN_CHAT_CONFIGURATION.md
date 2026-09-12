# 每聊天插件配置开发指南

本指南描述已实现的平台契约，供新增插件和改造全局配置插件使用。基础目录、Manifest、权限与生命周期遵循 [插件开发规范](../app/plugins/README.md)；翻译语言、提示词变量和具体界面见 [翻译配置说明](TRANSLATION_CHAT_CONFIG_UPGRADE.md)。

## 1. 配置归属与字段声明

| 内容 | 存储与读取方式 |
|---|---|
| 字段内置默认 | `config_schema.<key>.default` |
| 插件全局值 | 插件 `config.json` 的 `config`；全局专用字段继续用 `get_config()` |
| 聊天覆盖 | 主数据库 `chat_plugin_configs`；插件用 `context.config.resolve(chat_id=...)` |
| 可复用模板 | 主数据库 `plugin_config_templates`；保存完整可覆盖字段快照 |
| 插件授权、推送授权、群聊 @ 条件 | 统一聊天策略与权限记录，不能塞进配置模板 |
| 模型连接与凭据 | 现有模型配置与凭据入口，不随聊天配置复制 |

只有 `scope: "global_and_chat"` 且未被敏感字段规则排除的字段可覆盖。未声明 `scope` 的既有插件保持全局读取方式。敏感性由 `sensitive` 声明和现有字段名识别规则判断；密钥类字段应显式标记 `sensitive: true`，不要通过声明非敏感来绕过隔离。

下面是合并到示例插件中的配置片段，`reply_text` 支持独立设置，触发关键词仍为全局值：

```json
{
  "config_schema": {
    "trigger_keywords": {
      "type": "array",
      "title": "触发关键词",
      "group": "trigger",
      "default": ["示例"]
    },
    "reply_text": {
      "type": "string",
      "title": "回复内容",
      "group": "reply",
      "scope": "global_and_chat",
      "default": "已处理"
    }
  },
  "config": {
    "trigger_keywords": ["示例"],
    "reply_text": "已处理"
  }
}
```

有效值按内置默认 → 全局值 → 聊天覆盖计算；数组、对象均整体替换，不做深层合并。为兼容现有配置文件，解析器优先读取顶层同名全局键，再读取 `config` 中的值；新代码统一采用 `config`，不要在两处重复定义。

聊天只保存显式覆盖。覆盖值即使恰好等于全局值，仍属于聊天配置；API 不自动消除它。要继续跟随全局，必须清除对应覆盖，不能提交空字符串或 `null` 代替重置。

此处的字段 `scope` 与 Manifest 中固定为 `global` 的监听器 `scope.level` 是两套声明。执行顺序仍由中央顺序表管理，新增配置层不会自动改变触发逻辑。

## 2. 运行时接入

Runtime API 版本仍为 2。以下代码配合上面的字段，以及主规范中声明 `handle_text` 的 Manifest 使用：

```python
import logging

from app.core.event_bus import EventType
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)


def register(event_bus, subscribe, context):
    def handle_text(event):
        message = str(event.data.get("message") or "")
        keywords = get_config(
            "trigger_keywords", ["示例"], plugin_name=context.plugin_id
        ) or []
        if not any(word in message for word in keywords):
            return False
        wx = event.context.get("wx")
        chat_name = event.data.get("chat_name")
        if not wx or not chat_name:
            return False
        try:
            settings = context.config.resolve(
                chat_id=event.context.get("chat_id")
            )
        except Exception:
            logger.exception("插件 %s 读取聊天配置失败", context.plugin_id)
            return False
        return bool(wx.send_message(chat_name, settings["reply_text"]))

    subscribe(EventType.TEXT_MESSAGE_RECEIVED, handle_text)


def unregister():
    pass
```

`resolve()` 返回**当前插件可覆盖字段的有效值字典**，不包含全局专用字段，也不包含 `effective`、`sources` 等管理元数据。每次调用重新读取磁盘默认值，并打开、关闭独立数据库会话；不会返回 ORM 对象。每次业务处理读取一次，把快照显式传入模型请求或托管任务，避免处理中途反复读取导致前后规则不一致。

不要在 `register()` 中缓存某个聊天的解析结果，不要修改共享提示词、全局 `get_config()` 缓存或 `config.json` 来实现聊天切换。保存聊天配置无需重载插件，下一次解析即可读到新值；已经开始的任务继续使用其原快照。

`chat_id` 必须是有效的整数 `WeChatUser.id`。入站消息使用 EventBus 注入的 `event.context["chat_id"]`；缺少 ID、聊天不存在、配置损坏或校验失败时停止本次处理并记录错误，不回退到其他聊天或吞掉错误使用全局默认。

`resolve()` **只解析配置，不检查授权**。入站消息由 EventBus 检查插件权限；后台任务应自行取得目标聊天的数据库 ID，并按 [推送权限规范](../app/plugins/README.md) 查询 `#push` 授权，发送前再次检查。不能因为某聊天有独立配置就向其推送。

## 3. 管理接口与并发保存

`{plugin}` 使用插件管理器的完整插件 ID，包含多级目录时不得擅自截成末级名称；`{id}` 是数据库聊天 ID。

| 接口 | 用途 |
|---|---|
| `GET /api/capabilities/settings/{plugin}` | 全局设置描述 |
| `GET /api/capabilities/settings/{plugin}?user_id={id}` | 仅返回可覆盖字段，带有效值、来源和 `chat_config` |
| `PUT /api/capabilities/settings/{plugin}` | 以 `{"values": {...}}` 部分更新全局值；不是聊天保存接口 |
| `GET /api/chats/{id}/policy` | 获取聊天 `version` 和 `plugin_configs` |
| `PATCH /api/chats/{id}/policy` | 使用聊天版本原子保存独立配置，可与其他聊天策略一起提交 |

配置描述包含 `schema_version`、`overrides`、`defaults`、`effective`、`sources` 和 `defaults_revision`。`sources` 只有 `chat` 与 `global` 两种值，内置默认兜底也归为 `global`。`defaults_revision` 是默认值内容指纹，**不是**写入时的并发锁版本。

只更新当前插件的请求示例；`expected_version` 应来自刚读取的聊天策略 `version`：

```json
{
  "expected_version": 16,
  "plugin_configs": {
    "my_plugin": {
      "set": {"reply_text": "收到，我来处理"}
    }
  }
}
```

- 省略插件或字段表示保留；`plugin_configs: {}` 不清除已有配置。
- `set` 修改指定覆盖，`reset_fields: ["reply_text"]` 清除指定覆盖，`reset_all: true` 清除该插件全部覆盖。
- 同一字段不能同时出现在 `set` 与 `reset_fields`；`reset_all` 不能和非空的设置或重置字段混用。
- 插件配置与同次提交的其他策略字段在一个事务中保存，共用聊天 `policy_version`；过期版本返回 409，非法配置返回 422，失败整笔回滚。
- 只保存插件配置时不要附带未改动的 `plugin_grants`；省略授权表示保留，显式空列表会移除授权。

前端独立保存成功后必须同步聊天策略版本、配置状态，并清除该插件旧草稿，保留其他未保存表单内容。409 时重新获取策略并让用户处理冲突，不能无提示覆盖。

当前翻译编辑器已采用「保存并生效」直接 PATCH，并按改动字段生成覆盖。其他插件仍使用通用编辑器的「应用到聊天」草稿，再由聊天页保存。新增插件不能假设声明 `scope` 就会自动获得翻译专用的直接保存、双语／三语或试译界面。

## 4. 模板是可复用副本

模板按插件隔离，名称在同一插件内唯一。`values` 必须恰好包含该插件**所有可覆盖字段**，不能只传 `overrides`，也不能混入全局专用字段。创建时可采用当前完整有效值；名称去除首尾空白后为 1～80 个字符，不含控制字符。

| 接口 | 请求与响应 |
|---|---|
| `GET /api/capabilities/config-templates/{plugin}` | 返回 `{"templates": [...]}` |
| `POST /api/capabilities/config-templates/{plugin}` | 创建传 `name`、`values`；更新或改名另传 `template_id`、`expected_version`；返回模板对象 |
| `DELETE /api/capabilities/config-templates/{plugin}` | 请求体传 `template_id`、`expected_version`；成功返回 `{"deleted": true}` |

模板对象包含 `id`、`plugin_name`、`name`、`schema_version`、`version`、`values`。更新和删除比较模板自身 `version`，冲突返回 409；它与聊天策略版本无关。

导入是把模板值复制到编辑器，再走聊天保存协议，没有模板绑定或自动应用端点。模板保存立即持久化，但不会隐式保存当前聊天；修改、重命名或删除模板均不联动已导入的聊天。全局默认变化也不会改写已保存模板。

## 5. 校验、存储与升级

共享入口是 [plugin_chat_config_service.py](../app/services/plugin_chat_config_service.py) 的 `chat_fields()`、`validate_effective()` 和两个配置服务。通用校验覆盖字段白名单、基础类型、数值上下限和枚举；它不是完整 JSON Schema 校验器，不会仅凭 `items`、`minItems`、字符串长度等声明就执行全部业务约束。

翻译插件通过 `validate_effective()` 中的专用分支补充语言数量、唯一性、提示词变量等规则。目前没有可自动发现的插件自定义校验钩子；其他插件需要跨字段或嵌套约束时，应扩展统一校验入口并覆盖聊天保存、全局默认保存、模板保存及运行时解析，不能只在前端校验。全局保存也会校验新默认与已有聊天覆盖的组合，避免默认变更让独立配置失效。

`chat_plugin_configs` 以 `(user_id, plugin_name)` 唯一，`overrides_json` 只存覆盖；`plugin_config_templates` 以 `(plugin_name, name)` 唯一，`config_json` 存完整快照。两者当前 `schema_version` 均为 1，在数据库初始化时创建，随主数据库备份。配置不挂在可替换的权限行上：撤销、重建授权保留配置，删除聊天时通过 ORM 关系级联清理聊天覆盖。

不要为单个插件再建按聊天名称索引的配置文件或全局目标名单。增删字段、重命名插件 ID、收回字段的聊天覆盖权限时，必须检查已有覆盖与模板并显式迁移；未知字段或未知格式版本会被拒绝，而非自动丢弃。聊天损坏覆盖可通过 `reset_all` 显式清除；插件暂不可用时策略接口返回 `unavailable` 与错误信息，保留原记录。

插件配置迁移应先备份，保持可重复执行，不自动新增聊天授权；翻译助手旧提示词需由用户在新的全局默认或聊天设置中重新录入，公开版不包含本机专用迁移脚本。

## 6. 接入验收

- 两个聊天分别覆盖，运行时互不串配置；无覆盖时读取当前全局默认，数组整体替换。
- 部分修改保留其他继承关系，单字段重置和全部重置正确，授权撤销不删除配置。
- 聊天与模板版本冲突正确报错，非法配置使同次策略修改全部回滚。
- 模板完整快照可导入多个聊天，更新、删除模板不改动聊天；未知、敏感和全局专用字段拒绝覆盖。
- 全局变更与现有覆盖组合通过业务校验；未知聊天、损坏配置和缺少授权不会误发送。
- 前端按实际保存模式验收，并验证版本同步和其他未保存编辑的保留。

维护工作区的回归用例覆盖平台配置、翻译业务和浏览器交互；公开源码不分发测试目录。新增插件需补充自己的业务约束和处理器测试，不以翻译测试通过代替自身接入验证。
