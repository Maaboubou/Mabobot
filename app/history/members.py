"""Nickname-based member directory; edits share the archive's durable journal."""
import hashlib
import json
import uuid
from functools import lru_cache
from pypinyin import lazy_pinyin


@lru_cache(maxsize=4096)
def nickname_sort_key(name):
    return ''.join(lazy_pinyin(name.strip())).casefold()


def nickname_id(chat, name):
    return 'nickname:' + hashlib.sha256((chat + '\0' + name).encode()).hexdigest()[:32]


def member_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS chat_senders(id TEXT PRIMARY KEY,chat TEXT NOT NULL,name TEXT NOT NULL,UNIQUE(chat,name));
        CREATE TABLE IF NOT EXISTS chat_members(id TEXT PRIMARY KEY,chat TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS members_chat ON chat_members(chat);
        CREATE TABLE IF NOT EXISTS chat_member_names(chat TEXT NOT NULL,name TEXT NOT NULL,member_id TEXT NOT NULL,PRIMARY KEY(chat,name,member_id));
        CREATE INDEX IF NOT EXISTS member_names_id ON chat_member_names(member_id);
        CREATE INDEX IF NOT EXISTS message_display_sender ON messages(chat,sender,occurred);
    ''')


def project_member(db, row):
    db.execute('INSERT OR REPLACE INTO chat_members VALUES(?,?,?)', (row['id'],row['chat'],json.dumps(row,ensure_ascii=False)))
    db.execute('DELETE FROM chat_member_names WHERE member_id=?', (row['id'],))
    # Tombstoned names suppress automatic rediscovery from old message senders.
    db.executemany('INSERT OR IGNORE INTO chat_member_names VALUES(?,?,?)',
                   [(row['chat'], name, row['id']) for name in [row['current_name'], *row['aliases']]])


class MemberDirectory:
    def _member(self, db, chat, identity):
        saved = db.execute('SELECT payload FROM chat_members WHERE chat=? AND id=?',(chat,identity)).fetchone()
        if saved:
            row = json.loads(saved[0])
            return None if row.get('deleted') else row
        observed = db.execute('SELECT name FROM chat_senders WHERE chat=? AND id=?',(chat,identity)).fetchone()
        if observed:
            return {'id':identity,'chat':chat,'current_name':observed[0],'aliases':[],'version':0}
        return None

    def _resolve_members(self, db, chat, name):
        rows = db.execute('SELECT DISTINCT m.payload FROM chat_member_names n JOIN chat_members m ON m.id=n.member_id WHERE n.chat=? AND n.name=?',(chat,name)).fetchall()
        if rows:
            return [item for row in rows if not (item:=json.loads(row[0])).get('deleted')]
        row = self._member(db,chat,nickname_id(chat,name))
        return [row] if row else []

    def members(self, chat, query='', offset=0, limit=50):
        if len(query)>160 or offset<0 or offset>1000000:
            raise ValueError('成员查询超出范围')
        limit=max(1,min(100,limit))
        with self.connection() as db:
            # This directory contains names only, never message bodies.
            named = [json.loads(row[0]) for row in db.execute('SELECT payload FROM chat_members WHERE chat=?',(chat,))]
            named = [row for row in named if not row.get('deleted')]
            observed = db.execute('''SELECT s.id,s.name FROM chat_senders s WHERE s.chat=?
                AND NOT EXISTS(SELECT 1 FROM chat_member_names n WHERE n.chat=s.chat AND n.name=s.name)
                AND NOT EXISTS(SELECT 1 FROM chat_members m WHERE m.id=s.id)''',(chat,)).fetchall()
        items=named+[{'id':row[0],'current_name':row[1],'aliases':[],'version':0} for row in observed]
        query=query.strip().casefold()
        items=[row for row in items if not query or any(query in name.casefold() for name in [row['current_name'],*row['aliases']])]
        items.sort(key=lambda row:(nickname_sort_key(row['current_name']),row['current_name'].casefold(),row['id']))
        total=len(items)
        return {'items':[{key:row[key] for key in ('id','current_name','aliases','version')} for row in items[offset:offset+limit]],
                'total':total,'next_offset':offset+limit if offset+limit<total else None}

    def resolve_members(self,chat,name):
        with self.connection() as db:
            return self._resolve_members(db,chat,name)

    def save_member(self,chat,current_name,aliases,*,identity=None,version=None,actor='administrator',merge=False):
        current_name=str(current_name).strip()
        if not current_name or len(current_name)>160 or not isinstance(aliases,list) or len(aliases)>60:
            raise ValueError('昵称不能为空，最多 160 字；别名最多 60 个')
        clean=[]
        for name in aliases:
            if not isinstance(name,str) or len(name.strip())>160:
                raise ValueError('每个别名最多 160 字')
            name=name.strip()
            if name and name!=current_name and name not in clean:clean.append(name)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE');self._recover_if_needed(db)
            identity=identity or nickname_id(chat,current_name)
            old=self._member(db,chat,identity)
            if version is not None and (old or {}).get('version',0)!=version:
                raise ValueError('成员已被修改，请刷新后再保存')
            if identity!=nickname_id(chat,current_name) and old is None:
                raise ValueError('当前聊天中没有这个成员')
            blocked=list((old or {}).get('removed_aliases',[]))
            if old and old['current_name']!=current_name and old['current_name'] not in clean:
                clean.append(old['current_name'])
            if merge and old:
                clean=list(dict.fromkeys([*old['aliases'],*clean]))
                current_name=old['current_name']
            elif old and actor=='administrator':
                blocked=list(dict.fromkeys([*blocked,*[name for name in old['aliases'] if name not in clean and name!=current_name]]))
                blocked=[name for name in blocked if name not in clean and name!=current_name]
            if actor=='codex' and any(name in blocked for name in clean):
                raise ValueError('该别名曾被手动移除，请使用现有昵称')
            clean=[name for name in clean if name!=current_name]
            if len(clean)>60:raise ValueError('别名最多 60 个')
            row={'id':identity,'chat':chat,'kind':'chat_member','current_name':current_name,'aliases':clean,
                 'removed_aliases':blocked,'version':(old or {}).get('version',0)+1,'actor':actor}
            if old and all(old.get(key)==row.get(key) for key in ('current_name','aliases','removed_aliases')):
                return old
            self._journal(db,[{'event_id':uuid.uuid4().hex,'message':row,'annotation':True}]);db.commit()
            return row

    def add_member_alias(self,chat,nickname,alias,*,identity=None):
        # Optimistic version control prevents overwriting a simultaneous edit.
        with self.connection() as db:
            matches=[self._member(db,chat,identity)] if identity else self._resolve_members(db,chat,nickname)
        matches=[row for row in matches if row]
        if len(matches)!=1:
            return {'status':'ambiguous_member' if matches else 'member_not_found','members':matches}
        row=matches[0]
        saved=self.save_member(chat,row['current_name'],[alias],identity=row['id'],version=row['version'],actor='codex',merge=True)
        return {'status':'saved','member':saved}

    def delete_member(self,chat,identity,*,version=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE');self._recover_if_needed(db)
            row=self._member(db,chat,identity)
            if row is None:return False
            if version is not None and row['version']!=version:
                raise ValueError('成员已被修改，请刷新后再删除')
            row.update(kind='chat_member',deleted=True,version=row['version']+1,actor='administrator')
            self._journal(db,[{'event_id':uuid.uuid4().hex,'message':row,'annotation':True}]);db.commit()
            return True

    def merge_member(self, chat, nickname, new_name, reason, *, identity=None):
        """Recognize a rename without altering original message authorship."""
        new_name = str(new_name or '').strip()
        reason = str(reason or '').strip()
        if not new_name or len(new_name) > 160 or not 2 <= len(reason) <= 300:
            raise ValueError('请提供新昵称（最多 160 字）和改名依据（2–300 字）')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._recover_if_needed(db)
            sources = [self._member(db, chat, identity)] if identity else self._resolve_members(db, chat, nickname)
            sources = [row for row in sources if row]
            targets = self._resolve_members(db, chat, new_name)
            if len(sources) != 1 or len(targets) > 1:
                return {'status': 'ambiguous_member' if sources else 'member_not_found', 'members': sources if len(sources) != 1 else targets}
            source = sources[0]
            target = targets[0] if targets else None
            if not target:
                deleted = db.execute('''SELECT m.payload FROM chat_member_names n JOIN chat_members m ON m.id=n.member_id
                    WHERE n.chat=? AND n.name=?''', (chat, new_name)).fetchall()
                if any(json.loads(row[0]).get('deleted') for row in deleted):
                    return {'status': 'member_deleted_manually', 'members': []}
            if source['current_name'] == new_name and (not target or source['id'] == target['id']):
                return {'status': 'already_merged', 'member': source}
            # Old passages must not move the current nickname backwards after
            # a later rename. Live data needs only names and message order.
            latest_old = db.execute('SELECT occurred,seq FROM messages WHERE chat=? AND sender=? AND deleted=0 ORDER BY occurred DESC,seq DESC LIMIT 1', (chat, source['current_name'])).fetchone()
            latest_new = db.execute('SELECT occurred,seq FROM messages WHERE chat=? AND sender=? AND deleted=0 ORDER BY occurred DESC,seq DESC LIMIT 1', (chat, new_name)).fetchone()
            if latest_old and latest_new and tuple(latest_new) < tuple(latest_old):
                return {'status': 'new_nickname_has_older_messages', 'member': source}
            participants = [source] + ([target] if target and target['id'] != source['id'] else [])
            names = list(dict.fromkeys(name for row in participants for name in [row['current_name'], *row['aliases']]))
            blocked = list(dict.fromkeys(name for row in participants for name in row.get('removed_aliases', [])))
            if any(name in blocked for name in [*names, new_name]):
                return {'status': 'alias_removed_manually', 'members': []}
            aliases = [name for name in names if name != new_name]
            if len(aliases) > 60:
                raise ValueError('合并后别名超过 60 个，请先整理成员别名')
            merged = {**source, 'kind': 'chat_member', 'current_name': new_name, 'aliases': aliases,
                      'removed_aliases': blocked, 'version': source['version'] + 1, 'actor': 'codex',
                      'rename_reason': reason}
            updates = [{**row, 'kind': 'chat_member', 'deleted': True, 'merged_into': source['id'],
                        'version': row['version'] + 1, 'actor': 'codex'} for row in participants[1:]]
            self._journal(db, [{'event_id': uuid.uuid4().hex, 'message': merged,
                                'annotation': True, 'member_updates': updates}])
            db.commit()
            return {'status': 'merged', 'member': merged}
