"""Run only against a disposable staging database before creating real accounts."""
import json
import os
import secrets
import urllib.request
import urllib.error
from app import database, ph
from migrate import plan, apply
import hashlib

def request(path, payload=None, cookie=None, csrf=None, origin=None):
    headers = {'Origin':origin or os.environ['PUBLIC_ORIGIN']}
    if cookie: headers['Cookie']=cookie
    if csrf: headers['X-CSRF-Token']=csrf
    if payload is not None: headers['Content-Type']='application/json'
    req = urllib.request.Request('http://127.0.0.1:8000'+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status,json.load(r),r.headers
    except urllib.error.HTTPError as e:
        return e.code,{},e.headers

name='smoke-'+secrets.token_hex(8)
password=secrets.token_urlsafe(24)
with database() as db:
    for table in ('users','transactions','import_batches','settings','carry_anchors','simulation_events','fixed_snapshots','legacy_documents'):
        if db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
            raise SystemExit('Smoke test requires an empty disposable database')
    db.execute('INSERT INTO users(username,password_hash) VALUES (%s,%s)',(name,ph.hash(password)))
try:
    assert request('/health')[0]==200
    assert request('/api/transactions')[0]==401
    assert request('/api/login',{'username':name,'password':password},origin='https://evil.invalid')[0]==403
    code,body,headers=request('/api/login',{'username':name,'password':password})
    assert code==200
    cookie=headers['Set-Cookie'].split(';')[0]
    assert all(s in headers['Set-Cookie'] for s in ['Secure','HttpOnly','SameSite=strict'])
    assert request('/api/transactions',cookie=cookie)[0]==200
    assert request('/api/logout',{},cookie=cookie)[0]==403
    assert request('/api/logout',{},cookie=cookie,csrf=body['csrf'])[0]==200
    assert request('/api/me',cookie=cookie)[0]==401
    fixture={'tx':{'2026-09':{'smoke':{'date':'2026-09-01','name':'synthetic','cat':'식비','amt':1200,'type':'expense','who':'j','payMethod':'card'}}},'config':{'carryAnchors':{'2026-09':5000},'simEvents':[{'id':'test','amt':500,'ym':'2026-10'}]},'fixed':{'2026-09':[]}}
    p=plan(fixture,{})
    sha=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert apply(p,sha)==sha
    assert apply(p,sha)==sha
    with database() as db:
        assert db.execute('SELECT count(*) AS n FROM transactions').fetchone()['n']==1
    print('PASS: auth, Origin, secure cookie, CSRF, logout invalidation, SQL import, reconciliation, idempotency')
finally:
    with database() as db:
        db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=%s)',(name,))
        db.execute('DELETE FROM users WHERE username=%s',(name,))
        db.execute('DELETE FROM login_attempts WHERE username=%s',(name,))
        # Only the synthetic batch created by this test is removed.
        if 'sha' in globals():
            db.execute('DELETE FROM transactions WHERE batch_id=%s',(sha,))
            db.execute('DELETE FROM fixed_snapshots WHERE batch_id=%s',(sha,))
            db.execute('DELETE FROM legacy_documents WHERE batch_id=%s',(sha,))
            db.execute('DELETE FROM simulation_events WHERE id=%s',('test',))
            db.execute('DELETE FROM carry_anchors WHERE month=%s',('2026-09',))
            db.execute('DELETE FROM settings WHERE key IN (%s,%s)',('carryAnchors','simEvents'))
            db.execute('DELETE FROM import_batches WHERE id=%s',(sha,))
