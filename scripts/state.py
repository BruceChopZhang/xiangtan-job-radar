"""Persist public monitoring state; restoration failures must never reset history."""
import argparse
import base64
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from urllib.parse import urlsplit


def safe_output(text):
    """Keep Git diagnostics, but never expose tokens or Authorization headers."""
    token = os.environ.get('RADAR_GITHUB_TOKEN', '')
    if token:
        encoded = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        text = text.replace(encoded, '***').replace(token, '***')
    text = re.sub(r'(?i)(authorization\s*:\s*(?:basic|bearer)\s+)\S+', r'\1***', text)
    return re.sub(r'(https?://)[^/\s@]+@', r'\1***@', text)


def git_error(result, operation):
    return RuntimeError(safe_output(
        f'{operation}失败（Git退出码 {result.returncode}）。不会强制覆盖历史。\n'
        f'Git stdout:\n{result.stdout or "（空）"}\n'
        f'Git stderr:\n{result.stderr or "（空）"}'))


def git(root, *args, env=None):
    result = subprocess.run(['git', '-C', str(root), *args],
                            text=True, capture_output=True, env=env)
    if result.returncode:
        raise git_error(result, f'git {args[0]}')
    return result.stdout.strip()


def push_environment(remote):
    """Use the job token once, for this push only; do not copy checkout config."""
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    token = env.get('RADAR_GITHUB_TOKEN', '')
    url = urlsplit(remote)
    if not token:
        if env.get('GITHUB_ACTIONS') == 'true' and url.scheme in ('http', 'https'):
            raise RuntimeError('保存步骤缺少RADAR_GITHUB_TOKEN；请同步更新radar.yml。')
        return env  # Local tests and local Git credentials still work.
    if url.scheme not in ('http', 'https') or not url.netloc or url.username:
        raise RuntimeError('工作流令牌只用于无内嵌凭据的HTTP(S)远端。')
    key = f'http.{url.scheme}://{url.netloc}/.extraheader'
    count = int(env.get('GIT_CONFIG_COUNT', '0'))
    # Empty extraheader resets inherited headers, including worktree credentials.
    header = 'AUTHORIZATION: basic ' + base64.b64encode(
        f'x-access-token:{token}'.encode()).decode()
    for offset, value in enumerate(('', header)):
        env[f'GIT_CONFIG_KEY_{count + offset}'] = key
        env[f'GIT_CONFIG_VALUE_{count + offset}'] = value
    env['GIT_CONFIG_COUNT'] = str(count + 2)
    return env


def validate(path):
    if not path.is_file():
        raise RuntimeError('历史数据库缺失，停止以免清空历史')
    with sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('历史数据库损坏')
        for table in ('announcements', 'logs', 'revisions', 'sightings'):
            db.execute(f'SELECT 1 FROM {table} LIMIT 1')


def restore(root):
    state = root / 'state'
    if state.exists():
        raise RuntimeError('state目录已存在，禁止覆盖')
    result = subprocess.run(['git', '-C', str(root), 'ls-remote', '--exit-code',
                             '--heads', 'origin', 'refs/heads/data'],
                            text=True, capture_output=True)
    if result.returncode == 2:
        state.mkdir()
        git(state, 'init', '-b', 'data')
        print('首次云端运行：将建立新的监测历史。')
    elif result.returncode == 0:
        git(root, 'fetch', 'origin', 'refs/heads/data:refs/remotes/origin/data')
        git(root, 'worktree', 'add', '--detach', str(state), 'refs/remotes/origin/data')
        validate(state / 'data/radar.sqlite3')
        (root / 'data').mkdir(exist_ok=True)
        shutil.copy2(state / 'data/radar.sqlite3', root / 'data/radar.sqlite3')
        print('已恢复公告、修订和全部运行日志。')
    else:
        raise git_error(result, '检查历史分支（停止，不从空数据库开始）')


def save(root):
    state = root / 'state'
    if not (state / '.git').exists():
        raise RuntimeError('历史恢复未完成，禁止保存')
    validate(root / 'data/radar.sqlite3')
    target = state / 'data'
    target.mkdir(exist_ok=True)
    for name in ('radar.sqlite3', 'logs.json'):
        if (root / 'data' / name).is_file():
            shutil.copy2(root / 'data' / name, target / name)
            git(state, 'add', '-f', 'data/' + name)
    git(state, 'config', 'user.name', 'github-actions[bot]')
    git(state, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    if git(state, 'diff', '--cached', '--name-only'):
        git(state, 'commit', '-m', '保存公告历史与监测日志')
        remote = git(root, 'remote', 'get-url', 'origin')
        git(state, 'push', remote, 'HEAD:refs/heads/data', env=push_environment(remote))
        print('历史已保存到data分支。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['restore', 'save'])
    args = parser.parse_args()
    globals()[args.operation](Path.cwd())
