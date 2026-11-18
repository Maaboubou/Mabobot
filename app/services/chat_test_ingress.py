"""Web input adapter for the Assistant's normal trigger, judge and formatting rules."""
from datetime import datetime
from app.assistant.chat_log import ChatLogManager
from app.assistant.handler import AssistantHandler
from app.core.event_bus import Event, EventType


class TestConversationLog:
    INTERNAL_ACTION_MARKERS = ChatLogManager.INTERNAL_ACTION_MARKERS
    _is_internal_action_message = ChatLogManager._is_internal_action_message

    def __init__(self, session):
        self.session = session

    def get_context_messages(self, chat_name, limit=50):
        rows = list(self.session.recent_reader(chat_name, limit))
        for message in self.session.messages:
            if message['role'] not in ('user', 'assistant'):
                continue
            stamp = datetime.fromtimestamp(message.get('time', 0)).strftime('%Y-%m-%d %H:%M:%S')
            rows.append({'time': stamp, 'sender': message.get('sender', '助手'),
                         'is_bot': message['role']=='assistant', 'content': message['content']})
        return sorted(rows, key=lambda row: str(row.get('time', '')))[-limit:]

    def count_messages(self, chat_name):
        reader = getattr(self.session.recent_reader, '__self__', None)
        count = getattr(reader, 'count_messages', None)
        archived = count(chat_name) if count else self.session.source_store.snapshot(chat_name)
        return archived + sum(m['role'] in ('user','assistant') for m in self.session.messages)


class WebChatIngress(AssistantHandler):
    def __init__(self, session, listener_bus):
        super().__init__(chat_log_manager=TestConversationLog(session))
        self.session = session
        self.listener_bus = listener_bus
        self.event_bus = self
        self.candidate = self.approved = None
        self.ingress = {}
        self.reason = ''

    def get_chat_ingress_state(self, chat_name):
        return self.ingress

    def _record_decision(self, kind, data):
        self.reason = data.get('reason') or ''
        self.session.event(kind, data)

    def _arm_followup_timer_locked(self, chat_name, session):
        # Web sends one simulated event per turn. Drain its merge window synchronously.
        self.candidate = (chat_name, session['anchor_id'], session['generation'])

    def _dispatch_followup_judge(self, chat_name, snapshot):
        self._run_followup_judge(chat_name, snapshot)

    def publish(self, event):
        self.approved = event

    def _decision_persona(self, role_name):
        return self.session.persona

    def _decision_rules(self):
        rules = self.session.prepared.get('decision_rules')
        return rules[int(self.session.variant=='draft')] if rules else super()._decision_rules()

    def _call_auxiliary_model(self, task_type, messages, **kwargs):
        from app.assistant.handler import get_llm_manager
        result = get_llm_manager().call(plugin_name='assistant', call_type=task_type, messages=messages, **kwargs)
        return str(result or '').strip()

    def decide(self, text, sender, kind, quote, quote_sender=''):
        lab = self.session
        group = lab.prepared.get('is_group', False)
        if group and kind=='mention':
            text = '@' + self._bot_display_name_for_chat(lab.source_chat) + ' ' + text
        data = {'chat_name':lab.source_chat, 'chat_type':'group' if group else 'private',
                'sender':sender, 'message':text, '_chat_seq':lab.turn_number}
        if kind=='quote':
            data.update(quote_content=quote, quote_nickname=quote_sender or self._bot_display_name_for_chat(lab.source_chat))
        event = Event(type=EventType.QUOTE_TEXT_MESSAGE_RECEIVED if kind=='quote' else EventType.TEXT_MESSAGE_RECEIVED,
                      source='web_test', data=data)
        self.reason = ''
        if self.listener_bus:
            listeners, _, reason = self.listener_bus.eligible_listeners(event)
            plugins = [item.plugin_name for item in listeners if item.owner_kind=='plugin' and item.propagation!='observe']
            if plugins:
                lab.event('plugin_preview', {'tool':'插件权限预演','eligible':plugins,'executed':False})
            if reason or not any(item.owner_kind=='core' and item.plugin_name=='assistant' for item in listeners):
                return event, text, reason or '此聊天未启用助手', False
        if self._is_sender_ignored(lab.source_chat, sender):
            return event, text, '发言人在助手忽略名单中', False
        self.ingress = {**data, 'sequence':lab.turn_number}
        text = self._build_quote_augmented_content(text, quote if kind=='quote' else '')
        reason, locked = self.decide_text_trigger(event, text)
        if self.candidate:
            args, self.candidate = self.candidate, None
            self._submit_followup_judge(*args)
            if self.approved:
                event, self.approved = self.approved, None
                reason, locked = self.decide_text_trigger(event, text)
            else:
                reason = self.reason or '续聊判断未通过'
        return event, text, (self.reason or reason) if reason else '', locked

    def remember_reply(self, event, parts, role_name):
        if not parts:
            return
        self._open_followup_window(chat_name=self.session.source_chat, chat_type=event.data['chat_type'],
            event=event, context_messages=self.chat_log_manager.get_context_messages(self.session.source_chat, 50),
            response='\n'.join(parts), role_name=role_name, automatic=bool(event.data.get('_followup_approved')))
        self._clear_judge_cooldowns(self.session.source_chat)
