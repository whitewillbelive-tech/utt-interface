"""Loopback-only development server; exposes only allowlisted UI and draft reads."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from app.provider import SyntheticProvider, ReadOnlyAdapter, error

WEB = Path(__file__).with_name('web')
STATIC = {'/': ('index.html','text/html'), '/app.mjs': ('app.mjs','text/javascript'),
          '/style.css': ('style.css','text/css'), '/client.mjs': ('client.mjs','text/javascript'),
          '/response-validation.mjs': ('response-validation.mjs','text/javascript')}


def make_server(port=8765, mode='synthetic', copies=1):
    provider = SyntheticProvider(copies) if mode == 'synthetic' else ReadOnlyAdapter()

    class Handler(BaseHTTPRequestHandler):
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
            if self.path == '/api/mode':
                return self.reply(200, {'mode': mode, 'simulation_namespace': 'utt.synthetic.session.v1' if mode=='synthetic' else None})
            if self.path not in STATIC:
                return self.reply(404, {'error': 'NOT_FOUND'})
            name, mime = STATIC[self.path]
            self.reply(200, (WEB/name).read_bytes(), mime)

        def do_POST(self):
            if self.path != '/api/read':
                return self.reply(405, {'error':'READ_ONLY'})
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{self.headers.get("Host")}':
                return self.reply(403, {'error': 'ORIGIN_DENIED'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 16384 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    return self.reply(400, error('capabilities', 'INVALID_REQUEST'))
                payload = json.loads(self.rfile.read(n))
                if not isinstance(payload,dict) or set(payload)-{'request','sort','scenario'}:
                    raise ValueError()
                q = payload.get('request'); scenario = payload.get('scenario', 'normal')
                if scenario not in ('normal', 'connection', 'stale'):
                    raise ValueError()
                if scenario != 'normal' and isinstance(provider, SyntheticProvider):
                    result = error(q.get('operation') if isinstance(q,dict) else None,
                                   'CONNECTION_UNAVAILABLE' if scenario=='connection' else 'VERSION_MISMATCH')
                else:
                    result = provider.read(q, payload.get('sort','id_asc'))
                self.reply(200, result)
            except (ValueError, TypeError, KeyError):
                self.reply(400, error('capabilities','INVALID_REQUEST'))

    server = ThreadingHTTPServer(('127.0.0.1',port), Handler)
    server.provider = provider
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--mode',choices=['synthetic','readonly'],default='synthetic')
    parser.add_argument('--copies',type=int,default=1,help='Generated copies of seven fictional records (1..10000)')
    args = parser.parse_args()
    server = make_server(args.port, args.mode, args.copies)
    print(f'UTT {args.mode}: loopback port {server.server_port}; no operational data connected.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if isinstance(server.provider,SyntheticProvider): server.provider.close()

if __name__ == '__main__':
    main()
