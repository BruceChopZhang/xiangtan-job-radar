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

class SaveFix(unittest.TestCase):
    def test_git_error_preserves_stderr_without_credentials(self):
        import base64
        token='test-secret-token'
        encoded=base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        result=subprocess.CompletedProcess([],128,'',
            f'remote: Permission denied (HTTP 403)\nAuthorization: Basic {encoded}\n'
            f'https://x-access-token:{token}@example.test/repo.git')
        with patch.dict(state.os.environ,{'RADAR_GITHUB_TOKEN':token}), \
             patch.object(state.subprocess,'run',return_value=result):
            with self.assertRaises(RuntimeError) as error:
                state.git(Path('.'),'push','origin','HEAD:refs/heads/data')
        message=str(error.exception)
        self.assertIn('HTTP 403',message)
        self.assertIn('128',message)
        self.assertNotIn(token,message)
        self.assertNotIn(encoded,message)

    def test_push_rejection_does_not_overwrite_remote_history(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);remote=base/'remote.git'
            subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            def runner(name):
                root=base/name;root.mkdir()
                state.git(root,'init');state.git(root,'remote','add','origin',str(remote))
                state.restore(root)
                (root/'data').mkdir(exist_ok=True)
                db=connect(root/'data/radar.sqlite3')
                db.execute("INSERT INTO logs(run,source,state) VALUES(?, 's', '成功')",(name,))
                db.commit();db.close()
                return root
            # Two runners both observe a missing branch. The second must not replace it.
            first=runner('first');stale=runner('stale')
            state.save(first)
            original=state.git(remote,'rev-parse','refs/heads/data')
            with self.assertRaises(RuntimeError) as error:state.save(stale)
            self.assertIn('rejected',str(error.exception))
            self.assertEqual(state.git(remote,'rev-parse','refs/heads/data'),original)

    def test_missing_job_token_fails_clearly(self):
        with patch.dict(state.os.environ,{'GITHUB_ACTIONS':'true','RADAR_GITHUB_TOKEN':''}):
            with self.assertRaisesRegex(RuntimeError,'radar.yml'):
                state.push_environment('https://github.com/example/repo.git')

    def test_workflow_only_manual_and_token_scoped_to_save(self):
        # Runtime is standard-library-only; YAML parsing is also checked during release QA.
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/radar.yml').read_text()
        self.assertIn('workflow_dispatch:',text)
        self.assertNotRegex(text,r'(?m)^\s*(schedule|push|pull_request):')
        save_step=text.split('id: save',1)[1].split('      - name:',1)[0]
        self.assertIn('RADAR_GITHUB_TOKEN: ${{ github.token }}',save_step)
        self.assertIn('contents: write',text)


class AuthenticatedPush(unittest.TestCase):
    def test_first_and_later_save_send_exactly_one_authorization_header(self):
        """Actual smart HTTP Git push; reject missing/duplicate Authorization headers."""
        import base64
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import os
        import threading
        from urllib.parse import urlsplit
        backend=Path(state.git(Path('.'),'--exec-path'))/'git-http-backend'
        if not backend.is_file():self.skipTest('git-http-backend unavailable')
        token='test-local-push-token'
        expected='basic '+base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        requests=[]
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);remote=base/'remote.git'
            subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            state.git(remote,'config','http.receivepack','true')
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):self.handle_git()
                def do_POST(self):self.handle_git()
                def handle_git(self):
                    headers=self.headers.get_all('Authorization',[])
                    requests.append(headers)
                    if len(headers)!=1 or headers[0].lower()!=expected.lower():
                        self.send_response(403);self.end_headers();return
                    url=urlsplit(self.path)
                    env=os.environ.copy()
                    env.update(GIT_PROJECT_ROOT=str(base),GIT_HTTP_EXPORT_ALL='1',
                               PATH_INFO=url.path,QUERY_STRING=url.query,
                               REQUEST_METHOD=self.command,CONTENT_TYPE=self.headers.get('Content-Type',''),
                               CONTENT_LENGTH=self.headers.get('Content-Length','0'),REMOTE_USER='test',
                               REMOTE_ADDR='127.0.0.1')
                    body=self.rfile.read(int(env['CONTENT_LENGTH']))
                    result=subprocess.run([str(backend)],input=body,env=env,capture_output=True)
                    head,content=result.stdout.split(b'\r\n\r\n',1)
                    fields=[line.decode().split(':',1) for line in head.split(b'\r\n')]
                    status=next((int(v.strip().split()[0]) for k,v in fields if k.lower()=='status'),200)
                    self.send_response(status)
                    for key,value in fields:
                        if key.lower()!='status':self.send_header(key,value.strip())
                    self.end_headers();self.wfile.write(content)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/remote.git'
                for i in (1,2):
                    root=base/str(i);root.mkdir()
                    state.git(root,'init');state.git(root,'remote','add','origin',str(remote))
                    state.restore(root)
                    (root/'data').mkdir(exist_ok=True)
                    db=connect(root/'data/radar.sqlite3')
                    self.assertEqual(db.execute('SELECT count(*) FROM logs').fetchone()[0],i-1)
                    db.execute("INSERT INTO logs(run,source,state) VALUES(?, 's', '成功')",(str(i),))
                    db.commit();db.close()
                    state.git(root,'remote','set-url','origin',url)
                    # checkout v6-style conditional file, inherited by worktree only.
                    auth=base/f'checkout-credentials-{i}.config'
                    auth.write_text(f'[http "http://127.0.0.1:{server.server_port}/"]\n'
                                    f' extraheader = AUTHORIZATION: {expected}\n')
                    state.git(root,'config',f'includeIf.gitdir:{root}/.git.path',str(auth))
                    state.git(root,'config',f'includeIf.gitdir:{root}/.git/worktrees/*.path',str(auth))
                    with patch.dict(os.environ,{'RADAR_GITHUB_TOKEN':token}):
                        state.save(root)
                    self.assertNotIn(token,(root/'state'/'.git').read_text() if (root/'state'/'.git').is_file()
                                     else (root/'state/.git/config').read_text())
                self.assertGreater(len(requests),0)
                self.assertTrue(all(len(headers)==1 for headers in requests))
                self.assertEqual(len(state.git(remote,'log','--oneline','data').splitlines()),2)
            finally:
                server.shutdown();server.server_close();thread.join()
