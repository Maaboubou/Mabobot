# ChatBot 事件记忆与本地检索

`builtin_chatbot` 的旧滚动摘要和长期记忆文件已经停用。当前系统由两条可追溯链路组成：事件卡生成群级阶段记忆，原始消息独立生成人物证据账本与人物投影。两条链路都使用 SQLite 持久化，并在每次回复前只召回与当前话题相关的少量记忆。

新记忆的生产数据库是 `data/chat_memory.db`。旧目录 `data/chat_summaries` 不再读取。

## 一、整体链路

```text
微信消息
  → data/chat_logs/<群名>.jsonl
  → 后台按批抽取事件卡
  → 逐项证据约束 / 高风险主语复核
  → 本地 BGE 向量化
  → 语义去重 / 相邻事件冲突检查 / 建立版本链
  → 写入 data/chat_memory.db
  → 原始消息按稳定身份建立人物消息索引
  → 每个人独立生成高召回候选并做二次证据核验
  → 观察账本生成事实版本、跨时模式、关系和人物快照
  → 最终人物投影逐项再审计
  → 定期刷新群级阶段记忆
  → 回复前按当前话题检索
  → 阶段记忆 + 相关人物 + 少量事件卡进入主模型
```

事件抽取、Embedding、去重和阶段整理都在后台执行，不阻塞正常回复。Embedding 启动时也会在后台预热；本地模型不可用时，实时检索会自动退化为关键词等非向量信号。

### 1. 原始消息与处理游标

微信消息先写入 `data/chat_logs/<群名>.jsonl`。记忆状态为每个聊天保存独立处理游标，因此：

- 已处理的消息不会因进程重启而重复炼化。
- 第一次启用时，`memory_initial_backfill_messages` 决定最多回看多少条现有实时日志；默认 2000 条。
- 大规模、跨年份的外部历史记录不要依赖该初始回看参数，应使用后文的“历史记录手动炼化”流程。
- 每次成功接收消息都会尝试调度后台记忆任务；是否真正调用模型仍由积压消息数决定。

### 2. 事件卡抽取

当前默认参数为：

- 至少积累 20 条核心消息才允许处理。
- 目标批次为 40 条核心消息，单批最多 60 条。
- 核心批次前后各提供最多 12 条重叠消息。
- 单批最多生成 6 张事件卡，也可以生成 0 张。
- 核心消息和重叠证据合计默认最多 16000 Token。
- 一次后台任务最多连续处理 3 批，避免积压时突发调用。

正常实时处理会等待“目标 40 条核心消息 + 12 条后文证据”，所以批次尾部的省略主语、转述和未完成结果可以由后续消息消歧。前后重叠消息只作为证据，不能单独触发新事件；事件的锚点必须落在本批核心消息内。

事件抽取器会区分：

- 当事人在群内确认的事实。
- 主语本人的自述（`self_report`）。
- 群友对他人或外部对象的归因说法（`attributed_claim`）。
- 转发新闻、截图或外部说法。
- 未核实传闻、夸张、反讽和玩笑。

每张事件卡包含标题、摘要、起止时间、来源游标、参与者、关键词、观点归属、决定、未完成事项、事件类型、确定性、来源说明和重要度。除此之外，每项事实都必须保存：

- 事实文本、事实主语和消息说话人。
- 直接支持事实内容的消息游标。
- 直接支持主语绑定的消息游标。

模型给出的宽泛来源起止区间不再直接采用。服务端只接受实际存在的证据游标，要求锚点属于证据、至少一条事实证据落在核心批次，并据此重新计算事件来源区间。缺少逐项证据、只靠重叠区触发或锚点与证据脱节的卡会直接丢弃。系统同时保存服务端计算范围内的原始消息，供记忆库浏览器核对。

### 3. 本地向量化

事件卡由本机 CPU 上的 Embedding 模型生成向量，不调用在线大模型：

- 模型：`BAAI/bge-small-zh-v1.5`
- 运行时：FastEmbed / ONNX Runtime
- 向量维度：512
- CPU 线程：4
- 批量大小：8

项目不要求 GPU。Embedding 只负责相似度计算，不负责生成或修改记忆内容。

### 4. 高风险证据复核与隔离

人物近况、本人自述、第三方归因，以及医疗、生育、婚姻、财务、违法等容易造成严重误记的卡，会额外使用 `builtin_chatbot.memory_review` 路由。复核器只读取该卡证据附近的小段原文，逐项检查事实、主语、说话人和证据是否一致，重点识别群聊话题交错和“相邻人名被误当成上一段经历主语”。

- 完全受证据支持：标记为 `passed`，进入后续向量和检索流程。
- 主语冲突、事实拼接、证据不足：标记为 `quarantined`。
- 复核调用失败或没有返回该高风险卡的明确结论：按失败关闭策略隔离。

隔离卡仍保存在数据库和记忆库浏览器中作为审计记录，但不会进入 Embedding 补建、阶段记忆或回复前检索。隔离是系统自动完成的安全终态，不产生必须处理的人工任务；只有发现误判时才通过纠错入口修改。可以在每个聊天的 Memory Profile 中关闭该复核；默认开启。

### 5. 去重、相邻冲突检查与事件版本链

新事件写入前，系统先用本地向量寻找最近 30 天内的相似有效事件：

- 一般事件相似度低于 0.78：不作为语义候选。
- 相似度达到 0.78：使用 `builtin_chatbot.memory_review` 路由区分独立事件、纯重复和同话题新进展。
- 只有模型判断为纯重复，并且相似度达到 0.90，才跳过写入。
- 同话题出现新进展时保留新卡，并建立 `旧事件 → 新事件` 的版本关系。

向量阈值不再是唯一入口。来源范围相邻、重叠或仅间隔最多 3 条消息的卡会被强制交给关系检查，即使它们的向量相似度很低。关系检查会读取逐项 claims 和来源摘录；发现相邻话题被错误合并或主语冲突时，新卡会被隔离，而不是当成事实写入有效记忆。

被替代的旧卡不会删除，仍可审计和查看来源，但不再进入实时检索。实时检索还会使用 MMR 避免返回多张内容高度相似的卡片。

历史记录按时间顺序重放时，去重窗口以“事件发生时间”而不是炼化当天为基准，避免多年历史全部挤在同一个当前时间窗口里。

### 6. 阶段记忆和人物记忆

通常每累计 40 张新事件卡，系统通过 `builtin_chatbot.memory_synthesize` 路由更新：

- 当前阶段总览。
- 稳定事实和群内说法。
- 活跃话题、群体关系、未完成事项和不确定内容。

一次阶段整理默认最多读取 80 张事件、24000 Token，阶段文本最多约 6000 字符。如果新事件替代了旧事件，系统会提前刷新，避免阶段记忆继续保留已经过时的版本。

人物记忆不再从事件卡或阶段摘要二次推导，而是独立读取原始消息。每条原消息先写入
`memory_person_source_messages`，再按稳定身份建立 `memory_person_message_links`：
本人发送的是 `authored`，通过唯一确认别名识别到的相关消息是 `mention`。因此系统
处理的是“某个人跨时间的消息流”，而不是在一个群级窗口里碰巧抽到谁。索引进度和
积压量保存在 `memory_person_pipeline_state`，进程重启后可以接着处理。

人物结果分成四层：

1. `memory_person_observations`：不可变观察账本，保存原消息游标、原文摘录、说话人、
   `sender_id`、时间、来源关系、认识状态、敏感等级和质量状态。
2. `memory_person_fact_versions`：按 slot 保存可追溯事实版本，区分 `current`、
   `historical`、`planned`、`uncertain` 和 `disputed`。
3. `memory_person_patterns` / `memory_person_relationships`：跨时间模式和关系。稳定模式
   至少需要 3 个独立日期且跨度 30 天；不足时只能是 candidate。
4. `memory_person_snapshots`：面向回复和浏览器的物化视图，分当前概况、时间线、
   稳定特点、群内关系和待确认内容。每一项必须引用仍有效的 observation ID。
   “当前概况”不接受模型自由改写，而是由服务端从当前事实版本确定性生成；模型不能
   再把多个历史岗位或状态拼成一条当前事实。职业、单位、地点、群角色和当前状态
   的当前值进一步由服务端直接采用该字段最新的已核验原始观察，聚合模型只能整理
   它们的历史版本。稳定特点还必须有 `confirmed` pattern 支撑；点名某人的群内
   关系必须有该对象的 `current` relationship 支撑，不能通过其他群角色证据旁路。

在线默认每个人积累 30 条相关消息后进入队列，每批最多处理 80 条相关消息，并附带
相邻原文用于消歧；单批最多提出 16 条候选，长期价值候选门槛为 0.58。候选无论最终
是否通过都会写入 `memory_person_claim_candidates`，便于分析漏抽和误抽。寒暄、
临时输赢、商品或新闻链接、一次性排障、当天上下班和单次行为推断会被过滤。

低活跃人物不会无限等待：至少积累 8 条相关消息且最老积压已达到 14 天时，也会自动
进入同一条证据核验流水线。人物处理队列和人物投影队列由后台每 15 分钟独立扫描，
不依赖下一条群消息唤醒；有未投影观察时最长 7 天刷新人物资料。这里没有改变事件记忆
的 40 条核心消息加 12 条后文门槛，也没有改变阶段记忆每 40 张有效事件的刷新门槛。

候选观察由“人物观察与核验模型”进行第二次、独立的逐项证据核验。只有主语明确、
原文完整支持、字段匹配且为原子结论的候选才能进入 active 观察账本；不确定项隔离，
错误项拒绝。聚合为事实、模式、关系和快照后，还会进行第三次最终投影审计，防止
聚合模型把“去郁南报到”补写成某个具体单位，或把值班周期写成职业。服务端另外执行
不可绕过的规则：

- 机器人当前昵称和导入时指定的旧机器人 sender_id 不参与人物学习。
- 群名、`System`、`@chatroom` 形式的会话 ID，以及没有真实发送证据的目录项不会
  建立人物画像；系统身份即使出现在旧库中也不会进入人物索引。
- 群员向机器人提问、要求机器人判断或总结时，消息中的职业、家庭、健康、资产等
  高影响说法不会仅凭“这是该群员发出的消息”就作为本人事实入账。
- `self_report` 必须包含该稳定身份自己的证据消息，不能把其最后发言后的第三方说法
  拼入本人自述。
- 家庭、健康、资产和亲属关系等高影响自述必须出现明确第一人称锚点；省略主语的
  “听朝就做人老豆了”不能自动归到发言者本人。
- `可能`、`暗示`、`未否认`、`居住或工作` 等模糊结论不能作为 asserted 事实。
- 职业、任职单位、学校和地点中的高影响专有信息必须在该项引用原文中实际出现；
  聚合模型或旧观察增加的单位名称会被自动标为 rejected，并留下人物审计记录。
- 亲友患病、住院等健康信息不能挂在目标人物本人的健康字段下。
- 职业、单位、地点、群角色、计划和当前状态存在更新观察时，旧值只能保留在时间线。
  短期 `current_status` 超过 180 天没有确认就失效；职业、单位、地点和群角色使用
  365 天窗口，并从窗口内最近一条语义完整的原子观察确定当前值。配偶任职等嵌在
  家庭字段中的可变状态同样适用。
- 收货/快递地址、出差、旅游、培训期间住宿和酒店入住只证明当时到过或使用过该
  地点，不能推断为当前居住地或工作地；复合地点陈述只保留明确的当前原子子句。
  收到录取、通过考试、完成调整、购买或乘坐等一次性动作只能进入历史时间线。
- 一天内集中讨论或交易不等于稳定兴趣。由行为推导的兴趣、偏好、习惯和群角色必须
  满足 3 个独立日期、30 天跨度；只有本人明确自述的属性可以不等待重复观察。
- 稳定特点必须在服务端再次满足 3 个日期、30 天跨度；模型不能自行放宽。
- 家庭和资产至少为 medium，健康及精确地址为 high。默认回复检索不注入 high。

系统优先使用微信 `sender_id` 作为稳定身份。同一 `sender_id` 改昵称只新增已确认
别名，不会创建第二个人。实时消息还会保留微信层提供的 `sender_remark`；当它唯一
命中已有身份时，新群昵称直接成为该身份的确认别名。历史导出和实时日志同时存在时，
系统会以“内容一致、时间相差不超过 10 秒”的跨来源同消息指纹投票补齐缺失
`sender_id`，至少需要 3 条匹配和 2 条不同的非占位内容，单条图片或常见短句不能
触发身份合并。模型输出的陌生名字不能新建群成员。

身份目录会列出有无画像的全部身份，并根据确认别名冲突和“姓名（昵称）”形式给出
合并建议。合并会复制而不是破坏来源观察、消息链接和派生资料，并记录合并产物；
来源身份的确认昵称会提升为目标身份的确认别名，错误候选别名可明确标记为
rejected，不再参与解析。只要之后没有发生冲突编辑，合并即可从人物审计记录撤销。

在线每新增 10 条有效观察默认刷新一次人物投影；不足 10 条但已等待 7 天也会自动刷新，
人物浏览器仍保留纠错后的手动重建入口。增量
聚合会同时读取上一版事实、模式、关系和快照，旧的有效结论不会仅因本批未出现而
消失。观察是事实源，事实、模式、关系和快照都可以从观察账本重新生成。

### 7. 回复前检索

回复前，系统用“当前消息 + 最近 12 条消息”组成查询，并混合计算：

- 本地向量相似度。
- 关键词匹配。
- 当前发送者和事件参与者。
- 时间与重要度。
- MMR 相关性和多样性。

默认召回最多 6 张事件。主模型最终收到的记忆由三部分组成：

1. 当前阶段记忆。
2. 本轮相关人物资料。
3. 本轮相关历史事件。

三部分合计默认不超过 6000 Token，内部预算大致按阶段 35%、人物 15%、事件 50% 分配；剩余预算会优先补给事件。事件卡按完整单元加入，不会从中间截断。最近原始聊天始终比历史记忆优先，Prompt 也明确要求模型不要把记忆当作绝对完整事实。

人物注入不是把五年画像全文塞给主模型。系统先注入当前概况和少量稳定模式，再根据
问题词义从结构化事实、模式和关系中补充最相关条目；历史事实和不确定项在与问题
无关时不会进入上下文。例如询问学校时可召回教育事实，泛问“这个人怎样”时优先
返回职业、稳定兴趣和群内角色。默认不注入 high 敏感度条目。

## 二、模型与设置

LLM Manager 只公开三个记忆模型路由：

- `builtin_chatbot.memory_generate`：事件与人物候选生成。
- `builtin_chatbot.memory_review`：证据核验、关系判断和最终审计。
- `builtin_chatbot.memory_synthesize`：阶段摘要与人物资料归纳；属于高级覆盖，默认继承生成模型。

事件抽取、人物观察、分期整理等内部任务不再各自暴露独立映射；它们由代码固定输出
约束、Token 上限和超时，只复用上述路由选择主模型及 fallback。升级时会按固定优先级
将已有细分映射合并到三个路由，并删除旧映射点；不会继续保留隐藏的逐任务模型选择。

每个聊天还可以在 `ChatBot Settings → Memory Profile` 覆盖全局参数。高流量群建议先使用默认值：40 条核心消息一批、最多 60 条、前后各 12 条证据、最多 6 张卡、40 张事件刷新一次阶段记忆、回复时召回 6 张事件。

## 三、与 Codex 上下文的关系

Codex 主模型继续使用锚定追加上下文，但记忆按本轮动态检索：

- 本轮检索结果不会写入本地持久锚点。
- 锚点轮换时，只冻结阶段总览作为新线程检查点。
- 当前默认输入硬上限为 220000 Token，锚点轮换阈值为 205000 Token，并预留安全余量，适配约 256K 上下文的 Codex。
- RAG 用于补充长期相关信息，不能替代最近原始聊天。

## 四、查看、审计与人工纠错

### 记忆库浏览器

可从管理后台左侧的 `记忆库` 直接进入，也可以点击 `Users` 列表中每个用户右侧的
记忆库图标，或使用原有的 `ChatBot Settings → 记忆内容管理 → 打开记忆库`。记忆库
顶栏可以搜索并切换其他用户/群聊，切换时保留当前分类并刷新全部记忆数据。记忆库可以：

- 按关键词、日期和版本状态浏览事件卡。
- 查看阶段记忆和结构化人物资料。
- 查看人物的已确认/待确认别名、原消息观察、事实版本、状态、时间、置信度和证据。
- 人工增加或删除单条人物事实、确认别名、合并重复人物，并从人物审计记录撤销操作。
- 查看事件的原始来源消息及时间。
- 查看逐项事实的主语、说话人、证据游标和自动复核结论。
- 查看“当前有效”“更新自 #ID”“已被 #ID 替代”等版本关系。
- 查看被系统自动隔离、默认不参与回答的事件；只有发现误判时才进行人工纠正。
- 查看模型给出的版本关系理由。
- 直接删除事件卡；删除后立即退出有效记忆，但保留来源和可撤销快照。

### 本轮实际使用的记忆

进入 `LLMs → Records → builtin_chatbot.chat`。如果某次调用确实向最终 Prompt 注入了记忆，回复卡会显示群聊、角色和记忆数量，并提供“本轮记忆”入口，可查看实际进入 Prompt 的阶段、人物和事件，以及因 Token 预算被丢弃的候选。

调用历史默认每个来源保留最近 50 条。旧调用不会回填记忆审计信息，没有实际注入记忆的调用也不会显示“本轮记忆”。

### 完整纠错机制

发现事件卡主语、事实或版本关系错误时，应从记忆库打开对应事件，先查看原始来源，再选择：

- 作废错误事件。
- 关联一张已有正确事件。
- 创建一张修正版事件。

提交时填写错误说法、正确说法、原因和受影响人物。系统会：

1. 保留操作前快照和完整纠错记录。
2. 调整事件有效状态或创建修正版版本链。
3. 联动修复阶段记忆和相关人物资料。
4. 将生效中的人工纠错作为后续阶段整理的高优先级约束，防止错误说法重新出现。
5. 将生效中的人工纠错同时提供给后续事件抽取器，避免跨批次再次生成同一错误。

人物资料不再通过重写整段简介来纠错。管理员应修改最小事实单元：复核原消息观察、
增加正确事实、删除错误事实、确认别名或合并重复人物。人物操作写入独立审计记录。
删除人物事实还会建立防复活规则，避免后续聚合再次生成相同结论。人物记忆不再由
事件卡派生，因此事件纠错与人物证据纠错是两条独立审计链；所有事件纠错仍可在
“纠错记录”中查看并撤销。

事件卡上的“删除事件卡”是这一机制的快捷入口：它执行可审计的逻辑删除并联动修复派生记忆，而不是物理删除数据库行。纠错记录中会显示“删除事件卡”，管理员可以撤销恢复。

直接编辑阶段文本适合临时整理，但涉及具体错误事实时应优先使用事件纠错，否则下次自动阶段刷新可能再次从错误事件生成同一说法。

## 五、历史记录手动炼化

外部历史记录必须先在隔离实验库中炼化和检查，确认质量后再激活到生产库。不要把几十万条历史消息直接追加到实时 `chat_logs`。

以下命令都应在项目根目录执行，并使用项目虚拟环境中的 Python。示例中的 `python` 可替换为 Windows 下的 `.\.venv\Scripts\python.exe`。

### 1. 准备输入文件

当前准备器只接受 `ciphertalk-extracted-v2` JSON，不直接接受普通 TXT 或任意微信导出格式。仓库目前没有通用的原始导出转换器；其他格式需要先自行规范化。

最小结构如下：

```json
{
  "format": "ciphertalk-extracted-v2",
  "messages": [
    {
      "time": "2026-01-01T12:00:00+08:00",
      "sender": "wxid_xxx",
      "senderName": "群昵称",
      "type": 1,
      "subType": 0,
      "content": "消息正文",
      "mappedTypeName": "文本",
      "platformMessageId": "唯一消息ID"
    }
  ]
}
```

要求：

- `messages` 必须按时间升序。
- 每条消息必须有合法 ISO 时间和唯一 `platformMessageId`。
- `senderName` 优先作为记忆中的人物名称。
- 表情包、图片、视频、语音和“其他/未知”会保留在完整规范化来源中，但当前会直接排除，不送入记忆模型。
- 图片中的文字如需炼化，应在转换阶段另行形成可审计的 OCR 文本消息，并标记为可处理的文本类型。

本项目已经使用过的示例输入是：

```text
tmp/大中华区慈善联合会_提取版.json
```

### 2. 创建隔离实验

```bash
python -m app.plugins.builtin_chatbot.memory_experiment prepare \
  --source "tmp/大中华区慈善联合会_提取版.json" \
  --chat-name "大中华区慈善联合会" \
  --experiment-id "charity-history-v1"
```

该步骤只做本地校验、规范化、空实验库初始化和生产基线快照，不调用 LLM，也不修改生产记忆。输出目录为：

```text
data/memory_experiments/charity-history-v1/
```

主要文件：

- `manifest.json`：输入哈希、消息统计、规划参数和流水线版本。
- `source_messages.jsonl`：完整规范化来源。
- `memory_messages.jsonl`：可送入记忆模型的消息。
- `chat_memory.db`：隔离实验记忆库。
- `production_baseline.db`：准备时该群生产记忆的基线快照。
- `run_state.json`：进度、事件数、调用数和成本。

实验 ID 不允许覆盖已有目录。改变输入、Prompt 或关键参数后，应创建新的实验 ID，保留旧实验用于比较。

准备完成后、正式调用模型前执行校验：

```bash
python -m app.plugins.builtin_chatbot.memory_experiment verify \
  --workspace "data/memory_experiments/charity-history-v1"
```

该命令验证文件哈希、行数、实验库为空以及 LLM 调用数为零，因此只用于尚未开始的实验。

### 3. 小批试炼

建议先处理少量批次检查事件质量：

历史实验运行器为了保证已有实验可复现，目前使用“最多 60 条、12000 Token、最多 4 张卡”的连续非重叠抽取批次。它与实时链路共用逐项证据结构、Embedding、相邻冲突检查、去重、阶段整理和存储格式；提交一组相邻批次时，也会用事件证据附近消息执行高风险复核。不过，历史抽取本身尚未使用实时链路的前后各 12 条重叠证据。已有历史实验的结果不能声称经过了重叠上下文抽取；改变 Prompt 或启用新证据结构后，应创建新的实验 ID 做 A/B 对比，旧账本中已经完成的批次不会自动重新抽取。

```bash
python -m app.plugins.builtin_chatbot.memory_experiment_runner \
  --workspace "data/memory_experiments/charity-history-v1" \
  --concurrency 16 \
  --max-cost-yuan 35 \
  --max-batches 20
```

`--max-batches 20` 只限制本次处理的待办批次。可查看：

- `run_state.json`：总体进度、事件数、阶段更新和预计成本。
- `run_ledger.db`：每批状态及逐次 LLM Token、耗时和费用。
- `chat_memory.db`：生成的事件、人物和阶段记忆。

如果质量不满意，不要激活；调整抽取规则或模型后创建新实验 ID 重跑。隔离实验不会影响线上回复。

### 4. 完整并发炼化或断点续跑

确认小批结果后，去掉 `--max-batches`：

```bash
python -m app.plugins.builtin_chatbot.memory_experiment_runner \
  --workspace "data/memory_experiments/charity-history-v1" \
  --concurrency 16 \
  --max-cost-yuan 35 \
  --max-attempts 2
```

事件抽取可以并发，写库、向量化、去重和阶段整理按安全顺序提交。`--concurrency` 会限制在 1～256；当前机器已经验证 16 路可用，不需要按模型供应商的理论并发上限设置。

运行账本会持久化每一批的状态。进程中断或个别批次失败后，使用同一条命令重新运行，会继续未完成批次，而不是从头计费。只有 `run_state.json` 变为 `complete` 的实验才能激活。

`--max-cost-yuan` 是实验安全预算。费用按当前 DeepSeek 价格规则写入账本；如果以后更换模型或价格，应同步检查计费估算逻辑，供应商账单仍是最终依据。

### 5. 激活到生产记忆库

完成质量检查后执行：

```bash
python -m app.plugins.builtin_chatbot.memory_activation activate \
  --workspace "data/memory_experiments/charity-history-v1"
```

若历史文件来自旧群，而当前实时日志属于重新创建的新群，两边可能在时间上重叠却
没有共同消息。确认它们确实是两个独立来源后，显式使用：

```bash
python -m app.plugins.builtin_chatbot.memory_activation activate \
  --workspace "data/memory_experiments/<实验ID>" \
  --allow-disjoint-live-log
```

该选项会把实时日志物理游标设为 `0` 并全量补炼。默认激活仍要求历史末条消息与
实时日志在五分钟内精确对齐，普通同群续接不会自动放宽安全校验。

激活过程会：

1. 备份激活前的生产数据库。
2. 构建候选数据库，只替换目标聊天，保留其他聊天的记忆。
3. 复制历史来源消息到永久激活目录。
4. 按历史末尾时间在当前实时聊天日志中寻找边界。
5. 补炼历史导出结束后至激活时之间的实时消息。
6. 校验历史事件数、Embedding、人物、来源和版本关系。
7. 校验成功后用事务写入 `data/chat_memory.db`。

激活目录位于：

```text
data/memory_activations/<实验ID>-activated-<时间>/
```

其中的 `activation.json` 记录边界、补炼结果、生产校验、快照和回滚命令。激活后新的微信消息会从重定位后的实时游标继续进入正常后台记忆流程，不会重新炼化整份历史。

处于激活或可回滚状态时，不要删除该激活目录：事件来源浏览和回滚都依赖其中的 `history_messages.jsonl`、`production_before.db` 和清单。

查看激活状态：

```bash
python -m app.plugins.builtin_chatbot.memory_activation status \
  --activation "data/memory_activations/<激活目录>"
```

若补炼候选阶段因断电或系统重启中断，可复用已提交的消息游标继续：

```bash
python -m app.plugins.builtin_chatbot.memory_activation resume \
  --activation "data/memory_activations/<激活目录>"
```

续跑会先执行 SQLite `quick_check`，再从候选库现有游标补齐实时消息及缺失
Embedding。若生产事务其实已经提交但清单未及时落盘，则根据已注册历史来源恢复
`active` 状态，不会重复写入。

### 6. 回滚激活

如果上线后发现整体质量不满意，可按 `activation.json` 中的命令回滚，例如：

```bash
python -m app.plugins.builtin_chatbot.memory_activation rollback \
  --activation "data/memory_activations/<激活目录>"
```

回滚会先额外备份回滚前的生产库，再从激活前快照恢复目标聊天的事件、人物、阶段和来源注册；其他聊天不受影响。一次激活回滚后状态会变为 `rolled_back`，不能重复执行同一次回滚。

### 7. 人物记忆历史重炼

人物记忆直接读取原始 JSONL，不依赖事件卡。工具先复制生产数据库为隔离候选库，
再按“原始观察 → 分期证据 → 最终投影”处理。生产库在 `--activate-only` 前不会被
修改。

首次从零运行：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --source "data/memory_activations/<历史激活目录>/history_messages.jsonl" \
  --live-source "data/chat_logs/大中华区慈善联合会.jsonl" \
  --workspace "data/person_rebuilds/charity-person-memory" \
  --concurrency 48 \
  --batch-messages 120 \
  --overlap 16 \
  --input-token-budget 24000 \
  --max-observations-per-batch 16 \
  --candidate-memory-value 0.58 \
  --budget-cny 200 \
  --exclude-sender-name "GG" \
  --exclude-sender-name "刘局" \
  --exclude-sender-name "ogg" \
  --exclude-sender-name "OCR" \
  --exclude-sender-name "总结Bot" \
  --exclude-sender-id "<旧机器人sender_id>" \
  --exclude-sender-id "<当前机器人sender_id>" \
  --fresh
```

`--exclude-sender-*` 可重复。当前机器人昵称在线会自动排除；历史导出还应明确列出
曾用机器人昵称和稳定 sender_id，避免把机器人回复总结成群员技能和关系。

工作目录包含：

- `candidate.db`：隔离候选库。
- `observation_batches/`：每个原始批次的完成标记、过滤统计和 observation ID。
- `period_summaries/`：按人物和半年分期的证据压缩。
- `consolidation/`：最终人物投影检查点。
- `cost.json`：按缓存命中输入、未命中输入和输出 Token 记录的实际估算费用；在途
  预留也计入硬预算，超过 `--budget-cny` 会停止提交新调用。
- `report.json`：人物覆盖、观察/事实/模式/快照数量及完整性检查。

进程中断后使用相同命令但去掉 `--fresh`，已完成的人物相关批次不会重复调用。同一
人物的长历史也按有限波次并发处理，不必等待该人物上一批完成后才提交下一批；只有
整波成功才推进处理水位。单批返回损坏 JSON 时会自动拆成更小核心区间，执行器每次
最多提交两倍并发数的有限波次，避免一个错误留下上千个在途请求。

在全群付费炼化前，建议先选一个高活跃成员做质量试验：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --source "data/memory_activations/<历史激活目录>/history_messages.jsonl" \
  --live-source "data/chat_logs/大中华区慈善联合会.jsonl" \
  --workspace "data/person_rebuilds/charity-person-memory-pilot" \
  --only-person "刘天琪" \
  --concurrency 48 \
  --max-observations-per-batch 16 \
  --candidate-memory-value 0.58 \
  --budget-cny 10 \
  --fresh
```

`--only-person` 可重复；它保留完整群聊原文作为上下文，但只建立指定人物的相关消息
链接和画像。此类局部报告会被 `--activate-only` 明确拒绝，不能误覆盖生产群的完整
人物库。满意后应换用新的完整 workspace 去掉 `--only-person` 重跑全群。

如果只修改了聚合、时效或快照规则，可复用观察账本：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --source "data/memory_activations/<历史激活目录>/history_messages.jsonl" \
  --live-source "data/chat_logs/大中华区慈善联合会.jsonl" \
  --workspace "data/person_rebuilds/charity-person-memory" \
  --exclude-sender-name "GG" \
  --exclude-sender-name "刘局" \
  --rebuild-derived
```

审核 `report.json`、候选库人物快照和原始证据后再激活：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --workspace "data/person_rebuilds/charity-person-memory" \
  --activate-only
```

激活会先创建 `production-before-person-memory-activation-<时间>.db`，再在单一事务中只
替换目标聊天的人物记忆表。候选人物 ID 会按稳定 sender_id/昵称映射到生产身份，原始
人物消息索引、候选、observation、事实证据和快照引用会同时重映射。
`activation.json` 保存备份位置和结果。

若完整候选构建期间群内仍有新消息，人物候选的最后一条消息通常会早于事件激活的
实时游标。应在人物激活后、重启 bot/web 前执行一次独立尾部补齐：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --workspace "data/person_rebuilds/charity-person-memory" \
  --live-source "data/chat_logs/大中华区慈善联合会.jsonl" \
  --catchup-live-only \
  --budget-cny 200
```

补齐器以候选中最后一条实时原文的“时间 + 发送者 + 内容”为边界，不依赖可能因
日志压缩而移动的物理行号；尾部使用独立、稳定的来源命名空间，因此中断后可用同一
命令续跑而不会重复观察。它会先建立
`production-before-person-memory-live-catchup-<时间>.db`，再补索引人物队列，只刷新实际
产生新观察的人物，最后要求人物关联队列、资料刷新队列均为 0 且 SQLite
`quick_check` 为 `ok`。结果保存在 `live_catchup.json`。

`--activate-only` 不会把“建议别名”自动当成确认身份，也不会替管理员接受未经核对
的合并建议。历史重炼期间已经由原始聊天确认的重复身份，应先在正式人物目录执行
合并，或在停机并完整备份 `chat_memory.db` 后用管理接口同步，再执行人物记忆激活。这样
后续实时消息使用旧昵称时仍会解析到同一稳定人物，而不是只让历史快照看起来合并。

### 8. 全群人物别名审计

历史导出和实时日志可能采用不同群名片；实时日志又可能缺少稳定 `sender_id`。因此
一次改名可能错误地产生两份人物资料。批量补别名不能只让模型看人物简介，而应使用
“高召回发现 → 单候选全库复核 → 跨人物消歧 → 人工规则复核 → 备份激活”：

1. 模型前先使用稳定 `sender_id`、`sender_remark` 和跨来源同消息指纹统一账号名、
   群昵称及改名前后的身份。
2. 逐一读取所有活动人物的直接 @、相邻接话和已验证事件，发现昵称候选。
3. 每个候选独立扫描全库原消息，优先采用多日期、多人直接 @ 与目标接话证据。
4. 完整昵称边界匹配，避免短名 `A`、`Y` 误命中 `@AAA...`、`@yuga-khan`。同一段
   文本同时命中完整 @ 昵称及其中的短别名时，只保留最长完整昵称的所有者。例如
   `@AAA 专业炒粉画图黄工` 不会再把“黄工”挂到旁边接话的人。
5. 跨人物冲突检查覆盖全部活动身份，包括尚无人物快照的占位身份；短候选若已包含在
   另一身份的较长确认昵称中，直接进入身份冲突，不允许靠相邻接话胜出。
6. 同一别名被分给多人时，只在精确 @ 形成明显优势时确定唯一所有者；相邻接话只作
   弱证据，不能覆盖上述确定性身份信号。
7. 改名前后身份先在隔离副本合并；证据不足、泛称或关系对象错配保留为
   `review/rejected`，不写入身份目录。
8. “熊猫、蒙古、坦克、蘑菇”等兼有普通词义的昵称标为上下文敏感：仍可用于明确
   身份解析，但增量人物索引只在 @、尊称或直接称呼语境中自动挂到人物，避免普通
   话题污染画像。

先做单人质量校准：

```bash
python -m app.plugins.builtin_chatbot.person_alias_audit \
  --chat "污合之众" \
  --database "data/chat_memory.db" \
  --candidate-database "data/person_rebuilds/<人物重炼目录>/candidate.db" \
  --workspace "data/person_alias_audits/<审计目录>-pilot" \
  --only-person "Danjs" \
  --concurrency 1 \
  --budget-cny 2 \
  --input-token-budget 12000
```

局部报告带有 `partial=true`，激活器会明确拒绝。若发现重复身份，先编写
`identity_merges.json`，再创建只用于审计的合并副本：

```bash
python -m app.plugins.builtin_chatbot.person_alias_audit \
  --chat "污合之众" \
  --database "data/chat_memory.db" \
  --workspace "data/person_alias_audits/<审计目录>" \
  --identity-merge-plan "data/person_alias_audits/<审计目录>/identity_merges.json" \
  --prepare-database "data/person_alias_audits/<审计目录>/audit-base.db"
```

在副本上完整运行。`--expected-profile-count` 应使用身份合并后的活动人物快照数：

```bash
python -m app.plugins.builtin_chatbot.person_alias_audit \
  --chat "污合之众" \
  --database "data/person_alias_audits/<审计目录>/audit-base.db" \
  --candidate-database "data/person_rebuilds/<人物重炼目录>/candidate.db" \
  --workspace "data/person_alias_audits/<审计目录>" \
  --identity-merge-plan "data/person_alias_audits/<审计目录>/identity_merges.json" \
  --alias-policy "data/person_alias_audits/<审计目录>/alias_policy.json" \
  --expected-profile-count 59 \
  --concurrency 12 \
  --budget-cny 10 \
  --input-token-budget 12000
```

`discovery/`、`verification/`、`cost.json` 和 `report.json` 都可断点复用。
`alias_policy.json` 只处理已经人工核对的例外，例如拒绝“某人的儿子”之类主客体
错配、确认确定性简写或把普通词型昵称设为上下文敏感。它不能绕过跨人物唯一性检查。

停掉 bot/web 写入后激活：

```bash
python -m app.plugins.builtin_chatbot.person_alias_audit \
  --chat "污合之众" \
  --database "data/chat_memory.db" \
  --workspace "data/person_alias_audits/<审计目录>" \
  --activate-only
```

激活前会对生产库中的全部活动/占位身份做别名和 `sender_id` 冲突预检，并创建
`production-before-alias-activation-<时间>.db`。合并操作、批量别名操作都有独立
人物审计记录。合并后应强制刷新涉及的人物快照，使旧名下观察立即进入目标人物：

```bash
python -m app.plugins.builtin_chatbot.person_alias_audit \
  --chat "污合之众" \
  --database "data/chat_memory.db" \
  --workspace "data/person_alias_audits/<审计目录>" \
  --refresh-merged-only \
  --budget-cny 5
```

完成条件是活动人物快照数符合预期、确认别名没有多所有者、关键旧名均唯一解析、
合并目标的待刷新观察为 0，并且 SQLite `quick_check` 为 `ok`。

若只发现一个确定的身份或别名错误，不需要重新炼化全群。先用 SQLite 在线备份创建
回滚点，再通过人物审计执行 `move_alias` 和/或 `merge_people`，最后只对受影响的
目标人物强制刷新一次快照。定点修复仍需满足别名唯一、待刷新观察为 0 和
`quick_check=ok`。

如整体质量不满意，先停止写入，再使用激活记录中的备份回滚：

```bash
python -m app.plugins.builtin_chatbot.person_memory_rebuild \
  --chat "大中华区慈善联合会" \
  --database "data/chat_memory.db" \
  --workspace "data/person_rebuilds/charity-person-memory" \
  --rollback-backup "data/person_rebuilds/charity-person-memory/production-before-person-memory-activation-<时间>.db"
```

回滚前工具会额外保存当前生产库安全副本。不要在 Mabobot 正写数据库时手工复制或
逐表替换；正式激活和回滚前应先停止 bot/web 进程。

## 六、清理与安全边界

设置页支持按聊天清除：

- 阶段记忆。
- 人物资料。
- 事件卡及群级阶段记忆。
- 全部记忆。

“清除事件”不会删除独立的人物观察；“清除人物资料”会清除该聊天的人物原始
消息索引、队列状态、候选、观察、事实版本、模式、关系、快照和人物审计。
“清除全部记忆”只影响所选聊天，并把游标
移到当前实时日志位置，之后只处理新消息。它与“回滚历史激活”不同：清除是主动
删除当前聊天记忆，回滚是恢复激活前快照。

不要在程序运行时手工改写 `data/chat_memory.db`。需要批量导入、替换或回退时，使用隔离实验和激活工具；需要修正少量事实时，使用记忆库浏览器的纠错机制。

## 七、相关但独立：回复后的无 @ 续聊

“无 @ 连续对话”不属于记忆系统。它只在 Bot 回复后的短窗口内，用 `builtin_chatbot.followup_judge` 和少量即时上下文判断候选消息是否在语义上承接当前人机话题；Judge 通过后的正式回复仍会使用上述正常聊天和记忆检索链路。

续聊 Judge 使用受控的对话行为分类，而不是只检查问号或再次 @。回答机器人、反馈机器人要求核实的结果、继续追问、纠正、澄清和实质补充都属于承接；纯致谢、附和、表情、新话题和群友旁聊不属于承接。群聊允许话题交错：插入的无关消息不会直接切断当前话题，Judge 会逐条选择最新的承接消息作为回复目标。
