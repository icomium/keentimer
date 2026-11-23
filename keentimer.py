#!/opt/bin/python3
# KeenTimer - time-limited internet access per client for Keenetic (KeeneticOS 5.x)
import json, os, sys, time, threading, hashlib, secrets, subprocess, http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get('KT_PORT', 3000))
WEB_PORT = int(os.environ.get('KT_WEB_PORT', 80))
RCI_PORT = int(os.environ.get('KT_RCI_PORT', 79))
STATE_FILE = os.environ.get('KT_STATE', '/opt/etc/keentimer/state.json')
SESSION_TTL = 12 * 3600

lock = threading.RLock()
sessions = {}
state = {'clients': {}}


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
            print('reconcile error:', e, flush=True)
        time.sleep(5)


def release_all():
    load_state()
    for mac in list(state['clients']):
        set_access(mac, True)


def router_login(login, password):
    c = http.client.HTTPConnection('127.0.0.1', WEB_PORT, timeout=8)
    try:
        c.request('GET', '/auth')
        r = c.getresponse()
        r.read()
        if r.status == 200:
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
        return r2.status == 200
    except Exception:
        return False
    finally:
        c.close()


class H(BaseHTTPRequestHandler):
    server_version = 'KeenTimer'

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json', extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
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
            return self.send(200, PAGE, 'text/html')
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
            if router_login(str(d.get('login', '')), str(d.get('password', ''))):
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


PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KeenTimer</title>
<style>
:root{--bg:#0f1419;--panel:#171d25;--panel2:#1d2530;--line:#2a3441;--text:#e6edf3;--muted:#8b98a8;--accent:#2f81f7;--ok:#2ea043;--bad:#f85149}
[data-theme=light]{--bg:#f3f5f8;--panel:#fff;--panel2:#eef1f5;--line:#d8dee6;--text:#1c2430;--muted:#667385;--accent:#0969da}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;flex:1}header h1 span{color:var(--accent)}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 14px;cursor:pointer;font:inherit}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{background:var(--bad);border-color:var(--bad);color:#fff}button:disabled{opacity:.45;cursor:default}
main{max-width:1100px;margin:20px auto;padding:0 16px}
.tabs{display:flex;gap:6px;margin-bottom:16px}.tabs button.active{background:var(--accent);border-color:var(--accent);color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.dual{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center}
.list{height:420px;overflow:auto;background:var(--panel2);border:1px solid var(--line);border-radius:8px}
.list .it{padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none}.list .it.sel{background:var(--accent);color:#fff}.list .it small{display:block;color:var(--muted)}.list .it.sel small{color:#dbe7ff}
.mid{display:flex;flex-direction:column;gap:8px}h3{margin:0 0 8px;font-size:14px;color:var(--muted);font-weight:600}
input{width:100%;padding:8px 10px;margin-bottom:8px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;font:inherit}
.badge{padding:2px 10px;border-radius:99px;font-size:12px;color:#fff}.badge.working{background:var(--ok)}.badge.blocked{background:var(--bad)}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between}.row.it{flex-wrap:wrap}
.panel{margin-top:16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}.panel input{width:110px;margin:0}
.login{max-width:340px;margin:12vh auto}.err{color:var(--bad);min-height:20px}.muted{color:var(--muted)}
@media(max-width:720px){.dual{grid-template-columns:1fr}.mid{flex-direction:row;justify-content:center}}
</style></head><body>
<header><h1>Keen<span>Timer</span></h1><button id="theme">&#9790;</button><button id="logout" hidden>Выйти</button></header>
<main id="app"></main>
<script>
const $=s=>document.querySelector(s);let sel=new Set(),selR=new Set(),cur=null,tab=1,left=[],right=[],managed=[],timer=null;
const th=localStorage.kt_theme||'dark';document.documentElement.dataset.theme=th;
$('#theme').onclick=()=>{const n=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=n;localStorage.kt_theme=n};
async function api(p,b){const r=await fetch(p,b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(r.status===401){showLogin();throw 0}return r.json()}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function fmt(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+'ч ':'')+(h||m?m+'м ':'')+x+'с'}
function showLogin(){clearInterval(timer);$('#logout').hidden=true;$('#app').innerHTML='<div class="card login"><h3>Вход по учётной записи роутера</h3><input id="l" placeholder="Логин" autocomplete="username"><input id="p" type="password" placeholder="Пароль" autocomplete="current-password"><div class="err" id="e"></div><button class="primary" id="go">Войти</button></div>';
 const go=async()=>{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:$('#l').value,password:$('#p').value})});if(r.ok)start();else $('#e').textContent='Неверный логин или пароль'};$('#go').onclick=go;$('#p').onkeydown=e=>{if(e.key==='Enter')go()}}
function start(){$('#logout').hidden=false;$('#app').innerHTML='<div class="tabs"><button data-t="1">Выбор клиентов</button><button data-t="2">Управление временем</button></div><div id="view"></div>';
 document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{tab=+b.dataset.t;refresh(true)});refresh(true);clearInterval(timer);timer=setInterval(()=>refresh(false),5000)}
$('#logout').onclick=async()=>{await fetch('/api/logout',{method:'POST',body:'{}'});showLogin()};
async function refresh(full){document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',+b.dataset.t===tab));
 if(tab===1){if(!full&&document.activeElement&&document.activeElement.tagName==='INPUT')return;const a=await api('/api/clients'),b=await api('/api/managed');left=a.clients;managed=b.clients;draw1()}else{const b=await api('/api/managed');managed=b.clients;draw2()}}
function items(arr,set,att){return arr.map(c=>'<div class="it'+(set.has(c.mac)?' sel':'')+'" data-m="'+c.mac+'" data-a="'+att+'">'+esc(c.name)+'<small>'+esc(c.ip||'—')+' · '+c.mac+(c.online?' · онлайн':'')+'</small></div>').join('')}
function draw1(){const fl=($('#fl')||{}).value||'',fr=($('#fr')||{}).value||'';const L=left.filter(c=>(c.name+c.mac+c.ip).toLowerCase().includes(fl.toLowerCase()));const R=managed.filter(c=>(c.name+c.mac).toLowerCase().includes(fr.toLowerCase()));
 $('#view').innerHTML='<div class="card dual"><div><h3>Все клиенты роутера</h3><input id="fl" placeholder="Поиск" value="'+esc(fl)+'"><div class="list">'+items(L,sel,'l')+'</div></div><div class="mid"><button class="primary" id="toR">&rarr;</button><button id="toL">&larr;</button></div><div><h3>Доступ по времени включён</h3><input id="fr" placeholder="Поиск" value="'+esc(fr)+'"><div class="list">'+items(R,selR,'r')+'</div></div></div>';
 document.querySelectorAll('.it').forEach(e=>e.onclick=()=>{const s=e.dataset.a==='l'?sel:selR;s.has(e.dataset.m)?s.delete(e.dataset.m):s.add(e.dataset.m);draw1()});
 $('#fl').oninput=draw1;$('#fr').oninput=draw1;
 $('#toR').onclick=async()=>{await api('/api/enable',{macs:[...sel]});sel.clear();refresh(true)};$('#toL').onclick=async()=>{await api('/api/disable',{macs:[...selR]});selR.clear();refresh(true)}}
function draw2(){if(cur&&!managed.find(c=>c.mac===cur))cur=null;const c=managed.find(x=>x.mac===cur);
 const old=($('#cm')||{}).value||'';
 $('#view').innerHTML='<div class="card">'+(managed.length?managed.map(x=>'<div class="it row'+(x.mac===cur?' sel':'')+'" data-m="'+x.mac+'" style="padding:10px;border-bottom:1px solid var(--line);cursor:pointer"><span>'+esc(x.name)+' <span class="muted">'+esc(x.ip||'')+' '+x.mac+'</span></span><span><span class="muted">'+(x.remaining?fmt(x.remaining):'')+'</span> <span class="badge '+x.status+'">'+(x.status==='working'?'работает':'заблокирован')+'</span></span></div>').join(''):'<span class="muted">Список пуст. Добавьте клиентов на первой вкладке.</span>')+'</div>'+(c?'<div class="card panel"><b>'+esc(c.name)+'</b>'+[15,30,60,120,240].map(m=>'<button data-add="'+m+'">+'+(m<60?m+' мин':m/60+' ч')+'</button>').join('')+'<input id="cm" type="number" min="1" placeholder="минут" value="'+esc(old)+'"><button class="primary" id="addc">Добавить</button><button class="danger" id="blk" '+(c.status==='working'?'':'disabled')+'>Заблокировать</button></div>':'');
 document.querySelectorAll('.it').forEach(e=>e.onclick=()=>{cur=e.dataset.m;draw2()});
 if(c){const add=async m=>{if(m>0){await api('/api/add',{mac:cur,minutes:m});refresh(false)}};document.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>add(+b.dataset.add));$('#addc').onclick=()=>add(parseInt($('#cm').value));$('#blk').onclick=async()=>{await api('/api/block',{mac:cur});refresh(false)}}}
fetch('/api/managed').then(r=>r.ok?start():showLogin());
</script></body></html>'''

if __name__ == '__main__':
    if '--release' in sys.argv:
        release_all()
        sys.exit(0)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
