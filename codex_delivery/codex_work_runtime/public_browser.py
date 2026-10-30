"""Public, read-only Chromium browsing through an exact-grant fetch broker.

This module is a trusted adapter, never a general task-code runtime.  Grant
issuance and BrowserRuntime configuration belong to the controller/admin, not
the model or page.  Only the browser process is network-isolated; the fixed
driver forwards intercepted requests to this broker over bounded stdio IPC.
No account storage, task workspace, caller headers or arbitrary JS is accepted.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit

from .errors import WorkRuntimeError


MAX_RESOURCE = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_FRAME = 8 * 1024 * 1024
_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?\Z")
_SCOPE = frozenset({'source_event_ref', 'actor_ref', 'conversation_ref', 'task_ref', 'input_revision'})
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
SHANGHAI = ZoneInfo('Asia/Shanghai')
_MIMES = frozenset({"text/html", "text/plain", "text/css", "text/javascript",
                    "application/javascript", "application/json", "image/png",
                    "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
                    "image/x-icon", "font/woff", "font/woff2"})
_CHARSETS = frozenset({'utf-8', 'utf8', 'gb18030', 'gbk', 'gb2312', 'big5',
                       'windows-1252', 'iso-8859-1', 'us-ascii'})


def _content_type(value: str) -> str:
    # Forward only fixed MIME/charset data, never arbitrary upstream headers.
    # Dropping charset corrupts Chinese pages without an HTML meta declaration.
    if (not isinstance(value, str) or len(value) > 512
            or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise _fail('browser_content_type_rejected')
    mime, *parameters = value.split(';')
    mime = mime.strip().lower()
    if mime not in _MIMES:
        raise _fail('browser_content_type_rejected')
    charset = None
    for parameter in parameters:
        key, separator, raw = parameter.partition('=')
        if key.strip().lower() != 'charset':
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        raw = raw.lower()
        if not separator or charset is not None or raw not in _CHARSETS:
            raise _fail('browser_content_type_rejected')
        charset = raw
    return mime if charset is None else mime + '; charset=' + charset


def _fail(code: str) -> WorkRuntimeError:
    return WorkRuntimeError(code, "公共浏览未通过固定只读策略。")


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _fail("browser_dns_invalid") from None
    if (not address.is_global or address.is_multicast or address.is_reserved
            or (isinstance(address, ipaddress.IPv6Address)
                and (address.ipv4_mapped is not None or address.sixtofour is not None
                     or address.teredo is not None))):
        raise _fail("browser_destination_not_public")
    return str(address)


def _url(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in value) or any(c in value for c in '\\<>\"\'')):
        raise _fail("browser_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _fail("browser_url_invalid") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or "%" in parsed.netloc
            or port not in {None, 443 if parsed.scheme == "https" else 80}):
        raise _fail("browser_url_invalid")
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (not _HOST.fullmatch(host) or "." not in host
                or any(not label or len(label) > 63 or label.startswith("-")
                       or label.endswith("-") for label in host.split("."))):
            raise _fail("browser_url_invalid")
    else:
        _public_ip(host)
    # Do not normalize, append queries, or grant a whole origin.  In particular,
    # a private file/context cannot become an additional public search query.
    return value


@dataclass(frozen=True)
class PublicBrowserGrant:
    """Short-lived response from the authenticated Controller grant callback.

    This is not an authentication token.  Do not expose its constructor/issuer
    as a model tool or accept a model-supplied 'original_input'.
    """

    urls: tuple[str, ...]
    expires_at: datetime
    monotonic_deadline: float
    grant_ref: str
    input_digest: str

    @classmethod
    def from_callback_response(cls, response: dict, *, expected_scope: dict,
                               exact_urls: list[str]) -> PublicBrowserGrant:
        # Callback transport authentication is mandatory at the caller. Model
        # arguments, original_input supplied by a model and page content are
        # never accepted here as an authority source.
        try:
            if (not isinstance(expected_scope, dict) or set(expected_scope) != _SCOPE
                    or type(expected_scope['input_revision']) is not int or not 1 <= expected_scope['input_revision'] <= 2**53-1
                    or any(not isinstance(expected_scope[key], str) or _REF.fullmatch(expected_scope[key]) is None
                           for key in _SCOPE - {'input_revision'})
                    or not isinstance(exact_urls, list) or not 1 <= len(exact_urls) <= 64
                    or not isinstance(response, dict)
                    or set(response) != {'version', 'scope', 'urls', 'input_digest', 'grant_ref', 'expires_at'}
                    or type(response['version']) is not int or response['version'] != 1
                    or not isinstance(response['scope'], dict) or response['scope'] != expected_scope
                    or type(response['scope'].get('input_revision')) is not int
                    or response['urls'] != exact_urls or not isinstance(response['urls'], list)
                    or not isinstance(response['input_digest'], str) or _SHA.fullmatch(response['input_digest']) is None
                    or not isinstance(response['grant_ref'], str) or re.fullmatch(r'BG-[a-f0-9]{32}', response['grant_ref']) is None):
                raise ValueError('grant')
            checked = tuple(_url(url) for url in exact_urls)
            if len(set(checked)) != len(checked) or sum(map(len, checked)) > 32768:
                raise ValueError('urls')
            expires = datetime.fromisoformat(response['expires_at'])
            instant = _now()
            if (expires.utcoffset() != timedelta(hours=8) or expires.isoformat() != response['expires_at']
                    or not instant < expires <= instant + timedelta(seconds=300)):
                raise ValueError('expiry')
            return cls(checked, expires, time.monotonic() + (expires - instant).total_seconds(),
                       response['grant_ref'], response['input_digest'])
        except (ValueError, KeyError, TypeError, OverflowError):
            raise _fail('browser_grant_invalid') from None

    def remaining(self) -> float:
        remaining = min((self.expires_at - _now()).total_seconds(), self.monotonic_deadline - time.monotonic())
        if remaining <= 0:
            raise _fail('browser_grant_expired')
        return remaining

    def require(self, url: str) -> None:
        self.remaining()
        if _url(url) not in self.urls:
            raise _fail("browser_url_not_granted")


@dataclass(frozen=True)
class PublicResponse:
    url: str
    status: int
    content_type: str
    body: bytes
    location: str | None = None


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, *, tls: bool, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address, self.tls = address, tls

    def connect(self) -> None:
        # Numeric address has already passed the complete DNS-answer check;
        # create_connection does not re-resolve the hostname at connection time.
        address = ipaddress.ip_address(self.address)
        sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                             socket.SOCK_STREAM)
        deadline = time.monotonic() + self.timeout
        self.sock = sock
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, self.port))
            if self.tls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(remaining)
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


class PublicFetchBroker:
    """No proxy env, cookies, auth, referer, request body or caller headers.

    Redirects are returned to Chromium after validating the exact target and
    resolving all its DNS answers.  Chromium's next request is checked anew.
    A route interception gap fails at the OS network boundary.
    """

    def __init__(self, grant: PublicBrowserGrant, *, owner_deadline: bool = False):
        if type(owner_deadline) is not bool:
            raise ValueError('deadline strategy')
        self.grant = grant
        # Only the fixed subprocess owner selects this after installing its
        # independent per-request kill deadline. Other callers retain Timer.
        self.owner_deadline = owner_deadline
        self.requests = 0
        self.total_bytes = 0

    def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise _fail("browser_dns_failed") from None
        if not records or len(records) > 64:
            raise _fail("browser_dns_invalid")
        return tuple(dict.fromkeys(_public_ip(item[4][0]) for item in records))

    def fetch(self, url: str, method: str = "GET") -> PublicResponse:
        self.grant.require(url)
        if not isinstance(method, str) or method not in {"GET", "HEAD"}:
            raise _fail("browser_method_rejected")
        self.requests += 1
        if self.requests > 64:
            raise _fail("browser_request_limit")
        parsed = urlsplit(url)
        port = 443 if parsed.scheme == "https" else 80
        deadline = time.monotonic() + min(8, self.grant.remaining())
        addresses = self._resolve(parsed.hostname, port)
        self.grant.require(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _fail('browser_timeout')
        connection = _PinnedHTTP(parsed.hostname, port, addresses[0],
                                 tls=parsed.scheme == "https", timeout=remaining)
        expired = threading.Event()
        active_socket = []
        def abort():
            expired.set()
            active = active_socket[0] if active_socket else connection.sock
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        timer = None if self.owner_deadline else threading.Timer(remaining, abort)
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            self.grant.require(url)
            if expired.is_set():
                raise _fail('browser_timeout')
            path = parsed.path or "/"
            if '?' in url:
                path += "?" + parsed.query
            connection.request(method, path, headers={"Accept": "*/*", "Accept-Encoding": "identity",
                               "User-Agent": "Codex-Public-Browser/1", "Connection": "close"})
            if connection.sock is not None:
                active_socket.append(connection.sock)
            response = connection.getresponse()
            if response.getheader("Content-Disposition") is not None:
                raise _fail("browser_download_rejected")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise _fail("browser_encoding_rejected")
            content_type = response.getheader("Content-Type", "")
            location = None
            if response.status in {301, 302, 303, 307, 308}:
                target = response.getheader("Location", "")
                if not target:
                    raise _fail("browser_redirect_invalid")
                location = urljoin(url, target)
                self.grant.require(location)
                redirect = urlsplit(location)
                self._resolve(redirect.hostname, 443 if redirect.scheme == "https" else 80)
                body = b""
            else:
                content_type = _content_type(content_type)
                body = response.read(MAX_RESOURCE + 1)
                if len(body) > MAX_RESOURCE:
                    raise _fail("browser_resource_limit")
            self.total_bytes += len(body)
            self.grant.require(url)
            if expired.is_set():
                raise _fail('browser_timeout')
            if self.total_bytes > MAX_TOTAL:
                raise _fail("browser_total_limit")
            return PublicResponse(url, response.status, content_type if location is None else '', body, location)
        except (OSError, http.client.HTTPException):
            raise _fail("browser_fetch_failed") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()


@dataclass(frozen=True)
class BrowserRuntime:
    """Administrator-owned paths; never accept these from a work request."""

    node_binary: str
    playwright_module: str
    chromium_binary: str

    def validate(self) -> None:
        for value in (self.node_binary, self.chromium_binary):
            if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                raise _fail("browser_runtime_unavailable")
        if (not Path(self.playwright_module).is_absolute()
                or not (Path(self.playwright_module) / "package.json").is_file()):
            raise _fail("browser_runtime_unavailable")


def _isolation_prefix(root: Path, runtime: BrowserRuntime) -> list[str]:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        temporary = json.dumps(str(root.resolve()))
        chromium = Path(runtime.chromium_binary).resolve()
        chromium_root = next((parent for parent in chromium.parents if parent.suffix == ".app"), chromium.parent)
        # Only per-task UNIX sockets; deny all IP sockets and other user-home
        # reads, then allow the administrator-pinned browser runtime itself.
        policy = ("(version 1)(allow default)(deny network*)"
                  f"(allow network* (local unix-socket (subpath {temporary}))"
                  f" (remote unix-socket (subpath {temporary})))"
                  "(deny file-read* (subpath \"/Users\"))"
                  f"(allow file-read* (subpath {json.dumps(str(chromium_root))})"
                  f" (literal {json.dumps(str(Path(runtime.node_binary).resolve()))}))")
        return ["/usr/bin/sandbox-exec", "-p", policy]
    if sys.platform == "linux":
        binary = shutil.which("unshare")
        if binary:
            # Keep a non-root browser identity. Mapping to UID 0 makes Chromium
            # refuse its own sandbox even though the outer namespace is safe.
            return [binary, "--user", "--map-current-user", "--net", "--"]
    raise _fail("browser_network_boundary_unavailable")


_PROBE = r"""
const net = require('node:net');
const blocked = ['EPERM','EACCES','ENETUNREACH','EHOSTUNREACH','EADDRNOTAVAIL'];
async function denied(host) {
  return new Promise(resolve => {
    const s = net.createConnection({host, port: 9});
    const timer = setTimeout(() => {s.destroy(); resolve(false);}, 1500);
    s.on('connect', () => {clearTimeout(timer); s.destroy(); resolve(false);});
    s.on('error', e => {clearTimeout(timer); resolve(blocked.includes(e.code));});
  });
}
function deniedUDP() {
  return new Promise(resolve => {
    let s; try {s=require('node:dgram').createSocket('udp4');}
    catch(e) {resolve(blocked.includes(e.code));return;}
    const timer=setTimeout(()=>{s.close();resolve(false);},1500);
    const done=e=>{clearTimeout(timer);s.close();resolve(!!e && blocked.includes(e.code));};
    s.once('error',done);s.send(Buffer.from('probe'),53,'1.1.1.1',done);
  });
}
Promise.all([...['127.0.0.1','10.255.255.254','1.1.1.1','::1'].map(denied),deniedUDP()])
 .then(x => process.exit(x.every(Boolean) ? 0 : 3));
"""


_WORKER = r"""
const readline = require('node:readline');
const fs = require('node:fs');
const pending = new Map(); let seq = 0, config, browser;
const send = obj => process.stdout.write(JSON.stringify(obj)+'\n');
function fetchFromBroker(url, method, type) {
  return new Promise((resolve, reject) => {
    const id = ++seq; if (id > 64) return reject(Error('request_limit'));
    const timer = setTimeout(() => { pending.delete(id); reject(Error('broker_timeout')); }, 10000);
    pending.set(id, {resolve, reject, timer}); send({kind:'fetch', id, url, method, type});
  });
}
async function run(c) {
  const {chromium} = require(c.playwright);
  browser = await chromium.launch({executablePath:c.executable, headless:true, chromiumSandbox:true,
    env:{PATH:'/usr/bin:/bin', HOME:c.home, TMPDIR:c.home},
    args:['--disable-gpu','--in-process-gpu','--disable-background-networking','--disable-component-update','--disable-sync',
          '--disable-features=MediaRouter','--no-first-run','--no-default-browser-check']});
  const context = await browser.newContext({acceptDownloads:false, serviceWorkers:'block',
    viewport:{width:1280,height:900}, locale:'en-US', timezoneId:'Asia/Shanghai'});
  // Additional defense; OS isolation is mandatory and independently probed.
  if (typeof context.routeWebSocket !== 'function') throw Error('websocket_guard_unavailable');
  await context.routeWebSocket('**/*', ws => ws.close());
  const page = await context.newPage();
  // Playwright route handlers only see the first request of an HTTP redirect
  // chain. CDP Fetch pauses every hop, preserving browser URL/origin semantics.
  const cdp = await context.newCDPSession(page);
  await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
  cdp.on('Fetch.requestPaused', async event => {
    const req = event.request;
    try {
      if (!['GET','HEAD'].includes(req.method) || req.hasPostData || req.postData)
        throw Error('method_rejected');
      if (!['Document','Stylesheet','Script','Image','Font','XHR','Fetch','Other'].includes(event.resourceType))
        throw Error('resource_rejected');
      const r = await fetchFromBroker(req.url, req.method, event.resourceType);
      if (r.error) throw Error(r.error);
      const headers = {'content-type':r.content_type, 'x-content-type-options':'nosniff',
        'content-security-policy':"form-action 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'"};
      if (r.location) headers.location = r.location;
      await cdp.send('Fetch.fulfillRequest',{requestId:event.requestId,responseCode:r.status,
        responseHeaders:Object.entries(headers).map(([name,value])=>({name,value})),body:r.body});
    } catch (_) {
      try {await cdp.send('Fetch.failRequest',{requestId:event.requestId,errorReason:'BlockedByClient'});}
      catch (_) {} // A closed target cannot gain an alternate network path.
    }
  });
  page.on('popup', p => p.close());
  page.on('download', d => d.cancel());
  page.on('dialog', d => d.dismiss());
  await page.goto(c.url, {waitUntil:'networkidle',timeout:20000});
  const title = (await page.title()).slice(0,4096);
  const text = (await page.locator('body').innerText({timeout:5000})).slice(0,262144);
  const anchors = page.locator('a[href]'); const links = [];
  for (let i=0; i<Math.min(await anchors.count(),128); i++) {
    const link=anchors.nth(i);
    links.push({text:(await link.innerText()).slice(0,512), href:(await link.getAttribute('href')).slice(0,4096)});
  }
  const screenshot = c.screenshot ? (await page.screenshot({type:'png',fullPage:false})).toString('base64') : null;
  send({kind:'result',url:page.url(),title,text,links,screenshot});
  await browser.close(); process.exit(0);
}
readline.createInterface({input:process.stdin,crlfDelay:Infinity}).on('line', line => {
  if (Buffer.byteLength(line)>8*1024*1024) process.exit(2);
  let m; try {m=JSON.parse(line);} catch (_) {process.exit(2);}
  if (!config) {config=m; run(m).catch(async () => {
    send({kind:'error'}); if(browser) await browser.close(); process.exit(1);
  }); return;}
  const p=pending.get(m.id); if (!p) process.exit(2);
  pending.delete(m.id); clearTimeout(p.timer); p.resolve(m);
});
"""


@dataclass(frozen=True)
class BrowserResult:
    url: str
    title: str
    text: str
    links: tuple[dict[str, str], ...]
    screenshot_png: bytes | None
    rejected_requests: int


def validate_browser_result(message: dict, grant: PublicBrowserGrant, *, screenshot: bool,
                            rejected_requests: int = 0) -> BrowserResult:
    """Bounded result validation shared by fixed native and VM browser drivers."""
    try:
        if (not isinstance(message, dict) or set(message) != {'kind', 'url', 'title', 'text', 'links', 'screenshot'}
                or message['kind'] != 'result' or type(screenshot) is not bool
                or type(rejected_requests) is not int or not 0 <= rejected_requests <= 64
                or not isinstance(message['title'], str) or len(message['title']) > 4096
                or not isinstance(message['text'], str) or len(message['text']) > 262144
                or not isinstance(message['links'], list) or len(message['links']) > 128):
            raise ValueError('result')
        grant.require(message['url'])
        for entry in message['links']:
            if (not isinstance(entry, dict) or set(entry) != {'text', 'href'}
                    or not isinstance(entry['text'], str) or len(entry['text']) > 512
                    or not isinstance(entry['href'], str) or len(entry['href']) > 4096):
                raise ValueError('link')
        png = None
        if screenshot:
            encoded = message['screenshot']
            if not isinstance(encoded, str) or not 1 <= len(encoded) <= 6 * 1024**2:
                raise ValueError('screenshot')
            png = base64.b64decode(encoded, validate=True)
            if (not 33 <= len(png) <= 4 * 1024**2 or not png.startswith(b'\x89PNG\r\n\x1a\n')
                    or png[8:16] != b'\x00\x00\x00\rIHDR' or struct.unpack('!II', png[16:24]) != (1280, 900)):
                raise ValueError('png')
        elif message['screenshot'] is not None:
            raise ValueError('unexpected_screenshot')
        # Extracted links are data, never implicit navigation grants.
        return BrowserResult(message['url'], message['title'], message['text'],
                             tuple(dict(entry) for entry in message['links']), png, rejected_requests)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise _fail('browser_worker_protocol_invalid') from None


class PublicBrowser:
    """One navigation with fixed extraction/screenshot, fresh ephemeral profile.

    No click, fill, upload, evaluate, executable, profile, cookie or workspace
    argument is exposed.  Arbitrary task programs must not import this adapter
    inside their sandbox or access its broker IPC.  A missing OS boundary fails
    closed even if Playwright routing itself is operational.
    """

    def __init__(self, runtime: BrowserRuntime):
        runtime.validate()
        self.runtime = runtime

    def browse(self, grant: PublicBrowserGrant, url: str, *, screenshot: bool = True) -> BrowserResult:
        grant.require(url)
        if type(screenshot) is not bool:
            raise _fail("browser_action_invalid")
        return self._run(grant, url, screenshot, PublicFetchBroker(grant))

    def _run(self, grant: PublicBrowserGrant, url: str, screenshot: bool,
             broker: PublicFetchBroker) -> BrowserResult:
        # Private entry point permits a deterministic broker fixture in tests;
        # production callers always construct the real public-only broker.
        with tempfile.TemporaryDirectory(prefix="codex-public-browser-") as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            prefix = _isolation_prefix(root, self.runtime)
            environment = {"PATH": "/usr/bin:/bin", "HOME": directory, "TMPDIR": directory}
            try:
                probe = subprocess.run(prefix + [self.runtime.node_binary, "-e", _PROBE],
                                       env=environment, capture_output=True, timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                raise _fail("browser_network_boundary_unavailable") from None
            if probe.returncode != 0:
                raise _fail("browser_network_boundary_unqualified")
            executable = root / "chromium-isolated"
            executable.write_text("#!/bin/sh\nexec " + shlex.join(prefix + [self.runtime.chromium_binary])
                                  + ' "$@"\n', encoding="utf-8")
            executable.chmod(0o700)
            worker = root / "worker.cjs"
            worker.write_text(_WORKER, encoding="utf-8")
            worker.chmod(0o600)
            config = {"playwright": self.runtime.playwright_module, "executable": str(executable),
                      "home": directory, "url": url, "screenshot": screenshot}
            process = subprocess.Popen([self.runtime.node_binary, str(worker)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                                       start_new_session=True)
            rejected = 0
            last_request = 0
            try:
                self._write(process, config)
                deadline = time.monotonic() + 45
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    buffer = bytearray()
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(min(remaining, 1)):
                            if remaining <= 0:
                                raise _fail("browser_timeout")
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise _fail("browser_worker_failed")
                        buffer.extend(chunk)
                        if len(buffer) > MAX_FRAME:
                            raise _fail("browser_frame_limit")
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            try:
                                message = json.loads(line, object_pairs_hook=_pairs)
                            except (ValueError, UnicodeError):
                                raise _fail("browser_worker_protocol_invalid") from None
                            if not isinstance(message, dict):
                                raise _fail('browser_worker_protocol_invalid')
                            if message.get("kind") == "fetch":
                                if (set(message) != {'kind', 'id', 'url', 'method', 'type'}
                                        or type(message['id']) is not int or message['id'] != last_request + 1
                                        or not 1 <= message['id'] <= 64
                                        or not isinstance(message['url'], str) or not isinstance(message['method'], str)
                                        or not isinstance(message['type'], str)
                                        or message['type'] not in {'Document', 'Stylesheet', 'Script', 'Image', 'Font', 'XHR', 'Fetch', 'Other'}):
                                    raise _fail('browser_worker_protocol_invalid')
                                last_request = message['id']
                                reply = {"id": message.get("id")}
                                try:
                                    response = broker.fetch(message["url"], message["method"])
                                    reply.update(status=response.status, content_type=response.content_type,
                                                 body=base64.b64encode(response.body).decode(),
                                                 location=response.location)
                                except WorkRuntimeError as error:
                                    rejected += 1
                                    reply["error"] = error.code
                                self._write(process, reply)
                            elif message.get("kind") == "result":
                                return validate_browser_result(message, grant, screenshot=screenshot,
                                                               rejected_requests=rejected)
                            else:
                                raise _fail("browser_worker_failed")
            finally:
                # Include browser descendants on failure, timeout and success.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()

    @staticmethod
    def _write(process: subprocess.Popen, message: dict) -> None:
        frame = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(frame) > MAX_FRAME:
            raise _fail("browser_frame_limit")
        try:
            process.stdin.write(frame)
            process.stdin.flush()
        except BrokenPipeError:
            raise _fail("browser_worker_failed") from None
