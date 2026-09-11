#!/usr/bin/env python3
import os, json, math, time, hmac, hashlib, sqlite3, uuid, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def load_dotenv():
    env=ROOT/'.env'
    if not env.exists(): return
    for line in env.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1); k=k.strip(); v=v.strip().strip('\"').strip("'")
        os.environ.setdefault(k,v)
load_dotenv()
DB=Path(os.getenv('TRADEPILOT_DB', str(ROOT/'tradepilot_local.db')))
HOST=os.getenv('TRADEPILOT_HOST','0.0.0.0'); PORT=int(os.getenv('PORT',os.getenv('TRADEPILOT_PORT','8787')))
HARD_RISK=0.02; MAX_NOTIONAL=0.20; MAX_POSITIONS=4; PRICE_DRIFT_STOCK=0.005; PRICE_DRIFT_CRYPTO=0.0075
CONTROL_SECRET=os.getenv('TRADEPILOT_CONTROL_SECRET','')
MOCK_BROKERS=os.getenv('TRADEPILOT_MOCK_BROKERS','0') == '1'


def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), stock_capital REAL DEFAULT 100000, crypto_capital REAL DEFAULT 10000, stock_risk REAL DEFAULT .005, crypto_risk REAL DEFAULT .01, max_daily_loss REAL DEFAULT .02, max_positions INTEGER DEFAULT 4);
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, broker TEXT, asset TEXT, symbol TEXT, side TEXT, qty REAL, entry REAL, sl REAL, target REAL, exit REAL, status TEXT, pnl REAL DEFAULT 0, fees REAL DEFAULT 0, order_id TEXT, stop_order_id TEXT, target_order_id TEXT, notes TEXT);
    CREATE TABLE IF NOT EXISTS executions(id INTEGER PRIMARY KEY AUTOINCREMENT, execution_key TEXT UNIQUE, created_at TEXT, broker TEXT, symbol TEXT, action TEXT, status TEXT, order_id TEXT, response TEXT);
    CREATE TABLE IF NOT EXISTS mock_orders(order_id TEXT PRIMARY KEY, broker TEXT, symbol TEXT, side TEXT, qty REAL, status TEXT DEFAULT 'OPEN', filled_qty REAL DEFAULT 0, average_price REAL DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS paper_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, broker TEXT, asset TEXT, symbol TEXT, side TEXT, qty REAL, entry REAL, sl REAL, target REAL, exit REAL, status TEXT, pnl REAL DEFAULT 0, fees REAL DEFAULT 0, notes TEXT);
    CREATE TABLE IF NOT EXISTS app_state(id INTEGER PRIMARY KEY CHECK(id=1), live_enabled INTEGER DEFAULT 0, emergency_stop INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS broker_credentials(broker TEXT PRIMARY KEY, api_key TEXT DEFAULT '', api_secret TEXT DEFAULT '', access_token TEXT DEFAULT '', updated_at TEXT);
    INSERT OR IGNORE INTO app_state(id) VALUES(1);
    INSERT OR IGNORE INTO settings(id) VALUES(1);
    ''');
    # Safe upgrades for databases created by earlier local versions.
    for col, typ in [('stop_order_id','TEXT'),('target_order_id','TEXT')]:
        try: c.execute(f'ALTER TABLE trades ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError: pass
    c.commit(); c.close()
init_db()


def json_response(h, obj, code=200):
    b=json.dumps(obj, separators=(',',':')).encode(); h.send_response(code); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.send_header('Access-Control-Allow-Origin','*'); h.end_headers(); h.wfile.write(b)

def read_json(h):
    n=int(h.headers.get('Content-Length','0')); return json.loads(h.rfile.read(n) or b'{}')

def http_json(url, method='GET', body=None, headers=None, timeout=10):
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url,data=data,method=method,headers=headers or {})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw=e.read().decode(errors='replace'); raise RuntimeError(f'HTTP {e.code}: {raw[:500]}')

# ---------- indicators ----------
def ema(v,n):
    if len(v)<n:return [None]*len(v)
    out=[None]*(n-1); e=sum(v[:n])/n; out.append(e); k=2/(n+1)
    for x in v[n:]: e=(x-e)*k+e; out.append(e)
    return out

def rsi(v,n=14):
    if len(v)<=n:return [None]*len(v)
    gains=[max(v[i]-v[i-1],0) for i in range(1,len(v))]; losses=[max(v[i-1]-v[i],0) for i in range(1,len(v))]
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n; out=[None]*n
    out.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(n,len(gains)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n; out.append(100 if al==0 else 100-100/(1+ag/al))
    return out

def atr(rows,n=14):
    tr=[]
    for i,x in enumerate(rows):
        if i==0: tr.append(x['h']-x['l'])
        else: tr.append(max(x['h']-x['l'],abs(x['h']-rows[i-1]['c']),abs(x['l']-rows[i-1]['c'])))
    if len(tr)<n:return [None]*len(tr)
    a=sum(tr[:n])/n; out=[None]*(n-1)+[a]
    for x in tr[n:]: a=(a*(n-1)+x)/n; out.append(a)
    return out

def macd(v):
    e12=ema(v,12); e26=ema(v,26); m=[None if a is None or b is None else a-b for a,b in zip(e12,e26)]
    vals=[x for x in m if x is not None]; sig=ema(vals,9); out=[None]*(len(m)-len(vals))+sig
    return m,out

def adx(rows,n=14):
    if len(rows)<2*n+1:return [None]*len(rows)
    trs=[]; plus=[]; minus=[]
    for i in range(len(rows)):
        if i==0: trs.append(rows[i]['h']-rows[i]['l']);plus.append(0);minus.append(0);continue
        up=rows[i]['h']-rows[i-1]['h']; dn=rows[i-1]['l']-rows[i]['l']; plus.append(up if up>dn and up>0 else 0); minus.append(dn if dn>up and dn>0 else 0); trs.append(max(rows[i]['h']-rows[i]['l'],abs(rows[i]['h']-rows[i-1]['c']),abs(rows[i]['l']-rows[i-1]['c'])))
    atrv=sum(trs[1:n+1])/n; p=sum(plus[1:n+1])/n; m=sum(minus[1:n+1])/n; dx=[]; out=[None]*n
    for i in range(n,len(rows)):
        if i>n: atrv=(atrv*(n-1)+trs[i])/n;p=(p*(n-1)+plus[i])/n;m=(m*(n-1)+minus[i])/n
        pi=100*p/atrv if atrv else 0; mi=100*m/atrv if atrv else 0; dx.append(100*abs(pi-mi)/(pi+mi) if pi+mi else 0)
    if len(dx)>=n:
        a=sum(dx[:n])/n; vals=[None]*(n-1)+[a]
        for x in dx[n:]: a=(a*(n-1)+x)/n; vals.append(a)
        out += vals
    return (out+[None]*len(rows))[:len(rows)]

def signal(rows, capital, risk):
    closes=[x['c'] for x in rows]; e20=ema(closes,20);e50=ema(closes,50);rr=rsi(closes);aa=atr(rows);mm,ms=macd(closes);adxv=adx(rows)
    i=len(rows)-1; x=rows[i]; vals=[e20[i],e50[i],rr[i],aa[i],mm[i],ms[i],adxv[i]]
    if any(v is None for v in vals): return {'tradable':False,'side':'HOLD','reason':'Not enough data'}
    scoreL=scoreS=0
    if e20[i]>e50[i] and x['c']>e20[i]: scoreL+=2
    if e20[i]<e50[i] and x['c']<e20[i]: scoreS+=2
    if 50<=rr[i]<=75: scoreL+=2
    if 25<=rr[i]<=50: scoreS+=2
    if mm[i]>ms[i]: scoreL+=2
    if mm[i]<ms[i]: scoreS+=2
    if adxv[i]>=20: scoreL+=1;scoreS+=1
    hi=max(z['h'] for z in rows[-13:-1]); lo=min(z['l'] for z in rows[-13:-1])
    if x['c']>hi:scoreL+=1
    if x['c']<lo:scoreS+=1
    side='LONG' if scoreL>=7 and scoreL>scoreS else 'SHORT' if scoreS>=7 and scoreS>scoreL else 'HOLD'
    entry=x['c']; stop=entry-aa[i] if side=='LONG' else entry+aa[i]; target=entry+2*aa[i] if side=='LONG' else entry-2*aa[i]
    dist=abs(entry-stop); qty=math.floor((capital*min(risk,HARD_RISK))/dist) if dist else 0; qty=min(qty,math.floor(capital*MAX_NOTIONAL/entry)) if entry else 0
    return {'tradable':side!='HOLD','side':side,'score':max(scoreL,scoreS),'maxScore':10,'entry':entry,'sl':stop,'target':target,'qty':qty,'possibleLoss':qty*dist,'possibleProfit':qty*abs(target-entry),'rsi':rr[i],'atr':aa[i],'adx':adxv[i],'macd':mm[i],'macdSignal':ms[i],'candleTime':datetime.fromtimestamp(x['t']/1000,timezone.utc).isoformat(),'reason':'Score below threshold / confirmation missing' if side=='HOLD' else ''}

# ---------- market data ----------
def yahoo(symbol):
    p=urllib.parse.urlencode({'period1':int(time.time())-7*86400,'period2':int(time.time()),'interval':'5m','events':'history','includePrePost':'false'})
    j=http_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{p}',headers={'User-Agent':'TradePilot/1.0'})
    r=j['chart']['result'][0]; q=r['indicators']['quote'][0]; rows=[]
    for i,t in enumerate(r['timestamp']):
        if all(q[k][i] is not None for k in ('open','high','low','close','volume')): rows.append({'t':t*1000,'o':q['open'][i],'h':q['high'][i],'l':q['low'][i],'c':q['close'][i],'v':q['volume'][i]})
    return rows

def delta_public(symbol):
    end=int(time.time()); start=end-5*86400
    url=f'https://api.india.delta.exchange/v2/history/candles?symbol={urllib.parse.quote(symbol)}&resolution=5m&start={start}&end={end}'
    j=http_json(url,headers={'User-Agent':'TradePilot/1.0'}); r=j.get('result',j); rows=[]
    for x in r:
        rows.append({'t':int(x.get('time',x.get('timestamp',0)))*1000 if int(x.get('time',x.get('timestamp',0)))<10**12 else int(x.get('time',x.get('timestamp',0))),'o':float(x.get('open',0)),'h':float(x.get('high',0)),'l':float(x.get('low',0)),'c':float(x.get('close',0)),'v':float(x.get('volume',0))})
    rows.sort(key=lambda z:z['t'])
    return rows

# ---------- broker clients ----------
def delta_req(path,method='GET',body=None,params=None):
    cred=broker_creds('delta'); key=cred.get('api_key',''); secret=cred.get('api_secret','')
    if not key or not secret: raise RuntimeError('DELTA_API_KEY / DELTA_API_SECRET not configured')
    payload=json.dumps(body,separators=(',',':')) if body is not None else ''
    query=('?'+urllib.parse.urlencode(params)) if params else ''
    ts=str(int(time.time())); msg=method+ts+path+query+payload; sig=hmac.new(secret.encode(),msg.encode(),hashlib.sha256).hexdigest()
    return http_json('https://api.india.delta.exchange'+path+query,method,body,{'api-key':key,'timestamp':ts,'signature':sig,'User-Agent':'TradePilot-Local/1.0','Content-Type':'application/json','Accept':'application/json'})

def kite_req(path,params=None,method='GET',body=None):
    cred=broker_creds('zerodha'); token=cred.get('access_token',''); api=cred.get('api_key','')
    if not token or not api: raise RuntimeError('KITE_API_KEY / KITE_ACCESS_TOKEN not configured')
    url='https://api.kite.trade'+path
    if params and method=='GET': url+='?'+urllib.parse.urlencode(params)
    headers={'X-Kite-Version':'3','Authorization':f'token {api}:{token}','User-Agent':'TradePilot-Local/1.0'}
    if method in ('POST','PUT','DELETE'):
        data=urllib.parse.urlencode(body or {}).encode() if method in ('POST','PUT') else None
        req=urllib.request.Request(url,data=data,method=method,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=10) as r:return json.loads(r.read().decode())
        except urllib.error.HTTPError as e: raise RuntimeError(e.read().decode(errors='replace'))
    return http_json(url,method,body,headers)

def broker_creds(broker):
    c=db(); row=c.execute('select * from broker_credentials where broker=?',(broker,)).fetchone(); c.close()
    return dict(row) if row else {'broker':broker,'api_key':'','api_secret':'','access_token':''}

def broker_config_status():
    k=broker_creds('zerodha'); d=broker_creds('delta')
    kite_key=bool(k.get('api_key')); kite_token=bool(k.get('access_token'))
    delta_key=bool(d.get('api_key')); delta_secret=bool(d.get('api_secret'))
    return {
        'zerodha': {'configured': kite_key and kite_token, 'apiKeyPresent': kite_key, 'accessTokenPresent': kite_token, 'mode': 'live'},
        'delta': {'configured': delta_key and delta_secret, 'apiKeyPresent': delta_key, 'apiSecretPresent': delta_secret, 'mode': 'live'},
        'mockBrokers': MOCK_BROKERS, 'manualOnly': True
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_OPTIONS(self): self.send_response(204);self.end_headers()
    def do_GET(self):
        u=urllib.parse.urlparse(self.path); q=urllib.parse.parse_qs(u.query)
        try:
            if u.path=='/api/health': return json_response(self,{'ok':True,'mode':'local','manualOnly':True})
            if u.path=='/api/settings':
                r=dict(db().execute('select * from settings where id=1').fetchone());return json_response(self,r)
            if u.path=='/api/brokers/status':
                return json_response(self,broker_config_status())
            if u.path=='/api/brokers/kite/login-url':
                k=broker_creds('zerodha')
                if not k.get('api_key'): return json_response(self,{'error':'Save your Zerodha API key first'},400)
                redirect=f'http://127.0.0.1:{PORT}/'
                url='https://kite.zerodha.com/connect/login?v=3&api_key='+urllib.parse.quote(k['api_key'])
                return json_response(self,{'loginUrl':url,'redirectUrl':redirect})
            if u.path=='/api/brokers/credentials':
                k=broker_creds('zerodha'); d=broker_creds('delta')
                return json_response(self,{'zerodha':{'apiKeyPresent':bool(k.get('api_key')),'apiSecretPresent':bool(k.get('api_secret')),'accessTokenPresent':bool(k.get('access_token'))},'delta':{'apiKeyPresent':bool(d.get('api_key')),'apiSecretPresent':bool(d.get('api_secret'))}})
            if u.path=='/api/trades':
                real=[dict(x) for x in db().execute("select id,created_at,broker,asset,symbol,side,qty,entry,sl,target,exit,status,pnl,fees,order_id,notes,'LIVE' as mode from trades")]
                paper=[dict(x) for x in db().execute("select id+1000000 as id,created_at,broker,asset,symbol,side,qty,entry,sl,target,exit,status,pnl,fees,NULL as order_id,notes,'PAPER' as mode from paper_trades")]
                rows=sorted(real+paper,key=lambda x:x['id'],reverse=True)[:100];return json_response(self,rows)
            if u.path=='/api/scan/stock':
                symbol=q.get('symbol',['RELIANCE.NS'])[0];cfg=dict(db().execute('select * from settings where id=1').fetchone());s=signal(yahoo(symbol),float(cfg['stock_capital']),float(cfg['stock_risk']));s.update({'broker':'zerodha','symbol':symbol});return json_response(self,s)
            if u.path=='/api/scan/crypto':
                symbol=q.get('symbol',['BTCUSD'])[0];cfg=dict(db().execute('select * from settings where id=1').fetchone());s=signal(delta_public(symbol),float(cfg['crypto_capital']),float(cfg['crypto_risk']));s.update({'broker':'delta','symbol':symbol});return json_response(self,s)
            if u.path=='/api/broker/delta/positions': return json_response(self,delta_req('/v2/positions').get('result',[]))
            if u.path=='/api/broker/kite/positions': return json_response(self,kite_req('/portfolio/positions').get('data',{}).get('net',[]))
            if u.path=='/api/execution-status':
                broker=q.get('broker',[''])[0]; oid=q.get('order_id',[''])[0]
                if not broker or not oid: return json_response(self,{'error':'broker and order_id are required'},400)
                return json_response(self, self.execution_status(broker, oid))
            if u.path=='/api/reconcile':
                return json_response(self, self.reconcile_positions())
            if u.path=='/api/paper/tick':
                return json_response(self, self.paper_tick())
            if u.path=='/api/state':
                st=dict(db().execute('select * from app_state where id=1').fetchone());return json_response(self,st)
            if u.path=='/api/executions':
                rows=[dict(x) for x in db().execute('select * from executions order by id desc limit 100')];return json_response(self,rows)
            return self.serve_file()
        except Exception as e: return json_response(self,{'error':str(e)},400)
    def do_PUT(self):
        try:
            u=urllib.parse.urlparse(self.path); b=read_json(self)
            if u.path=='/api/settings':
                allowed={'stock_capital','crypto_capital','stock_risk','crypto_risk','max_daily_loss','max_positions'}
                vals={k:b[k] for k in allowed if k in b}
                if 'stock_capital' in vals and float(vals['stock_capital'])<=0: raise ValueError('Stock capital must be positive')
                if 'crypto_capital' in vals and float(vals['crypto_capital'])<=0: raise ValueError('Crypto capital must be positive')
                for k in ('stock_risk','crypto_risk','max_daily_loss'):
                    if k in vals and not (0<float(vals[k])<=HARD_RISK): raise ValueError(f'{k} must be greater than 0 and no more than 2%')
                if 'max_positions' in vals and not (1<=int(vals['max_positions'])<=MAX_POSITIONS): raise ValueError('max_positions must be between 1 and 4')
                c=db()
                if vals:
                    sets=', '.join(f'{k}=?' for k in vals); c.execute(f'update settings set {sets} where id=1',tuple(vals.values())); c.commit()
                r=dict(c.execute('select * from settings where id=1').fetchone()); c.close(); return json_response(self,r)
            if u.path=='/api/state':
                c=db(); st=dict(c.execute('select * from app_state where id=1').fetchone())
                requested_live=bool(b.get('live_enabled',st['live_enabled']))
                emergency=1 if bool(b.get('emergency_stop',st['emergency_stop'])) else 0
                supplied_secret=str(b.get('controlSecret') or '')
                # Live permission is itself consequential: require the same local control secret
                # used for live entries whenever a secret has been configured. Disarming/emergency
                # stop remains possible without the secret so the operator can always fail safe.
                if requested_live and CONTROL_SECRET and supplied_secret != CONTROL_SECRET:
                    c.close(); return json_response(self,{'error':'Valid TradePilot control secret is required to enable LIVE execution'},403)
                live=1 if requested_live else 0
                c.execute('update app_state set live_enabled=?, emergency_stop=? where id=1',(live,emergency)); c.commit(); c.close()
                return json_response(self,{'live_enabled':live,'emergency_stop':emergency})
            return json_response(self,{'error':'Unknown endpoint'},404)
        except Exception as e: return json_response(self,{'error':str(e)},400)
    def do_POST(self):
        try:
            u=urllib.parse.urlparse(self.path); b=read_json(self)
            if u.path=='/api/brokers/credentials':
                broker=str(b.get('broker','')).lower()
                if broker not in ('zerodha','delta'): return json_response(self,{'error':'Unsupported broker'},400)
                api_key=str(b.get('apiKey') or '').strip(); api_secret=str(b.get('apiSecret') or '').strip(); access_token=str(b.get('accessToken') or '').strip()
                if broker=='zerodha' and (not api_key or not api_secret): return json_response(self,{'error':'Zerodha API key and API secret are required'},400)
                if broker=='delta' and (not api_key or not api_secret): return json_response(self,{'error':'Delta API key and API secret are required'},400)
                c=db(); c.execute('insert into broker_credentials(broker,api_key,api_secret,access_token,updated_at) values(?,?,?,?,?) on conflict(broker) do update set api_key=excluded.api_key,api_secret=excluded.api_secret,access_token=excluded.access_token,updated_at=excluded.updated_at',(broker,api_key,api_secret,access_token,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
                return json_response(self,{'success':True,'broker':broker,'message':'Credentials saved locally. Secrets are never returned by the API.'})
            if u.path=='/api/brokers/kite/generate-token':
                request_token=str(b.get('requestToken') or '').strip()
                k=broker_creds('zerodha')
                if not request_token: return json_response(self,{'error':'Request token is required'},400)
                if not k.get('api_key') or not k.get('api_secret'): return json_response(self,{'error':'Save the Zerodha API key and API secret first'},400)
                checksum=hashlib.sha256((k['api_key']+request_token+k['api_secret']).encode()).hexdigest()
                form=urllib.parse.urlencode({'api_key':k['api_key'],'request_token':request_token,'checksum':checksum}).encode()
                req=urllib.request.Request('https://api.kite.trade/session/token',data=form,method='POST',headers={'X-Kite-Version':'3','Content-Type':'application/x-www-form-urlencoded','User-Agent':'TradePilot-Local/1.0'})
                try:
                    with urllib.request.urlopen(req,timeout=15) as r: out=json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    raw=e.read().decode(errors='replace'); raise RuntimeError(f'Kite token exchange failed: {raw[:500]}')
                token=str((out.get('data') or {}).get('access_token') or '')
                if not token: raise RuntimeError('Kite did not return an access token')
                c=db(); c.execute('update broker_credentials set access_token=?,updated_at=? where broker=?',(token,datetime.now(timezone.utc).isoformat(),'zerodha')); c.commit(); c.close()
                return json_response(self,{'success':True,'message':'Zerodha access token generated and stored locally.','accessTokenPresent':True})
            if u.path=='/api/brokers/test':
                broker=str(b.get('broker','')).lower()
                if broker=='zerodha':
                    out=kite_req('/user/profile'); return json_response(self,{'success':True,'broker':'zerodha','profile':out.get('data',{})})
                if broker=='delta':
                    out=delta_req('/v2/wallet/balances'); return json_response(self,{'success':True,'broker':'delta','balances':out.get('result',[])})
                return json_response(self,{'error':'Unsupported broker'},400)
            if u.path=='/api/prepare':
                broker=b['broker']; s=b['signal']; qty=max(1,int(b.get('qty') or s.get('qty') or 0)); return json_response(self,{'broker':broker,'symbol':s['symbol'],'side':s['side'],'qty':qty,'entry':s['entry'],'sl':s['sl'],'target':s['target'],'possibleLoss':abs(s['entry']-s['sl'])*qty,'possibleProfit':abs(s['target']-s['entry'])*qty,'manualOnly':True})
            if u.path=='/api/paper/execute': return self.paper_execute(b)
            if u.path=='/api/paper/close': return self.paper_close(b)
            if u.path=='/api/paper/mark': return self.paper_mark(b)
            if u.path=='/api/mock/exit': return self.mock_exit(b)
            if u.path=='/api/execute': return self.execute(b)
            return json_response(self,{'error':'Unknown endpoint'},404)
        except Exception as e:return json_response(self,{'error':str(e)},400)
    def mock_order(self, broker, symbol, side, qty, price, filled=True, conn=None):
        oid=f'MOCK-{broker.upper()}-{uuid.uuid4().hex[:12]}'
        own=conn is None; c=conn or db(); status='COMPLETE' if filled else 'OPEN'; fq=qty if filled else 0; c.execute('insert into mock_orders(order_id,broker,symbol,side,qty,status,filled_qty,average_price,created_at) values(?,?,?,?,?,?,?,?,?)',(oid,broker,symbol,side,qty,status,fq,price if filled else 0,datetime.now(timezone.utc).isoformat()));
        if own: c.commit(); c.close()
        return {'result':{'id':oid,'order_id':oid,'status':'COMPLETED','state':'COMPLETED','filled_size':qty,'filled_quantity':qty,'average_fill_price':price,'average_price':price}}

    def mock_status(self, broker, oid):
        c=db(); row=c.execute('select * from mock_orders where order_id=?',(str(oid),)).fetchone(); c.close()
        if not row: raise RuntimeError('Mock order not found')
        return {'status':str(row['status']).upper(),'filled':float(row['filled_qty']),'avg':float(row['average_price']),'data':dict(row)}

    def execution_status(self, broker, oid):
        if MOCK_BROKERS:
            m=self.mock_status(broker, oid)
            raw_status=m['status']; filled=m['filled']; avg=m['avg']; data=m['data']
        elif broker=='delta':
            out=delta_req('/v2/orders/'+urllib.parse.quote(str(oid)))
            data=out.get('result') or {}
            raw_status=str(data.get('state') or data.get('status') or data.get('order_status') or '').upper()
            filled=float(data.get('size') or data.get('filled_size') or data.get('filled_qty') or 0)
            avg=float(data.get('average_fill_price') or data.get('average_price') or data.get('avg_fill_price') or 0)
        elif broker=='zerodha':
            out=kite_req('/orders/'+urllib.parse.quote(str(oid)))
            arr=out.get('data') or []
            data=arr[-1] if arr else {}
            raw_status=str(data.get('status') or '').upper()
            filled=float(data.get('filled_quantity') or 0)
            avg=float(data.get('average_price') or 0)
        else: raise RuntimeError('Unsupported broker')
        mapped='FILLED' if raw_status in ('FILLED','COMPLETED','COMPLETE') else 'PARTIALLY_FILLED' if filled>0 and raw_status not in ('CANCELLED','REJECTED') else 'REJECTED' if raw_status in ('REJECTED','CANCELLED','EXPIRED') else 'SUBMITTED'
        c=db(); trade=c.execute('select * from trades where order_id=? order by id desc limit 1',(str(oid),)).fetchone()
        protection={}
        if trade:
            if mapped=='FILLED':
                c.execute('update trades set status=?,entry=?,qty=? where id=?',('OPEN',avg or trade['entry'],filled or trade['qty'],trade['id']))
                # Kite needs explicit protective exits; create them exactly once after a verified fill.
                if broker=='zerodha' and not trade['stop_order_id'] and filled>0:
                    sym=trade['symbol'].replace('.NS',''); trans_exit='SELL' if trade['side'] in ('BUY','LONG') else 'BUY'
                    if MOCK_BROKERS:
                        sl=self.mock_order('zerodha',sym,trans_exit,int(filled),float(trade['sl']),filled=False,conn=c)
                        tp=self.mock_order('zerodha',sym,trans_exit,int(filled),float(trade['target']),filled=False,conn=c)
                    else:
                        sl=kite_req('/orders/regular','POST',body={'variety':'regular','exchange':'NSE','tradingsymbol':sym,'transaction_type':trans_exit,'quantity':int(filled),'product':'MIS','order_type':'SL-M','trigger_price':float(trade['sl']),'validity':'DAY','tag':'TradePilot-SL'})
                        tp=kite_req('/orders/regular','POST',body={'variety':'regular','exchange':'NSE','tradingsymbol':sym,'transaction_type':trans_exit,'quantity':int(filled),'product':'MIS','order_type':'LIMIT','price':float(trade['target']),'validity':'DAY','tag':'TradePilot-TP'})
                    if MOCK_BROKERS:
                        so=str((sl.get('result') or {}).get('id','')); to=str((tp.get('result') or {}).get('id',''))
                    else:
                        so=str((sl.get('data') or {}).get('order_id','')); to=str((tp.get('data') or {}).get('order_id',''))
                    if not so or not to:
                        raise RuntimeError('Protective exit order creation failed; position requires immediate manual review')
                    c.execute('update trades set stop_order_id=?,target_order_id=?,notes=? where id=?',(so,to,'Protective exits placed; OCO reconciliation active.',trade['id']))
                    protection={'stopOrderId':so,'targetOrderId':to}
            elif mapped in ('REJECTED',): c.execute('update trades set status=? where id=?',('REJECTED',trade['id']))
            c.commit()
        c.close()
        # Reconcile a previously protected Kite position on every verification call.
        # Close the first SQLite connection before opening the reconciliation writer;
        # otherwise SQLite can remain locked by the current request thread.
        if broker=='zerodha' and trade and (trade['stop_order_id'] or trade['target_order_id']):
            protection=self.reconcile_trade_exits(trade['id'])
        return {'broker':broker,'orderId':str(oid),'brokerStatus':raw_status,'status':mapped,'filledQty':filled,'averageFillPrice':avg,'protection':protection}

    def kite_order_status(self, oid):
        if MOCK_BROKERS: return self.mock_status('zerodha',oid)['status'], self.mock_status('zerodha',oid)['filled'], self.mock_status('zerodha',oid)['avg'], self.mock_status('zerodha',oid)['data']
        out=kite_req('/orders/'+urllib.parse.quote(str(oid)))
        arr=out.get('data') or []; data=arr[-1] if arr else {}
        return str(data.get('status') or '').upper(), float(data.get('filled_quantity') or 0), float(data.get('average_price') or 0), data

    def cancel_kite(self, oid):
        if MOCK_BROKERS:
            c=db(); c.execute("update mock_orders set status='CANCELLED' where order_id=?",(str(oid),)); c.commit(); c.close(); return {'success':True,'mock':True}
        return kite_req('/orders/regular/'+urllib.parse.quote(str(oid)),'DELETE')

    def reconcile_trade_exits(self, trade_id):
        c=db(); trade=c.execute('select * from trades where id=?',(trade_id,)).fetchone()
        if not trade or trade['broker']!='zerodha': c.close(); return {}
        so,to=trade['stop_order_id'],trade['target_order_id']
        statuses={}
        for name,oid in (('stop',so),('target',to)):
            if oid:
                st,filled,avg,_=self.kite_order_status(oid); statuses[name]={'id':oid,'status':st,'filled':filled,'avg':avg}
        stop_f=statuses.get('stop',{}).get('status') in ('COMPLETE','FILLED')
        target_f=statuses.get('target',{}).get('status') in ('COMPLETE','FILLED')
        closed=False; exit_price=None; reason=None
        if stop_f or target_f:
            winner='stop' if stop_f else 'target'; winner_data=statuses[winner]; exit_price=winner_data.get('avg') or (trade['sl'] if winner=='stop' else trade['target']); reason='STOP_LOSS' if winner=='stop' else 'TARGET'
            loser=to if winner=='stop' else so
            if loser and statuses.get('target' if winner=='stop' else 'stop',{}).get('status') not in ('COMPLETE','FILLED','CANCELLED','REJECTED'):
                try:self.cancel_kite(loser)
                except Exception as e:
                    c.execute('update trades set notes=? where id=?',(f'Exit {reason} filled; sibling cancellation failed: {e}',trade_id))
            direction=1 if trade['side'] in ('BUY','LONG') else -1
            pnl=(float(exit_price)-float(trade['entry']))*float(trade['qty'])*direction
            c.execute('update trades set status=?,exit=?,pnl=?,notes=? where id=?',('CLOSED',exit_price,pnl,f'Closed by {reason}; sibling exit reconciled.',trade_id));closed=True
        c.commit(); c.close()
        return {'stopOrderId':so,'targetOrderId':to,'statuses':statuses,'closed':closed,'exitPrice':exit_price,'reason':reason}

    def reconcile_delta_trades(self, positions):
        c=db(); results=[]
        by_symbol={str(p.get('symbol') or p.get('product_symbol') or '').upper():p for p in positions if str(p.get('symbol') or p.get('product_symbol') or '')}
        rows=c.execute("select * from trades where broker='delta' and status in ('SUBMITTED','PARTIALLY_FILLED','OPEN') order by id").fetchall()
        for trade in rows:
            pos=by_symbol.get(trade['symbol'].upper())
            if pos is None:
                results.append({'tradeId':trade['id'],'symbol':trade['symbol'],'status':'UNVERIFIED_NO_POSITION'})
                continue
            try: size=float(pos.get('size') or 0)
            except Exception: size=0
            side_mult=1 if trade['side'] in ('LONG','BUY') else -1
            signed=size
            if signed and ((signed>0) != (side_mult>0)):
                # Position exists in the opposite direction; this is not the position opened by this trade.
                results.append({'tradeId':trade['id'],'symbol':trade['symbol'],'status':'POSITION_DIRECTION_MISMATCH','positionSize':size})
                continue
            if size == 0:
                # A zero position after a verified entry means the position was closed externally or by its bracket.
                # Use the latest fill for this symbol to recover an actual exit price when available.
                try:
                    fills=delta_req('/v2/fills',params={'product_symbols':trade['symbol'],'page_size':50}).get('result',[])
                except Exception:
                    fills=[]
                exit_fill=None
                for f in fills:
                    if str(f.get('order_id')) != str(trade['order_id']):
                        exit_fill=f; break
                if exit_fill is not None:
                    exit_price=float(exit_fill.get('price') or 0)
                    commission=float(exit_fill.get('commission') or 0)
                    direction=1 if trade['side'] in ('LONG','BUY') else -1
                    pnl=(exit_price-float(trade['entry']))*float(trade['qty'])*direction-commission
                    c.execute('update trades set status=?,exit=?,pnl=?,fees=?,notes=? where id=?',('CLOSED',exit_price,pnl,abs(commission),'Closed after Delta position reconciliation.',trade['id']))
                    results.append({'tradeId':trade['id'],'symbol':trade['symbol'],'status':'CLOSED','exitPrice':exit_price,'pnl':pnl})
                else:
                    c.execute('update trades set status=?,notes=? where id=?',('CLOSED_UNVERIFIED','Position is flat; exit fill not recovered yet.',trade['id']))
                    results.append({'tradeId':trade['id'],'symbol':trade['symbol'],'status':'CLOSED_UNVERIFIED'})
            else:
                if trade['status'] != 'OPEN': c.execute('update trades set status=? where id=?',('OPEN',trade['id']))
                results.append({'tradeId':trade['id'],'symbol':trade['symbol'],'status':'OPEN','positionSize':size,'entryPrice':pos.get('entry_price') or pos.get('entryPrice')})
        c.commit(); c.close(); return results

    def reconcile_positions(self):
        result={'zerodha':[],'delta':[],'errors':[]}
        try:
            rows=db().execute("select id from trades where broker='zerodha' and status='OPEN' and (stop_order_id is not null or target_order_id is not null)").fetchall()
            for r in rows: result['zerodha'].append(self.reconcile_trade_exits(r['id']))
        except Exception as e: result['errors'].append('Zerodha: '+str(e))
        try:
            result['delta']=delta_req('/v2/positions').get('result',[])
            result['deltaTrades']=self.reconcile_delta_trades(result['delta'])
        except Exception as e: result['errors'].append('Delta: '+str(e))
        return result

    def paper_execute(self,b):
        if b.get('manualConfirmed') is not True: return json_response(self,{'error':'Explicit Execute confirmation is required'},400)
        broker=str(b.get('broker','')).lower(); s=b.get('signal') or {}; symbol=str(s.get('symbol','')).upper(); side=str(s.get('side','')).upper()
        if broker not in ('zerodha','delta') or not symbol or side not in ('BUY','SELL','LONG','SHORT'): return json_response(self,{'error':'Invalid paper order'},400)
        qty=max(1,int(b.get('qty') or s.get('qty') or 0)); entry=float(s.get('entry') or 0); sl=float(s.get('sl') or 0); target=float(s.get('target') or 0)
        if not s.get('tradable') or not entry or not sl or not target: return json_response(self,{'error':'Signal is not tradable or lacks prices'},400)
        cfg=dict(db().execute('select * from settings where id=1').fetchone()); capital=float(cfg['stock_capital'] if broker=='zerodha' else cfg['crypto_capital']); risk=min(float(cfg['stock_risk'] if broker=='zerodha' else cfg['crypto_risk']),HARD_RISK)
        if abs(entry-sl)*qty > capital*risk: return json_response(self,{'error':'Paper order exceeds hard 2% risk limit'},403)
        if entry*qty > capital*MAX_NOTIONAL: return json_response(self,{'error':'Paper order exceeds hard 20% notional limit'},403)
        open_count=int(db().execute("select count(*) from paper_trades where status='OPEN'").fetchone()[0])
        if open_count>=min(int(cfg['max_positions']),MAX_POSITIONS): return json_response(self,{'error':'Maximum paper positions reached'},409)
        c=db(); now=datetime.now(timezone.utc).isoformat(); cur=c.execute('insert into paper_trades(created_at,broker,asset,symbol,side,qty,entry,sl,target,status,notes) values(?,?,?,?,?,?,?,?,?,?,?)',(now,broker,'stock' if broker=='zerodha' else 'crypto',symbol,side,qty,entry,sl,target,'OPEN','Manual paper execution')); tid=cur.lastrowid; c.commit(); c.close()
        return json_response(self,{'success':True,'paper':True,'tradeId':tid,'status':'OPEN','broker':broker,'symbol':symbol,'qty':qty,'entry':entry,'sl':sl,'target':target})

    def paper_tick(self):
        """Advance all open paper positions using the latest available market price.
        A supplied/manual price can be used by POST /api/paper/mark for deterministic tests;
        this endpoint fetches the latest public quote for each open symbol where possible.
        """
        c=db(); rows=c.execute("select * from paper_trades where status='OPEN' order by id").fetchall(); results=[]
        for t in rows:
            try:
                if t['broker']=='zerodha':
                    sym=t['symbol'].replace('.NS','')
                    q=kite_req('/quote/ltp',params={'i':'NSE:'+sym})
                    data=(q.get('data') or {}).get('NSE:'+sym,{})
                    price=float(data.get('last_price') or 0)
                else:
                    q=delta_req('/v2/tickers/'+urllib.parse.quote(t['symbol'])).get('result',{})
                    price=float(q.get('close') or q.get('mark_price') or q.get('last_price') or 0)
                if not price: raise RuntimeError('No current price')
                result=self._paper_apply_price(c,t,price)
                results.append(result)
            except Exception as e:
                results.append({'tradeId':t['id'],'symbol':t['symbol'],'status':'UNCHANGED','error':str(e)})
        c.commit(); c.close(); return {'checked':len(rows),'results':results}

    def _paper_apply_price(self,c,t,price):
        side=t['side'].upper(); hit=None
        if side in ('BUY','LONG'):
            if price <= float(t['sl']): hit='STOP_LOSS'
            elif price >= float(t['target']): hit='TARGET'
        else:
            if price >= float(t['sl']): hit='STOP_LOSS'
            elif price <= float(t['target']): hit='TARGET'
        if not hit:
            unreal=(price-float(t['entry']))*float(t['qty'])*(1 if side in ('BUY','LONG') else -1)
            return {'tradeId':t['id'],'symbol':t['symbol'],'status':'OPEN','price':price,'unrealizedPnl':unreal}
        pnl=(price-float(t['entry']))*float(t['qty'])*(1 if side in ('BUY','LONG') else -1)
        c.execute('update paper_trades set status=?,exit=?,pnl=?,notes=? where id=?',('CLOSED',price,pnl,f'Paper engine auto-close: {hit}',t['id']))
        return {'tradeId':t['id'],'symbol':t['symbol'],'status':'CLOSED','exit':price,'pnl':pnl,'reason':hit}

    def paper_mark(self,b):
        tid=int(b.get('trade_id') or 0); price=float(b.get('price') or 0)
        if not tid or not price: return json_response(self,{'error':'trade_id and price are required'},400)
        c=db(); t=c.execute("select * from paper_trades where id=? and status='OPEN'",(tid,)).fetchone()
        if not t: c.close(); return json_response(self,{'error':'Open paper trade not found'},404)
        result=self._paper_apply_price(c,t,price); c.commit(); c.close(); return json_response(self,result)

    def paper_close(self,b):
        tid=int(b.get('trade_id') or 0); exit_price=float(b.get('exit') or 0)
        if not tid or not exit_price: return json_response(self,{'error':'trade_id and exit are required'},400)
        c=db(); t=c.execute("select * from paper_trades where id=? and status='OPEN'",(tid,)).fetchone()
        if not t: c.close(); return json_response(self,{'error':'Open paper trade not found'},404)
        direction=1 if t['side'] in ('BUY','LONG') else -1; pnl=(exit_price-float(t['entry']))*float(t['qty'])*direction
        c.execute('update paper_trades set status=?,exit=?,pnl=? where id=?',('CLOSED',exit_price,pnl,tid)); c.commit(); c.close(); return json_response(self,{'success':True,'tradeId':tid,'status':'CLOSED','exit':exit_price,'pnl':pnl})

    def mock_exit(self,b):
        if not MOCK_BROKERS: return json_response(self,{'error':'Mock brokers are disabled'},403)
        oid=str(b.get('order_id') or ''); price=float(b.get('price') or 0)
        if not oid or not price: return json_response(self,{'error':'order_id and price are required'},400)
        c=db(); row=c.execute('select * from mock_orders where order_id=?',(oid,)).fetchone()
        if not row: c.close(); return json_response(self,{'error':'Mock order not found'},404)
        c.execute("update mock_orders set status='COMPLETE',filled_qty=qty,average_price=? where order_id=?",(price,oid)); c.commit(); c.close(); return json_response(self,{'success':True,'orderId':oid,'status':'COMPLETE','price':price})

    def execute(self,b):
        if b.get('manualConfirmed') is not True: return json_response(self,{'error':'Explicit Execute confirmation is required'},400)
        broker=b['broker'];s=b['signal'];qty=max(1,int(b.get('qty') or s.get('qty') or 0)); symbol=s['symbol'].upper()
        state=dict(db().execute('select * from app_state where id=1').fetchone())
        if int(state['emergency_stop']): return json_response(self,{'error':'Emergency stop is active'},403)
        if not int(state['live_enabled']): return json_response(self,{'error':'Live execution is disabled. Enable LIVE mode first.'},403)
        if CONTROL_SECRET and b.get('controlSecret') != CONTROL_SECRET: return json_response(self,{'error':'Invalid TradePilot control secret'},403)
        if not s.get('tradable'): return json_response(self,{'error':'This signal is not tradable'},400)
        # local risk guard
        if qty<=0:return json_response(self,{'error':'Invalid quantity'},400)
        cfg=dict(db().execute('select * from settings where id=1').fetchone())
        capital=float(cfg['stock_capital'] if broker=='zerodha' else cfg['crypto_capital'])
        risk=min(float(cfg['stock_risk'] if broker=='zerodha' else cfg['crypto_risk']),HARD_RISK)
        open_count=int(db().execute("select count(*) from trades where status IN ('SUBMITTED','OPEN','PARTIALLY_FILLED')").fetchone()[0])
        if open_count>=min(int(cfg['max_positions']),MAX_POSITIONS): return json_response(self,{'error':'Maximum open positions reached'},409)
        today=datetime.now(timezone.utc).date().isoformat()
        realized=float(db().execute("select coalesce(sum(pnl),0) from trades where status='CLOSED' and substr(created_at,1,10)=?",(today,)).fetchone()[0])
        if realized <= -capital*min(float(cfg['max_daily_loss']),HARD_RISK): return json_response(self,{'error':'Daily loss limit reached'},403)
        if abs(float(s.get('entry') or 0)-float(s.get('sl') or 0))*qty > capital*risk: return json_response(self,{'error':'Requested quantity exceeds the hard 2% risk limit'},403)
        key=f'{broker}:{symbol}:{s.get("candleTime")}:{s["side"]}'
        c=db();
        try:c.execute('insert into executions(execution_key,created_at,broker,symbol,action,status) values(?,?,?,?,?,?)',(key,datetime.now(timezone.utc).isoformat(),broker,symbol,'ENTRY','CLAIMED'));c.commit()
        except sqlite3.IntegrityError:return json_response(self,{'error':'This signal has already been executed'},409)
        try:
            # Re-check live price immediately before sending.
            if MOCK_BROKERS:
                ltp=float(s.get('mockLivePrice') or s.get('entry') or 0)
                if not ltp: raise RuntimeError('Mock live price unavailable')
                if ltp*qty > capital*MAX_NOTIONAL: raise RuntimeError('Current-price order exceeds hard 20% notional limit')
                move=abs((ltp-float(s['entry']))/float(s['entry'])) if float(s.get('entry') or 0) else 0
                drift=PRICE_DRIFT_CRYPTO if broker=='delta' else PRICE_DRIFT_STOCK
                if move>drift: raise RuntimeError(f'Price moved {move*100:.2f}% from signal; review setup again')
                dist=abs(float(s['entry'])-float(s['sl'])); s['entry']=ltp
                s['sl']=ltp-dist if s['side'] in ('BUY','LONG') else ltp+dist
                s['target']=ltp+2*dist if s['side'] in ('BUY','LONG') else ltp-2*dist
                out=self.mock_order(broker,symbol,s['side'],qty,ltp); oid=str((out.get('result') or {}).get('id','')); status='SUBMITTED'
            elif broker=='delta':
                live=delta_req('/v2/tickers/'+urllib.parse.quote(symbol)).get('result',{})
                ltp=float(live.get('close') or live.get('mark_price') or live.get('last_price') or 0)
                if not ltp: raise RuntimeError('Live Delta price unavailable')
                if ltp*qty > capital*MAX_NOTIONAL: raise RuntimeError('Current-price order exceeds hard 20% notional limit')
                move=abs((ltp-float(s['entry']))/float(s['entry'])) if float(s.get('entry') or 0) else 0
                if move>PRICE_DRIFT_CRYPTO: raise RuntimeError(f'Price moved {move*100:.2f}% from signal; review setup again')
                if abs(ltp-float(s['sl']))*qty > capital*risk: raise RuntimeError('Requested quantity exceeds the hard 2% crypto risk limit')
                dist=abs(float(s['entry'])-float(s['sl'])); s['entry']=ltp; s['sl']=ltp-dist if s['side']=='LONG' else ltp+dist; s['target']=ltp+2*dist if s['side']=='LONG' else ltp-2*dist
                # product lookup
                prod=delta_req('/v2/products/'+urllib.parse.quote(symbol)).get('result',{});pid=prod.get('id')
                if not pid: raise RuntimeError('Delta product id unavailable')
                side='buy' if s['side']=='LONG' else 'sell'
                body={'product_id':pid,'product_symbol':symbol,'size':qty,'side':side,'order_type':'market_order','time_in_force':'ioc','reduce_only':False,'bracket_stop_trigger_method':'last_traded_price','bracket_stop_loss_price':str(s['sl']),'bracket_take_profit_price':str(s['target'])}
                out=delta_req('/v2/orders','POST',body); oid=str((out.get('result') or {}).get('id',''));status='SUBMITTED'
            elif broker=='zerodha':
                quote=kite_req('/quote/ltp',params={'i':'NSE:'+symbol.replace('.NS','')})
                qdata=(quote.get('data') or {}).get('NSE:'+symbol.replace('.NS',''),{})
                ltp=float(qdata.get('last_price') or 0)
                if not ltp: raise RuntimeError('Live Kite price unavailable')
                if ltp*qty > capital*MAX_NOTIONAL: raise RuntimeError('Current-price order exceeds hard 20% notional limit')
                move=abs((ltp-float(s['entry']))/float(s['entry'])) if float(s.get('entry') or 0) else 0
                if move>PRICE_DRIFT_STOCK: raise RuntimeError(f'Price moved {move*100:.2f}% from signal; review setup again')
                if abs(ltp-float(s['sl']))*qty > capital*risk: raise RuntimeError('Requested quantity exceeds the hard 2% stock risk limit')
                dist=abs(float(s['entry'])-float(s['sl'])); s['entry']=ltp; s['sl']=ltp-dist if s['side'] in ('BUY','LONG') else ltp+dist; s['target']=ltp+2*dist if s['side'] in ('BUY','LONG') else ltp-2*dist
                trans='BUY' if s['side'] in ('BUY','LONG') else 'SELL'
                out=kite_req('/orders/regular','POST',body={'variety':'regular','exchange':'NSE','tradingsymbol':symbol.replace('.NS',''),'transaction_type':trans,'quantity':qty,'product':'MIS','order_type':'MARKET','validity':'DAY','tag':'TradePilot'})
                oid=str((out.get('data') or {}).get('order_id',''));status='SUBMITTED'
            else: raise RuntimeError('Unsupported broker')
            c.execute('update executions set status=?,order_id=?,response=? where execution_key=?',(status,oid,json.dumps(out),key));c.execute('insert into trades(created_at,broker,asset,symbol,side,qty,entry,sl,target,status,order_id,notes) values(?,?,?,?,?,?,?,?,?,?,?,?)',(datetime.now(timezone.utc).isoformat(),broker,'stock' if broker=='zerodha' else 'crypto',symbol,s['side'],qty,s['entry'],s['sl'],s['target'],'SUBMITTED',oid,'Manual Execute'));c.commit();return json_response(self,{'success':True,'status':status,'orderId':oid,'brokerResponse':out,'manual':True})
        except Exception as e:
            # If no broker order was created, release the idempotency claim so a stale/invalid ticket can be reviewed and retried.
            if 'oid' not in locals() or not oid:
                c.execute('delete from executions where execution_key=?',(key,))
            else:
                c.execute('update executions set status=?,response=? where execution_key=?',('ERROR',str(e),key))
            c.commit();raise
    def serve_file(self):
        path=ROOT/(urllib.parse.urlparse(self.path).path.lstrip('/') or 'index.html')
        if not path.exists() or not path.is_file(): self.send_error(404);return
        data=path.read_bytes();ct='text/html' if path.suffix=='.html' else 'text/plain';self.send_response(200);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

if __name__=='__main__':
    print(f'TradePilot Local running at http://{HOST}:{PORT}')
    print('Manual live execution only: scanner never places orders.')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
