"""Exact-target GET facts and an optional independently composed read-only POST."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import re
import socket
import threading
import time

from .contract import ObserverError,canonical


class _Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,handler):
        self._slots=threading.BoundedSemaphore(4)
        super().__init__(address,handler)
    def process_request(self,request,client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self._slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self._slots.release()
    def handle_error(self,*args):pass


def create_server(host,port,*,service=None,preview_qualification=None):
    preview_slots=threading.BoundedSemaphore(2)
    class Handler(BaseHTTPRequestHandler):
        server_version='HAArtifactObserver/0.0.5'
        def setup(self):
            super().setup();self._started=time.monotonic();self.connection.settimeout(5)
            # A socket timeout alone restarts on each received byte. Bound both
            # the request line and headers before BaseHTTPRequestHandler reads
            # them, so four slow clients cannot hold every worker indefinitely.
            self._header_deadline=self._started+5
            self._header_timer=threading.Timer(5,self._expire_headers)
            self._header_timer.daemon=True;self._header_timer.start()
        def _expire_headers(self):
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        def parse_request(self):
            try:
                parsed=super().parse_request()
                return parsed and time.monotonic()<self._header_deadline
            finally:self._header_timer.cancel()
        def finish(self):
            self._header_timer.cancel()
            super().finish()
        def log_message(self,*args):pass
        def reply(self,code,value):
            body=canonical(value);self.send_response(code)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
        def do_GET(self):
            lengths=self.headers.get_all('Content-Length',[])
            if (len(lengths)>1 or lengths not in ([],['0'])
                    or self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding')):
                self.reply(400,{'error':{'code':'observer_body_rejected'}});return
            if self.path=='/healthz':
                self.reply(200,{'status':'ok','version':1,'observation_enabled':bool(service and service.enabled)});return
            if service is None or not service.enabled:
                self.reply(404,{'error':{'code':'not_found'}});return
            match=re.fullmatch(r'/v1/artifacts/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})',self.path)
            if match is None:
                self.reply(404,{'error':{'code':'not_found'}});return
            authorizations=self.headers.get_all('Authorization',[])
            if len(authorizations)!=1:
                self.reply(401,{'error':{'code':'observer_not_authorized'}});return
            self.connection.settimeout(5)
            try:self.reply(200,service.observe(match[1],authorizations[0]))
            except ObserverError as error:
                status={'observer_not_authorized':401,'observer_target_denied':403,'observer_reference_invalid':400}.get(error.code,503)
                self.reply(status,{'error':{'code':error.code}})
            except Exception:self.reply(503,{'error':{'code':'observer_unavailable'}})
        def do_POST(self):
            if self.path!='/internal/v1/work-preview/qualification':
                self.no_method();return
            if preview_qualification is None:
                self.reply(404,{'error':{'code':'not_found'}});return
            authorizations=self.headers.get_all('Authorization',[])
            lengths=self.headers.get_all('Content-Length',[])
            content_types=self.headers.get_all('Content-Type',[])
            if len(authorizations)!=1:
                self.reply(401,{'error':{'code':'observer_not_authorized'}});return
            if (len(lengths)!=1 or re.fullmatch(r'[0-9]{1,4}',lengths[0]) is None or not 1<=int(lengths[0])<=4096
                    or len(content_types)!=1 or content_types[0].lower()!='application/json'
                    or self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding')):
                self.reply(400,{'error':{'code':'observer_body_rejected'}});return
            if not preview_slots.acquire(blocking=False):
                self.reply(503,{'error':{'code':'observer_busy'}});return
            deadline=self._started+2
            if time.monotonic()>=deadline:
                preview_slots.release();self.reply(503,{'error':{'code':'observer_qualification_unavailable'}});return
            def expire():
                try:self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            timer=threading.Timer(max(0,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
            try:
                self.connection.settimeout(2)
                raw=self.rfile.read(int(lengths[0]))
                if len(raw)!=int(lengths[0]) or time.monotonic()>=deadline:raise ValueError()
                result=preview_qualification.qualify(raw,authorizations[0],deadline=deadline)
                if type(result) is not tuple or len(result)!=2:raise ValueError()
                body,mac=result
                if (not isinstance(body,bytes) or not 1<=len(body)<=65536 or not isinstance(mac,str)
                        or re.fullmatch(r'[a-f0-9]{64}',mac) is None or time.monotonic()>=deadline):raise ValueError()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
                self.send_header('X-Qualification-MAC',mac);self.end_headers();self.wfile.write(body)
            except Exception:
                self.reply(503,{'error':{'code':'observer_qualification_unavailable'}})
            finally:timer.cancel();preview_slots.release()
        def no_method(self):self.reply(405,{'error':{'code':'observer_method_rejected'}})
        do_PUT=no_method
        do_PATCH=no_method
        do_DELETE=no_method
        do_OPTIONS=no_method
        do_TRACE=no_method
        do_CONNECT=no_method
    return _Server((host,port),Handler)
