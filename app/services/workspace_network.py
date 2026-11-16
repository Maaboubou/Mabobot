"""A single validated public HTTP proxy exposed to a network-isolated workspace.

Only the proxy Unix socket is mounted into the sandbox; its loopback listener
runs inside the private network namespace. Host TCP and Unix sockets stay hidden.
"""
from contextlib import ExitStack
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
from urllib.parse import urlsplit

from app.services.codex_browser_proxy import PinnedPublicProxy
from app.services.codex_browser_tool import PublicUrlGuard


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


class WorkspaceNetwork:
    def __enter__(self):
        self.stack = ExitStack()
        try:
            proxy = self.stack.enter_context(PinnedPublicProxy(PublicUrlGuard().resolve_public_endpoint,
                connect_timeout_seconds=10, client_timeout_seconds=25))
            endpoint = urlsplit(proxy.playwright_settings['server'])
            root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='mabobot-egress-')))
            self.path = root / 'proxy.sock'
            owner = self
            self.connections, self.lock = set(), threading.Lock()
            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=10) as upstream:
                        with owner.lock:
                            owner.connections.update((self.request, upstream))
                        try:
                            proxy._relay_bidirectional(self.request, upstream)
                        except OSError:
                            pass
                        finally:
                            with owner.lock:
                                owner.connections.difference_update((self.request, upstream))
            self.server = _Server(str(self.path), Handler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            for conn in self.connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.connections.clear()
        self.thread.join(timeout=2)
        self.stack.close()


# This supervisor runs INSIDE bwrap. It cannot reach the host network, and the
# only mounted socket forwards through PublicUrlGuard + PinnedPublicProxy.
SANDBOX_PROXY_RUNNER = r'''
import os,select,socket,socketserver,subprocess,sys,threading
class Handler(socketserver.BaseRequestHandler):
 def handle(self):
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as upstream:
   upstream.connect('/run/mabobot-egress.sock')
   peers=[self.request,upstream]
   while True:
    ready,_,_=select.select(peers,[],[],25)
    if not ready:return
    for source in ready:
     data=source.recv(65536)
     if not data:return
     (upstream if source is self.request else self.request).sendall(data)
class Server(socketserver.ThreadingMixIn,socketserver.TCPServer):
 daemon_threads=True
server=Server(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
proxy='http://127.0.0.1:'+str(server.server_address[1])
for name in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','all_proxy'):os.environ[name]=proxy
os.environ['NO_PROXY']=os.environ['no_proxy']=''
try:sys.exit(subprocess.call(sys.argv[1:]))
finally:server.shutdown();server.server_close()
'''
