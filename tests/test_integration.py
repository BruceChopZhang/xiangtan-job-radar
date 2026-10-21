import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from radar.core import run
from test_radar import PAGE,SOURCE

def response(text='',error='',http=200):
    return dict(text=text,error=error,http=http,visited='2026-10-21T10:00:00+08:00',seconds=.1,snapshot='')
class Integration(unittest.TestCase):
    def test_repeat_and_partial_failure_keep_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sources.json').write_text(json.dumps([SOURCE]))
            article='<title>教师招聘公告</title><a href="a.pdf">附件</a>'
            with patch('radar.core.fetch',side_effect=[response(PAGE),response(article)]),patch('radar.core.time.sleep'):
                self.assertEqual(run(root),0)
            with patch('radar.core.fetch',side_effect=[response(PAGE),response(error='timeout',http=None)]),patch('radar.core.time.sleep'):
                self.assertEqual(run(root),1)
            c=sqlite3.connect(root/'data/radar.sqlite3')
            self.assertEqual(c.execute('select count(*) from announcements').fetchone()[0],1)
            self.assertIn('a.pdf',c.execute('select attachments from announcements').fetchone()[0])
            self.assertEqual(c.execute('select count(*) from logs').fetchone()[0],4)
            self.assertEqual(c.execute('select count(distinct run) from logs').fetchone()[0],2)
            self.assertEqual(c.execute("select state from logs where kind='栏目' order by id").fetchall(),[('成功',),('部分失败',)])
            self.assertIn('部分失败',(root/'site/index.html').read_text(encoding='utf-8'))
            c.close()
    def test_source_failure_does_not_clear_data(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sources.json').write_text(json.dumps([SOURCE]))
            with patch('radar.core.fetch',return_value=response(error='HTTP 503',http=503)):
                self.assertEqual(run(root),1)
            self.assertIn('HTTP 503',(root/'site/index.html').read_text(encoding='utf-8'))
