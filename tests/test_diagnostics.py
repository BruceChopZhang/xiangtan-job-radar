"""Failure visibility and history continuity without live network dependencies."""
import contextlib
from email.message import Message
import errno
import io
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from radar.core import (AccessRestriction, RecordedRedirects, RedirectFailure,
                        fetch, parse_list, run)
from test_integration import response
from test_radar import PAGE, SOURCE


class Diagnostics(unittest.TestCase):
    def test_connection_failures_are_classified_without_fake_http_status(self):
        cases=[(URLError(OSError(errno.ENETUNREACH,'Network is unreachable')),'网络连接异常'),
               (URLError(socket.gaierror(-2,'Name or service not known')),'DNS解析失败'),
               (URLError(TimeoutError('timed out')),'请求超时'),
               (TimeoutError('timed out'),'请求超时'),
               (URLError('timed out'),'请求超时'),
               (URLError(ssl.SSLCertVerificationError('certificate verify failed')),'TLS连接异常'),
               (URLError(ConnectionResetError('reset by peer')),'网络连接异常')]
        for error,kind in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d, \
                 patch('radar.core.urlopen',side_effect=error):
                result=fetch(SOURCE['url'],Path(d))
                self.assertEqual(result['error_type'],kind)
                self.assertIsNone(result['http'])
                self.assertIn(str(error.reason if isinstance(error,URLError) else error),result['error'])
                if 'Network is unreachable' in str(error):self.assertIn('网络不可达',result['error'])

    def test_http_error_keeps_status_reason_and_target(self):
        for code,reason in ((403,'Forbidden'),(429,'Too Many Requests'),(503,'Service Unavailable')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as d, \
                 patch('radar.core.urlopen',side_effect=HTTPError(SOURCE['url'],code,reason,{},None)):
                r=fetch(SOURCE['url'],Path(d))
                self.assertEqual(r['http'],code)
                self.assertIn(reason,r['error'])
                self.assertIn(SOURCE['url'],r['error'])

    def test_redirect_policy_records_same_host_and_blocks_cross_host_before_following(self):
        trace=[]
        handler=RecordedRedirects(SOURCE['url'],trace)
        req=Request(SOURCE['url'])
        redirected=handler.redirect_request(req,None,301,'Moved',{},'/new/index.htm')
        self.assertEqual(redirected.full_url,'https://example.gov.cn/new/index.htm')
        with self.assertRaises(RedirectFailure):
            handler.redirect_request(req,None,302,'Moved',{},'https://other.example/check')
        with self.assertRaises(RedirectFailure):
            handler.redirect_request(req,None,302,'Moved',{},'http://example.gov.cn/check')
        self.assertIn('HTTP 301',trace[0])
        self.assertEqual(len(trace),3)

    def test_redirect_failure_retains_code_and_chain(self):
        def rejected(req,timeout):
            req.radar_redirects.append('HTTP 302: origin → https://other.example/')
            raise RedirectFailure('跨域重定向，未访问目标',302)
        with tempfile.TemporaryDirectory() as d,patch('radar.core.urlopen',side_effect=rejected):
            r=fetch(SOURCE['url'],Path(d))
            self.assertEqual(r['http'],302)
            self.assertEqual(r['error_type'],'重定向异常')
            self.assertIn('HTTP 302',r['info'])

    def test_success_keeps_response_diagnostics(self):
        class Page(io.BytesIO):
            status=200
            url='https://example.gov.cn/new/index.htm'
            headers=Message()
        def opened(req,timeout):
            req.radar_redirects.append('HTTP 301: old → new')
            result=Page(PAGE.encode());result.headers['Content-Type']='text/html; charset=utf-8'
            return result
        with tempfile.TemporaryDirectory() as d,patch('radar.core.urlopen',side_effect=opened), \
             patch.dict('os.environ',{'RADAR_SAVE_SNAPSHOTS':'0'}):
            r=fetch(SOURCE['url'],Path(d))
            self.assertEqual(r['error'],'')
            self.assertIn('招考资讯',r['info'])
            self.assertIn('HTTP 301',r['info'])
            self.assertEqual(r['final_url'],Page.url)

    def test_encoding_error_is_not_an_empty_list(self):
        class Page(io.BytesIO):
            status=200;url=SOURCE['url'];headers=Message()
        with tempfile.TemporaryDirectory() as d, \
             patch('radar.core.urlopen',return_value=Page(b'\xff\xfe')):
            r=fetch(SOURCE['url'],Path(d))
            self.assertEqual(r['http'],200)
            self.assertEqual(r['error_type'],'响应处理异常')
            self.assertIn('UnicodeDecodeError',r['error'])

    def test_comment_widget_does_not_make_a_valid_list_an_access_failure(self):
        page=PAGE+'<div>请输入验证码</div>'+'<p>公开栏目页面内容</p>'*100
        self.assertEqual(len(parse_list(page,SOURCE)[0]),1)
        with self.assertRaises(AccessRestriction):
            parse_list('<title>安全验证</title>请输入验证码',SOURCE)

    def test_console_dashboard_and_json_explain_failures_and_keep_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sources.json').write_text(json.dumps([SOURCE]))
            article='<title>教师招聘公告</title><a href="a.pdf">附件</a>'
            with patch('radar.core.fetch',side_effect=[response(PAGE),response(article)]), \
                 patch('radar.core.time.sleep'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(root),0)
            db=sqlite3.connect(root/'data/radar.sqlite3')
            original=db.execute('SELECT url,first_seen,attachments FROM announcements').fetchall();db.close()
            cases=[(response(error='[网络连接异常] 网络不可达；Network is unreachable',http=None),'网站访问失败','网络不可达'),
                   (response('<title>招考资讯</title>栏目结构已改变'),'网页解析失败','未识别出带日期'),
                   (response(PAGE.replace('教师招聘公告','通知').replace('招聘','通知')),'成功：本页无关键词公告','本轮新增0条')]
            for r,expected,reason in cases:
                with self.subTest(expected=expected),patch('radar.core.fetch',return_value=r), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    code=run(root)
                self.assertEqual(code,0 if expected.startswith('成功') else 1)
                self.assertIn(expected,out.getvalue());self.assertIn(reason,out.getvalue())
                page=(root/'site/index.html').read_text()
                self.assertIn(reason,page.split('<h2>历史公告')[0])
                logs=json.loads((root/'data/logs.json').read_text())
                self.assertEqual(logs[-1]['state'],expected)
                self.assertIn(reason,logs[-1]['error'])
                db=sqlite3.connect(root/'data/radar.sqlite3')
                self.assertEqual(db.execute('SELECT url,first_seen,attachments FROM announcements').fetchall(),original)
                db.close()
            self.assertEqual(len(logs),5)

    def test_second_success_distinguishes_no_new_announcements(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sources.json').write_text(json.dumps([SOURCE]))
            article='<title>教师招聘公告</title>'
            for _ in range(2):
                with patch('radar.core.fetch',side_effect=[response(PAGE),response(article)]), \
                     patch('radar.core.time.sleep'),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run(root),0)
            logs=json.loads((root/'data/logs.json').read_text())
            self.assertEqual(logs[-2]['state'],'成功：无新公告（本页已收录）')
            self.assertIn('本轮新增0条',logs[-2]['error'])

    def test_access_restriction_stops_later_articles_and_records_partial_failure(self):
        page=PAGE+'<li><a href="content_2.html">第二次招聘</a><span>2026-10-01</span></li>'
        blocked=response('<title>安全验证</title>请输入验证码')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sources.json').write_text(json.dumps([SOURCE]))
            with patch('radar.core.fetch',side_effect=[response(page),blocked]) as mocked, \
                 patch('radar.core.time.sleep'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(root),1)
            self.assertEqual(mocked.call_count,2)
            logs=json.loads((root/'data/logs.json').read_text())
            self.assertEqual(logs[0]['state'],'部分失败')
            self.assertIn('访问限制',logs[0]['error'])
            self.assertEqual(logs[1]['state'],'网站访问失败')
