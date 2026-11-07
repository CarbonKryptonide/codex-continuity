import concurrent.futures
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import continuity as c


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='test-work-', dir=Path(__file__).parent)
        self.base = Path(self.tmp.name)
        self.home = self.base / '用户 空格' / '.codex'
        self.home.mkdir(parents=True)
        self.root = self.base / 'backup'
        (self.home / 'config.toml').write_text('model="example"\n[features]\nmemories=true\n', encoding='utf-8')
        (self.home / 'AGENTS.md').write_text('User guidance', encoding='utf-8')
        self.state = sqlite3.connect(self.home / 'state_5.sqlite')
        self.state.execute('PRAGMA journal_mode=WAL')
        self.state.executescript('''
        CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, cwd TEXT,
          title TEXT, archived INT, project_id TEXT, creator_account_id TEXT, updated_at INT,
          history_mode TEXT, agent_role TEXT, agent_path TEXT, attached_thread_parent_id TEXT);
        CREATE TABLE projects (id TEXT, metadata TEXT);
        CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
        CREATE TABLE thread_attachments (id TEXT, thread_id TEXT, payload TEXT);
        ''')
        self.history = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        self.history.execute('PRAGMA journal_mode=WAL')
        self.history.executescript('''
        CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INT, status TEXT);
        CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INT,
          item_json TEXT, item_type TEXT, updated_at_ordinal INT, completed_at_ms INT);
        ''')
        self.add_session('main-a')
        self.add_session('main-b')
        self.add_session('archived', archived=1)
        self.add_session('archived-child', source=json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'archived'}}}))
        self.state.commit()
        self.history.commit()

    def tearDown(self):
        self.state.close()
        self.history.close()
        self.tmp.cleanup()

    def add_session(self, sid, archived=0, source='vscode'):
        path = self.home / 'sessions' / (sid + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        # Incomplete native trailing writes must not corrupt the archived JSONL.
        path.write_bytes(b'{"type":"session_meta","payload":{}}\n{"partial":')
        self.state.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (sid, str(path), source, str(self.home), sid, archived, None, 'account-a', 1,
              'paginated', None, None, None))
        self.history.execute('INSERT INTO thread_turns VALUES (?,?,?,?)', (sid,'turn-1',0,'completed'))
        for ordinal, role in enumerate(['userMessage','agentMessage']):
            obj = {'type':role,'id':'item-'+str(ordinal),'text':'Done','phase':'final'}
            if role == 'userMessage':
                obj['content'] = [{'type':'text','text':'Task'}]
            self.history.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)',
                (sid,'turn-1','item-'+str(ordinal),ordinal,json.dumps(obj),role,ordinal,1))

    def note(self, sid):
        value = {k:[] for k in c.NOTE_FIELDS}
        value.update(goal='Continue task',status='in_progress',session_id=sid)
        p = self.base / (sid + '-input.json')
        c.write_json(p,value)
        return p

    def test_live_wal_export_archive_exclusion_and_idempotency(self):
        source_hashes = {str(p):c.digest(p) for p in self.home.glob('*.sqlite*') if not p.name.endswith('-shm')}
        r = c.init(self.root,self.home,'main-a')
        self.assertEqual(r['selected_threads'],2)
        self.assertEqual(r['errors'],[])
        self.assertFalse((self.root/'sessions'/'archived').exists())
        self.assertFalse((self.root/'sessions'/'archived-child').exists())
        self.assertEqual(c.read_json(self.root/'sessions/main-a/meta.json')['counts']['items'],2)
        self.assertEqual(c.digest(self.root/'sessions/main-a/raw/main-a.jsonl'),c.hashlib.sha256(b'{"type":"session_meta","payload":{}}\n').hexdigest())
        r2 = c.sync(self.root,self.home,'main-a')
        self.assertEqual(len(r2['unchanged']),2)
        self.assertEqual(r2['exported'],[])
        for path, expected in source_hashes.items():
            self.assertEqual(c.digest(path),expected,'Source DB/WAL changed')
        self.assertTrue(c.check(self.root,self.home,True)['ok'])

    def test_tampering_is_detected_even_when_json_remains_valid(self):
        c.init(self.root,self.home,'main-a')
        p = self.root/'sessions/main-a/raw/items.jsonl'
        p.write_text(p.read_text(encoding='utf-8').replace('Done','Fake'),encoding='utf-8')
        r = c.check(self.root,self.home,True)
        self.assertFalse(r['ok'])
        self.assertTrue(any('hash mismatch' in e for e in r['errors']))

    def test_two_processes_keep_independent_handoffs_and_valid_index(self):
        c.init(self.root,self.home,'main-a')
        paths = [self.note(s) for s in ['main-a','main-b']]
        def run(sid,p):
            result = subprocess.run([sys.executable,str(Path(c.__file__).resolve()),'--root',str(self.root),
                '--home',str(self.home),'record','--session',sid,'--input',str(p)],capture_output=True,text=True)
            return result.returncode,result.stderr
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args:run(*args),zip(['main-a','main-b'],paths)))
        self.assertEqual(results,[(0,''),(0,'')])
        catalog = c.read_json(self.root/'environment/catalog.json')
        self.assertEqual({m['session_id'] for m in catalog},{'main-a','main-b'})
        self.assertTrue(all(m['summary_status']=='已整理' for m in catalog))

    def test_invalid_handoff_does_not_replace_previous_record(self):
        c.init(self.root,self.home,'main-a')
        p = self.note('main-a')
        c.record(self.root,'main-a',p)
        previous = c.digest(self.root/'sessions/main-a/handoff.json')
        obj = c.read_json(p);obj['status']='guessed_complete';c.write_json(p,obj)
        with self.assertRaises(ValueError):c.record(self.root,'main-a',p)
        self.assertEqual(c.digest(self.root/'sessions/main-a/handoff.json'),previous)

    def test_private_snapshot_propagates_consumer_errors_without_retry(self):
        with self.assertRaisesRegex(OSError,'consumer failure'):
            with c.snapshot_db(self.home/'state_5.sqlite',self.root/'.work'):
                raise OSError('consumer failure')

    def test_config_redaction_keeps_valid_toml(self):
        raw = 'model="unchanged"\n[mcp_servers.x]\nenv={API_TOKEN="hidden", OTHER="hidden2"}\n[mcp_servers.y.env]\nNAME="hidden3"\n'
        cleaned = c.sanitized_toml(raw)
        self.assertNotIn('hidden',cleaned)
        parsed = c.tomllib.loads(cleaned)
        self.assertEqual(parsed['model'],'unchanged')

    def test_nested_package_manifests_are_hash_checked(self):
        folder=self.base/'package'
        (folder/'nested').mkdir(parents=True)
        (folder/'manifest.json').write_text('{}',encoding='utf-8')
        (folder/'nested/manifest.json').write_text('{"resource":true}',encoding='utf-8')
        files=c.file_manifest(folder)
        self.assertNotIn('manifest.json',files)
        self.assertIn('nested/manifest.json',files)

    def test_new_account_current_session_keeps_old_archive_and_adds_new_work(self):
        self.state.execute('UPDATE threads SET creator_account_id=? WHERE id=?',('account-b','main-b'))
        self.state.commit()
        first=c.init(self.root,self.home,'main-a')
        self.assertEqual(first['selected_threads'],1)
        self.assertFalse((self.root/'sessions/main-b/meta.json').exists())
        second=c.sync(self.root,self.home,'main-b')
        self.assertEqual(second['selected_threads'],2)
        self.assertTrue((self.root/'sessions/main-a/meta.json').exists())
        self.assertTrue((self.root/'sessions/main-b/meta.json').exists())
        self.assertEqual(set(c.read_json(self.root/'environment/source-account.json')['authorized_account_ids']),{'account-a','account-b'})

    def test_recorded_bundled_node_works_without_terminal_path(self):
        node=c.resolve_node(self.root)
        if not node:
            self.skipTest('No compatible Node available for the actual bundled-runtime regression check')
        c.write_json(self.root/'environment/inventory.json',{'executables':{'node':node}})
        with mock.patch.dict(c.os.environ,{'PATH':''}):
            self.assertEqual(c.resolve_node(self.root),str(Path(node).resolve()))

    def test_session_sync_exports_newly_discovered_sessions_too(self):
        c.init(self.root,self.home,'main-a')
        self.add_session('new-session')
        self.state.commit();self.history.commit()
        result=c.sync(self.root,self.home,'main-a',only_session='main-a')
        self.assertIn('new-session',result['exported'])
        self.assertTrue(c.check(self.root,self.home)['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
