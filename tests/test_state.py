import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from radar.core import connect

spec=importlib.util.spec_from_file_location('state',Path(__file__).resolve().parents[1]/'scripts/state.py')
state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)

class Persistence(unittest.TestCase):
    def test_two_fresh_runners_keep_history(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);remote=base/'remote.git'
            subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            for i in (1,2):
                root=base/str(i);root.mkdir()
                state.git(root,'init');state.git(root,'remote','add','origin',str(remote))
                state.restore(root)
                (root/'data').mkdir(exist_ok=True)
                db=connect(root/'data/radar.sqlite3')
                self.assertEqual(db.execute('SELECT count(*) FROM logs').fetchone()[0],i-1)
                db.execute("INSERT INTO logs(run,source,state) VALUES(?, 's', '失败')",(str(i),))
                db.commit();db.close()
                state.save(root)
            db=sqlite3.connect(base/'2/state/data/radar.sqlite3')
            self.assertEqual(db.execute('SELECT count(*) FROM logs').fetchone()[0],2)
            db.close()
            self.assertEqual(len(state.git(base/'2/state','log','--oneline').splitlines()),2)
    def test_remote_failure_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(state.subprocess,'run',return_value=subprocess.CompletedProcess([],128,'','network error')):
                with self.assertRaises(RuntimeError):state.restore(Path(d))
            self.assertFalse((Path(d)/'state').exists())
    def test_missing_database_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):state.validate(Path(d)/'missing.sqlite3')
