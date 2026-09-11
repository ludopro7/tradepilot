#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

ROOT=Path(__file__).resolve().parent
port='8799'
with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
    db=f.name

env=os.environ.copy(); env['TRADEPILOT_DB']=db; env['TRADEPILOT_PORT']=port
p=subprocess.Popen([sys.executable, str(ROOT/'server.py')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
base=f'http://127.0.0.1:{port}'

def req(path, method='GET', body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(base+path, data=data, method=method, headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=3) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def expect(code, path, method='GET', body=None, contains=None):
    got, out=req(path,method,body)
    assert got==code, (path,got,out)
    if contains: assert contains in str(out), (path,out)
    return out

try:
    for _ in range(30):
        try:
            if req('/api/health')[0]==200: break
        except Exception: pass
        time.sleep(.1)
    else: raise AssertionError('server did not start')
    assert expect(200,'/api/health')['manualOnly'] is True
    assert expect(200,'/api/state')['live_enabled']==0

    sig={'symbol':'TEST','side':'LONG','tradable':True,'entry':100,'sl':99,'target':102,'candleTime':'2026-09-11T15:00:00Z','qty':10}
    expect(400,'/api/paper/execute','POST',{'broker':'zerodha','signal':sig,'qty':10},'Explicit Execute confirmation')
    expect(403,'/api/paper/execute','POST',{'broker':'zerodha','signal':sig,'qty':2000,'manualConfirmed':True},'hard 2% risk')
    opened=expect(200,'/api/paper/execute','POST',{'broker':'zerodha','signal':sig,'qty':10,'manualConfirmed':True})
    tid=opened['tradeId']
    marked=expect(200,'/api/paper/mark','POST',{'trade_id':tid,'price':101})
    assert marked['status']=='OPEN'
    closed=expect(200,'/api/paper/mark','POST',{'trade_id':tid,'price':102})
    assert closed['status']=='CLOSED' and closed['reason']=='TARGET' and closed['pnl']==20

    # Live path must remain fail-closed.
    expect(403,'/api/execute','POST',{'broker':'zerodha','signal':sig,'qty':10,'manualConfirmed':True},'Live execution is disabled')
    # Emergency stop also blocks even if live is later enabled.
    expect(200,'/api/state','PUT',{'live_enabled':True})
    expect(200,'/api/state','PUT',{'live_enabled':False,'emergency_stop':True})
    expect(403,'/api/execute','POST',{'broker':'zerodha','signal':sig,'qty':10,'manualConfirmed':True},'Emergency stop')
    print('TradePilot local self-test: PASS')
finally:
    p.terminate()
    try: p.wait(timeout=3)
    except subprocess.TimeoutExpired: p.kill()
    try: os.unlink(db)
    except OSError: pass
