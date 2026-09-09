"""Persistent desktop compatibility status; UIA runs only on explicit request."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .constants import PROJECT_ROOT


def detect_version():
    import psutil
    from mabowx.core.win32 import get_version_by_path
    versions = set()
    for p in psutil.process_iter(['name', 'exe']):
        if (p.info['name'] or '').lower() in {'weixin.exe', 'wechat.exe'} and p.info['exe']:
            version = get_version_by_path(p.info['exe'])
            if version:
                versions.add(version)
    if len(versions) > 1:
        raise RuntimeError('检测到多个微信版本，请只保留需要检查的客户端')
    return next(iter(versions), None)


class CompatibilityMonitor:
    def __init__(self, root=PROJECT_ROOT, *, probe=detect_version):
        self.root = Path(root)
        self.folder = self.root / 'data' / 'compatibility'
        self.state_file = self.folder / 'state.json'
        self.probe = probe
        self.lock = threading.RLock()
        self.running = False
        self.error = ''
        self.state = {}
        self.cached = None
        self.checked_at = 0
        try:
            self.state = json.loads(self.state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    def _save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.state_file)

    def _report(self, name):
        relative = self.state.get(name)
        if not relative:
            return None
        path = (self.folder / relative).resolve()
        if not path.is_relative_to(self.folder.resolve()):
            raise ValueError('报告路径无效')
        return path

    def snapshot(self):
        with self.lock:
            if self.cached is None or time.monotonic() - self.checked_at > 10:
                self.checked_at = time.monotonic()
                try:
                    from mabowx.core.selectors import load_profile, create_version_profile
                    version = self.probe()
                    if version:
                        create_version_profile(version, self.root / 'mabowx' / 'selectors')
                    selection = load_profile(version)['_selection'] if version else None
                    copied_from = load_profile(version).get('copied_from') if version else None
                    previous = self.state.get('last_version')
                    if version and previous != version:
                        self.state['previous_version'] = previous
                        self.state['last_version'] = version
                        self._save()
                    self.cached = {'version': version, 'selection': selection, 'copied_from': copied_from, 'detection_error': ''}
                except Exception as exc:
                    self.cached = {'version': None, 'selection': None, 'detection_error': str(exc)}
            result = copy.deepcopy(self.cached)
            latest = self.state.get('summary')
            baseline = self.state.get('baseline_version')
            result.update(running=self.running, error=self.error, latest=latest,
                          baseline_version=baseline, previous_version=self.state.get('previous_version'),
                          needs_check=bool(result['version'] and (not latest or latest['version'] != result['version'])),
                          has_report=bool(self._report('latest') and self._report('latest').is_file()),
                          has_diff=bool(self._report('diff') and self._report('diff').is_file()),
                          can_set_baseline=bool(latest and latest['version'] == result['version'] and not latest['failures'] and not self.running))
            return result

    def start(self):
        with self.lock:
            if self.running:
                return {'ok': False, 'error': '兼容性检查正在运行，请等待结果'}
            snapshot = self.snapshot()
            if not snapshot['version']:
                return {'ok': False, 'error': snapshot['detection_error'] or '请先启动微信并打开聊天页面'}
            self.running = True
            self.error = ''
        threading.Thread(target=self._worker, name='wechat-compatibility', daemon=True).start()
        return {'ok': True}

    def _run(self, *args):
        python = self.root / '.venv' / 'Scripts' / 'python.exe'
        command = [str(python) if python.exists() else sys.executable,
                   '-m', 'mabowx.tools.compatibility', *map(str, args)]
        result = subprocess.run(command, cwd=self.root, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=180,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or '检查进程异常退出')[-1500:])

    def _worker(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            out = self.folder / 'runs' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
            self._run('capture', '--scenario', 'main-chat', '--out', out, '--screenshot')
            with self.lock:
                baseline = self._report('baseline')
            diff = None
            if baseline and baseline.is_file():
                diff = out / 'diff.json'
                self._run('compare', baseline, out/'report.json', '--out', diff)
            self.register(out/'report.json', diff)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            with self.lock:
                self.running = False

    def register(self, path, diff=None):
        """Import completed evidence from a managed run; never accept UI paths."""
        path = Path(path)
        report = json.loads(path.read_text(encoding='utf-8'))
        failures = [r for r in report['checks'] if r['status'] != 'pass']
        summary = {'version': report['version'], 'created_at': report['created_at'],
                   'passed': len(report['checks'])-len(failures), 'total': len(report['checks']),
                   'failures': failures, 'functions_pending': sum(v['status'] == 'not_run' for v in report['functions'].values())}
        if diff:
            d = json.loads(Path(diff).read_text(encoding='utf-8'))
            summary['changes'] = {'selectors': len(d['selector_changes']), 'windows': len(d['structure_changes'])}
        with self.lock:
            self.state.update(latest=str(path.resolve().relative_to(self.folder.resolve())), summary=summary)
            self.state['diff'] = str(Path(diff).resolve().relative_to(self.folder.resolve())) if diff else None
            self._save()

    def set_baseline(self):
        with self.lock:
            if not self.snapshot()['can_set_baseline']:
                return {'ok': False, 'error': '仅能将当前版本定位全部通过的报告设为基线'}
            self.state['baseline'] = self.state['latest']
            self.state['baseline_version'] = self.state['summary']['version']
            self._save()
        return {'ok': True}

    def document(self, diff=False):
        with self.lock:
            path = self._report('diff' if diff else 'latest')
            if not path or not path.with_suffix('.md').is_file():
                raise FileNotFoundError('尚无对应报告，请先运行检查')
            return path.with_suffix('.md')
