"""Loopback-only development server; exposes only allowlisted UI and draft reads."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from app.provider import SyntheticProvider, ReadOnlyAdapter, error
from app.transport import ConfigError, load_transport_from_env

WEB = Path(__file__).with_name('web')
STATIC = {'/': ('index.html','text/html'), '/app.mjs': ('app.mjs','text/javascript'),
          '/style.css': ('style.css','text/css'), '/client.mjs': ('client.mjs','text/javascript'),
          '/response-validation.mjs': ('response-validation.mjs','text/javascript')}


def make_server(port=8765, mode='synthetic', copies=1, *, provider=None, connect_env=False):
    if mode not in ('synthetic', 'readonly') or (connect_env and (mode != 'readonly' or provider is not None)):
        raise ValueError('Invalid server mode/configuration')
    if provider is None:
        provider = (SyntheticProvider(copies) if mode == 'synthetic' else
                    ReadOnlyAdapter(load_transport_from_env() if connect_env else None))

    class Handler(BaseHTTPRequestHandler):
        def allowed_origin(self):
            hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host')
            origin = self.headers.get('Origin')
            return (len(self.headers.get_all('Host', [])) == 1 and host in hosts
                    and (origin is None or origin == f'http://{host}'))

        def log_message(self, *_):
            pass  # No query, record, source, body, or exception logging.

        def reply(self, status, body, mime='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime+'; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed_origin():
                return self.reply(403, {'error': 'ORIGIN_DENIED'})
            if self.path == '/api/mode':
                return self.reply(200, {'mode': mode, 'simulation_namespace': 'utt.synthetic.session.v1' if mode=='synthetic' else None})
            if self.path not in STATIC:
                return self.reply(404, {'error': 'NOT_FOUND'})
            name, mime = STATIC[self.path]
            self.reply(200, (WEB/name).read_bytes(), mime)

        def do_POST(self):
            if self.path != '/api/read':
                return self.reply(405, {'error':'READ_ONLY'})
            if not self.allowed_origin():
                return self.reply(403, {'error': 'ORIGIN_DENIED'})
            try:
                self.connection.settimeout(15)
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise ValueError()
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 16384 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    return self.reply(400, error('capabilities', 'INVALID_REQUEST'))
                payload = json.loads(self.rfile.read(n))
                if not isinstance(payload,dict) or set(payload)-{'request','sort','scenario'}:
                    raise ValueError()
                q = payload.get('request'); scenario = payload.get('scenario', 'normal')
                if scenario not in ('normal', 'connection', 'stale') or (mode != 'synthetic' and scenario != 'normal'):
                    raise ValueError()
                status = 200
                if scenario != 'normal' and isinstance(provider, SyntheticProvider):
                    result = error(q.get('operation') if isinstance(q,dict) else None,
                                   'CONNECTION_UNAVAILABLE' if scenario=='connection' else 'VERSION_MISMATCH')
                else:
                    if isinstance(provider, ReadOnlyAdapter):
                        status, result = provider.read_http(q, payload.get('sort','id_asc'))
                    else:
                        result = provider.read(q, payload.get('sort','id_asc'))
                self.reply(status, result)
            except (ValueError, TypeError, KeyError):
                self.reply(400, error('capabilities','INVALID_REQUEST'))
            except (OSError, TimeoutError):
                self.close_connection = True
            except Exception:
                self.reply(503, error('capabilities','CONNECTION_UNAVAILABLE'))

    server = ThreadingHTTPServer(('127.0.0.1',port), Handler)
    server.provider = provider
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--mode',choices=['synthetic','readonly'],default='synthetic')
    parser.add_argument('--copies',type=int,default=1,help='Generated copies of seven fictional records (1..10000)')
    parser.add_argument('--connect-env', action='store_true', help='Explicitly load read transport in the approved data runtime')
    args = parser.parse_args()
    try:
        server = make_server(args.port, args.mode, args.copies, connect_env=args.connect_env)
    except (ConfigError, ValueError):
        parser.exit(2, 'UTT: 읽기 실행환경 설정을 확인하세요. 설정값은 출력하지 않습니다.\n')
    print(f'UTT {args.mode}: loopback port {server.server_port}; '
          f'{"explicit transport configured; connection unverified" if args.connect_env else "no operational data connected"}.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if isinstance(server.provider,SyntheticProvider): server.provider.close()

if __name__ == '__main__':
    main()
