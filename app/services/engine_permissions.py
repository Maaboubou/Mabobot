"""OpenCode host tool capabilities, independent of a running Codex process."""
import importlib.util
import os
import shutil


def host_tool_support():
    from app.services.codex_browser_tool import get_codex_browser_tool
    # These tools enforce their boundaries on the host/WSL worker. They do not
    # use Codex permission profiles or require a Codex account to be running.
    sandbox = os.name == 'nt' or bool(shutil.which('bwrap'))
    browser = bool(get_codex_browser_tool().enabled and importlib.util.find_spec('playwright'))
    return dict(engine='opencode', checked=True, permission_profiles=sandbox,
        runtime_workspace_roots=sandbox, network_proxy=sandbox, auto_review=False,
        bounded_auto_review=False, web_search=bool(importlib.util.find_spec('ddgs')),
        browser=browser, reason='由宿主搜索、浏览器和隔离工作区执行；不依赖 Codex 运行时。工具调用时校验权限和运行环境。')


def selected_engine(user):
    """Console capability display follows the same engine/Profile selection."""
    from app.services.execution_store import execution_db_path
    from pathlib import Path
    import json
    import sqlite3
    path = Path(execution_db_path())
    if not path.exists():
        return 'codex'
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT config_json FROM engine_config_versions ORDER BY revision DESC LIMIT 1').fetchone()
    except sqlite3.OperationalError:
        return 'codex'
    config = json.loads(row[0]) if row else {}
    if not config.get('enabled'):
        return 'codex'
    profile_id = user.assistant_policy.codex_profile_id if user is not None and user.assistant_policy else ''
    profile = config.get('profiles', {}).get(profile_id)
    if profile:
        return profile['engine']
    chat = config.get('chats', {}).get('wechat:' + str(user.id) if user is not None else '', {})
    engine = chat.get('engine', 'inherit')
    return config.get('default_engine', 'codex') if engine in {'inherit', 'follow'} else engine
