# Codex 直接生成与出站验证

`direct` 模式使用独立执行分支，不再借用 `codex_text_only` 的普通代理调用。每个请求启动一个短生命周期 App Server，创建新的临时线程，并把任务规则放入 `baseInstructions`、原文放入 `turn/start.input`。不恢复会话，不读取聊天历史，不调用 Mabobot 的 Lens、Skills、附件交付或工具包装逻辑。

## 出站边界

独立分支在 Codex 所在的 Linux/WSL 进程环境中启动仅监听 `127.0.0.1` 的临时 HTTP 校验端点。端口和随机路径只交给本次 App Server；Windows 宿主通过私有 stdio 管道控制 worker，不转发账号凭据到 Windows。

Codex 继续负责原 Profile 的认证和续期。适配器从原生有效配置解析原提供方，只把本次线程的推理 URL 指向校验端点；校验通过后，原生认证请求发送到该提供方原有的 Responses 地址。请求体保持原始字节，不重写、不移除不合规内容后放行，不自动重试，不跟随 HTTP 重定向。认证仅在本机内存中经过转发，不写入日志、能力证据或任务快照。

发往上游前逐项核对：

- 模型、推理强度、JSON Schema 与本次任务一致。
- 规则和原文独立分层，消息的角色、顺序及完整文本准确匹配。
- 没有工具声明（包括 `input` 中的 `additional_tools`）、历史、人格、项目指令、Skills、Lens 或额外输入。
- 仅接受已列明的模型和 HTTP/SSE 传输字段，拒绝续接历史的字段。
- 每个请求最多转发一次；取消或超时后不再转发。

响应中的工具调用在传给原生引擎之前被拒绝；模型不符和截断分别报错。取消针对原线程和原 turn。未确认的断线或取消保留 `interrupted_unknown`，不自动重放。证据保存在各自请求句柄上，并发调用不共享“最后一次请求”状态。

## 能力探针与限制

AI 运行中心的 Codex Profile **直接生成**测试已接入相同分支。只有指定连接实际完成、返回预期结果、实际模型一致且出站核对成功，才登记该 Profile/generation/model/effort 绑定的 `strict_generation=verified`。正常任务仍逐次验证实际请求，不仅依赖历史探针结果。

首版支持单段文本和 JSON Schema。多模态附件、显式 `temperature` / `max_tokens`、`json_object`、非 Responses 协议及签名型提供方保持不可用，不静默丢弃参数。宿主继续执行 JSON/schema、skip 和交付校验。工具模式与旧 Codex 兼容路径保持原实现。

本机安装的 **Codex 0.169.0** 实际探针仍检测到 `additional_tools` 和额外消息，两个并发请求均返回 `outbound_contract_mismatch`，上游收到 **0** 个请求。因此此次实现与测试不表示该安装已支持严格直接生成，也没有启用生产能力、修改账号或运行真实计费调用。合成协议测试中的成功路径不能代替真实账号验收。

## 使用与验证

在 AI 运行中心选择 Codex Profile、模型和推理强度，执行“直接生成”能力测试。只有本次指定连接完成且出站契约通过时才记录为已验证；其他模型、账号及参数不继承结果。

当前自动化覆盖 Linux stdio worker、本机 HTTP 转发、请求隔离及取消；Windows → WSL 的命令分支已接入，此轮未在 Windows 宿主执行验收。合成协议测试通过不能代替真实账号验收。

接口依据为本机生成的 App Server schema；线程、turn 和完成状态的公开接口说明见 [Codex App Server 文档](https://learn.chatgpt.com/docs/app-server)。严格边界以实际捕获结果为准，不能从临时线程或纯文本开关推断通过。
