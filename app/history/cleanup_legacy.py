"""Explicit, idempotent removal of retired generated-memory data.

Run cleanup() only while Web is stopped, or through the one-shot startup
marker before telemetry is loaded. cleanup_artifacts() may run after the old
engine is disabled. Exports are verified before removal; the archive and its
members are untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timezone

LEGACY_DIRECTORIES = (
    'chatbot_anchor_contexts', 'memory_activations', 'memory_correction_backups',
    'memory_experiments', 'person_alias_audits', 'person_rebuilds', 'memory_backups',
    'models/fastembed',
)


def is_memory_task(task: str) -> bool:
    return str(task).startswith(('assistant.memory_', 'builtin_chatbot.memory_'))


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value) -> None:
    temp = path.with_name(path.name + '.cleanup-tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def clean_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    removed = 0
    temp = path.with_name(path.name + '.cleanup-tmp')
    try:
        with path.open(encoding='utf-8') as source, temp.open('w', encoding='utf-8') as target:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)  # Fail closed on malformed data.
                task = row.get('key') or f"{row.get('plugin_name', '')}.{row.get('call_type', '')}"
                if is_memory_task(task):
                    removed += 1
                    continue
                row.pop('memory_trace', None)
                target.write(json.dumps(row, ensure_ascii=False) + '\n')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return removed


def cleanup_artifacts(project: Path) -> dict:
    data = project / 'data'
    report = {'exports': [], 'removed_paths': []}
    # The two historical WeChat exports remain usable for future reimports.
    for source in sorted((data / 'memory_experiments').glob('*/source_messages.jsonl')):
        manifest_path = source.parent / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        dest = data / 'chat_archive' / 'imports' / source.parent.name / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        sha = digest(source)
        if dest.exists():
            if digest(dest) != sha:
                raise ValueError(f'Conflicting preserved export: {dest}')
        else:
            shutil.copyfile(source, dest)
        if digest(dest) != sha:
            raise ValueError(f'Export verification failed: {dest}')
        item = {'chat_name': manifest['chat_name'], 'sha256': sha,
                'path': str(dest.relative_to(project)), 'bytes': dest.stat().st_size}
        write_json(dest.parent / 'manifest.json', item)
        report['exports'].append(item)

    paths = [data / name for name in LEGACY_DIRECTORIES]
    paths += list(data.glob('chat_memory.db*'))
    paths += list((data / 'plugins').glob('*/persistent/anchor_contexts'))
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            continue
        report['removed_paths'].append(str(path.relative_to(project)))
    previous = data / 'legacy_memory_artifact_cleanup_report.json'
    if report['exports'] or report['removed_paths']:
        write_json(previous, report)
    elif previous.exists():
        report = json.loads(previous.read_text(encoding='utf-8'))
    return report


def cleanup(project: Path) -> dict:
    data = project / 'data'
    report = {'completed_at': None, 'exports': [], 'usage': {}, 'removed_paths': []}
    report.update(cleanup_artifacts(project))

    usage = data / 'llm_usage_v2.sqlite3'
    if usage.exists():
        with sqlite3.connect(usage, timeout=60) as db:
            for table in ('usage_request', 'usage_aggregate'):
                tasks = [row[0] for row in db.execute(f'SELECT DISTINCT task FROM {table}') if is_memory_task(row[0])]
                count = 0
                for task in tasks:
                    count += db.execute(f'DELETE FROM {table} WHERE task=?', (task,)).rowcount
                report['usage'][table] = count
            db.commit()
            db.execute('VACUUM')
    for name in ('llm_call_history.jsonl', 'llm_chat_cache_diagnostics.jsonl'):
        report['usage'][name] = clean_jsonl(data / name)

    mappings_path = data / 'llm_mappings.json'
    mappings = json.loads(mappings_path.read_text(encoding='utf-8')) if mappings_path.exists() else {}
    for owner in ('assistant', 'builtin_chatbot'):
        if owner in mappings:
            mappings[owner] = {k: v for k, v in mappings[owner].items() if not k.startswith('memory_')}
    if mappings_path.exists():
        write_json(mappings_path, mappings)
    models_path = data / 'llm_models.json'
    if models_path.exists():
        models = json.loads(models_path.read_text(encoding='utf-8'))
        # A model reused by another task remains available.
        referenced = json.dumps(mappings)
        for model in ('codex-memory', 'codex-memory-high'):
            if json.dumps(model) not in referenced:
                models.pop(model, None)
        write_json(models_path, models)

    runtime = data / 'codex_runtime.db'
    if runtime.exists():
        with sqlite3.connect(runtime, timeout=60) as db:
            ids = [row[0] for row in db.execute('SELECT request_id, job_json FROM codex_jobs')
                   if json.loads(row[1]).get('profile') == 'memory']
            for request_id in ids:
                db.execute('DELETE FROM codex_job_events WHERE request_id=?', (request_id,))
                db.execute('DELETE FROM codex_jobs WHERE request_id=?', (request_id,))
            report['usage']['codex_jobs'] = len(ids)
            db.commit()
            db.execute('VACUUM')

    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_json(data / 'legacy_memory_cleanup_report.json', report)
    return report


def run_pending_cleanup(project: Path) -> None:
    marker = project / 'data' / '.remove_legacy_memory'
    if marker.exists():
        cleanup(project)
        marker.unlink()
