import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from pydantic import BaseModel, Field

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ph = PasswordHasher()
DUMMY = ph.hash(secrets.token_urlsafe(32))

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

@app.get('/health')
def health():
    with database() as db:
        db.execute('SELECT 1')
    return {'status':'ok'}

@app.post('/api/login')
def login(data: Login, request: Request, response: Response):
    origin(request)
    now = datetime.now(timezone.utc)
    with database() as db:
        # Serialize concurrent failures for a username to prevent throttle bypass.
        db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (data.username,))
        attempt = db.execute('SELECT * FROM login_attempts WHERE username=%s', (data.username,)).fetchone()
        if attempt and attempt['retry_at'] > now:
            raise HTTPException(429, 'Try later')
        user = db.execute('SELECT * FROM users WHERE username=%s', (data.username,)).fetchone()
        valid = False
        try:
            valid = ph.verify(user['password_hash'] if user else DUMMY, data.password)
        except VerificationError:
            pass
        if not valid or not user or user['disabled']:
            failures = (attempt['failures'] if attempt else 0)+1
            retry = now + timedelta(seconds=min(900, 2**min(failures,10)))
            db.execute('INSERT INTO login_attempts VALUES (%s,%s,%s) ON CONFLICT(username) DO UPDATE SET failures=EXCLUDED.failures,retry_at=EXCLUDED.retry_at', (data.username, failures, retry))
            db.commit()
            raise HTTPException(401, 'Invalid credentials')
        db.execute('DELETE FROM login_attempts WHERE username=%s', (data.username,))
        old = request.cookies.get('__Host-budget')
        if old:
            db.execute('DELETE FROM sessions WHERE token_hash=%s', (digest(old),))
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
    with database() as db:
        db.execute('DELETE FROM sessions WHERE token_hash=%s', (user['token_hash'],))
    response.delete_cookie('__Host-budget',secure=True,httponly=True,samesite='strict',path='/')
    return {'ok':True}

@app.get('/api/transactions')
def transactions(response: Response, user=Depends(authenticated), limit: int=100, offset: int=0):
    if not 1<=limit<=500 or offset<0:
        raise HTTPException(422, 'Invalid pagination')
    response.headers['Cache-Control']='no-store'
    with database() as db:
        return db.execute('SELECT id,occurred_on,name,category,amount,direction,owner,payment_method,memo FROM transactions ORDER BY occurred_on DESC,id LIMIT %s OFFSET %s', (limit,offset)).fetchall()
