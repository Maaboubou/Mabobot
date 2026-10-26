"""Chat-bound task facts and explicit artifact reuse, independent of web search."""
import json
from app.services.work_coordination import get_work_coordinator


def dynamic_tool_specs():
    return [{"type": "namespace", "name": "work", "description": "本聊天的共享办事进度；查询不启动任务、不重发文件。", "tools": [
        {"type": "function", "name": "list", "description": "查询本聊天插件与助手最近的任务状态。先检查是否已有同一任务，再决定是否执行。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"type": "function", "name": "get", "description": "刷新指定任务的真实进度和交付状态；completed不等于已发送。", "inputSchema": {"type": "object", "properties": {"work_id": {"type": "string"}}, "required": ["work_id"], "additionalProperties": False}},
        {"type": "function", "name": "prepare_redelivery", "description": "仅当用户要求发送已有结果（含再发一次）时使用。复用本聊天任务的已有文件到当前输出目录，供正常交付流程发送；不重新执行任务。", "inputSchema": {"type": "object", "properties": {"work_id": {"type": "string"}}, "required": ["work_id"], "additionalProperties": False}},
    ]}]


def execute(tool, arguments, chat, *, context=None):
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError('work arguments must be an object')
    service = get_work_coordinator()
    if tool == 'prepare_redelivery' and set(arguments) == {'work_id'} and isinstance(arguments['work_id'], str):
        if context is None or context.chat_id != chat or context.workspace_access != 'read_write':
            raise ValueError('当前聊天不允许准备文件交付')
        from app.services.codex_browser_tool import _is_link_like, _is_within, _runtime_path
        if (_is_link_like(context.output_dir) or not _is_within(context.output_dir, context.request_dir)
                or _is_link_like(context.request_dir)):
            raise ValueError('输出目录不安全')
        from app.services.wechat_content_review import get_wechat_content_review, ContentReviewError
        try:
            paths = service.prepare_redelivery(chat, arguments['work_id'], context.request_id,
                                               context.output_dir, get_wechat_content_review())
        except ContentReviewError as exc:
            raise ValueError(str(exc)) from exc
        return {'status':'ready_for_delivery', 'work_id':arguments['work_id'],
                'files':[_runtime_path(path, context) for path in paths],
                'message':'已有文件准备完毕，将由本轮正常附件流程交付；无需重新生成或下载'}
    if tool == 'list' and not arguments:
        records = service.recent(chat)
    elif tool == 'get' and set(arguments) == {'work_id'} and isinstance(arguments['work_id'], str):
        record = service.get(chat, arguments['work_id'])
        records = [record] if record else []
    else:
        raise ValueError('invalid work tool or arguments')
    return {'tasks': [{k: w[k] for k in ('id', 'capability', 'subject', 'parameters', 'title', 'status', 'message', 'delivered', 'deliveries')} for w in records]}
