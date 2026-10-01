import csv
import hashlib
import io
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ph = PasswordHasher()
DUMMY = ph.hash(secrets.token_urlsafe(32))
PASSTHROUGH = {'대납', '정산', '카드대금', '부업'}

@contextmanager
def database():
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        yield conn

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def origin(request):
    if request.headers.get('origin') != os.environ['PUBLIC_ORIGIN']:
        raise HTTPException(403, 'Invalid origin')

def authenticated(request: Request):
    with database() as db:
        row = db.execute('SELECT s.*, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=%s AND expires_at>now() AND NOT u.disabled', (digest(request.cookies.get('__Host-budget','')),)).fetchone()
    if not row:
        raise HTTPException(401, 'Login required')
    if request.method not in ('GET','HEAD'):
        origin(request)
        if not secrets.compare_digest(request.headers.get('x-csrf-token',''), row['csrf']):
            raise HTTPException(403, 'Invalid CSRF token')
    return row

class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)

class Transaction(BaseModel):
    occurred_on: date
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    amount: int = Field(gt=0, le=9_000_000_000_000_000)
    direction: str
    owner: str
    payment_method: str | None = None
    memo: str = Field(default='', max_length=1000)

    @field_validator('direction')
    @classmethod
    def valid_direction(cls, v):
        if v not in {'income','expense'}: raise ValueError('invalid direction')
        return v

    @field_validator('owner')
    @classmethod
    def valid_owner(cls, v):
        if v not in {'j','m','b'}: raise ValueError('invalid owner')
        return v

    @field_validator('payment_method')
    @classmethod
    def valid_payment(cls, v):
        if v not in {None,'cash','card'}: raise ValueError('invalid payment method')
        return v

@app.get('/health')
def health():
    with database() as db: db.execute('SELECT 1')
    return {'status':'ok'}

@app.post('/api/login')
def login(data: Login, request: Request, response: Response):
    origin(request)
    now = datetime.now(timezone.utc)
    with database() as db:
        db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (data.username,))
        attempt = db.execute('SELECT * FROM login_attempts WHERE username=%s', (data.username,)).fetchone()
        if attempt and attempt['retry_at'] > now: raise HTTPException(429, 'Try later')
        user = db.execute('SELECT * FROM users WHERE username=%s', (data.username,)).fetchone()
        try: valid = ph.verify(user['password_hash'] if user else DUMMY, data.password)
        except VerificationError: valid = False
        if not valid or not user or user['disabled']:
            failures = (attempt['failures'] if attempt else 0)+1
            retry = now + timedelta(seconds=min(900, 2**min(failures,10)))
            db.execute('INSERT INTO login_attempts VALUES (%s,%s,%s) ON CONFLICT(username) DO UPDATE SET failures=EXCLUDED.failures,retry_at=EXCLUDED.retry_at', (data.username, failures, retry))
            db.commit()
            raise HTTPException(401, 'Invalid credentials')
        db.execute('DELETE FROM login_attempts WHERE username=%s', (data.username,))
        old = request.cookies.get('__Host-budget')
        if old: db.execute('DELETE FROM sessions WHERE token_hash=%s', (digest(old),))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO sessions VALUES (%s,%s,%s,%s)', (digest(token),user['id'],csrf,now+timedelta(hours=12)))
    response.set_cookie('__Host-budget',token,secure=True,httponly=True,samesite='strict',path='/',max_age=43200)
    response.headers['Cache-Control']='no-store'
    return {'csrf':csrf}

@app.get('/api/me')
def me(response: Response, user=Depends(authenticated)):
    response.headers['Cache-Control']='no-store'
    return {'username':user['username'], 'csrf':user['csrf']}

@app.post('/api/logout')
def logout(response: Response, user=Depends(authenticated)):
    with database() as db: db.execute('DELETE FROM sessions WHERE token_hash=%s', (user['token_hash'],))
    response.delete_cookie('__Host-budget',secure=True,httponly=True,samesite='strict',path='/')
    return {'ok':True}

@app.get('/api/summary')
def summary(month: str, response: Response, user=Depends(authenticated)):
    if len(month)!=7 or month[4]!='-': raise HTTPException(422,'Use YYYY-MM')
    try: start=date.fromisoformat(month+'-01')
    except ValueError: raise HTTPException(422,'Use YYYY-MM')
    end=date(start.year+1,1,1) if start.month==12 else date(start.year,start.month+1,1)
    response.headers['Cache-Control']='no-store'
    with database() as db:
        rows=db.execute('SELECT direction,owner,category,sum(amount)::bigint amount FROM transactions WHERE occurred_on >= %s AND occurred_on < %s GROUP BY direction,owner,category',(start,end)).fetchall()
        fixed=db.execute('SELECT items FROM fixed_snapshots WHERE month=%s',(month,)).fetchone()
    income=sum(r['amount'] for r in rows if r['direction']=='income' and r['category'] not in PASSTHROUGH)
    expenses=sum(r['amount'] for r in rows if r['direction']=='expense' and r['category'] not in PASSTHROUGH)
    pass_net=sum(r['amount']*(1 if r['direction']=='income' else -1) for r in rows if r['category'] in PASSTHROUGH)
    by_category={}
    for r in rows:
        if r['direction']=='expense' and r['category'] not in PASSTHROUGH:
            by_category[r['category']]=by_category.get(r['category'],0)+r['amount']
    fixed_total=sum(int(x.get('amt',x.get('amount',0))) for x in (fixed['items'] if fixed else []) if x.get('payMethod',x.get('payment_method','cash'))!='card')
    return {'month':month,'income':income,'expenses':expenses,'fixed_cash':fixed_total,'net_before_carry':income-expenses-fixed_total+pass_net,'pass_net':pass_net,'categories':sorted([{'name':k,'amount':v} for k,v in by_category.items()],key=lambda x:-x['amount'])}

@app.get('/api/transactions')
def transactions(month: str | None = None, q: str = '', response: Response = None, user=Depends(authenticated), limit: int=500, offset: int=0):
    if not 1<=limit<=500 or offset<0: raise HTTPException(422,'Invalid pagination')
    if month and (len(month)!=7 or month[4]!='-'): raise HTTPException(422,'Use YYYY-MM')
    response.headers['Cache-Control']='no-store'
    clauses=[]; params=[]
    if month:
        clauses.append("occurred_on >= %s::date AND occurred_on < (%s::date + interval '1 month')"); params += [month+'-01',month+'-01']
    if q:
        clauses.append('(name ILIKE %s OR category ILIKE %s OR memo ILIKE %s)'); params += [f'%{q}%']*3
    where=(' WHERE '+' AND '.join(clauses)) if clauses else ''
    with database() as db:
        return db.execute('SELECT id,occurred_on,name,category,amount,direction,owner,payment_method,memo FROM transactions'+where+' ORDER BY occurred_on DESC,id LIMIT %s OFFSET %s',params+[limit,offset]).fetchall()

@app.post('/api/transactions',status_code=201)
def add_transaction(data: Transaction, user=Depends(authenticated)):
    row=data.model_dump()
    with database() as db:
        return db.execute('INSERT INTO transactions(id,source_month,occurred_on,name,category,amount,direction,owner,payment_method,memo,legacy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,occurred_on,name,category,amount,direction,owner,payment_method,memo',(str(uuid.uuid4()),row['occurred_on'].strftime('%Y-%m'),row['occurred_on'],row['name'].strip(),row['category'].strip(),row['amount'],row['direction'],row['owner'],row['payment_method'],row['memo'].strip(),Jsonb({}))).fetchone()

@app.put('/api/transactions/{tx_id}')
def edit_transaction(tx_id: str, data: Transaction, user=Depends(authenticated)):
    row=data.model_dump()
    with database() as db:
        saved=db.execute('UPDATE transactions SET source_month=%s,occurred_on=%s,name=%s,category=%s,amount=%s,direction=%s,owner=%s,payment_method=%s,memo=%s,updated_at=now() WHERE id=%s RETURNING id,occurred_on,name,category,amount,direction,owner,payment_method,memo',(row['occurred_on'].strftime('%Y-%m'),row['occurred_on'],row['name'].strip(),row['category'].strip(),row['amount'],row['direction'],row['owner'],row['payment_method'],row['memo'].strip(),tx_id)).fetchone()
    if not saved: raise HTTPException(404,'Transaction not found')
    return saved

@app.delete('/api/transactions/{tx_id}',status_code=204)
def delete_transaction(tx_id: str, user=Depends(authenticated)):
    with database() as db: deleted=db.execute('DELETE FROM transactions WHERE id=%s RETURNING id',(tx_id,)).fetchone()
    if not deleted: raise HTTPException(404,'Transaction not found')

@app.post('/api/import/csv')
def import_csv(items: list[Transaction], response: Response, user=Depends(authenticated)):
    if not items or len(items)>5000: raise HTTPException(422,'CSV must contain 1–5000 transactions')
    canonical=json.dumps([x.model_dump(mode='json') for x in items],ensure_ascii=False,sort_keys=True,separators=(',',':'))
    batch=hashlib.sha256(canonical.encode()).hexdigest()
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('csv:' || %s))",(batch,))
        prior=db.execute('SELECT report FROM import_batches WHERE id=%s',(batch,)).fetchone()
        if prior:
            response.status_code=200
            return {'count':prior['report']['count'],'duplicate':True}
        db.execute('INSERT INTO import_batches(id,report) VALUES (%s,%s)',(batch,Jsonb({'count':len(items),'source':'csv'})))
        for i,item in enumerate(items):
            r=item.model_dump()
            db.execute('INSERT INTO transactions(id,source_month,occurred_on,name,category,amount,direction,owner,payment_method,memo,legacy,batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(f'csv/{batch}/{i}',r['occurred_on'].strftime('%Y-%m'),r['occurred_on'],r['name'].strip(),r['category'].strip(),r['amount'],r['direction'],r['owner'],r['payment_method'],r['memo'].strip(),Jsonb(r),batch))
    return {'count':len(items),'duplicate':False}

@app.get('/')
def index(): return FileResponse('/app/static/index.html')

app.mount('/static', StaticFiles(directory='/app/static'), name='static')
