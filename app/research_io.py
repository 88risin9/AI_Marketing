"""Bounded search APIs and a public-web reader; never returns credentials or raw errors."""
import http.client
import ipaddress
import json
import os
import re
import queue
import threading
import socket
import ssl
import stat
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, urljoin, urlencode
from . import ai
from .db import now

class ResearchError(RuntimeError): pass

KEYS = ('RESEARCH_SEARCH_PROVIDER', 'RESEARCH_SEARCH_API_KEY', 'RESEARCH_READER_ENABLED', 'RESEARCH_HTTP_TIMEOUT')

def config():
    values = {}
    path = ai.ENV_PATH
    if path.exists():
        if path.is_symlink() or (os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise ResearchError('本地配置权限不正确，请运行 chmod 600 .env。')
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                if '=' not in line or line.lstrip().startswith('#'): continue
                key, value = line.split('=', 1)
                if key.strip() in KEYS: values[key.strip()] = value.strip().strip('\"\'')
        except OSError: raise ResearchError('无法读取研究配置，请检查 .env。') from None
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    provider = values.get('RESEARCH_SEARCH_PROVIDER', 'brave').lower()
    key = values.get('RESEARCH_SEARCH_API_KEY', '').strip()
    if provider not in ('brave', 'tavily', 'disabled') or any(ord(c) < 32 for c in key):
        raise ResearchError('搜索服务配置不正确，请选择 brave、tavily 或 disabled。')
    try: timeout = int(values.get('RESEARCH_HTTP_TIMEOUT', '15'))
    except ValueError: raise ResearchError('网页超时应为 5 到 30 秒。') from None
    if not 5 <= timeout <= 30: raise ResearchError('网页超时应为 5 到 30 秒。')
    enabled = values.get('RESEARCH_READER_ENABLED', 'true').lower()
    if enabled not in ('true', 'false'): raise ResearchError('RESEARCH_READER_ENABLED 应为 true 或 false。')
    return {'provider': provider, 'key': key, 'reader': enabled == 'true', 'timeout': timeout}

def capabilities():
    model = ai.get_config()
    result = {'model': model['configured'], 'model_name': model.get('model', ''), 'search': False,
              'reader': False, 'search_provider': '', 'missing': [], 'cost': None,
              'cost_note': '仅显示实际调用次数；未配置可靠单价，不计算费用。'}
    try:
        cfg = config()
        result.update(search=bool(cfg['key']) and cfg['provider'] != 'disabled', reader=cfg['reader'], search_provider=cfg['provider'])
    except ResearchError as exc: result['missing'].append(str(exc))
    if not result['model']: result['missing'].append('模型未接入：请配置 V1 的 AI_API_KEY 与 AI_MODEL。')
    if not result['search']: result['missing'].append('搜索未接入：请配置 RESEARCH_SEARCH_API_KEY 和服务商。')
    if not result['reader']: result['missing'].append('网页读取未启用。')
    return result

def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        raise ResearchError('网址格式不正确。')
    try:
        part = urlsplit(value)
        host = (part.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port = part.port
    except (ValueError, UnicodeError): raise ResearchError('网址格式不正确。') from None
    if part.scheme not in ('http', 'https') or not host or part.username or part.password or port not in (None, 80, 443):
        raise ResearchError('只支持不含账号信息的公开 HTTP/HTTPS 网页，端口须为 80 或 443。')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '%' in host or '\\' in value:
        raise ResearchError('禁止访问本机、内网或特殊用途地址。')
    try:
        if not ipaddress.ip_address(host).is_global: raise ResearchError('禁止访问本机、内网或特殊用途地址。')
    except ValueError: pass
    netloc = '[' + host + ']' if ':' in host else host
    if port and port != (443 if part.scheme == 'https' else 80): netloc += ':' + str(port)
    return urlunsplit((part.scheme, netloc, part.path or '/', part.query, ''))

def domain(value):
    return (urlsplit(canonical_url(value)).hostname or '').removeprefix('www.')

def public_addresses(host, port, timeout=15):
    # DNS itself may block independently of socket HTTP timeouts. A daemon resolver
    # limits how long a research worker waits, without changing process-wide DNS.
    output = queue.Queue(maxsize=1)
    def resolve():
        try: output.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError: output.put(None)
    threading.Thread(target=resolve, daemon=True).start()
    try: addresses = output.get(timeout=max(0.1, timeout))
    except queue.Empty: raise ResearchError('网页域名解析超时，已停止本次读取。') from None
    if not addresses: raise ResearchError('无法解析网页域名，请检查网址或稍后重试。')
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global or (getattr(address, 'ipv4_mapped', None) and not address.ipv4_mapped.is_global):
            raise ResearchError('网页域名解析到内网或特殊用途地址，已停止访问。')
    return addresses

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout, tls=False):
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self.tls = tls
    def connect(self):
        family, kind, proto, _, sockaddr = self.address
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(sockaddr)
            if self.tls: sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except Exception:
            sock.close()
            raise

def fetch_bytes(url, *, method='GET', headers=None, body=None, timeout=15, redirects=3, max_bytes=2_000_000):
    """Pin checked DNS address to the socket; re-check every redirect; ignore environment proxies."""
    url = canonical_url(url)
    started = time.monotonic()
    for hop in range(redirects + 1):
        part = urlsplit(url)
        port = part.port or (443 if part.scheme == 'https' else 80)
        address = public_addresses(part.hostname, port, timeout - (time.monotonic() - started))[0]
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0: raise ResearchError('网页请求超时，已有资料已保留。')
        conn = PinnedHTTP(part.hostname, port, address, remaining, part.scheme == 'https')
        try:
            request_headers = {'User-Agent': 'TradeWorkbenchResearch/2.0 (public company research)', 'Accept-Encoding': 'identity', **(headers or {})}
            conn.request(method, urlunsplit(('', '', part.path or '/', part.query, '')), body, request_headers)
            transport_socket = conn.sock
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if method != 'GET' or hop == redirects: raise ResearchError('服务重定向过多或接口地址发生变化，请检查配置。')
                location = response.getheader('Location')
                if not location: raise ResearchError('网页重定向无有效地址。')
                url = canonical_url(urljoin(url, location))
                # Never forward any API credential to a redirected host.
                if headers and any(k.lower() in ('authorization', 'x-subscription-token') for k in headers):
                    raise ResearchError('搜索服务发生重定向，已停止请求。')
                continue
            if response.status >= 400:
                raise ResearchError(f'网页或搜索服务暂时无法访问（HTTP {response.status}）；请检查服务权限、额度或网址。')
            chunks = []; received = 0
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0: raise ResearchError('网页请求超时，已有资料已保留。')
                if transport_socket: transport_socket.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - received))
                if not chunk: break
                chunks.append(chunk); received += len(chunk)
                if received > max_bytes: raise ResearchError('网页内容超过读取上限，已停止。')
            data = b''.join(chunks)
            return url, dict((k.lower(), v) for k, v in response.getheaders()), data
        except ResearchError: raise
        except (OSError, http.client.HTTPException, UnicodeError):
            raise ResearchError('网页或搜索请求失败，已有资料已保留；请检查网络后重试。') from None
        finally: conn.close()
    raise ResearchError('无法完成网页请求。')

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0; self.title_depth = 0; self.parts = []; self.titles = []; self.links = []
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'): self.hidden += 1
        if tag == 'title': self.title_depth += 1
        if tag == 'a' and not self.hidden:
            href = dict(attrs).get('href', '')
            if href: self.links.append(href[:2000])
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'): self.hidden = max(0, self.hidden - 1)
        if tag == 'title': self.title_depth = max(0, self.title_depth - 1)
    def handle_data(self, data):
        if not self.hidden:
            if self.title_depth: self.titles.append(data)
            self.parts.append(data)

def read_page(url):
    cfg = config()
    if not cfg['reader']: raise ResearchError('网页读取未启用；可以手动提供企业资料。')
    final, headers, data = fetch_bytes(url, timeout=cfg['timeout'])
    media = headers.get('content-type', '').lower()
    if not any(t in media for t in ('text/html', 'application/xhtml+xml', 'text/plain')):
        raise ResearchError('当前网页读取仅支持 HTML 和纯文本；PDF 目录请人工摘录并注明来源。')
    charset = re.search(r'charset=([\w-]+)', media)
    encoding = charset[1] if charset else 'utf-8'
    try: content = data.decode(encoding, errors='replace')
    except LookupError: content = data.decode('utf-8', errors='replace')
    parser = TextParser()
    if 'html' in media: parser.feed(content)
    else: parser.parts = [content]
    text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()[:16000]
    if len(text) < 40: raise ResearchError('网页可读内容太少，可能需要 JavaScript、登录或人工核验。')
    links = []
    for href in parser.links[:300]:
        try: link = canonical_url(urljoin(final, href))
        except ResearchError: continue
        if link not in links: links.append(link)
    emails = sorted(set(re.findall(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text)))[:20]
    return {'url': final, 'requested_url': url, 'title': re.sub(r'\s+', ' ', ' '.join(parser.titles)).strip()[:300],
            'text': text, 'links': links[:80], 'emails': emails, 'retrieved_at': now(),
            'issues': ['网页发布日期及当前有效性尚待核实。'], 'kind': 'unknown'}

def search_web(query):
    cfg = config()
    if not cfg['key'] or cfg['provider'] == 'disabled': raise ResearchError('搜索未接入，请配置本地搜索密钥。')
    query = str(query).strip()
    if not query or len(query) > 500 or len(query.split()) > 70: raise ResearchError('搜索词过长或为空。')
    if cfg['provider'] == 'brave':
        _, _, data = fetch_bytes('https://api.search.brave.com/res/v1/web/search?' + urlencode({'q': query, 'count': 8, 'text_decorations': 'false', 'extra_snippets': 'false'}),
                                 headers={'Accept': 'application/json', 'X-Subscription-Token': cfg['key']}, timeout=cfg['timeout'], redirects=0)
    else:
        body = json.dumps({'query': query, 'search_depth': 'basic', 'max_results': 8, 'include_answer': False, 'include_raw_content': False}).encode()
        _, _, data = fetch_bytes('https://api.tavily.com/search', method='POST', body=body,
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg['key']}, timeout=cfg['timeout'], redirects=0)
    try:
        result = json.loads(data)
        rows = result.get('web', {}).get('results', []) if cfg['provider'] == 'brave' else result['results']
        if not isinstance(rows, list): raise ValueError()
        cleaned = []
        for row in rows[:8]:
            try: url = canonical_url(row.get('url', ''))
            except (ResearchError, AttributeError): continue
            cleaned.append({'url': url, 'title': str(row.get('title', ''))[:300],
                            'snippet': str(row.get('description', row.get('content', '')))[:1000], 'queried_at': now()})
        return cleaned
    except (ValueError, TypeError, AttributeError, KeyError): raise ResearchError('搜索服务返回格式不正确，已有资料已保留。') from None
