"""Read-only compatibility evidence: capture, compare, draft, record.

Run with Windows Python: python -m mabowx.tools.compatibility --help
Comparisons and profile drafts also work without Windows.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import yaml

from mabowx.core.selectors import FIELDS, load_profile, version_key

SCHEMA = 1
# This is intentionally also a coverage manifest: untested actions stay pending.
CHECKS = {
    'navigation.tab_bar': 'main', 'session.container': 'main',
    'session.list': 'main', 'search.input_group': 'main', 'search.input': 'main',
    'chat.page': 'chat', 'chat.message_list': 'chat',
    'chat.input': 'chat', 'chat.send_button': 'chat',
}
FUNCTIONS = {
    'switch_chat': '切换指定测试会话，确认标题与消息列表属于目标会话',
    'send_text': '在指定测试会话发送文本，确认内容和接收对象',
    'send_file': '在指定测试会话发送文件，确认文件名与接收对象',
    'download_image': '下载指定图片，核对图片内容与原消息',
    'download_quote_image': '下载指定引用图片，核对引用归属与图片内容',
    'history': '读取历史消息，核对顺序、完整性和去重',
    'group_info': '读取群信息，核对群名及成员信息',
    'update_dialog': '出现更新弹窗时忽略更新，确认弹窗消失',
}
ATTRS = {'control_type': 'ControlTypeName', 'name': 'Name',
         'class_name': 'ClassName', 'automation_id': 'AutomationId'}


def node_at(profile, path):
    for part in path.split('.'):
        profile = profile[part]
    return profile


def matches(row, selector):
    if not selector or set(selector) - FIELDS:
        raise ValueError(f'不支持的定位规则: {selector}')
    return all(row.get(ATTRS[k]) == v for k, v in selector.items())


def evaluate(windows, profile):
    results = []
    for path, scope in CHECKS.items():
        candidates = [w for w in windows if w['kind'] == 'main' or (scope == 'chat' and w['kind'] == 'chat')]
        if not candidates:
            results.append({'check': path, 'window': None, 'status': 'not_observed', 'count': 0})
        for w in candidates:
            rows = w['rows'][1:]
            parent_path = {'session.list': 'session.container', 'search.input': 'search.input_group'}.get(path)
            if parent_path:
                parents = [r for r in rows if matches(r, node_at(profile, parent_path))]
                if len(parents) != 1:
                    results.append({'check': path, 'window': w['key'], 'status': 'incomplete' if w['errors'] else 'blocked',
                                    'count': 0, 'reason': f'父控件 {parent_path} 匹配数为 {len(parents)}'})
                    continue
                rows = [r for r in rows if r['path'].startswith(parents[0]['path'] + '/')]
            found = [r['path'] for r in rows if matches(r, node_at(profile, path))]
            status = 'pass' if len(found) == 1 else ('missing' if not found else 'ambiguous')
            if w['errors']:
                status = 'incomplete'
            results.append({'check': path, 'window': w['key'], 'status': status,
                            'count': len(found), 'paths': found})
    return results


def structure(window):
    # Ignore IDs of live objects, message text, child indices and geometry.
    # Collapse duplicate structural signatures so new messages do not look
    # like an application layout redesign.
    return sorted({json.dumps([r.get('ControlTypeName'), r.get('ClassName'),
                               r.get('AutomationId') if not str(r.get('AutomationId', '')).startswith('session_item_') else 'session_item_*',
                               r.get('Name') if r.get('ControlTypeName') == 'ButtonControl' else None],
                              ensure_ascii=False) for r in window['rows']})


def compare(before, after):
    if before['schema'] != SCHEMA or after['schema'] != SCHEMA:
        raise ValueError('报告格式版本不兼容')
    if before['scenario'] != after['scenario']:
        raise ValueError('场景名称不一致，请在同一场景下重新采集')
    b = {(r['window'], r['check']): r for r in before['checks']}
    a = {(r['window'], r['check']): r for r in after['checks']}
    changes = [{'window': k[0], 'check': k[1], 'before': b.get(k), 'after': a.get(k)}
               for k in b.keys() | a.keys()
               if (b.get(k, {}).get('status'), b.get(k, {}).get('count')) !=
                  (a.get(k, {}).get('status'), a.get(k, {}).get('count'))]
    bw = {w['key']: w for w in before['windows']}
    aw = {w['key']: w for w in after['windows']}
    tree = []
    for key in sorted(bw.keys() & aw.keys()):
        old, new = set(structure(bw[key])), set(structure(aw[key]))
        if old != new:
            tree.append({'window': key, 'added': sorted(new-old), 'removed': sorted(old-new)})
    return {'schema': SCHEMA, 'before_version': before['version'], 'after_version': after['version'],
            'same_profile': before['profile_sha256'] == after['profile_sha256'],
            'selector_changes': sorted(changes, key=lambda r: (str(r['window']), r['check'])),
            'structure_changes': tree, 'windows_added': sorted(aw.keys()-bw.keys()),
            'windows_removed': sorted(bw.keys()-aw.keys()),
            'current_failures': [r for r in after['checks'] if r['status'] != 'pass'],
            'functional_results': after['functions'],
            'limitations': '控件差异可能来自消息类型或界面状态；定位通过不代表操作通过。结构比较忽略文本与重复项，原始树仍保留。'}


def write_report(folder, report):
    (folder/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 微信兼容性检查', '', f"微信版本：{report['version']}",
             f"场景：{report['scenario']}", f"配置选择：`{report['selection']}`", '',
             '**这是只读定位检查；未执行发送、切换、下载操作。**', '',
             '| 窗口 | 检查项 | 结果 | 匹配数 |', '|---|---|---|---|']
    lines += [f"| {r['window']} | {r['check']} | {r['status']} | {r['count']} |" for r in report['checks']]
    lines += ['', '## 功能验收', '', '| 功能 | 结果 | 验收内容 | 证据 |', '|---|---|---|---|']
    for k, v in report['functions'].items():
        evidence = v.get('evidence', '').replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {k} | {v['status']} | {FUNCTIONS[k]} | {evidence} |")
    lines += ['', '## 采集限制', '', '失败也可能由未打开聊天、窗口布局、权限或控件读取失败导致，不自动判为版本不兼容。',
              'JSON 含当前界面文字，仅本地保存。截图可选。没有自动修改配置或标记版本已兼容。']
    for w in report['windows']:
        if w['errors']:
            lines.append(f"- {w['key']}: {w['errors']}")
    (folder/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def capture(args):
    from mabowx.core import uia
    from mabowx.core.win32 import get_process_path, get_version_by_path
    from mabowx.core.locks import ui_transaction
    import psutil
    import win32gui
    import win32process
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    roots = []
    def add(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            if psutil.Process(pid).name().lower() in {'weixin.exe', 'wechat.exe'}:
                roots.append((hwnd, pid))
        except psutil.Error:
            pass
    win32gui.EnumWindows(add, None)
    if not roots:
        raise RuntimeError('未找到可见微信窗口；请打开已登录微信后重试')
    versions = {get_version_by_path(get_process_path(pid)) for _, pid in roots}
    if len(versions) != 1 or None in versions or '' in versions:
        raise RuntimeError(f'微信版本无法唯一确定: {versions}')
    version = versions.pop()
    profile = load_profile(args.profile_version or version)
    windows, counts = [], Counter()
    with ui_transaction(timeout=10):
        for hwnd, pid in roots:
            c = uia.control_from_handle(hwnd)
            cls = c.ClassName
            kind = 'main' if cls == profile['window']['main']['class_name'] else ('chat' if cls == profile['search']['sub_window']['class_name'] else 'other')
            title = c.Name
            key_base = 'main' if kind == 'main' else kind + ':' + hashlib.sha256(title.encode()).hexdigest()[:12]
            counts[key_base] += 1
            key = key_base + ':' + str(counts[key_base])
            w = {'key': key, 'kind': kind, 'hwnd': hwnd, 'pid': pid, 'title': title, 'rows': [], 'errors': []}
            stack = [(c, '0', 0)]
            started = time.monotonic()
            while stack:
                if len(w['rows']) >= args.max_nodes or time.monotonic()-started > 30:
                    w['errors'].append('节点数或时间达到上限，采集不完整')
                    break
                node, path, depth = stack.pop()
                row = {'path': path}
                for attr in (*ATTRS.values(), 'IsEnabled', 'IsOffscreen'):
                    try:
                        row[attr] = getattr(node, attr)
                    except Exception as e:
                        w['errors'].append(f'{path}.{attr}: {type(e).__name__}')
                try:
                    rect = node.BoundingRectangle
                    row['rect'] = [rect.left, rect.top, rect.right, rect.bottom]
                    children = node.GetChildren()
                    if children and depth >= 30:
                        w['errors'].append(f'{path}: 深度达到上限')
                    else:
                        stack.extend((ch, path+'/'+str(i), depth+1) for i,ch in reversed(list(enumerate(children))))
                except Exception as e:
                    w['errors'].append(f'{path}: {type(e).__name__}')
                w['rows'].append(row)
            if args.screenshot:
                try:
                    from PIL import ImageGrab
                    ImageGrab.grab(bbox=tuple(win32gui.GetWindowRect(hwnd)), all_screens=True).save(out/(key.replace(':','_')+'.png'))
                except Exception as e:
                    w['errors'].append(f'截图失败: {e}')
            windows.append(w)
    encoded = yaml.safe_dump({k:v for k,v in profile.items() if k != '_selection'}, allow_unicode=True, sort_keys=True)
    (out/'profile.yaml').write_text(encoded, encoding='utf-8')
    report = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(),
              'scenario': args.scenario, 'version': version, 'version_source': 'Windows FileVersion',
              'selection': profile['_selection'],
              'profile_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
              'windows': windows, 'checks': evaluate(windows, profile),
              'functions': {k: {'status': 'not_run', 'evidence': ''} for k in FUNCTIONS}}
    write_report(out, report)
    print(json.dumps({'report': str(out/'report.md'), 'version': version, 'selection': report['selection'],
                      'checks': dict(Counter(r['status'] for r in report['checks']))}, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('capture', help='只读采集当前窗口和定位结果')
    p.add_argument('--out', required=True)
    p.add_argument('--scenario', required=True, help='例如 main-chat；前后必须使用相同场景')
    p.add_argument('--profile-version', help='显式沿用旧配置进行诊断')
    p.add_argument('--max-nodes', type=int, default=2500)
    p.add_argument('--screenshot', action='store_true')
    p = sub.add_parser('compare', help='比较两份报告，不访问微信')
    p.add_argument('before'); p.add_argument('after'); p.add_argument('--out', required=True)
    p = sub.add_parser('draft', help='复制内置配置到新版本草稿，不自动安装')
    p.add_argument('--version', required=True); p.add_argument('--from-version', required=True); p.add_argument('--out', required=True)
    p = sub.add_parser('record', help='记录已手动执行的功能结果，不自动执行操作')
    p.add_argument('report'); p.add_argument('--function', choices=FUNCTIONS, required=True)
    p.add_argument('--status', choices=['pass','fail','blocked'], required=True)
    p.add_argument('--evidence', required=True)
    args = parser.parse_args(argv)
    if args.command == 'capture':
        if args.max_nodes < 1:
            parser.error('--max-nodes 必须大于零')
        capture(args)
    elif args.command == 'compare':
        report = compare(json.loads(Path(args.before).read_text(encoding='utf-8')), json.loads(Path(args.after).read_text(encoding='utf-8')))
        out = Path(args.out)
        with out.open('x', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        md = out.with_suffix('.md')
        lines = ['# 微信兼容性差异', '',
                 f"版本：{report['before_version']} → {report['after_version']}",
                 f"是否使用相同配置内容：{report['same_profile']}", '',
                 f"定位结果变化：{len(report['selector_changes'])} 项；结构变化：{len(report['structure_changes'])} 个窗口。",
                 f"当前非通过检查：{len(report['current_failures'])} 项。", '',
                 '| 窗口 | 检查项 | 升级前 | 升级后 |', '|---|---|---|---|']
        for r in report['selector_changes']:
            lines.append(f"| {r['window']} | {r['check']} | {(r['before'] or {}).get('status', '未采集')} | {(r['after'] or {}).get('status', '未采集')} |")
        lines += ['', f"新增窗口：{report['windows_added']}", f"未再观察到的窗口：{report['windows_removed']}", '',
                  '## 结构差异', '']
        for r in report['structure_changes']:
            lines += [f"### {r['window']}", '', '```json', json.dumps(r, ensure_ascii=False, indent=2), '```', '']
        lines += ['## 功能验收状态', '']
        lines += [f"- {k}: {v['status']}" for k, v in report['functional_results'].items()]
        lines += ['', report['limitations'], '', f'详细证据：[JSON]({out.name})']
        with md.open('x', encoding='utf-8') as f:
            f.write('\n'.join(lines)+'\n')
        print(str(md))
    elif args.command == 'draft':
        version_key(args.version)
        profile = load_profile(args.from_version)
        source = profile.pop('_selection')['profile_version']
        profile.update(version=args.version, copied_from=source, validation='pending')
        with Path(args.out).open('x', encoding='utf-8') as f:
            yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
    else:
        path = Path(args.report)
        report = json.loads(path.read_text(encoding='utf-8'))
        if report['schema'] != SCHEMA or path.name != 'report.json':
            raise ValueError('需要 capture 生成的 report.json')
        report['functions'][args.function] = {'status': args.status, 'evidence': args.evidence,
                                             'recorded_at': datetime.now(timezone.utc).isoformat(), 'method': 'manual'}
        write_report(path.parent, report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
