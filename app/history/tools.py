"""Chat-scoped lookup and local files, with compact per-call output."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
from functools import lru_cache
import json
import threading
import sqlite3
import time
from typing import Any

from app.history.store import ArchiveStore, encode


from app.assistant.prompt_defaults import DEFAULT_RULES
HISTORY_INSTRUCTIONS = DEFAULT_RULES["history"]["text"]


@lru_cache(maxsize=1)
def get_archive() -> ArchiveStore:
    return ArchiveStore()


_sessions = OrderedDict()
_sessions_lock = threading.Lock()


def history_session_for(chat: str, logical_id: str, request_id: str, *, max_calls: int = 0, max_bytes: int = 0, files_provider=None):
    """Share a logical reply's budget across protocol-repair/retry threads."""
    key = (chat, logical_id or request_id)
    now = time.monotonic()
    with _sessions_lock:
        for expired in [key for key, (used, _) in _sessions.items() if now - used > 1800]:
            _sessions.pop(expired, None)
        existing = _sessions.pop(key, None)
        session = existing[1] if existing else HistorySession(
            chat, logical_id or request_id, max_calls=max(0, int(max_calls)),
            max_bytes=max(0, int(max_bytes)))
        if existing:
            # The new thread lacks prior tool text. Keep budget and pointers,
            # but permit bounded rereads in this context window.
            with session.lock:
                session.returned.clear()
        session.files_provider = files_provider
        _sessions[key] = (now, session)
        while len(_sessions) > 128:
            _sessions.popitem(last=False)
        return session


def dynamic_tool_specs() -> list[dict]:
    common = {"sender": {"type": "string", "description": "Exact displayed name or sender ID."},
              "sender_ref": {"type": "string", "description": "Short sender reference from a previous result; use instead of sender to distinguish identical names."},
              "start": {"type": "string", "description": "Inclusive ISO date/time; defaults to Asia/Shanghai."},
              "end": {"type": "string", "description": "Exclusive ISO date/time."},
              "cursor": {"type": "string", "description": "Opaque pagination cursor returned by this tool."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Number of matches; default 6, increase for broader research."},
              "chars": {"type": "integer", "minimum": 80, "maximum": 12000, "description": "Characters per message; increase when full context is needed."}}
    def tool(name, description, properties, required=()):
        return {"type": "function", "name": name, "description": description,
                "inputSchema": {"type": "object", "additionalProperties": False,
                                "properties": properties, "required": list(required)}}
    return [{"type": "namespace", "name": "history",
             "description": "Search current-chat history, open complete local files for Python/SQL analysis, and maintain nickname aliases.",
             "tools": [
                 tool('files','Open complete current-chat archive.sqlite3, members.json and a standalone Python helper in your workspace. Use native shell/Python/SQL for broad research or whole-history statistics; return only findings. No browser needed.', {}),
                 tool('people','List or search current nicknames and aliases; omit query to list members.',
                      {'query':{'type':'string','maxLength':160},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100}}),
                 tool('add_alias','Directly save a newly discovered nickname alias. Specify nickname or a member_ref from people. Additive; cannot rename or delete.',
                      {'nickname':{'type':'string','maxLength':160},'member_ref':{'type':'string'},'alias':{'type':'string','minLength':1,'maxLength':160}},('alias',)),
                 tool('merge_member','When a rename is clearly established, set the new current nickname and merge its existing member entry. Preserve all old names and original messages. Specify nickname or member_ref and a concise reason.',
                      {'nickname':{'type':'string','maxLength':160},'member_ref':{'type':'string'},'new_name':{'type':'string','minLength':1,'maxLength':160},'reason':{'type':'string','minLength':2,'maxLength':300}},('new_name','reason')),
                 tool("search", "Search literal text; defaults to 6 excerpts, increase limit/chars as needed. Use files for batch queries and statistics.",
                      {**common, "query": {"type": "string", "minLength": 1, "maxLength": 200}}, ("query",)),
                 tool("range", "Browse a date range in pages, newest first. A page is not a complete summary.", common),
                 tool("read", "Expand a short message reference and nearby messages. Long content has a next_offset.",
                      {"ref": {"type": "string"}, "before": {"type": "integer", "minimum": 0, "maximum": 50},
                       "after": {"type": "integer", "minimum": 0, "maximum": 50},
                       "chars": common['chars'], "offset": {"type": "integer", "minimum": 0}}, ("ref",)),
             ]}]


@dataclass
class HistorySession:
    chat: str
    request_id: str
    store: Any = None
    # Zero disables the cumulative cap. Keep individual results compact while
    # allowing the model to keep working or open files when the task needs it.
    max_calls: int = 0
    max_bytes: int = 0
    response_bytes: int = 48000
    files_provider: Any = None
    calls: int = 0
    used_bytes: int = 0
    refs: dict = field(default_factory=dict)
    senders: dict = field(default_factory=dict)
    cursors: dict = field(default_factory=dict)
    returned: set = field(default_factory=set)
    member_refs: dict = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)

    def _ref(self, identity: str) -> str:
        if identity not in self.refs:
            self.refs[identity] = f"m{len(self.refs) + 1}"
        return self.refs[identity]

    def execute(self, name: str, arguments: Any) -> dict:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("history arguments must be an object")
        allowed = {"search": {"query", "sender", "sender_ref", "start", "end", "cursor", "limit", "chars"},
                   "range": {"sender", "sender_ref", "start", "end", "cursor", "limit", "chars"},
                   "read": {"ref", "before", "after", "offset", "chars"},
                   'files':set(), 'people':{'query','offset','limit'},'add_alias':{'nickname','member_ref','alias'},
                   'merge_member':{'nickname','member_ref','new_name','reason'}}
        if name not in allowed or set(arguments) - allowed[name]:
            raise ValueError("unsupported history tool or parameters")
        with self.lock:
            store = self.store or get_archive()
            if name == 'files':
                if self.files_provider is None:
                    return {'status':'unavailable','message':'Local archive files are unavailable in this runtime; use history search/read.'}
                result = {'status':'ok', **self.files_provider(store, self.chat)}
                store.record_lookup(self.chat,self.request_id,name,{'status':'ok','snapshot':result['snapshot'],
                    'messages':result['messages'],'incremental':result['incremental'],'updated_rows':result['updated_rows']})
                return result
            if ((self.max_calls and self.calls >= self.max_calls)
                    or (self.max_bytes and self.used_bytes >= self.max_bytes - 256)):
                store.record_lookup(self.chat,self.request_id,name,{'status':'budget_exhausted','filters':arguments})
                return {"status": "budget_exhausted", "coverage": "partial", "message": "Use history.files to continue local analysis; do not claim complete history coverage from excerpts."}
            self.calls += 1
            if name in {'people','add_alias','merge_member'}:
                return self._members_tool(store,name,arguments)
            options = dict(arguments)
            chars = options.pop('chars', None)
            limit = max(1, min(100, int(options.pop('limit', 6))))
            sender_ref = options.pop("sender_ref", "")
            if sender_ref:
                if options.get("sender"):
                    raise ValueError("use sender or sender_ref, not both")
                sender_id = next((key for key, ref in self.senders.items() if ref == sender_ref), None)
                if sender_id is None:
                    raise ValueError("unknown sender reference in this reply")
                options["sender"] = sender_id
            result = {"status": "ok", "messages": [], "coverage": "partial"}
            rows = []
            offset = 0
            if name == "read":
                identity = next((key for key, ref in self.refs.items() if ref == options.get("ref")), None)
                if identity is None:
                    raise ValueError("unknown reference in this reply")
                offset = int(options.get("offset", 0))
                if offset < 0:
                    raise ValueError("invalid content offset")
                rows = store.read(self.chat, identity, before=min(50, max(0, int(options.get("before", 0)))),
                                  after=min(50, max(0, int(options.get("after", 0)))))
            else:
                cursor = options.pop("cursor", "")
                if cursor:
                    saved = self.cursors.get(cursor)
                    if not saved:
                        raise ValueError("unknown pagination cursor")
                    saved_options, page, snapshot = saved
                    if options and options != saved_options:
                        raise ValueError("cursor filters cannot change")
                    if store.snapshot(self.chat) != snapshot:
                        return {"status": "snapshot_changed", "message": "Repeat the search; archive changed while paging."}
                    options = dict(saved_options)
                else:
                    page = 0
                if name == "search" and not str(options.get("query", "")).strip():
                    raise ValueError("search query is required")
                try:
                    search = store.search(self.chat, **options, offset=page, limit=limit)
                except sqlite3.OperationalError as exc:
                    if "interrupted" not in str(exc):
                        raise
                    return {"status": "query_too_broad", "coverage": "unknown", "message": "Narrow the date range or sender; the scan budget was reached."}
                result.update(cache_hit=search["cache_hit"], incremental=search.get("incremental", False), coverage=search["coverage"])
                if search.get('status')=='ambiguous_sender':
                    result.update(status='ambiguous_sender',message='昵称对应多个成员，请缩小到当前昵称。')
                for identity in search["ids"]:
                    rows.extend(store.read(self.chat, identity))
            result["calls_remaining"] = self.max_calls - self.calls if self.max_calls else None
            output_budget = self._output_budget()
            # Reserve room for continuation metadata before fitting any rows.
            if name != 'read':
                result['next_cursor'] = None
            delivered = []
            for row in rows:
                ref = self._ref(row["id"])
                content_offset = offset if name == "read" and ref == arguments.get("ref") else 0
                text = row["content"]
                if name != "read":
                    query = str(options.get("query", ""))
                    hit = text.lower().find(query.lower()) if query else 0
                    content_offset = max(0, hit - 60)
                max_chars = max(80, min(12000, int(chars))) if chars is not None else (1200 if name == 'read' else 180)
                excerpt = text[content_offset:content_offset + max_chars]
                part = {"ref": ref, "time": str(row.get("time") or row["occurred_at"])[:50],
                        "sender": row["sender"][:160], "kind": str(row.get("message_type") or "text")[:40],
                        "is_bot": row.get("is_bot"), "content": excerpt, "offset": content_offset}
                if row.get("sender_id"):
                    sender_id = str(row["sender_id"])
                    if sender_id not in self.senders:
                        self.senders[sender_id] = f"s{len(self.senders)+1}"
                    part["sender_ref"] = self.senders[sender_id]
                else:
                    part["identity_unknown"] = True
                if row.get("correction"):
                    part["correction"] = row["correction"]
                if row.get("evidence_only"):
                    part["evidence_only"] = True
                enrichment = row.get("image_enrichment") or {}
                if enrichment.get("description"):
                    part["image_description"] = str(enrichment["description"])[:300]
                key = (row["id"], row["version"], content_offset, len(excerpt))
                if key in self.returned:
                    part = {"ref": ref, "already_returned": True, "offset": content_offset}
                    if len(encode({**result, "messages": result["messages"] + [part]}).encode()) > output_budget - 128:
                        result['truncated'] = True
                        break
                else:
                    if content_offset + len(excerpt) < len(text):
                        part["next_offset"] = content_offset + len(excerpt)
                    # Clip individual text until the entire envelope fits. This
                    # preserves valid JSON and reports the exact continuation.
                    while len(encode({**result, "messages": result["messages"] + [part]}).encode()) > output_budget - 128:
                        if len(part.get("content", "")) < 16:
                            part = None
                            break
                        part["content"] = part["content"][:len(part["content"]) // 2]
                        part["next_offset"] = content_offset + len(part["content"])
                    if part is None:
                        result["truncated"] = True
                        break
                    self.returned.add((row["id"], row["version"], content_offset, len(part["content"])))
                result["messages"].append(part)
                delivered.append(row['id'])
            if name != 'read':
                # Advance by delivered messages, never by clipped-away rows.
                if len(delivered) < len(rows) or search['next_offset'] is not None:
                    next_cursor = f'p{len(self.cursors)+1}'
                    self.cursors[next_cursor] = (dict(options), page + len(delivered), search['snapshot'])
                    result['next_cursor'] = next_cursor
                else:
                    result.pop('next_cursor', None)
            elif len(delivered) < len(rows):
                result['message'] = 'Some neighbors did not fit; use history.files for complete local context.'
            if not rows and result['status']=='ok':
                result["status"] = "no_match"
            size = len(encode(result).encode())
            self.used_bytes += size
            store.record_lookup(self.chat, self.request_id, name,
                                {"filters": arguments, "source_ids": delivered, "status":result['status'],
                                 "returned_bytes": size, "calls": self.calls,
                                 "cache_hit": result.get("cache_hit", False), "truncated": result.get("truncated", False)})
            return result

    def _output_budget(self):
        return min(self.response_bytes, self.max_bytes-self.used_bytes) if self.max_bytes else self.response_bytes

    def _members_tool(self,store,name,arguments):
        if name=='people':
            query=str(arguments.get('query') or '').strip()
            found=store.members(self.chat,query=query,offset=int(arguments.get('offset',0)),limit=int(arguments.get('limit',30)))
            result={'status':'ok','members':[],'more':found['next_offset'] is not None,'total':found['total'],'next_offset':found['next_offset']}
            rows=found['items']
        else:
            nickname=str(arguments.get('nickname') or '').strip()
            reference=arguments.get('member_ref')
            if bool(nickname)==bool(reference):raise ValueError('请指定 nickname 或 member_ref')
            identity=None
            if reference:
                identity=next((key for key,value in self.member_refs.items() if value==reference),None)
                if identity is None:raise ValueError('无效的本次成员引用')
            if name == 'merge_member':
                found=store.merge_member(self.chat,nickname,arguments.get('new_name'),arguments.get('reason'),identity=identity)
            else:
                found=store.add_member_alias(self.chat,nickname,arguments.get('alias'),identity=identity)
            result={'status':found['status'],'members':[]}
            rows=[found['member']] if found.get('member') else found.get('members',[])[:6]
        for row in rows:
            ref=self.member_refs.setdefault(row['id'],f'n{len(self.member_refs)+1}')
            item={'member_ref':ref,'current_name':row['current_name'],'aliases':list(row['aliases'])}
            while len(encode({**result,'members':result['members']+[item]}).encode())>self._output_budget()-128 and item['aliases']:
                item['aliases'].pop();item['aliases_truncated']=True
            if len(encode({**result,'members':result['members']+[item]}).encode())>self._output_budget()-128:
                result['truncated']=True;break
            result['members'].append(item)
        if name == 'people' and len(result['members']) < len(rows):
            result.update(more=True,next_offset=int(arguments.get('offset',0))+len(result['members']))
        size=len(encode(result).encode());self.used_bytes+=size
        store.record_lookup(self.chat,self.request_id,name,{'filters':arguments,'returned_bytes':size,'calls':self.calls})
        return result
