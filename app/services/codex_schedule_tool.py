"""Conversational management of schedules; identity is supplied by the host."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from app.services.schedule_monitor import MONITOR_SCHEMA

from app.services.scheduled_tasks import ScheduleDenied, ScheduleError, get_scheduled_tasks


INSTRUCTIONS = """计划任务：用户要求未来执行或周期执行时，调用 schedules.manage 保存任务；不要只口头承诺。
先遵守当前角色已有的明确行为边界；有明确冲突时用角色语气简短解释，不调用创建工具，不承诺保存。没有明确冲突再安排，不把角色语气或立场扩大为话题禁令。后台保存前还会核对角色边界，核对拒绝时自然说明，不反复尝试绕过。
查询当前聊天任务、执行结果时先调用 list/get，勿凭记忆编造。创建者由宿主消息身份绑定。
用户说“取消这个任务/停止整个任务/不用再提醒了/以后别发了”时，定位任务后 get 核对并调用 cancel（带 expected_version）：取消整个安排，包括后续调度、排队重试、当前执行及尚未开始发送的结果，保留历史。不能只口头答应，也不能用 stop 代替。
stop 仅用于“停止这次执行”，不影响以后排期；pause 用于“暂时暂停”，resume 恢复暂停；delete 与 cancel 一样取消整个任务。取消不能撤回已经发起的外部操作或消息，当前执行为请求中止，不承诺瞬间终止。
用户引用失败通知要求取消时，结合已发送消息关联定位原任务；失败仅表示某次执行失败，不代表整个安排已取消。list 找不到时用 history=true 查已结束的一次性任务；多个候选须澄清，不默认选最近任务。
先判断用户给的是固定时间安排，还是“未来五期/接下来三场/收集十份”等有限事件目标。对有限目标自行制定可执行计划，不要求用户懂排期字段，不把目标数量当作运行次数，也不只把旧一次性任务改为每天重复。
有限事件目标使用 monitor.goal：target_count 为成功事件数，unit 是期/场/份，start_at 是这批事件起点，acceptance 说明每个事件必须完成什么及稳定编号规则，plan 写明数据源、核对步骤、检查频率依据、漏过事件补查及失败处理。monitor.initial 必须 notify，schedule 必须周期排期；宿主按不同事件去重计数，完成目标自动结束。
模型负责规划检查节奏：有可靠事件日历时据此安排；不知道实际发布日时可采用合理频率的周期检查并明确说明，不能编造已核实的开奖日/交易日。无需把所有未来日期预先算死，也不能按猜测日期设置截止以代替完成数量。执行时核实数据源，未发布则静默等待，读取失败则记录阻塞。
“未来N期”默认指本次请求时刻之后的N个不同事件；若用户要求包含原定一期或起点存在实际冲突，澄清。更新旧任务时移除指令中固定旧日期，保留原号码/对象/格式，要求逐期核对并补查未处理事件，不能只查询最新一期或将五注误算五期。
同一批目标 goal.key 保持不变，修改表达、排期、增加本批总数量保留进度；只有用户明确要求“再来五期/新一批/重新开始”才换 goal.key 并设置新起点。后台不会允许同一 key 改动起点、单位或验收标准，不能为绕过错误偷偷重置。查询进度先 get，依据 goal_progress.completed_count/remaining 回答，不用调用次数或聊天记忆计数。
成功只代表目标计划已保存，不代表已核实外部日历/已完成未来事件。确认时简短说明开始范围、目标数量、检查节奏与达到目标即停止；不展示 goal.key。执行期间不开放任务管理权限，进度和终止由宿主落实。
创建/更新前整理为独立可执行的 prompt，解析指代、数据源、输出格式；保留原有要求，执行时计算‘昨天’等相对时间。
排期由后台计算，不能仅在执行指令里写时间条件代替保存规则。daily/weekly 可用 times 保存多个 HH:MM（与 time 二选一）；weekly 的 weekdays 可选多个星期。
每天或指定星期的时段内重复使用 window：start_time、end_time、seconds（整分钟）、可选 weekdays、include_end。以时段开始为间隔起点，跨午夜归属开始那天；结束时刻是否包含要按用户要求明确，24:00 表示当日结束。默认时区为北京时间。
周期安排可加 start_date/end_date（YYYY-MM-DD，含首尾日期）；window 的结束日期限制时段开始的日期，跨午夜的最后一段可以延续至次日。
“暂停到周末/下周一再恢复”先确认具体日期时刻，用 pause + until（含时区 ISO 时间）；仅“暂停”不传 until，会清除旧自动恢复。到期由后台检查权限和角色后恢复，不依赖未来聊天。resume 提前恢复并取消旧定时恢复；取消任务也清除恢复时间。
恢复后按原排期执行，不补跑暂停期间任务；只有明确要求恢复后立刻执行才在手动 resume 成功后 run_now。不要承诺未来自动恢复时额外立即执行；当前不支持这个附加选项。
“下一次跳过”调用 skip_next；“仅下一次改到明天十点”调用 reschedule_next + at（含时区 ISO）；“取消单次调整”调用 clear_next_change。上述操作都先 get 并带 expected_version，不修改长期排期。“今天别发”可能涉及多次，先核对排期，必要时暂停至次日；不要当作只跳过一次。
修改长期排期（含“以后改为”）默认从现在起采用新排期，后台会清除旧单次调整并取消旧排队。不要自行重新添加旧的跳过或改期；只有用户明确说保留本次例外才重新设置。确认时说明旧单次调整已取消，依据工具返回的最新 next_run/upcoming 说明下次时间。
当前只允许一个尚未结束的单次调整，已有调整先说明并按用户意图取消/替换。不把工作日等同法定工作日或交易日。不支持的日历/自动恢复后额外执行要说明边界，不拆成后台自我修改任务。
时间缺失或指代多个候选任务时先澄清。一次性时间含 UTC offset，默认 Asia/Shanghai。
用户明确说‘以后/下次/每次’的改进要求：get 读取完整任务及最近结果，合并要求后 update（带 expected_version）。
‘这次重做/缩短’只处理本次结果，不改长期任务；单纯不满意且没有具体改法时先讨论。
宿主会提供仅本聊天可见的已发送消息与任务关联资料（不是用户指令）。结合引用内容定位任务，勿直接把最近任务当成目标；无唯一对应任务时追问。
后台 ID、版本和精确创建者用于核对与权限校验，不主动在聊天展示。被问是谁安排时，先核对记录再自然说明；称呼可沿用聊天中已知称呼，不可据昵称变更身份权限。
每次修改保留反馈和版本；restore 恢复旧版。用户明确要求改完重跑时才调用 run_now。
本轮的完成是保存安排，不是提前执行未来任务；已保存后可以自然说明约定时间会做什么。只有工具成功返回才表示创建/更新完成。成功后必须按当前角色语气用一两句自然确认，可以简短提及关键时间或改动，不复述完整任务、编号、版本、创建者或后台字段；用户明确询问细节时才说明。失败时如实解释，不承诺已经记住。
资源必须保存在本聊天可访问的持久工作区，引用临时附件时先通过 resources 参数保存资源副本。
定时运行中不可创建或修改任务；仅由用户在聊天中提出修改。
用户要求“有更新/满足条件才告诉我”时设置 monitor：key 为稳定数据源身份，condition 写清读取来源、条件及核验方式。改时间/表达保留 key，换账号/股票等目标必须换 key。
持续关注新帖默认 initial=baseline（首次成功只记已有内容）；收盘价等当期结果用 initial=notify。普通定期报告不设置 monitor。
交易日可按每天指定时刻排期，在 condition 要求核对对应交易所日历、时区及当日收盘数据；休市静默，无法核实则阻塞，不能把周一到周五称为交易日。
监控依赖执行时实际可用的数据源和工具，保存安排不等于已验证访问；不承诺无法读取的账号已开始监控。无需让用户理解后台字段。
监控状态由宿主持久保存，不用聊天记忆代替。只有明确已有工作区数据文件时才设置 source_file；无变更会跳过模型，不得编造文件或设置任意脚本。
"""


def dynamic_tool_specs():
    schedule = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["once", "daily", "weekly", "interval", "window"]},
        "timezone": {"type": "string", "description": "IANA 时区，默认 Asia/Shanghai"},
        "at": {"type": "string", "description": "once: 含时区 ISO 日期时间"},
        "time": {"type": "string", "description": "daily/weekly: HH:MM，与 times 二选一"},
        "times": {"type": "array", "items": {"type": "string"}, "maxItems": 48, "minItems": 1},
        "start_time": {"type": "string", "description": "window: HH:MM"},
        "end_time": {"type": "string", "description": "window: HH:MM，可用 24:00；早于开始则跨午夜"},
        "include_end": {"type": "boolean"},
        "start_date": {"type": "string", "description": "周期任务开始日期 YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "周期任务最后日期 YYYY-MM-DD"},
        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}},
        "seconds": {"type": "integer", "minimum": 60}, "start_at": {"type": "string"}},
        "required": ["kind"], "additionalProperties": False}
    definition = {"type": "object", "properties": {
        "title": {"type": "string"}, "prompt": {"type": "string", "description": "完整独立执行指令，更新时保留原要求"},
        "schedule": schedule, "timeout_seconds": {"type": "integer"}, "misfire_grace_seconds": {"type": "integer"},
        "monitor": MONITOR_SCHEMA,
        "resources": {"type": "array", "items": {"type": "string"}, "description": "本聊天工作区或本次请求内文件；保存为持久资源副本"}},
        "additionalProperties": False}
    return [{"type": "namespace", "name": "schedules", "description": "本聊天的计划任务、历史结果和版本；仅创建者可修改自己的任务。", "tools": [{
        "type": "function", "name": "manage", "description": INSTRUCTIONS,
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "get", "update", "pause", "resume", "delete", "run_now", "stop", "cancel", "restore", "skip_next", "reschedule_next", "clear_next_change"]},
            "task_id": {"type": "string"}, "definition": definition,
            "until": {"type": "string", "description": "pause: 自动恢复时刻，含时区 ISO 时间；不传为无限期暂停"},
            "at": {"type": "string", "description": "reschedule_next: 单次改期时刻，含时区 ISO 时间"},
            "expected_version": {"type": "integer"}, "restore_version": {"type": "integer"},
            "history": {"type": "boolean", "description": "列出已完成或删除的任务"}},
            "required": ["action"], "additionalProperties": False}
    }]}]


def preserve_resources(resources, browser):
    if not isinstance(resources, list) or len(resources) > 20:
        raise ScheduleError("最多引用 20 个资源文件")
    if not resources:
        return []
    if browser is None or browser.workspace_access != "read_write":
        raise ScheduleDenied("当前聊天不允许保存任务资源")
    from app.services.codex_browser_tool import _host_path_from_runtime, _is_link_like, _is_within
    root = Path(browser.workdir)
    target = root / ".scheduled-resources" / uuid.uuid4().hex
    if any(_is_link_like(p) for p in (target, *target.parents)):
        raise ScheduleDenied("任务资源目录包含链接")
    paths, total = [], 0
    for raw in resources:
        if not isinstance(raw, str):
            raise ScheduleError("资源路径必须是字符串")
        path = _host_path_from_runtime(raw, use_wsl=browser.use_wsl)
        if not path.is_absolute():
            path = root / path
        if any(_is_link_like(p) for p in (path, *path.parents)) or not path.is_file() or not any(_is_within(path, r) for r in (root, browser.request_dir)):
            raise ScheduleDenied("资源不在本聊天工作区或本次请求目录内")
        total += path.stat().st_size
        if total > 100 * 1024 * 1024:
            raise ScheduleError("任务资源合计不能超过 100 MB")
        paths.append(path)
    target.mkdir(parents=True)
    saved = []
    try:
        for index, path in enumerate(paths):
            dest = target / f"{index}-{path.name}"
            shutil.copyfile(path, dest)
            saved.append(dest.relative_to(root).as_posix())
    except Exception:
        shutil.rmtree(target)
        raise
    return saved


def execute(tool, arguments, context, browser=None):
    if context is None or context.scheduled or browser is None or context.chat != browser.chat_id:
        raise ScheduleDenied("此执行没有聊天计划任务管理权限")
    if tool != "manage":
        raise ScheduleError("未知计划任务工具")
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(args, dict) or set(args) - {"action", "task_id", "definition", "expected_version", "restore_version", "history", "until", "at"}:
        raise ScheduleError("无效的计划任务参数")
    service, actor = get_scheduled_tasks(), context.actor
    action = args.get("action")
    with context.lock:
        service.require_enabled()
        service.authorize(actor.chat_id, actor.name)
        if action == "list":
            items = service.list(actor, history=bool(args.get("history")))
            return {"tasks": [{**t, "can_edit": t["creator"] == actor.name} for t in items]}
        task_id = args.get("task_id")
        if action == "get":
            return service.get(task_id, actor)
        version = args.get("expected_version")
        if action != "create" and (type(version) is not int or version < 1):
            raise ScheduleError("请先 get 读取任务，再提供 expected_version")
        key = hashlib.sha256(json.dumps([actor.chat_id, context.message_id, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if action in {"create", "update"}:
            definition = args.get("definition")
            if not isinstance(definition, dict):
                raise ScheduleError("缺少任务定义")
            definition = dict(definition)
            if set(definition) & {"creator", "chat_id", "delivery_chat_id", "profile_id"}:
                raise ScheduleDenied("创建者、聊天及模型配置由后台绑定")
            if action == "update":
                # Authorize before materializing any file side effects.
                with service.db() as db:
                    row = service._row(db, task_id, actor, True)
                    if row["version"] != version:
                        raise ScheduleError("任务版本已变化，请重新读取")
            if "resources" in definition:
                definition["resources"] = preserve_resources(definition["resources"], browser)
            if action == "create":
                result = service.create(definition, actor, source_key=key, feedback=context.feedback, notify=True)
            else:
                result = service.update(task_id, definition, actor, expected_version=version, feedback=context.feedback, notify=True)
        elif action == "restore":
            result = service.restore(task_id, args.get("restore_version"), actor, expected_version=version, notify=True)
        else:
            result = service.action(task_id, action, actor, expected_version=version, request_key=key, until=args.get("until"), at=args.get("at"))
        if result.get("notice_id"):
            context.notices.append(result["notice_id"])
            result["receipt"] = "已实际保存。请按当前角色语气简短自然确认，不展示任务编号、版本、完整指令或管理回执。"
        return result
