import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError

KEYWORDS = r'招聘|选调|遴选|人才引进|招募'
DATE = r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?'


class AccessRestriction(ValueError):
    """An access/verification page is not an empty recruitment list."""


class RedirectFailure(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class RecordedRedirects(HTTPRedirectHandler):
    def __init__(self, original, trace):
        self.original, self.trace = original, trace

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.trace.append(f'HTTP {code}: {req.full_url} → {target}')
        before, after = urlsplit(self.original), urlsplit(target)
        if before.netloc.lower() != after.netloc.lower() or after.scheme not in ('http', 'https'):
            raise RedirectFailure(f'跨域或非HTTP重定向，未访问目标：{target}', code)
        if before.scheme == 'https' and after.scheme != 'https':
            raise RedirectFailure(f'HTTPS降级重定向，未访问目标：{target}', code)
        return super().redirect_request(req, fp, code, msg, headers, target)


def urlopen(req, timeout):
    # Keep a patchable entry point for tests; block unsafe redirects before following.
    return build_opener(RecordedRedirects(req.full_url, req.radar_redirects)).open(req, timeout=timeout)


def access_check(root):
    visible = root.text()
    headings = ' '.join(n.text() for n in root.nodes() if n.tag in ('title', 'h1'))
    if any(x in visible for x in ('访问过于频繁', '访问被拒绝')) or \
       re.search(r'人机验证|访问验证|安全验证|Access Denied|Service Unavailable', headings, re.I) or \
       ('请输入验证码' in visible and len(visible.strip()) < 500):
        raise AccessRestriction('网站返回访问限制、验证或服务异常页；不重试、不绕过，不能判定无公告或无附件')

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')

def canonical(url):
    p = urlsplit(url)
    return urlunsplit(('https', p.netloc.lower(), p.path, p.query, ''))

class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []
    def text(self):
        if self.tag in ('script','style'):
            return ''
        return ''.join(c if isinstance(c, str) else c.text() for c in self.children)
    def nodes(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.nodes()

class DOM(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.current)
        self.current.children.append(n)
        if tag not in ('meta','link','img','br','hr','input','source','wbr','embed','area','base','param'):
            self.current = n
    def handle_startendtag(self, tag, attrs):
        pass
    def handle_endtag(self, tag):
        n = self.current
        while n.parent:
            if n.tag == tag:
                self.current = n.parent
                return
            n = n.parent
    def handle_data(self, data):
        self.current.children.append(data)

def parse_list(text, source):
    root = DOM(text).root
    access_check(root)
    if source['marker'] not in text:
        raise ValueError(f'栏目标识“{source["marker"]}”缺失，可能改版或返回错误页；不能判定无公告')
    found, rows = {}, 0
    for a in root.nodes():
        href = a.attrs.get('href','')
        if a.tag != 'a' or not re.search(r'content_\d+\.html', href):
            continue
        url = canonical(urljoin(source['url'], href))
        if urlsplit(url).netloc != urlsplit(source['url']).netloc:
            continue
        n = a
        while n.parent and n.tag not in ('li','tr'):
            n = n.parent
        d = re.search(DATE, n.text())
        if not d:
            continue
        rows += 1
        title = a.attrs.get('title') or a.text().strip()
        if re.search(KEYWORDS, title):
            found[url] = dict(url=url,title=title.strip(),published=f'{d[1]}-{int(d[2]):02}-{int(d[3]):02}')
    if rows == 0:
        raise ValueError('未识别出带日期的公告行，可能改版；不能判定无公告')
    return list(found.values()), rows

def parse_attachments(text, url, expected_title=None):
    root = DOM(text).root
    access_check(root)
    if not any(n.tag in ('h1','title') and n.text().strip() for n in root.nodes()) or 'content_' not in url:
        raise ValueError('正文结构校验失败')
    if expected_title:
        normalized = re.sub(r'\s+', '', expected_title)
        headings = [re.sub(r'\s+', '', n.text()) for n in root.nodes() if n.tag in ('title','h1','h2','h3')]
        if not any(normalized in title for title in headings):
            raise ValueError('正文标题与公告不符，可能为拦截或错误页；不能判定无附件')
    result = {}
    for n in root.nodes():
        href = n.attrs.get('href','')
        if n.tag == 'a' and re.search(r'\.(pdf|xlsx?|docx?|zip|rar)(?:[?#]|$)', href, re.I):
            full = urljoin(url, href)
            if urlsplit(full).scheme in ('http','https'):
                result[full] = {'name':n.text().strip() or '附件','url':full}
    return list(result.values())

def connect(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS announcements(url TEXT PRIMARY KEY,title TEXT,published TEXT,first_seen TEXT,last_seen TEXT,attachments TEXT,detail_state TEXT);
    CREATE TABLE IF NOT EXISTS sightings(source TEXT,url TEXT,PRIMARY KEY(source,url));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,run TEXT,source TEXT,url TEXT,kind TEXT,visited TEXT,http INTEGER,state TEXT,count INTEGER,error TEXT,seconds REAL,snapshot TEXT);
    CREATE TABLE IF NOT EXISTS revisions(url TEXT,seen TEXT,payload TEXT,PRIMARY KEY(url,payload));''')
    return c

def save(c, item, source, stamp):
    c.execute("INSERT INTO announcements VALUES(?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,published=excluded.published,last_seen=excluded.last_seen,detail_state='待获取（保留历史附件）'", (item['url'],item['title'],item['published'],stamp,stamp,'[]','待获取'))
    c.execute('INSERT OR IGNORE INTO sightings VALUES(?,?)',(source,item['url']))
    c.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?)',(item['url'],stamp,json.dumps(item,ensure_ascii=False,sort_keys=True)))
    c.commit()

def fetch(url, directory):
    start = time.monotonic()
    visited = now()
    status, text, error, snapshot = None, '', '', ''
    final_url, info, error_type = url, '', ''
    redirects = []
    phase = '请求'
    try:
        req = Request(url, headers={'User-Agent':'XiangtanRecruitmentRadar/0.1 (personal public-notice monitor)'})
        req.radar_redirects = redirects
        with urlopen(req, timeout=15) as r:
            status = r.status
            final_url = r.url
            if urlsplit(final_url).netloc.lower() != urlsplit(url).netloc.lower():
                raise RedirectFailure(f'最终地址跨域：{final_url}', status)
            if status != 200:
                raise ValueError(f'非正常正文响应 HTTP {status}（可能为空响应或缓存响应）；不能判定无公告')
            raw = r.read(5_000_001)
            if len(raw)>5_000_000:
                raise ValueError('响应超过5MB限制')
            charset = re.search(br'charset\s*=\s*["\x27]?([\w-]+)',raw[:4096],re.I)
            phase = '解码'
            encoding = charset[1].decode() if charset else r.headers.get_content_charset() or 'utf-8'
            if encoding.lower() in ('gb2312', 'gbk'):
                encoding = 'gb18030'  # Superset used by Chinese government pages.
            text = raw.decode(encoding)
            phase = '响应校验'
            title = next((n.text().strip() for n in DOM(text).root.nodes() if n.tag == 'title'), '无标题')
            info = f'最终地址={final_url}；响应={len(raw)}字节；编码={encoding}；页面标题={title[:160]}'
            content_type = r.headers.get('Content-Type', '')
            if content_type:
                info += f'；Content-Type={content_type}'
            if content_type and not re.search(r'(?:text/html|application/xhtml\+xml)', content_type, re.I):
                raise ValueError(f'响应类型不是HTML：{content_type}')
            if os.environ.get('RADAR_SAVE_SNAPSHOTS', '1') != '0':
                phase = '保存响应'
                snapshot = str(directory / (hashlib.sha256(raw).hexdigest()+'.html'))
                Path(snapshot).write_bytes(raw)
                snapshot = 'data/snapshots/' + Path(snapshot).name
    except RedirectFailure as e:
        status, error_type = e.code, '重定向异常'
        error = str(e)
    except HTTPError as e:
        status, final_url = e.code, e.geturl()
        error_type = '重定向异常' if e.code in (301, 302, 303, 307, 308) else 'HTTP错误'
        error = f'HTTP {e.code} {e.reason}；最终地址={final_url}；不重试、不绕过限制'
        e.close()
    except Exception as e:
        reason = e.reason if isinstance(e, URLError) else e
        if phase in ('解码', '保存响应'):
            error_type, hint = '响应处理异常', f'{phase}失败'
        elif isinstance(reason, (TimeoutError, socket.timeout)) or \
             (isinstance(reason, str) and re.search(r'timed?\s*out|timeout', reason, re.I)):
            error_type, hint = '请求超时', '15秒请求超时；未能完整读取网页'
        elif isinstance(reason, socket.gaierror):
            error_type, hint = 'DNS解析失败', '无法解析官方域名'
        elif isinstance(reason, ssl.SSLError):
            error_type, hint = 'TLS连接异常', 'HTTPS握手或证书校验失败；不会关闭证书校验'
        elif isinstance(reason, (ConnectionError, OSError)) or isinstance(e, URLError):
            error_type, hint = '网络连接异常', '无法建立或完整读取连接'
            if getattr(reason, 'errno', None) in (101, 10051):
                hint = '运行环境网络不可达；尚未取得网页，未执行公告解析'
        else:
            error_type, hint = '响应处理异常', '响应读取、解码或保存失败'
        error = f'{hint}；{type(reason).__name__}: {reason}'
    if redirects:
        info = (info + '；' if info else '') + '重定向链=' + ' | '.join(redirects)
    if error:
        error = f'[{error_type}] {error}'
    return dict(http=status,text=text,error=error,error_type=error_type,info=info,
                final_url=final_url,redirects=redirects,
                seconds=round(time.monotonic()-start,3),snapshot=snapshot,visited=visited)


def request_summary(source, url, kind, result, state, count, summary):
    http = result['http'] if result['http'] is not None else '未取得响应'
    print(f'[{source}][{kind}] {state} | HTTP={http} | 条数={count} | '
          f'耗时={result["seconds"]}秒 | URL={url} | {(summary or "无错误").replace(chr(10), " ").replace(chr(13), " ")}', flush=True)

def log(c,run,source,url,kind,result,state,count=0,error=''):
    summary = error or result['error']
    if result.get('info'):
        summary += ('；' if summary else '') + result['info']
    c.execute('INSERT INTO logs(run,source,url,kind,visited,http,state,count,error,seconds,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(run,source,url,kind,result['visited'],result['http'],state,count,summary,result['seconds'],result['snapshot']))
    c.commit()
    request_summary(source, url, kind, result, state, count, summary)

def render(c, output, sources):
    e = lambda x: html.escape(str(x if x is not None else '—'),quote=True)
    logs = c.execute('SELECT * FROM logs ORDER BY id DESC').fetchall()
    rows = c.execute('SELECT * FROM announcements ORDER BY published DESC,url').fetchall()
    latest = {}
    for l in logs:
        if l['kind']=='栏目': latest.setdefault(l['source'],l)
    cards = ''
    for s in sources:
        l=latest.get(s['id'])
        state=l['state'] if l else '未运行'
        elapsed=c.execute('SELECT coalesce(sum(seconds),0) FROM logs WHERE source=? AND run=?',(s['id'],l['run'])).fetchone()[0] if l else 0
        tone='bad' if '失败' in state else 'ok'
        http=e(l['http']) if l and l['http'] is not None else '未取得响应'
        summary=e(l['error']) if l else '尚无日志'
        cards+=f'<article><h3>{e(s["name"])}</h3><b class="{tone}">{e(state)}</b><p>{e(l["visited"] if l else "尚无日志")}</p><p>HTTP：{http}</p><p>{summary}</p><p>本轮请求耗时合计：{elapsed:.1f} 秒（不含间隔等待）</p><a href="{e(s["url"])}">官方栏目</a></article>'
    notices=''
    for r in rows:
        attachments=json.loads(r['attachments'])
        links=' '.join(f'<a href="{e(a["url"])}">{e(a["name"])}</a>' for a in attachments)
        notices+=f'<tr><td><a href="{e(r["url"])}">{e(r["title"])}</a></td><td>{e(r["published"])}</td><td>{e(r["first_seen"])}</td><td>{e(r["detail_state"])}<br>{links}</td></tr>'
    history=''.join(f'<tr><td>{e(l["visited"])}</td><td>{e(l["source"])} / {e(l["kind"])}</td><td>{e(l["http"])}</td><td>{e(l["state"])}</td><td>{e(l["count"])}</td><td>{e(l["seconds"])}</td><td>{e(l["error"])}</td><td><a href="{e(l["url"])}">原链接</a></td></tr>' for l in logs)
    document=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>湘潭招聘雷达</title><style>body{{font:16px/1.65 system-ui;margin:30px auto;max-width:1300px;padding:0 18px;background:#f3f6fa;color:#172c42}}a{{color:#065fa7}}.bad{{color:#b42318}}.ok{{color:#087443}}.cards{{display:flex;gap:14px;flex-wrap:wrap}}article{{background:white;overflow-wrap:anywhere;padding:18px;border:1px solid #d0dce6;border-radius:10px;flex:1;min-width:220px}}table{{border-collapse:collapse;background:white;width:100%;font-size:14px}}td,th{{padding:10px;border:1px solid #d0dce6;text-align:left}}.scroll{{overflow:auto}}.note{{background:#fff0cf;padding:16px}}footer{{margin-top:30px}}</style><h1>湘潭招聘雷达 <small>第一阶段</small></h1><p>生成时间：{e(now())}（北京时间）</p><p class="note">仅监测下列3个栏目当前首页，不代表湘潭全域，也不保证历史完整。公告包含拟聘用、体检等进度信息，不等于正在报名。失败表示信息未知；成功仅表示本页已读取。超过48小时未运行请检查。未实现附件内容解析、资格匹配和每周简报。</p><div class="cards">{cards}</div><h2>历史公告（{len(rows)}条）</h2><p>附件仅识别常见文件扩展名链接；动态下载、图片附件可能遗漏。请核对官方正文。未进行报考资格判定。</p><div class="scroll"><table><tr><th>公告／官方原文</th><th>发布日期</th><th>首次发现</th><th>附件链接获取状态</th></tr>{notices}</table></div><h2>完整监测日志</h2><p>抓取条数：栏目为识别到的相关公告数；正文为附件链接数。HTTP 200仍可能解析失败。历史公告与日志保存在数据目录；云端默认不保存原始网页快照。网站访问失败、网页解析失败均不代表没有岗位。</p><div class="scroll"><table><tr><th>访问时间</th><th>来源／类型</th><th>HTTP</th><th>结果</th><th>条数</th><th>耗时秒</th><th>错误／摘要</th><th>官方链接</th></tr>{history}</table></div><footer>开源个人项目 · 无个人资料 · 资格条件以官方公告及招聘单位解释为准</footer></html>'''
    output.mkdir(parents=True,exist_ok=True)
    (output/'index.html').write_text(document,encoding='utf-8')
    (output/'.nojekyll').touch()

def run(root):
    sources=json.loads((root/'sources.json').read_text(encoding='utf-8'))
    data=root/'data';data.mkdir(exist_ok=True)
    stamp=now();run_id=stamp+'_'+uuid.uuid4().hex[:12];snapshots=data/'snapshots';snapshots.mkdir(exist_ok=True)
    c=connect(data/'radar.sqlite3');failed=False
    for source in sources:
        r=fetch(source['url'],snapshots)
        items=[];error=r['error'];scanned=0;new_count=0
        state='响应处理失败' if r.get('error_type') == '响应处理异常' else '网站访问失败'
        if not error:
            try:
                items,scanned=parse_list(r['text'],source)
                new_count=sum(c.execute('SELECT 1 FROM announcements WHERE url=?',(item['url'],)).fetchone() is None for item in items)
                state='成功' if new_count else ('成功：无新公告（本页已收录）' if items else '成功：本页无关键词公告')
            except AccessRestriction as ex:
                state='网站访问失败';error=str(ex)
            except Exception as ex:
                state='网页解析失败';error=f'{type(ex).__name__}: {ex}'
        if error:failed=True
        summary=error or f'识别公告行{scanned}条；相关公告{len(items)}条；本轮新增{new_count}条（按官方链接去重）'
        log(c,run_id,source['id'],source['url'],'栏目',r,'正文处理中' if items else state,len(items),summary)
        details_failed=0;detail_errors=[]
        for item in items:
            save(c,item,source['id'],stamp)
        render(c,root/'site',sources)
        for item in items:
            # Serial requests; do not retry or bypass an access restriction.
            time.sleep(.5)
            d=fetch(item['url'],snapshots);err=d['error'];attachments=[]
            detail_state='响应处理失败' if d.get('error_type') == '响应处理异常' else '网站访问失败'
            blocked=False
            if not err:
                try:
                    attachments=parse_attachments(d['text'],item['url'],item['title'])
                    detail_state='成功'
                except AccessRestriction as ex:
                    detail_state='网站访问失败';err=str(ex);blocked=True
                except Exception as ex:
                    detail_state='网页解析失败';err=f'{type(ex).__name__}: {ex}'
            if err:
                details_failed+=1;failed=True
                detail_errors.append(f'{detail_state}：{err}（{item["url"]}）')
                c.execute('UPDATE announcements SET detail_state=? WHERE url=?',(f'{detail_state}（保留历史附件，可能过期）',item['url']))
            else:
                payload=json.dumps(attachments,ensure_ascii=False)
                c.execute('UPDATE announcements SET attachments=?,detail_state=? WHERE url=?',(payload,'已读取（仅链接识别）',item['url']))
                c.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?)',(item['url'],stamp,payload))
            log(c,run_id,source['id'],item['url'],'正文',d,detail_state,len(attachments),err)
            render(c,root/'site',sources)
            if d['http'] in (403,429) or blocked or '访问限制' in err:
                break
        if details_failed:
            state='部分失败'
            summary+=f'；正文失败{details_failed}篇；首个原因：{detail_errors[0]}；其余见正文日志（可能因访问限制提前停止）'
        if r.get('info'):
            summary+='；'+r['info']
        c.execute("UPDATE logs SET state=?,error=? WHERE run=? AND source=? AND kind='栏目'",(state,summary,run_id,source['id']))
        c.commit()
        render(c,root/'site',sources)
        request_summary(source['name'],source['url'],'栏目汇总',r,state,len(items),summary)
    render(c,root/'site',sources)
    (data/'logs.json').write_text(json.dumps([dict(r) for r in c.execute('SELECT * FROM logs')],ensure_ascii=False,indent=2),encoding='utf-8')
    c.close()
    return 1 if failed else 0
