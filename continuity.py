"""Local, account-neutral Codex continuity archive. No credentials or cloud writes."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

VERSION = '1.0.0'
DEFAULT_HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
DEFAULT_ROOT = Path.home() / 'Codex-Continuity'
SESSION_RE = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
SECRET_KEY = re.compile(r'(^|_)(api_?key|access_?token|refresh_?token|id_?token|password|secret|authorization|cookie|token)($|_)', re.I)
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache'}
RULE_START = '<!-- codex-continuity:start -->'
RULE_END = '<!-- codex-continuity:end -->'


def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def digest(path, limit=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        left = limit
        while left is None or left > 0:
            data = f.read(1024 * 1024 if left is None else min(left, 1024 * 1024))
            if not data:
                if left not in (None, 0):
                    raise OSError('Source shortened while reading: ' + str(path))
                break
            h.update(data)
            if left is not None:
                left -= len(data)
    return h.hexdigest()


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path, obj):
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


@contextlib.contextmanager
def lock(root, timeout=60):
    """All generated index writes share a process lock; session notes are independent."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.continuity.lock'
    with path.open('a+b') as f:
        if path.stat().st_size == 0:
            f.write(b'0')
            f.flush()
        start = time.monotonic()
        while True:
            try:
                f.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start > timeout:
                    raise TimeoutError('Another continuity update holds the lock; retry later.')
                time.sleep(0.1)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def copy_prefix(source, dest, length):
    with source.open('rb') as src, dest.open('wb') as out:
        left = length
        while left:
            chunk = src.read(min(left, 1024 * 1024))
            if not chunk:
                raise OSError('Source shortened while copying: ' + str(source))
            out.write(chunk)
            left -= len(chunk)


@contextlib.contextmanager
def snapshot_db(source, work):
    """Copy a stable DB and WAL prefix without opening/writing the live SQLite store.

    A WAL may grow after our boundary. Database checkpoints or WAL resets are
    detected by hashing the DB and the exact copied WAL prefix before/after.
    SQLite recovers only the private copy, then quick_check validates it.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='readonly-snapshot-', dir=work) as tmp:
        dest = Path(tmp) / source.name
        wal = Path(str(source) + '-wal')
        last = None
        for attempt in range(5):
            for p in Path(tmp).iterdir():
                if p.is_file():
                    p.unlink()
            try:
                base_hash = digest(source)
                wal_size = wal.stat().st_size if wal.exists() else 0
                wal_hash = digest(wal, wal_size) if wal_size else None
                shutil.copyfile(source, dest)
                if wal_size:
                    copy_prefix(wal, Path(str(dest) + '-wal'), wal_size)
                if digest(source) != base_hash or digest(dest) != base_hash:
                    raise OSError('Source checkpointed while copying')
                if wal_size and (not wal.exists() or digest(wal, wal_size) != wal_hash
                                 or digest(Path(str(dest) + '-wal')) != wal_hash):
                    raise OSError('Source WAL reset while copying')
                con = sqlite3.connect(dest)
                result = con.execute('PRAGMA quick_check').fetchone()[0]
                if result != 'ok':
                    con.close()
                    raise sqlite3.DatabaseError(result)
                con.row_factory = sqlite3.Row
                con.execute('PRAGMA query_only=ON')
                break
            except (OSError, sqlite3.DatabaseError) as exc:
                last = exc
                if attempt == 4:
                    raise RuntimeError('Could not obtain stable read-only snapshot: ' + str(source)) from last
                time.sleep(0.15)
        try:
            yield con
        finally:
            con.close()


def sanitize(value, key=''):
    if SECRET_KEY.search(key):
        return '[REDACTED: reauthenticate or supply locally]'
    if isinstance(value, dict):
        return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key) for v in value]
    if isinstance(value, str):
        value = re.sub(r'\b(?:sk|rk)-[A-Za-z0-9_-]{12,}\b', '[REDACTED]', value)
        value = re.sub(r'(?i)\bBearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
    return value


def sanitized_toml(text):
    """Keep formatting and types, but remove credential values and inline env tables."""
    result = []
    section = ''
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped.strip('[]').lower()
        match = re.match(r'^(\s*)([A-Za-z0-9_.-]+)\s*=', line)
        key = match.group(2) if match else ''
        if key and (SECRET_KEY.search(key) or key == 'env' or section.endswith('.env')):
            result.append(match.group(1) + key + ' = "[REDACTED: restore locally]"')
        else:
            result.append(sanitize(line))
    output = '\n'.join(result) + '\n'
    tomllib.loads(output)
    return output


def walk_files(source):
    source = Path(source)
    if not source.exists():
        return
    seen = set()
    for folder, dirs, files in os.walk(source, followlinks=True):
        resolved = str(Path(folder).resolve()).casefold()
        if resolved in seen:
            dirs[:] = []
            continue
        seen.add(resolved)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(('.pyc', '.pyo')):
                continue
            yield Path(folder) / name


def copy_tree(source, target):
    count = 0
    for src in walk_files(source):
        dst = target / src.relative_to(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        count += 1
    return count


def valid_id(value):
    if not isinstance(value, str) or not SESSION_RE.fullmatch(value):
        raise ValueError('Invalid session identifier')
    return value


def locate_log(row):
    p = Path(row.get('rollout_path') or '__missing__')
    if p.is_file():
        return p
    compressed = Path(str(p) + '.zst')
    return compressed if compressed.is_file() else p


def source_info(row):
    source = row.get('source') or ''
    try:
        val = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        val = {}
    sub = val.get('subagent', {}) if isinstance(val, dict) else {}
    spawn = sub.get('thread_spawn', {}) if isinstance(sub, dict) else {}
    parent = spawn.get('parent_thread_id') or row.get('attached_thread_parent_id')
    if row.get('agent_role') == 'continuity_keeper' or 'continuity_keeper' in (row.get('agent_path') or ''):
        kind = 'maintenance'
    elif isinstance(sub, dict) and sub.get('other') == 'guardian':
        kind = 'guardian'
    elif sub:
        kind = 'subagent'
    else:
        kind = 'main'
    return kind, parent


def archived_project(project):
    meta = project.get('metadata')
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return False
    return isinstance(meta, dict) and (meta.get('archived') is True or meta.get('isArchived') is True
                                     or meta.get('status') == 'archived')


def select_threads(rows, projects, account_id):
    excluded = {}
    accounts = set(account_id) if isinstance(account_id, (list, set, tuple)) else ({account_id} if account_id else set())
    archived_projects = {p['id'] for p in projects if archived_project(p)}
    for row in rows:
        if row.get('archived'):
            excluded[row['id']] = 'archived_session'
        elif row.get('project_id') in archived_projects:
            excluded[row['id']] = 'archived_project'
        elif accounts and row.get('creator_account_id') not in accounts | {None, ''}:
            excluded[row['id']] = 'different_account'
    changed = True
    while changed:
        changed = False
        for row in rows:
            _, parent = source_info(row)
            if row['id'] not in excluded and parent in excluded:
                excluded[row['id']] = 'descendant_of_excluded_session'
                changed = True
    return [r for r in rows if r['id'] not in excluded], excluded


def raw_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    count = 0
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
                count += 1
        if path.exists() and digest(path) == digest(temp):
            temp.unlink()
        else:
            os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return count


def snippet(item):
    if item.get('type') == 'agentMessage':
        return item.get('text', '')
    if item.get('type') == 'userMessage':
        content = item.get('content', [])
        if isinstance(content, str):
            return content
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
    return ''


def export_native(row, dest, history):
    sid = valid_id(row['id'])
    counts = {'turns': 0, 'items': 0}
    last_user = ''
    last_assistant = ''
    latest = None
    asset_paths = set()
    if history:
        turns = (dict(r) for r in history.execute('SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal', (sid,)))
        counts['turns'] = raw_jsonl(dest / 'raw' / 'turns.jsonl', turns)
        def items():
            nonlocal last_user, last_assistant, latest
            query = 'SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal,item_id'
            for raw in history.execute(query, (sid,)):
                rec = dict(raw)
                item = json.loads(rec.pop('item_json'))
                rec['item'] = item
                latest = {'turn_id': rec.get('turn_id'), 'item_id': rec.get('item_id'),
                          'rollout_ordinal': rec.get('rollout_ordinal'),
                          'updated_at_ordinal': rec.get('updated_at_ordinal')}
                if item.get('type') == 'userMessage':
                    last_user = snippet(item)
                elif item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                    last_assistant = snippet(item)
                for k in ['path', 'savedPath']:
                    if isinstance(item.get(k), str):
                        asset_paths.add(item[k])
                if item.get('type') == 'userMessage':
                    for c in item.get('content', []):
                        if isinstance(c, dict):
                            for k in ['path', 'imagePath', 'image_path', 'url']:
                                if isinstance(c.get(k), str):
                                    asset_paths.add(c[k])
                yield rec
        counts['items'] = raw_jsonl(dest / 'raw' / 'items.jsonl', items())
    source = locate_log(row)
    raw_log = None
    raw_complete_bytes = 0
    if source.is_file() and 'archived_sessions' not in source.parts:
        dst = dest / 'raw' / source.name
        size = source.stat().st_size
        copy_prefix(source, dst.with_suffix(dst.suffix + '.tmp'), size)
        tmp = dst.with_suffix(dst.suffix + '.tmp')
        if source.suffix == '.jsonl' and size:
            with tmp.open('r+b') as f:
                end = size
                while True:
                    tail_start = max(0, end - 1024 * 1024)
                    f.seek(tail_start)
                    tail = f.read(end - tail_start)
                    boundary = tail.rfind(b'\n')
                    if boundary >= 0:
                        raw_complete_bytes = tail_start + boundary + 1
                        break
                    if tail_start == 0:
                        raise ValueError('No complete JSONL record at source boundary: ' + str(source))
                    end = tail_start
                f.truncate(raw_complete_bytes)
        else:
            raw_complete_bytes = size
        os.replace(tmp, dst)
        raw_log = str(dst.relative_to(dest))
        if counts['items'] == 0 and source.suffix == '.jsonl':
            # Legacy logs remain usable even without a paginated projection.
            with dst.open(encoding='utf-8') as f:
                for line in f:
                    rec = json.loads(line)
                    if rec.get('type') == 'response_item':
                        payload = rec.get('payload', {})
                        if payload.get('role') == 'user':
                            content = payload.get('content', [])
                            last_user = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
                        elif payload.get('role') == 'assistant':
                            last_assistant = '\n'.join(x.get('text', '') for x in payload.get('content', []) if isinstance(x, dict))
    if not counts['items'] and not raw_log:
        raise FileNotFoundError('No history items or readable native log for session ' + sid)
    preview = '# 最近的原始对话摘录（未经任务验收）\n\n'
    preview += '此文件仅帮助定位，不能据此认定任务完成。继续任务须阅读交接文件并核对原始记录。\n\n'
    preview += '## 最近用户消息\n\n' + last_user[-16000:] + '\n\n## 最近助手回复\n\n' + last_assistant[-20000:] + '\n'
    atomic_text(dest / 'recent.md', preview)
    return counts, latest, raw_log, raw_complete_bytes, asset_paths


def copy_referenced_assets(paths, home, dest):
    result = []
    allowed = [(home / n).resolve() for n in ['attachments', 'generated_images']]
    for value in sorted(paths):
        p = Path(value)
        if not p.is_absolute():
            continue
        exists = p.is_file()
        entry = {'source': str(p), 'exists': exists, 'copied': False}
        if exists and any(p.resolve().is_relative_to(a) for a in allowed):
            name = hashlib.sha256(str(p).encode()).hexdigest()[:12] + '-' + p.name
            target = dest / 'assets' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            entry.update(copied=True, backup=str(target.relative_to(dest)), sha256=digest(target))
        result.append(entry)
    return result


def resolve_node(root):
    """Reuse Codex's recorded Node when an ordinary terminal has no Node on PATH."""
    candidates = [shutil.which('node')]
    saved = root / 'environment' / 'inventory.json'
    if saved.is_file():
        candidates.append(read_json(saved).get('executables', {}).get('node'))
    candidates.append(str(Path.home() / '.cache' / 'codex-runtimes' / 'codex-primary-runtime'
                          / 'dependencies' / 'node' / 'bin' / 'node.exe'))
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen or not Path(candidate).is_file():
            continue
        seen.add(candidate)
        try:
            probe = subprocess.run([candidate, '-e',
                "process.exit(typeof require('node:zlib').createZstdDecompress==='function'?0:1)"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(Path(candidate).resolve())
    return None


def inventory(root, home):
    cfg_path = home / 'config.toml'
    cfg = tomllib.loads(cfg_path.read_text(encoding='utf-8-sig')) if cfg_path.is_file() else {}
    locations = [home / n for n in ['AGENTS.md', 'config.toml', 'agents', 'rules', 'skills', 'memories', 'memories_v2', 'plugins']]
    locations.append(Path.home() / '.agents' / 'skills')
    obj = {'generated_at': now(), 'codex_home': str(home), 'archive_root': str(root),
           'default_model': cfg.get('model'), 'default_effort': cfg.get('model_reasoning_effort'),
           'memory_config': sanitize(cfg.get('memories', {})), 'features': sanitize(cfg.get('features', {})),
           'plugins': sanitize(cfg.get('plugins', {})), 'marketplaces': sanitize(cfg.get('marketplaces', {})),
           'mcp_servers': sanitize(cfg.get('mcp_servers', {})),
           'locations': [{'path': str(p), 'exists': p.exists()} for p in locations],
           'executables': {name: shutil.which(name) for name in ['codex', 'git', 'node', 'markitdown', 'ego-browser']},
           'python': sys.executable,
           'scope': 'local, current or unattributed legacy account, non-archived sessions only',
           'cloud_and_other_devices': 'not_verified; connect/export separately before claiming account-wide completeness',
           'credentials': 'not_backed_up; login and reconnect on new account'}
    obj['executables']['node'] = resolve_node(root)
    # Environment variable names are useful; their values must not enter inventory.
    for server in obj['mcp_servers'].values():
        if isinstance(server, dict) and isinstance(server.get('env'), dict):
            server['env'] = {k: '[REDACTED: supply locally]' for k in server['env']}
    write_json(root / 'environment' / 'inventory.json', obj)
    return obj


def sync(root, home, current_session=None, only_session=None):
    work = root / '.work'
    report = {'started_at': now(), 'source': 'stable private DB+WAL copies; source files are read-only',
              'archived_excluded': True, 'errors': [], 'warnings': [], 'exported': [], 'unchanged': []}
    with lock(root):
        with snapshot_db(home / 'state_5.sqlite', work) as state:
            rows = [dict(r) for r in state.execute('SELECT * FROM threads')]
            projects = [dict(r) for r in state.execute('SELECT * FROM projects')]
            edges = [dict(r) for r in state.execute('SELECT * FROM thread_spawn_edges')]
            attachments = [dict(r) for r in state.execute('SELECT * FROM thread_attachments')]
        account_file = root / 'environment' / 'source-account.json'
        account_info = read_json(account_file) if account_file.exists() else {}
        account_id = account_info.get('creator_account_id')
        accounts = set(account_info.get('authorized_account_ids', []))
        if account_id:
            accounts.add(account_id)
        if current_session:
            match = next((r for r in rows if r['id'] == current_session), None)
            if match and match.get('creator_account_id'):
                actual = match['creator_account_id']
                if actual not in accounts:
                    if account_id:
                        report['warnings'].append('New current-session account added for ongoing continuity; original archive remains intact.')
                    accounts.add(actual)
                if not account_id:
                    account_id = actual
                write_json(account_file, {'creator_account_id': account_id,
                    'authorized_account_ids': sorted(accounts), 'note': 'Opaque identifiers, not credentials. Added only from explicit current-session evidence.'})
        selected, excluded = select_threads(rows, projects, accounts)
        active_ids = {r['id'] for r in selected}
        report['source_threads'] = len(rows)
        report['selected_threads'] = len(selected)
        report['excluded'] = excluded
        report['unattributed_legacy'] = [r['id'] for r in selected if not r.get('creator_account_id')]
        if report['unattributed_legacy']:
            report['warnings'].append('Legacy sessions without creator identity included as locally owned, explicitly listed.')
        write_json(root / 'environment' / 'projects.json', [p for p in projects if not archived_project(p)])
        write_json(root / 'environment' / 'relationships.json', [e for e in edges if e['parent_thread_id'] in active_ids and e['child_thread_id'] in active_ids])
        attach_map = {}
        for a in attachments:
            if a['thread_id'] in active_ids:
                attach_map.setdefault(a['thread_id'], []).append(a)
        with snapshot_db(home / 'thread_history_1.sqlite', work) as history:
            for row in selected:
                sid = valid_id(row['id'])
                dest = root / 'sessions' / sid
                if only_session and sid != only_session and (dest / 'meta.json').exists():
                    continue
                old = read_json(dest / 'meta.json') if (dest / 'meta.json').exists() else {}
                stats = history.execute('SELECT count(*),max(rollout_ordinal),max(updated_at_ordinal),max(completed_at_ms) FROM thread_items WHERE thread_id=?', (sid,)).fetchone()
                path = locate_log(row)
                source_size = path.stat().st_size if path.is_file() else None
                fingerprint = [row.get('updated_at_ms') or row.get('updated_at'), list(stats), source_size]
                if old.get('fingerprint') == fingerprint and old.get('export_ok'):
                    report['unchanged'].append(sid)
                    continue
                try:
                    dest.mkdir(parents=True, exist_ok=True)
                    counts, cutoff, raw_log, raw_bytes, asset_paths = export_native(row, dest, history)
                    kind, parent = source_info(row)
                    meta = {'schema_version': 1, 'session_id': sid, 'title': row.get('name') or row.get('title') or sid,
                            'cwd': row.get('cwd'), 'project_id': row.get('project_id'), 'kind': kind,
                            'parent_session_id': parent, 'archived': False, 'exported_at': now(),
                            'source_updated_at': row.get('updated_at_ms') or row.get('updated_at'),
                            'history_mode': row.get('history_mode'), 'fingerprint': fingerprint,
                            'counts': counts, 'coverage': cutoff, 'native_log': raw_log,
                            'native_log_complete_bytes': raw_bytes, 'native_source_path': row.get('rollout_path'),
                            'export_ok': True, 'model': row.get('model'), 'reasoning_effort': row.get('reasoning_effort'),
                            'raw_hashes': {p.name: digest(p) for p in (dest / 'raw').iterdir() if p.is_file()}}
                    write_json(dest / 'meta.json', meta)
                    write_json(dest / 'source-metadata.json', row)
                    write_json(dest / 'attachments.json', attach_map.get(sid, []))
                    write_json(dest / 'assets.json', copy_referenced_assets(asset_paths, home, dest))
                    if not (dest / 'handoff.md').exists():
                        atomic_text(dest / 'handoff.md', '# 待按需整理的交接记录\n\n原始历史已留存，尚未进行任务事实验收。\n\n'
                                    + '请先读 meta.json、recent.md，再按需读取 raw/。不要把最近助手的声明直接当作已验证事实。\n')
                    report['exported'].append(sid)
                except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                    report['errors'].append({'session_id': sid, 'error': str(exc)})
        inventory(root, home)
        # Previously exported records stay on disk for reversibility, but leave active index immediately.
        report['retained_out_of_scope_existing'] = []
        for p in (root / 'sessions').glob('*/meta.json'):
            if p.parent.name not in active_ids:
                report['retained_out_of_scope_existing'].append(p.parent.name)
        write_json(root / 'environment' / 'active-session-ids.json', sorted(active_ids))
        report['finished_at'] = now()
        write_json(root / 'reports' / 'last-sync.json', report)
        build_index(root)
    return report


def md_cell(text):
    return str(text or '').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def build_index(root):
    active_file = root / 'environment' / 'active-session-ids.json'
    active = set(read_json(active_file)) if active_file.exists() else set()
    sessions = []
    for p in (root / 'sessions').glob('*/meta.json'):
        if p.parent.name in active:
            meta = read_json(p)
            note = p.parent / 'handoff.json'
            handoff = read_json(note) if note.exists() else {}
            meta['summary_status'] = '未整理'
            if handoff:
                meta['summary_status'] = '已整理' if handoff.get('coverage') == meta.get('coverage') else '摘要待更新'
            meta['task_status'] = handoff.get('status', 'unreviewed')
            sessions.append(meta)
    sessions.sort(key=lambda m: m.get('source_updated_at') or 0, reverse=True)
    mains = [m for m in sessions if m['kind'] == 'main']
    aux = [m for m in sessions if m['kind'] != 'main']
    text = '# Codex 未归档工作目录\n\n'
    text += f'生成时间：{now()}。主会话 {len(mains)}，关联／辅助记录 {len(aux)}。\n\n'
    text += '阅读入口：[START_HERE.md](START_HERE.md)。仅包含本地、未归档范围；不代表云端和其他设备已备份。\n\n'
    text += '## 主会话\n\n| 会话原始标题 | 工作目录 | 任务状态 | 交接状态 | 原始记录 |\n|---|---|---|---|---|\n'
    for m in mains:
        sid = m['session_id']
        text += f'| [{md_cell(m["title"])}](sessions/{sid}/handoff.md) | {md_cell(m["cwd"])} | {m["task_status"]} | {m["summary_status"]} | [记录](sessions/{sid}/recent.md) |\n'
    text += '\n## 辅助记录\n\n| ID | 类型 | 父会话 | 历史条目 |\n|---|---|---|---|\n'
    for m in aux:
        sid = m['session_id']
        text += f'| [{sid}](sessions/{sid}/meta.json) | {m["kind"]} | {m.get("parent_session_id") or "未标记"} | {m["counts"]["items"]} |\n'
    text += '\n检查报告：[reports/last-check.json](reports/last-check.json)；同步报告：[reports/last-sync.json](reports/last-sync.json)。\n'
    atomic_text(root / 'INDEX.md', text)
    write_json(root / 'environment' / 'catalog.json', sessions)


NOTE_FIELDS = ['goal', 'constraints', 'decisions', 'completed', 'verified', 'unverified', 'failures', 'next_steps', 'artifacts', 'status']


def record(root, sid, input_path):
    sid = valid_id(sid)
    obj = read_json(input_path)
    missing = [k for k in NOTE_FIELDS if k not in obj]
    if missing:
        raise ValueError('Missing handoff fields: ' + ', '.join(missing))
    if obj['status'] not in ['in_progress', 'awaiting_user', 'blocked', 'complete', 'unreviewed']:
        raise ValueError('Invalid task status')
    for key in NOTE_FIELDS:
        if key in ['goal', 'status']:
            if not isinstance(obj[key], str):
                raise ValueError(key + ' must be text')
        elif not isinstance(obj[key], list) or not all(isinstance(v, str) for v in obj[key]):
            raise ValueError(key + ' must be an array of text')
    with lock(root):
        dest = root / 'sessions' / sid
        meta = read_json(dest / 'meta.json')
        if obj.get('session_id', sid) != sid:
            raise ValueError('Handoff session mismatch')
        obj.update(session_id=sid, updated_at=now(), coverage=meta.get('coverage'))
        obj.setdefault('source', 'Maintenance submission; author identity must be checked against the actual agent call')
        prior = dest / 'handoff.json'
        if prior.exists():
            revision = dest / 'revisions' / (str(time.time_ns()) + '.json')
            revision.parent.mkdir(exist_ok=True)
            shutil.copyfile(prior, revision)
        write_json(prior, obj)
        labels = {'goal':'目标', 'constraints':'限制', 'decisions':'已确认决定', 'completed':'已完成',
                  'verified':'已验证', 'unverified':'未验证／限制', 'failures':'失败与阻塞',
                  'next_steps':'下一步', 'artifacts':'产物与证据'}
        text = '# ' + meta['title'] + '\n\n'
        text += f'会话：{sid}；状态：{obj["status"]}；更新时间：{obj["updated_at"]}。\n\n'
        text += '覆盖截止点：`' + json.dumps(obj['coverage'], ensure_ascii=False) + '`。\n'
        for key, title in labels.items():
            text += '\n## ' + title + '\n\n'
            value = obj[key]
            text += (value if isinstance(value, str) else '\n'.join('- ' + s for s in value) or '- 无已记录内容') + '\n'
        atomic_text(dest / 'handoff.md', text)
        build_index(root)
    return {'session_id': sid, 'updated_at': obj['updated_at'], 'coverage': obj['coverage']}


def file_manifest(folder):
    return {str(p.relative_to(folder)).replace('\\', '/'): {'bytes': p.stat().st_size, 'sha256': digest(p)}
            for p in walk_files(folder) if p != folder / 'manifest.json'}


def prepare_switch(root, home, run_sync=True, current_session=None):
    if run_sync:
        sync(root, home, current_session)
    with lock(root):
        stamp = dt.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        dest = root / 'snapshots' / stamp
        dest.mkdir(parents=True)
        for name in ['AGENTS.md', 'AGENTS.override.md']:
            p = home / name
            if p.is_file():
                shutil.copyfile(p, dest / name)
        atomic_text(dest / 'config.sanitized.toml', sanitized_toml((home / 'config.toml').read_text(encoding='utf-8-sig')))
        counts = {}
        for name in ['agents', 'rules', 'skills', 'memories', 'memories_v2', 'plugins/cache']:
            src = home / name
            if src.exists():
                counts[name] = copy_tree(src, dest / 'codex' / name)
        user_skills = Path.home() / '.agents' / 'skills'
        if user_skills.exists():
            counts['user-agents-skills'] = copy_tree(user_skills, dest / 'user-agents-skills')
        inventory(root, home)
        copy_tree(root / 'environment', dest / 'environment')
        atomic_text(dest / 'RESTORE.md', '此快照不包含登录凭据。先登录新账号，再按 START_HERE.md 接续。\n'
                    'config.sanitized.toml 中的占位符不能直接用于外部服务；原位配置仍在，恢复前逐项合并。\n'
                    '项目文件和运行环境保留原位；插件需要重新核验授权。\n'
                    '共享记忆整体保存，可能提及历史任务；已归档会话正文未单独导出。\n')
        write_json(dest / 'manifest.json', {'created_at': now(), 'counts': counts, 'files': file_manifest(dest)})
        write_json(root / 'environment' / 'latest-snapshot.json', {'path': str(dest), 'created_at': now()})
    return {'snapshot': str(dest), 'files': len(read_json(dest / 'manifest.json')['files'])}


def check(root, home, deep=False):
    errors = []
    warnings = []
    active_path = root / 'environment' / 'active-session-ids.json'
    active = read_json(active_path) if active_path.exists() else []
    totals = {'sessions': 0, 'main_sessions': 0, 'auxiliary_sessions': 0, 'turns': 0, 'items': 0,
              'raw_files': 0, 'summarized': 0, 'stale_summaries': 0}
    raw_manifest = {}
    compressed_paths = []
    for sid in active:
        dest = root / 'sessions' / valid_id(sid)
        if not (dest / 'meta.json').exists():
            errors.append('Missing session export: ' + sid)
            continue
        meta = read_json(dest / 'meta.json')
        totals['sessions'] += 1
        totals['main_sessions' if meta['kind'] == 'main' else 'auxiliary_sessions'] += 1
        totals['turns'] += meta['counts']['turns']
        totals['items'] += meta['counts']['items']
        if meta.get('archived') or not meta.get('export_ok'):
            errors.append('Out-of-scope or unsuccessful session: ' + sid)
        for p in (dest / 'raw').glob('*'):
            if p.suffix == '.tmp':
                continue
            totals['raw_files'] += 1
            if deep:
                actual_hash = digest(p)
                raw_manifest[str(p.relative_to(root)).replace('\\', '/')] = {'sha256': actual_hash, 'bytes': p.stat().st_size}
                if meta.get('raw_hashes', {}).get(p.name) != actual_hash:
                    errors.append('Raw history hash mismatch: ' + str(p))
                if p.name.endswith('.jsonl.zst'):
                    compressed_paths.append(str(p))
                if p.suffix == '.jsonl':
                    count = 0
                    try:
                        with p.open(encoding='utf-8') as f:
                            for line in f:
                                json.loads(line)
                                count += 1
                        expected = meta['counts'].get('items' if p.name == 'items.jsonl' else 'turns') if p.name in ['items.jsonl','turns.jsonl'] else None
                        if expected is not None and count != expected:
                            errors.append('History row count mismatch: ' + str(p))
                    except (ValueError, UnicodeError) as exc:
                        errors.append('Invalid exported JSONL: ' + str(p) + ': ' + str(exc))
        if (dest / 'handoff.json').exists():
            totals['summarized'] += 1
            if read_json(dest / 'handoff.json').get('coverage') != meta.get('coverage'):
                totals['stale_summaries'] += 1
    compression_validation = {'files': 0, 'records': 0}
    if deep and compressed_paths:
        node = resolve_node(root)
        if node:
            validator = r'''const fs=require('node:fs'),z=require('node:zlib');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>input+=c);
process.stdin.on('end',async()=>{let records=0,errors=[];
for(const p of JSON.parse(input)){try{let pending='';
 const stream=fs.createReadStream(p).pipe(z.createZstdDecompress());stream.setEncoding('utf8');
 for await(const chunk of stream){pending+=chunk;let pos;
  while((pos=pending.indexOf('\n'))>=0){const line=pending.slice(0,pos);pending=pending.slice(pos+1);if(line.trim()){JSON.parse(line);records++;}}}
 if(pending.trim()){JSON.parse(pending);records++;}
}catch(e){errors.push({path:p,error:e.name});}}
process.stdout.write(JSON.stringify({files:JSON.parse(input).length,records,errors}));});'''
            completed = subprocess.run([node, '-e', validator], input=json.dumps(compressed_paths),
                text=True, encoding='utf-8', capture_output=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=180)
            if completed.returncode:
                errors.append('Compressed log validator failed: ' + completed.stderr[-300:])
            else:
                compression_validation = json.loads(completed.stdout)
                compression_validation['node_path'] = node
                errors.extend('Invalid compressed log: ' + x['path'] + ': ' + x['error'] for x in compression_validation.get('errors', []))
        else:
            warnings.append('Node with zstd support not found; compressed logs hash-checked but decompression not verified.')
    report_path = root / 'reports' / 'last-sync.json'
    if report_path.exists():
        report = read_json(report_path)
        errors.extend('Export failed for ' + e['session_id'] + ': ' + e['error'] for e in report['errors'])
        warnings.extend(report.get('warnings', []))
        if report.get('retained_out_of_scope_existing'):
            warnings.append('Previously backed-up sessions now excluded remain physically retained; see last-sync.json.')
    inventory_path = root / 'environment' / 'inventory.json'
    if inventory_path.exists():
        for loc in read_json(inventory_path)['locations']:
            if not Path(loc['path']).exists():
                warnings.append('Missing environment location: ' + loc['path'])
    latest = root / 'environment' / 'latest-snapshot.json'
    if latest.exists() and deep:
        snap = Path(read_json(latest)['path'])
        manifest = read_json(snap / 'manifest.json')
        for rel, expected in manifest['files'].items():
            p = snap / rel
            if not p.is_file() or digest(p) != expected['sha256']:
                errors.append('Snapshot hash mismatch: ' + rel)
    warnings.append('Cloud-only and other-device Codex histories are not verified by this local archive.')
    warnings.append('An active final reply may be added to the native source after this check; next sync catches it.')
    if totals['stale_summaries']:
        warnings.append('Some handoff summaries lag behind exported history; see INDEX.md.')
    deployment_path = root / 'reports' / 'installation.json'
    deployment = read_json(deployment_path) if deployment_path.exists() else {'status': 'not_verified'}
    if deployment.get('status') == 'blocked' or deployment.get('global_configuration_installed') is False:
        warnings.append('Deployment/global maintenance is not installed; see reports/installation.json and DEPLOYMENT_STATUS.md.')
    runtime_path = root / 'reports' / 'luna-runtime.json'
    runtime = read_json(runtime_path) if runtime_path.exists() else {'status': 'not_verified'}
    if runtime.get('status') != 'passed':
        warnings.append('Luna maintenance has not completed an end-to-end runtime validation; configured/requested model is not proof of completion.')
    result = {'checked_at': now(), 'ok': not errors, 'deep': deep, 'totals': totals,
              'errors': errors, 'warnings': warnings, 'archived_excluded': True,
              'maintenance_model': 'gpt-6-luna', 'maintenance_effort': 'medium',
              'compressed_validation': compression_validation,
              'deployment': deployment, 'maintenance_runtime': runtime}
    with lock(root):
        if deep:
            write_json(root / 'reports' / 'raw-manifest.json', {'created_at': now(), 'files': raw_manifest})
        write_json(root / 'reports' / 'last-check.json', result)
        text = '# 备份检查报告\n\n时间：' + result['checked_at'] + '\n\n'
        text += '**结果：' + ('通过' if result['ok'] else '未通过') + '**\n\n'
        text += '\n'.join('- ' + k + ': ' + str(v) for k, v in totals.items()) + '\n\n'
        text += '## 错误\n\n' + ('\n'.join('- ' + e for e in errors) or '- 无') + '\n\n'
        text += '## 限制与待办\n\n' + '\n'.join('- ' + w for w in warnings) + '\n'
        atomic_text(root / 'CHECK_REPORT.md', text)
    return result


def init(root, home, current_session=None):
    root.mkdir(parents=True, exist_ok=True)
    for name in ['sessions', 'environment', 'reports', 'snapshots', 'tools', 'requests']:
        (root / name).mkdir(exist_ok=True)
    write_json(root / 'environment' / 'settings.json', {'version': VERSION, 'created_at': now(),
        'codex_home': str(home), 'root': str(root), 'exclude_archived': True,
        'maintenance_model': 'gpt-6-luna', 'maintenance_effort': 'medium', 'schedule': 'AGENTS only; no hooks or scheduled task'})
    return sync(root, home, current_session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ['init', 'sync', 'prepare-switch']:
        p = subs.add_parser(name)
        p.add_argument('--current-session')
        if name == 'sync':
            p.add_argument('--session')
        if name == 'prepare-switch':
            p.add_argument('--no-sync', action='store_true')
    p = subs.add_parser('record')
    p.add_argument('--session', required=True)
    p.add_argument('--input', type=Path, required=True)
    p = subs.add_parser('check')
    p.add_argument('--deep', action='store_true')
    subs.add_parser('index')
    args = parser.parse_args()
    root = args.root.resolve()
    home = args.home.resolve()
    if args.command == 'init':
        result = init(root, home, args.current_session)
    elif args.command == 'sync':
        result = sync(root, home, args.current_session, args.session)
    elif args.command == 'record':
        result = record(root, args.session, args.input)
    elif args.command == 'prepare-switch':
        result = prepare_switch(root, home, not args.no_sync, args.current_session)
    elif args.command == 'check':
        result = check(root, home, args.deep)
    else:
        with lock(root):
            build_index(root)
        result = {'index': str(root / 'INDEX.md')}
    # Keep potentially private titles and messages off command output.
    if args.command in ['sync', 'init']:
        result = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in result.items()}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if args.command == 'check' and not result['ok']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
