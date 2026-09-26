"""Project-wide inventory and preview-bound, recoverable managed-cache cleanup."""
from __future__ import annotations

import heapq
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class StorageError(RuntimeError):
    pass


class StorageService:
    VERSION = 2
    LABELS = {
        'attachments': '聊天附件', 'generated': '生成内容', 'chat_archive': '聊天档案与数据库',
        'models': '本地模型', 'managed_plugins': '插件数据', 'cache': '插件缓存与临时文件',
        'diagnostics': '日志与诊断', 'backups': '备份', 'trash': '回收区（含清单）',
        'environment': '运行环境', 'source': '程序与版本记录', 'temporary': '其他临时文件',
        'other': '其他文件',
    }
    # Longest prefix wins. Every file belongs to exactly one category.
    PATHS = {
        'attachments': ['tmp/wechat_files', 'mabowx文件下载'],
        'generated': ['tmp/images/codex', 'data/daily_reports', 'data/weekly_reports', 'data/chat_summaries', 'data/codex_chat_scopes'],
        'chat_archive': ['data/chat_archive', 'data/chat_logs'],
        'models': ['data/models'], 'managed_plugins': ['data/plugins'],
        'cache': ['tmp/plugins'],
        'diagnostics': ['logs', 'mabowx_logs', 'wxauto_logs', 'data/maintenance_reports',
                        'data/llm_call_history.jsonl'],
        'backups': ['data/system_backups', 'data/backups', 'data/migration_backups'],
        'trash': ['data/system_trash'],
        'environment': ['.venv', 'venv', 'node_modules', 'web/node_modules'],
        'source': ['.git', 'app', 'web', 'tests', 'docs', 'scripts', 'mabowx', 'mabobot_launcher'],
        'temporary': ['tmp', 'scratch', '.pytest_cache', '.ruff_cache', '__pycache__'],
    }

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.cache_path = self.project_root / 'data/storage_inventory.json'
        self.trash_root = self.project_root / 'data/system_trash'
        self._lock = threading.RLock()
        self._previews: dict[str, dict] = {}
        self._prefixes = sorted(((p, k) for k, paths in self.PATHS.items() for p in paths),
                                key=lambda pair: len(pair[0]), reverse=True)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _safe(self, relative: str) -> Path:
        """Reject links in any component, including links that stay inside the project."""
        path = Path(relative)
        if path.is_absolute() or path.root or path.drive or not path.parts or '..' in path.parts or '\\' in relative:
            raise StorageError('无效的存储路径')
        current = self.project_root
        for part in path.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if self._link_info(info):
                raise StorageError('不允许操作符号链接或目录联接')
        return current

    @staticmethod
    def _link_info(info):
        # Python < 3.12 has no is_junction; Windows reparse attributes cover it.
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    @staticmethod
    def _write(path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _walk(self, root: Path, errors: list, operation=None):
        """Use scandir metadata; never resolve every file on a Windows mount."""
        stack = [root]
        while stack:
            folder = stack.pop()
            if operation:
                operation.check_cancelled()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if self._link_info(info):
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                stack.append(Path(entry.path))
                            elif stat.S_ISREG(info.st_mode):
                                yield Path(entry.path), info
                        except OSError:
                            errors.append(str(Path(entry.path).relative_to(self.project_root)))
            except OSError:
                errors.append(str(folder.relative_to(self.project_root)))

    @staticmethod
    def _managed(relative):
        parts = Path(relative).parts
        return (len(parts) >= 3 and parts[:2] == ('tmp', 'plugins')) or (
            len(parts) >= 5 and parts[:2] == ('data', 'plugins') and parts[3] == 'cache')

    def _category(self, relative):
        if self._managed(relative):
            return 'cache'
        parts = Path(relative).parts
        if len(parts) >= 5 and parts[:2] == ('data', 'plugins') and parts[3] == 'generated':
            return 'generated'
        for prefix, category in self._prefixes:
            if relative == prefix or relative.startswith(prefix + '/'):
                return category
        if relative.startswith('data/') and re.search(r'\.(db|sqlite3?)(-(wal|shm|journal))?$', relative):
            return 'chat_archive'
        return 'other'

    def scan(self, operation: Any = None) -> dict:
        with self._lock:
            rows = {key: dict(category=key, label=label, bytes=0, files=0, largest=[], paths={})
                    for key, label in self.LABELS.items()}
            errors = []
            count, last_update = 0, time.monotonic()
            for path, info in self._walk(self.project_root, errors, operation):
                relative = path.relative_to(self.project_root).as_posix()
                if relative == 'data/storage_inventory.json' or relative.startswith('data/storage_inventory.json.'):
                    continue
                row = rows[self._category(relative)]
                row['bytes'] += info.st_size
                row['files'] += 1
                heapq.heappush(row['largest'], (info.st_size, relative))
                if len(row['largest']) > 20:
                    heapq.heappop(row['largest'])
                parts = Path(relative).parts
                group = '/'.join(parts[:3] if parts[:2] == ('data', 'plugins') else parts[:2])
                row['paths'][group] = row['paths'].get(group, 0) + info.st_size
                count += 1
                if operation and time.monotonic() - last_update > 1:
                    operation.progress(0, f'已统计 {count:,} 个文件')
                    last_update = time.monotonic()
            for row in rows.values():
                row['largest'] = [dict(path=p, bytes=s) for s, p in sorted(row['largest'], reverse=True)]
                row['paths'] = [dict(path=p, bytes=s) for p, s in sorted(row['paths'].items(), key=lambda x: x[1], reverse=True)[:20]]
            result = dict(version=self.VERSION, scanned_at=self._now(), scan_required=False,
                          categories=sorted(rows.values(), key=lambda row: row['bytes'], reverse=True),
                          total_classified_bytes=sum(row['bytes'] for row in rows.values()),
                          total_classified_files=count, incomplete=bool(errors),
                          unreadable_count=len(errors), unreadable_paths=errors[:20])
            self._write(self._safe('data/storage_inventory.json'), result)
            return result

    def overview(self):
        try:
            result = json.loads(self._safe('data/storage_inventory.json').read_text(encoding='utf-8'))
            if result.get('version') != self.VERSION:
                raise ValueError('outdated inventory')
        except (OSError, ValueError):
            result = dict(scanned_at=None, categories=[], scan_required=True)
        usage = shutil.disk_usage(self.project_root)
        result['disk'] = dict(total=usage.total, free=usage.free, used=usage.used)
        result['trash'] = self.list_trash()
        return result

    @staticmethod
    def _fingerprint(info):
        return [info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino]

    def cleanup_preview(self, retention_days=7, operation=None):
        if not 1 <= int(retention_days) <= 3650:
            raise StorageError('保留天数必须在 1 到 3650 之间')
        with self._lock:
            cutoff = time.time() - int(retention_days) * 86400
            roots = [self._safe('tmp/plugins')]
            plugins = self._safe('data/plugins')
            if plugins.exists():
                for plugin in plugins.iterdir():
                    try:
                        candidate = self._safe((plugin / 'cache').relative_to(self.project_root).as_posix())
                        if candidate.is_dir():
                            roots.append(candidate)
                    except StorageError:
                        continue
            files, errors = [], []
            for root in roots:
                if not root.exists():
                    continue
                for path, info in self._walk(root, errors, operation):
                    if info.st_mtime <= cutoff:
                        files.append(dict(path=path.relative_to(self.project_root).as_posix(),
                                          bytes=info.st_size, fingerprint=self._fingerprint(path.lstat())))
            files.sort(key=lambda item: item['bytes'], reverse=True)
            token = uuid.uuid4().hex
            now = time.time()
            self._previews = {key: value for key, value in self._previews.items() if value['expires'] > now}
            if len(self._previews) >= 10:
                self._previews.pop(next(iter(self._previews)))
            self._previews[token] = dict(files=files, expires=now + 1800, retention_days=int(retention_days))
            return dict(preview_id=token, retention_days=int(retention_days), files=len(files),
                        bytes=sum(item['bytes'] for item in files),
                        sample=[dict(path=f['path'], bytes=f['bytes']) for f in files[:100]],
                        expires_at=datetime.fromtimestamp(now + 1800, timezone.utc).isoformat(),
                        unreadable_count=len(errors), recoverable=True)

    def _invalidate(self):
        self._safe('data/storage_inventory.json').unlink(missing_ok=True)

    def cleanup_managed(self, *, preview_id: str, confirmation: str):
        if confirmation != '清理托管缓存':
            raise StorageError('请确认将预览中的缓存移入回收区')
        with self._lock:
            preview = self._previews.pop(preview_id, None)
            if not preview or preview['expires'] <= time.time():
                raise StorageError('预览已过期或已使用，请重新预览')
            batch_id = uuid.uuid4().hex
            batch = self._safe('data/system_trash/' + batch_id)
            manifest = dict(id=batch_id, created_at=self._now(), files=preview['files'])
            self._write(batch / 'manifest.json', manifest)
            moved, size, skipped, errors = 0, 0, 0, []
            moved_rows = []
            self._invalidate()
            for item in preview['files']:
                try:
                    source = self._safe(item['path'])
                    info = source.lstat()
                    if not self._managed(item['path']) or not stat.S_ISREG(info.st_mode) or self._fingerprint(info) != item['fingerprint']:
                        skipped += 1
                        continue
                    destination = self._safe(f'data/system_trash/{batch_id}/files/' + item['path'])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(source, destination)
                    moved += 1
                    moved_rows.append(item)
                    size += info.st_size
                except FileNotFoundError:
                    skipped += 1
                except (OSError, StorageError) as exc:
                    errors.append(dict(path=item['path'], error=str(exc)))
            manifest['remaining'] = moved_rows
            self._write(batch / 'manifest.json', manifest)
            return dict(batch_id=batch_id, moved_to_trash=moved, bytes=size, skipped=skipped,
                        errors=errors[:100], error_count=len(errors), recoverable=True, freed_bytes=0)

    def _manifest(self, batch_id):
        if not re.fullmatch(r'[a-f0-9]{32}', batch_id):
            raise StorageError('无效的回收批次')
        try:
            data = json.loads(self._safe(f'data/system_trash/{batch_id}/manifest.json').read_text(encoding='utf-8'))
            if not isinstance(data['files'], list):
                raise ValueError()
            for row in data['files']:
                if not self._managed(row['path']):
                    raise ValueError()
                self._safe(f'data/system_trash/{batch_id}/files/' + row['path'])
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageError('无法读取回收批次清单') from exc

    def list_trash(self):
        # Read metadata only on page load; actual file sizes are checked during each action.
        root = self._safe('data/system_trash')
        rows = []
        if not root.exists():
            return rows
        for path in root.iterdir():
            if not re.fullmatch(r'[a-f0-9]{32}', path.name):
                continue
            try:
                manifest = json.loads(self._safe(f'data/system_trash/{path.name}/manifest.json').read_text(encoding='utf-8'))
                remaining = manifest.get('remaining', manifest['files'])
                if remaining:
                    rows.append(dict(id=path.name, created_at=manifest['created_at'],
                                     files=len(remaining), bytes=sum(row['bytes'] for row in remaining)))
            except (OSError, ValueError, KeyError, TypeError, StorageError):
                continue
        return sorted(rows, key=lambda row: row['created_at'], reverse=True)

    def trash_action(self, batch_id, *, action, confirmation):
        if action not in {'restore', 'purge'} or confirmation != {'restore': '恢复缓存', 'purge': '永久删除'}[action]:
            raise StorageError('请确认回收区操作')
        with self._lock:
            manifest = self._manifest(batch_id)
            processed, size, errors, remaining = 0, 0, [], []
            self._invalidate()
            for item in manifest['files']:
                try:
                    source = self._safe(f'data/system_trash/{batch_id}/files/' + item['path'])
                    if not source.exists():
                        continue
                    info = source.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise StorageError('回收文件类型已改变')
                    if action == 'restore':
                        destination = self._safe(item['path'])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        # link fails if destination exists: never overwrite a newly generated cache.
                        os.link(source, destination)
                    source.unlink()
                    processed += 1
                    size += info.st_size
                except (OSError, StorageError) as exc:
                    remaining.append(item)
                    errors.append(dict(path=item['path'], error=str(exc)))
            manifest['remaining'] = remaining
            self._write(self._safe(f'data/system_trash/{batch_id}/manifest.json'), manifest)
            return dict(processed=processed, bytes=size, remaining=len(remaining),
                        errors=errors[:100], error_count=len(errors),
                        freed_bytes=size if action == 'purge' else 0)


_service: Optional[StorageService] = None
_service_lock = threading.Lock()


def get_storage_service() -> StorageService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = StorageService()
    return _service
