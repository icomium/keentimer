#!/opt/bin/python3
# KeenTimer - time-limited internet access per client for Keenetic (KeeneticOS 5.x)
import json, os, re, sys, time, threading, hashlib, secrets, subprocess, http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get('KT_PORT', 3000))
WEB_HOST = os.environ.get('KT_WEB_HOST', '')
WEB_PORT = int(os.environ.get('KT_WEB_PORT', 80))
RCI_PORT = int(os.environ.get('KT_RCI_PORT', 79))
CHECK_INTERVAL = int(os.environ.get('KT_INTERVAL', 60))
STATE_FILE = os.environ.get('KT_STATE', '/opt/etc/keentimer/state.json')
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_TTL = 12 * 3600

lock = threading.RLock()
sessions = {}
state = {'clients': {}}

FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect width='64' height='64' rx='14' fill='#4976d2'/>"
           "<circle cx='32' cy='35' r='17' fill='none' stroke='#fff' stroke-width='5'/>"
           "<path d='M32 25v10l7 5' fill='none' stroke='#fff' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/>"
           "<path d='M26 9h12' stroke='#fff' stroke-width='5' stroke-linecap='round'/></svg>")

FONTS_CSS = """@font-face{font-family:'Onest';src:url('/fonts/Onest-cyrillic.woff2') format('woff2');font-weight:400 700;font-style:normal;font-display:swap;unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:'Onest';src:url('/fonts/Onest-latin.woff2') format('woff2');font-weight:400 700;font-style:normal;font-display:swap;unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'JetBrains Mono';src:url('/fonts/JetBrainsMono-cyrillic.woff2') format('woff2');font-weight:400 500;font-style:normal;font-display:swap;unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:'JetBrains Mono';src:url('/fonts/JetBrainsMono-latin.woff2') format('woff2');font-weight:400 500;font-style:normal;font-display:swap;unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
"""

HEAD_EXTRA = (b"<link rel='icon' type='image/svg+xml' href='/favicon.svg'>"
              b"<link rel='stylesheet' href='/fonts.css'>")
FONT_RE = re.compile(r'^[A-Za-z0-9_.-]+\.woff2$')
STATIC_CACHE = {'Cache-Control': 'public, max-age=86400'}


def log(*a):
    print(time.strftime('%F %T'), *a, flush=True)


def load_page():
    try:
        with open(os.path.join(APP_DIR, 'index.html'), 'rb') as f:
            return f.read().replace(b'</head>', HEAD_EXTRA + b'</head>', 1)
    except OSError:
        return b'index.html not found'


def load_state():
    global state
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {'clients': {}}
    state.setdefault('clients', {})


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def rci(body=None, path='/rci/'):
    c = http.client.HTTPConnection('127.0.0.1', RCI_PORT, timeout=8)
    try:
        if body is None:
            c.request('GET', path)
        else:
            c.request('POST', path, json.dumps(body), {'Content-Type': 'application/json'})
        r = c.getresponse()
        data = r.read().decode('utf-8', 'replace')
        if r.status >= 400:
            raise RuntimeError('RCI HTTP %d' % r.status)
        return json.loads(data) if data else {}
    finally:
        c.close()


def ndmc(cmd):
    subprocess.run(['ndmc', '-c', cmd], check=False, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def list_hosts():
    d = rci(path='/rci/show/ip/hotspot')
    hosts = d.get('host', []) if isinstance(d, dict) else d
    if isinstance(hosts, dict):
        hosts = [hosts]
    out = {}
    for h in hosts:
        mac = (h.get('mac') or '').lower()
        if not mac:
            continue
        out[mac] = {
            'mac': mac,
            'name': h.get('name') or h.get('hostname') or mac,
            'ip': h.get('ip', ''),
            'online': str(h.get('active', '')).lower() in ('true', '1', 'yes'),
            'registered': bool(h.get('registered', True)),
            'permit': (h.get('access') == 'permit') if 'access' in h else (not h.get('deny', False)),
        }
    return out


def set_access(mac, permit, name=None, registered=True):
    try:
        if not registered and name:
            rci({'known': {'host': {'mac': mac, 'name': name}}})
        rci({'ip': {'hotspot': {'host': {'mac': mac, 'permit' if permit else 'deny': True}}}})
    except Exception:
        ndmc('ip hotspot host %s %s' % (mac, 'permit' if permit else 'deny'))


def remaining(c):
    return max(0, int(c.get('expires', 0) - time.time()))


def reconcile():
    with lock:
        if not state['clients']:
            return
        try:
            hosts = list_hosts()
        except Exception:
            return
        for mac, c in state['clients'].items():
            h = hosts.get(mac)
            want = remaining(c) > 0
            if h is None:
                set_access(mac, want, c.get('name'), False)
            elif h['permit'] != want:
                set_access(mac, want, h['name'], h['registered'])


def worker():
    while True:
        try:
            reconcile()
        except Exception as e:
            log('reconcile error:', e)
        time.sleep(CHECK_INTERVAL)


def release_all():
    load_state()
    for mac in list(state['clients']):
        set_access(mac, True)


def router_login(host, login, password):
    host = WEB_HOST or host or '127.0.0.1'
    c = http.client.HTTPConnection(host, WEB_PORT, timeout=8)
    try:
        c.request('GET', '/auth')
        r = c.getresponse()
        r.read()
        if r.status != 401:
            log('auth: GET /auth on %s:%d returned %d %s' % (host, WEB_PORT, r.status, r.getheader('X-Detail') or ''))
            return False
        ch = r.getheader('X-NDM-Challenge') or ''
        realm = r.getheader('X-NDM-Realm') or ''
        cookie = (r.getheader('Set-Cookie') or '').split(';')[0]
        md5 = hashlib.md5(('%s:%s:%s' % (login, realm, password)).encode()).hexdigest()
        sha = hashlib.sha256((ch + md5).encode()).hexdigest()
        c.request('POST', '/auth', json.dumps({'login': login, 'password': sha}),
                  {'Content-Type': 'application/json', 'Cookie': cookie})
        r2 = c.getresponse()
        r2.read()
        if r2.status != 200:
            log('auth: POST /auth for "%s" returned %d' % (login, r2.status))
        return r2.status == 200
    except Exception as e:
        log('auth error:', e)
        return False
    finally:
        c.close()


class H(BaseHTTPRequestHandler):
    server_version = 'KeenTimer'

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json', extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        extra = extra or {}
        if ctype.startswith('text/') or ctype == 'application/json':
            ctype += '; charset=utf-8'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        if 'Cache-Control' not in extra:
            self.send_header('Cache-Control', 'no-store')
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def js(self, obj, code=200, extra=None):
        self.send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def token(self):
        for p in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = p.strip().partition('=')
            if k == 'kt_session':
                return v
        return ''

    def authed(self):
        t = self.token()
        exp = sessions.get(t)
        if exp and exp > time.time():
            return True
        sessions.pop(t, None)
        return False

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self.send(200, load_page(), 'text/html')
        if self.path in ('/favicon.svg', '/favicon.ico'):
            return self.send(200, FAVICON, 'image/svg+xml', STATIC_CACHE)
        if self.path == '/fonts.css':
            return self.send(200, FONTS_CSS, 'text/css', STATIC_CACHE)
        if self.path.startswith('/fonts/'):
            name = self.path[7:]
            p = os.path.join(APP_DIR, 'fonts', name)
            if FONT_RE.match(name) and os.path.isfile(p):
                with open(p, 'rb') as f:
                    return self.send(200, f.read(), 'font/woff2', STATIC_CACHE)
            return self.js({'error': 'not found'}, 404)
        if not self.authed():
            return self.js({'error': 'auth'}, 401)
        if self.path == '/api/clients':
            try:
                hosts = list_hosts()
            except Exception as e:
                return self.js({'error': str(e)}, 502)
            with lock:
                managed = set(state['clients'])
            return self.js({'clients': [h for m, h in sorted(hosts.items(), key=lambda x: x[1]['name'].lower()) if m not in managed]})
        if self.path == '/api/managed':
            try:
                hosts = list_hosts()
            except Exception:
                hosts = {}
            with lock:
                rows = []
                for mac, c in state['clients'].items():
                    rem = remaining(c)
                    h = hosts.get(mac, {})
                    rows.append({'mac': mac, 'name': h.get('name') or c.get('name') or mac,
                                 'ip': h.get('ip', ''), 'online': h.get('online', False),
                                 'remaining': rem, 'status': 'working' if rem > 0 else 'blocked'})
            rows.sort(key=lambda r: r['name'].lower())
            return self.js({'clients': rows})
        self.js({'error': 'not found'}, 404)

    def do_POST(self):
        d = self.body()
        if self.path == '/api/login':
            local_ip = self.connection.getsockname()[0]
            if router_login(local_ip, str(d.get('login', '')), str(d.get('password', ''))):
                t = secrets.token_hex(24)
                sessions[t] = time.time() + SESSION_TTL
                return self.js({'ok': True}, extra={'Set-Cookie': 'kt_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d' % (t, SESSION_TTL)})
            time.sleep(1)
            return self.js({'error': 'bad credentials'}, 403)
        if self.path == '/api/logout':
            sessions.pop(self.token(), None)
            return self.js({'ok': True}, extra={'Set-Cookie': 'kt_session=; Path=/; Max-Age=0'})
        if not self.authed():
            return self.js({'error': 'auth'}, 401)
        macs = [str(m).lower() for m in d.get('macs', [])]
        with lock:
            if self.path == '/api/enable':
                try:
                    hosts = list_hosts()
                except Exception:
                    hosts = {}
                for m in macs:
                    state['clients'].setdefault(m, {'name': hosts.get(m, {}).get('name', m), 'expires': 0})
            elif self.path == '/api/disable':
                for m in macs:
                    if state['clients'].pop(m, None) is not None:
                        set_access(m, True)
            elif self.path == '/api/add':
                m = str(d.get('mac', '')).lower()
                minutes = int(d.get('minutes', 0))
                c = state['clients'].get(m)
                if not c or minutes <= 0 or minutes > 60 * 24 * 365:
                    return self.js({'error': 'bad request'}, 400)
                c['expires'] = max(time.time(), c.get('expires', 0)) + minutes * 60
            elif self.path == '/api/block':
                c = state['clients'].get(str(d.get('mac', '')).lower())
                if not c:
                    return self.js({'error': 'bad request'}, 400)
                c['expires'] = 0
            else:
                return self.js({'error': 'not found'}, 404)
            save_state()
        reconcile()
        self.js({'ok': True})


if __name__ == '__main__':
    if '--release' in sys.argv:
        release_all()
        sys.exit(0)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
