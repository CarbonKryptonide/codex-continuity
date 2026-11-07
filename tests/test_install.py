from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

import continuity as c
import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='test-work-',dir=Path(__file__).parent)
        self.base=Path(self.tmp.name)
        self.source=self.base/'source'
        self.source.mkdir()
        original=Path(install.__file__).parent
        for name in ['continuity.py','install.py','INSTALL.ps1']:
            shutil.copyfile(original/name,self.source/name)
        shutil.copytree(original/'templates',self.source/'templates')
        self.root=self.base/"archive with space's"
        self.home=self.base/'fake-home'

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        result=install.install(self.source,self.root,self.home,True,dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.home.exists())

    def test_fresh_install_and_repeat_preserve_defaults(self):
        self.home.mkdir()
        (self.home/'config.toml').write_text('model="example"\nmodel_reasoning_effort="high"\n[features]\nmemories=true\n',encoding='utf-8')
        (self.home/'AGENTS.md').write_text('Keep my guidance\n',encoding='utf-8')
        install.install(self.source,self.root,self.home,True)
        result=install.install(self.source,self.root,self.home,True)
        self.assertTrue(result['global_configuration_installed'])
        cfg=tomllib.loads((self.home/'config.toml').read_text(encoding='utf-8'))
        self.assertEqual(cfg['model'],'example')
        self.assertEqual(cfg['model_reasoning_effort'],'high')
        self.assertEqual(cfg['sandbox_workspace_write']['writable_roots'],[str(self.root)])
        guidance=(self.home/'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(guidance.startswith('Keep my guidance'))
        self.assertEqual(guidance.count(c.RULE_START),1)
        agent=tomllib.loads((self.home/'agents/continuity_keeper.toml').read_text(encoding='utf-8'))
        self.assertEqual(agent['model'],'gpt-6-luna')
        self.assertEqual(agent['model_reasoning_effort'],'medium')
        wrapper=(self.root/'continuity.ps1').read_text(encoding='utf-8-sig')
        self.assertNotIn('@@',wrapper)
        self.assertIn(str(self.home).replace("'","''"),wrapper)
        self.assertTrue((self.root/'continuity.ps1').read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_active_override_receives_rule(self):
        self.home.mkdir()
        (self.home/'AGENTS.override.md').write_text('Active override\n',encoding='utf-8')
        (self.home/'AGENTS.md').write_text('Base guidance\n',encoding='utf-8')
        install.install(self.source,self.root,self.home,True)
        self.assertEqual((self.home/'AGENTS.md').read_text(encoding='utf-8'),'Base guidance\n')
        self.assertIn(c.RULE_START,(self.home/'AGENTS.override.md').read_text(encoding='utf-8'))

    def test_archive_inside_checkout_is_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            install.install(self.source,self.source/'private-data',self.home,True)
        self.assertFalse((self.source/'private-data').exists())

    def test_invalid_explicit_node_does_not_modify_configuration(self):
        with self.assertRaises(FileNotFoundError):
            install.install(self.source,self.root,self.home,True,node=self.base/'missing-node')
        self.assertFalse(self.root.exists())
        self.assertFalse(self.home.exists())

    def test_powershell_installer_and_generated_wrapper(self):
        shell=shutil.which('powershell') or shutil.which('pwsh')
        if not shell:
            self.skipTest('PowerShell is not installed on this runner')
        result=subprocess.run([shell,'-NoProfile','-File',str(self.source/'INSTALL.ps1'),
            '-Root',str(self.root),'-CodexHome',str(self.home),'-Python',sys.executable,'-SkipInitialBackup'],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        wrapped=subprocess.run([shell,'-NoProfile','-File',str(self.root/'continuity.ps1'),'--help'],
            capture_output=True,text=True)
        self.assertEqual(wrapped.returncode,0,wrapped.stderr)
        self.assertIn('prepare-switch',wrapped.stdout)


if __name__=='__main__':
    unittest.main(verbosity=2)
