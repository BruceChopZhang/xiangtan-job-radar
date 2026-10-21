"""Persist public monitoring state; restoration failures must never reset history."""
import argparse
from pathlib import Path
import shutil
import sqlite3
import subprocess


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


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
        raise RuntimeError('无法检查历史分支；停止，不从空数据库开始。')


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
        auth = subprocess.run(['git', '-C', str(root), 'config', '--get-regexp',
                               r'http\..*\.extraheader'], text=True, capture_output=True)
        args = []
        if auth.returncode not in (0, 1):
            raise RuntimeError('无法读取Git认证配置')
        for line in auth.stdout.splitlines():
            key, value = line.split(' ', 1)
            args.extend(['-c', f'{key}={value}'])
        result = subprocess.run(['git', '-C', str(state), *args, 'push', remote, 'HEAD:refs/heads/data'],
                                text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('历史保存失败；检查仓库写入权限或分支冲突。不会强制覆盖。')
        print('历史已保存到data分支。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['restore', 'save'])
    args = parser.parse_args()
    globals()[args.operation](Path.cwd())
