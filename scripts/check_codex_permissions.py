"""Opt-in local sandbox smoke test; no model call, credentials or live chat data.

Run with the same Codex binary/OS as production. All fixtures live in a temporary
folder. A failed/unsupported sandbox exits nonzero, never retries unrestricted.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    fixture_parent = Path(__file__).resolve().parents[1] / 'tmp'
    fixture_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mabobot_permissions_check_', dir=fixture_parent) as directory:
        root = Path(directory)
        home, scope, sibling = root / 'codex', root / 'scope', root / 'other-chat'
        for folder in (home, scope, sibling):
            folder.mkdir()
        (sibling / 'private.txt').write_text('private-fixture')
        (scope / 'input.txt').write_text('scope-fixture')
        (scope / 'escape').symlink_to(sibling, target_is_directory=True)
        os.environ['CODEX_HOME'] = str(home)
        os.environ['DATABASE_URL'] = 'sqlite:///' + str(root / 'probe.db')
        os.environ['MABOBOT_PERMISSION_TEST_SECRET'] = 'must-not-reach-shell'
        from app.services.codex_app_server import CodexAppServerManager
        from app.services.codex_proxy.client import ISOLATED_PROFILES
        class FixtureManager(CodexAppServerManager):
            def _command(self):
                command = super()._command()
                # command/exec has no runtimeWorkspaceRoots field. Materialize
                # the same roots via profile configuration for this probe only.
                for profile in ISOLATED_PROFILES:
                    command.extend(['-c', f'permissions.{profile}.workspace_roots={{' + json.dumps(str(scope)) + '=true}'])
                return command
        manager = FixtureManager(workdir=str(scope), state_path=root / 'state.json', experimental_api=True)
        results = {}
        try:
            manager.start()
            thread = manager._request('thread/start', {'permissions': 'mabobot-isolated-ro-offline', 'runtimeWorkspaceRoots': [str(scope)], 'cwd': str(scope), 'approvalPolicy': 'never', 'ephemeral': True}, timeout=10)
            results['thread_profile_confirmed'] = thread.get('activePermissionProfile', {}).get('id') == 'mabobot-isolated-ro-offline' and thread.get('runtimeWorkspaceRoots') == [str(scope)]
            from app.services.codex_permission_service import LEGACY_DEFAULTS, resolve_permission
            from app.services.codex_permission_instructions import permission_instructions
            policy = resolve_permission(LEGACY_DEFAULTS, {'boundary_action': 'auto_review'},
                                        is_group=True, support=manager.permission_support)
            developer = permission_instructions(user_id=1, permissions=policy, scope_root=scope, workdir=scope)
            settings = dict(model='gpt-5.6-sol', reasoning_effort='high', web_search_mode='disabled',
                            reasoning_summary='inherit', timeout=15, workdir=scope,
                            permission_profile='mabobot-isolated-rw-offline', approval_policy='on-request',
                            runtime_workspace_roots=[scope], developer_instructions=developer)
            reviewed_thread = manager._start_thread(**settings)
            results['auto_review_thread_confirmed'] = bool(reviewed_thread) and policy.approval_policy == 'on-request'
            # Codex does not persist a resumable rollout until the first model
            # turn. Resume transport is covered by unit tests; this probe makes
            # no model calls and must not invent a persisted conversation.
            def execute(profile, command):
                response = manager._request('command/exec', {'command': command, 'cwd': str(scope),
                    'permissionProfile': profile, 'timeoutMs': 10000}, timeout=15)
                return response
            ro, rw = 'mabobot-isolated-ro-offline', 'mabobot-isolated-rw-offline'
            results['read_scope'] = execute(ro, ['cat', str(scope / 'input.txt')]).get('stdout') == 'scope-fixture'
            results['readonly_denies_write'] = execute(ro, ['/bin/sh', '-c', 'printf blocked > output.txt']).get('exitCode') != 0 and not (scope / 'output.txt').exists()
            results['write_scope'] = execute(rw, ['/bin/sh', '-c', 'printf allowed > output.txt']).get('exitCode') == 0 and (scope / 'output.txt').read_text() == 'allowed'
            results['deny_other_chat'] = execute(rw, ['cat', str(sibling / 'private.txt')]).get('exitCode') != 0
            results['deny_symlink_escape'] = execute(rw, ['cat', str(scope / 'escape/private.txt')]).get('exitCode') != 0
            results['secret_not_inherited'] = execute(rw, ['/bin/sh', '-c', 'test -z "$MABOBOT_PERMISSION_TEST_SECRET"']).get('exitCode') == 0
            results['proxy_verified'] = bool(manager.permission_support.get('network_proxy'))
            if results['proxy_verified']:
                # Literal public IP avoids transparent Fake-IP DNS answers,
                # which are correctly rejected by the private-network guard.
                public = execute('mabobot-isolated-rw-public', ['python3', '-c', 'import urllib.request;print(urllib.request.urlopen("https://1.1.1.1/cdn-cgi/trace",timeout=8).status)'])
                results['public_https'] = public.get('exitCode') == 0 and public.get('stdout', '').strip() == '200'
            print(json.dumps({'checks': results, 'capabilities': manager.permission_support}, ensure_ascii=False, indent=2))
            return 0 if all(results.values()) else 1
        finally:
            manager.stop()


if __name__ == '__main__':
    raise SystemExit(main())
