import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from radar.core import parse_list, parse_attachments, canonical, connect, save, fetch, render
from radar.extensions import parse_attachment, match_position

SOURCE={'id':'s','name':'模拟栏目','url':'https://example.gov.cn/123/index.htm','marker':'招考资讯'}
PAGE='''<title>招考资讯</title><ul><li><a href="content_1.html" title="教师招聘公告">招聘</a><span>2026-10-01</span></li><li><a href="content_1.html#top">教师招聘公告</a><span>2026-10-01</span></li></ul>'''
class Tests(unittest.TestCase):
    def test_relative_date_dedup(self):
        items,count=parse_list(PAGE,SOURCE)
        self.assertEqual(len(items),1)
        self.assertEqual(count,2)
        self.assertEqual(items[0]['url'],'https://example.gov.cn/123/content_1.html')
        self.assertEqual(items[0]['published'],'2026-10-01')
    def test_table_layout(self):
        items,_=parse_list('<title>招考资讯</title><table><tr><td><a href="content_2.html">公开选调</a></td><td>2026年9月2日</td></tr></table>',SOURCE)
        self.assertEqual(items[0]['published'],'2026-09-02')
    def test_changed_or_blocked_page_is_failure(self):
        for page in ['<title>招考资讯</title>','验证码','招考资讯请输入验证码']:
            with self.assertRaises(ValueError):parse_list(page,SOURCE)
    def test_valid_nonmatching_rows(self):
        items,count=parse_list(PAGE.replace('教师招聘公告','通知').replace('招聘','通知'),SOURCE)
        self.assertEqual(items,[])
        self.assertEqual(count,2)
    def test_attachments(self):
        a=parse_attachments('<title>招聘公告</title><a href="../files/a.xlsx?x=1">岗位表</a><a href="javascript:alert(1)">下载</a>','https://example.gov.cn/123/content_1.html')
        self.assertEqual(a,[{'name':'岗位表','url':'https://example.gov.cn/files/a.xlsx?x=1'}])
    def test_comment_captcha_does_not_block_public_article(self):
        page='<title>招聘公告</title><h1>招聘公告</h1><input placeholder="请输入验证码"><a href="a.pdf">附件</a>'
        self.assertEqual(len(parse_attachments(page,'https://example.gov.cn/content_1.html')),1)
    def test_real_block_page(self):
        with self.assertRaises(ValueError):
            parse_attachments('<title>访问验证</title><p>请输入验证码</p>','https://example.gov.cn/content_1.html')
    def test_http_200_wrong_article_title_is_failure(self):
        with self.assertRaises(ValueError):
            parse_attachments('<title>人机验证</title><input placeholder="验证码">','https://example.gov.cn/content_1.html','教师招聘公告')
    def test_history_first_seen_and_revisions(self):
        with tempfile.TemporaryDirectory() as d:
            c=connect(Path(d)/'db')
            item=parse_list(PAGE,SOURCE)[0][0]
            save(c,item,'s','t1');save(c,item,'s','t2');save(c,item,'other','t3')
            self.assertEqual(c.execute('SELECT count(*) FROM announcements').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT first_seen,last_seen FROM announcements').fetchone()[:],('t1','t3'))
            item['title']='修订公告';save(c,item,'s','t4')
            self.assertEqual(c.execute('SELECT count(*) FROM revisions').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM sightings').fetchone()[0],2)
            c.close()
    def test_http_error_records_code(self):
        with tempfile.TemporaryDirectory() as d,patch('radar.core.urlopen',side_effect=HTTPError(SOURCE['url'],403,'Forbidden',{},None)):
            r=fetch(SOURCE['url'],Path(d))
            self.assertEqual(r['http'],403)
            self.assertTrue(r['error'])
    def test_timeout_unknown_http(self):
        with tempfile.TemporaryDirectory() as d,patch('radar.core.urlopen',side_effect=TimeoutError('timeout')):
            r=fetch(SOURCE['url'],Path(d))
            self.assertIsNone(r['http']);self.assertIn('TimeoutError',r['error'])
    def test_html_escaping_and_no_private_profile(self):
        with tempfile.TemporaryDirectory() as d:
            c=connect(Path(d)/'db');item=parse_list(PAGE,SOURCE)[0][0];item['title']='<script>alert(1)</script>';save(c,item,'s','t1')
            render(c,Path(d)/'site',[SOURCE]);text=(Path(d)/'site/index.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text);self.assertNotIn('中职在编',text)
            c.close()
    def test_interfaces_explicitly_unimplemented(self):
        for f in (parse_attachment,match_position):
            with self.assertRaises(NotImplementedError):f()
if __name__=='__main__':unittest.main()

class ConfiguredLayouts(unittest.TestCase):
    def test_configured_sources_with_synthetic_rows(self):
        import json
        root=Path(__file__).resolve().parent.parent
        sources=json.loads((root/'sources.json').read_text(encoding='utf-8'))
        for source in sources:
            with self.subTest(source=source['id']):
                page=f'<title>{source["marker"]}</title><ul><li><a href="content_1.html">公开招聘公告</a><span>2026-10-01</span></li></ul>'
                items,scanned=parse_list(page,source)
                self.assertEqual(scanned,1)
                self.assertEqual(len(items),1)
