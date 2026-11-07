"""Install the local archive and optionally add scoped Codex instructions."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import sys
import tomllib
import uuid

import continuity as c


ASSETS = ['continuity.py', 'install.py']
TEMPLATES = ['START_HERE.md', 'MAINTENANCE.md', 'global-rule.md',
             'continuity_keeper.toml', 'continuity.ps1']


def merge_root(config, root):
    parsed = tomllib.loads(config)
    roots = parsed.get('sandbox_workspace_write', {}).get('writable_roots', [])
    if any(str(p).casefold() == str(root).casefold() for p in roots):
        return config
    roots = list(roots) + [str(root)]
    line = 'writable_roots = ' + json.dumps(roots, ensure_ascii=False) + '\n'
    header = re.search(r'^\[sandbox_workspace_write\][ \t]*\r?$', config, re.M)
    if not header:
        updated = config.rstrip() + '\n\n[sandbox_workspace_write]\n' + line
    else:
        start = header.end()
        following = re.search(r'^\[', config[start:], re.M)
        stop = start + following.start() if following else len(config)
        section = config[start:stop]
        existing = re.search(r'^\s*writable_roots\s*=\s*\[[\s\S]*?\][^\n]*\n?', section, re.M)
        if existing:
            section = section[:existing.start()] + '\n' + line + section[existing.end():]
        else:
            section = '\n' + line + section
        updated = config[:start] + section + config[stop:]
    expected = json.loads(json.dumps(parsed))
    expected.setdefault('sandbox_workspace_write', {})['writable_roots'] = roots
    if tomllib.loads(updated) != expected:
        raise ValueError('Config merge changed unrelated settings')
    return updated


def render(source, root, python, home):
    values = {}
    for name in TEMPLATES:
        text = (source / 'templates' / name).read_text(encoding='utf-8-sig')
        text = text.replace('@@ARCHIVE_ROOT@@', str(root))
        text = text.replace('@@PYTHON_PS@@', "'" + str(python).replace("'", "''") + "'")
        text = text.replace('@@CODEX_HOME_PS@@', "'" + str(home).replace("'", "''") + "'")
        values[name] = text
    tomllib.loads(values['continuity_keeper.toml'])
    return values


def install(source, root, home, configure=False, node=None, dry_run=False):
    source, root, home = source.resolve(), root.resolve(), home.resolve()
    if root == source or root.is_relative_to(source):
        raise ValueError('Choose an archive directory outside the source checkout')
    candidate = Path(node).expanduser().resolve() if node else None
    if candidate and not candidate.is_file():
        raise FileNotFoundError(candidate)
    rendered = render(source, root, sys.executable, home)
    override = home / 'AGENTS.override.md'
    guidance = override if override.is_file() and override.read_text(encoding='utf-8-sig').strip() else home / 'AGENTS.md'
    paths = [guidance, home / 'config.toml', home / 'agents' / 'continuity_keeper.toml']
    old_agents = paths[0].read_text(encoding='utf-8-sig') if paths[0].exists() else ''
    old_config = paths[1].read_text(encoding='utf-8-sig') if paths[1].exists() else ''
    rule = rendered['global-rule.md'].strip()
    if c.RULE_START in old_agents:
        begin = old_agents.index(c.RULE_START)
        end = old_agents.index(c.RULE_END, begin) + len(c.RULE_END)
        new_agents = old_agents[:begin] + rule + old_agents[end:]
    else:
        new_agents = old_agents.rstrip() + '\n\n' + rule + '\n'
    new_config = merge_root(old_config, root)
    plan = {'archive_root': str(root), 'codex_home': str(home), 'configure_global': configure,
            'global_files': [str(p) for p in paths] if configure else [],
            'maintenance_model': 'gpt-6-luna', 'maintenance_effort': 'medium', 'dry_run': dry_run}
    if dry_run:
        return plan
    root.mkdir(parents=True, exist_ok=True)
    for name in ['tools', 'environment', 'reports', 'requests', 'sessions', 'snapshots']:
        (root / name).mkdir(exist_ok=True)
    for name in ASSETS:
        target = root / 'tools' / name
        if (source / name).resolve() != target.resolve():
            shutil.copyfile(source / name, target)
    for name, text in rendered.items():
        if name not in ['global-rule.md', 'continuity_keeper.toml']:
            c.atomic_text(root / name, '\ufeff' + text if name.endswith('.ps1') else text)
        c.atomic_text(root / 'tools' / 'templates' / name, (source / 'templates' / name).read_text(encoding='utf-8-sig'))
    result = {'archive_installed': True, 'root': str(root), 'global_configuration_installed': False}
    if configure:
        backup = root / 'tools' / 'install-backups' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        backup.mkdir(parents=True)
        before = {}
        for path in paths:
            if path.exists():
                if path.name == 'config.toml':
                    c.atomic_text(backup / 'config.redacted.toml', c.sanitized_toml(old_config))
                    before[str(path)] = {'sha256': c.digest(path), 'backup': 'config.redacted.toml',
                        'restore_mode': 'merge_only_sandbox_workspace_write',
                        'old_sandbox_workspace_write': tomllib.loads(old_config).get('sandbox_workspace_write')}
                else:
                    shutil.copyfile(path, backup / path.name)
                    before[str(path)] = {'sha256': c.digest(path), 'backup': path.name}
            else:
                before[str(path)] = {'backup': None}
        c.atomic_text(paths[0], new_agents)
        c.atomic_text(paths[1], new_config)
        c.atomic_text(paths[2], rendered['continuity_keeper.toml'])
        c.write_json(backup / 'changes.json', {'before': before, 'after': {str(p): c.digest(p) for p in paths}})
        c.atomic_text(backup / 'ROLLBACK.md', 'Remove only the marked continuity AGENTS section, the added writable root, and this generated agent.\n'
                      'Preserve later user changes. Restore instruction/agent originals only when their current hashes match changes.json after hashes.\n'
                      'config.redacted.toml is not a complete restore file: merge only old_sandbox_workspace_write; do not replace credentials with placeholders.\n')
        result.update(global_configuration_installed=True, rollback=str(backup))
    if node:
        prior_path = root / 'environment' / 'inventory.json'
        prior = c.read_json(prior_path) if prior_path.exists() else {}
        prior.setdefault('executables', {})['node'] = str(candidate)
        c.write_json(prior_path, prior)
    c.inventory(root, home)
    c.write_json(root / 'reports' / 'installation.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    p.add_argument('--root', type=Path, default=c.DEFAULT_ROOT)
    p.add_argument('--home', type=Path, default=c.DEFAULT_HOME)
    p.add_argument('--configure', action='store_true')
    p.add_argument('--node', type=Path)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    print(json.dumps(install(a.source, a.root, a.home, a.configure, a.node, a.dry_run), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
