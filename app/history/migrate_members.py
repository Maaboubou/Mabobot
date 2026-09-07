"""Import the old nickname directory without invoking any model."""
import argparse
import json
import sqlite3
from pathlib import Path
from app.history.store import ArchiveStore
from app.history.members import nickname_id


def migrate_members(store, legacy):
    db=sqlite3.connect(Path(legacy).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        identities={row['id']:dict(row) for row in db.execute('SELECT * FROM memory_person_identities')}
        grouped={}
        for alias in db.execute("SELECT * FROM memory_person_aliases WHERE status='confirmed' ORDER BY id"):
            person=identities.get(alias['person_id']);seen=set()
            while person and person['status']!='active' and person.get('merged_into_person_id') and person['id'] not in seen:
                seen.add(person['id']);person=identities.get(person['merged_into_person_id'])
            if not person or person['status']!='active':continue
            key=(person['chat_name'],person['id'])
            item=grouped.setdefault(key,{'canonical':person['canonical_name'],'names':[]})
            for name in [person['canonical_name'],alias['alias_name']]:
                name=name.strip()
                if name and name not in item['names']:item['names'].append(name)
        report={'members':0,'aliases':0,'preserved_edits':0,'chats':{}}
        for (chat,legacy_id),item in grouped.items():
            identity='legacy-member:'+str(legacy_id)
            with store.connection() as connection:
                saved=connection.execute('SELECT payload FROM chat_members WHERE chat=? AND id=?',(chat,identity)).fetchone()
                existing=json.loads(saved[0]) if saved else None
                if existing and existing.get('actor')!='legacy_import':
                    report['preserved_edits']+=1;continue
                placeholders=','.join('?' for _ in item['names'])
                latest=connection.execute('SELECT sender FROM messages WHERE chat=? AND sender IN ('+placeholders+') ORDER BY occurred DESC,seq DESC LIMIT 1',(chat,*item['names'])).fetchone()
            current=latest[0] if latest else item['canonical']
            aliases=[name for name in item['names'] if name!=current]
            # Annotation identity is deliberately separate from live nicknames.
            # The old platform IDs are preserved in raw data but not required here.
            payload={'kind':'chat_member','current_name':current,'aliases':aliases,'removed_aliases':[],
                     'version':1,'actor':'legacy_import','legacy_person_id':legacy_id}
            if not existing:
                store.annotate(chat,identity,payload)
            report['members']+=1;report['aliases']+=len(aliases)
            report['chats'][chat]=report['chats'].get(chat,0)+1
        return report
    finally:db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('data/chat_archive'))
    parser.add_argument('--legacy',type=Path,default=Path('data/chat_memory.db'))
    args=parser.parse_args()
    store=ArchiveStore(args.root)
    report=migrate_members(store,args.legacy)
    (store.root/'member-migration-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
