import csv
import calendar
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
from pydantic import BaseModel, Field, field_validator, model_validator

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
    payment_account_id: str | None = None
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

class FixedSnapshot(BaseModel):
    items: list[dict] = Field(max_length=200)

class SalarySettings(BaseModel):
    enabled: bool = False
    salary_j: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    salary_m: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    day_j: int = Field(default=10, ge=1, le=31)
    day_m: int = Field(default=17, ge=1, le=31)

class CarryAnchor(BaseModel):
    amount: int = Field(ge=-9_000_000_000_000_000, le=9_000_000_000_000_000)

class CardBenefit(BaseModel):
    id: str = Field(default_factory=lambda:str(uuid.uuid4()))
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default='*', max_length=80)
    reward_type: str = 'rate'
    rate_bps: int = Field(default=0, ge=0, le=10000)
    fixed_amount: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    cap_amount: int | None = Field(default=None, ge=0, le=9_000_000_000_000_000)
    minimum_spend: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    period: str = 'monthly'
    notes: str = Field(default='', max_length=500)

    @field_validator('reward_type')
    @classmethod
    def valid_reward_type(cls,v):
        if v not in {'rate','fixed'}: raise ValueError('invalid reward type')
        return v

    @field_validator('period')
    @classmethod
    def valid_benefit_period(cls,v):
        if v not in {'monthly','yearly'}: raise ValueError('invalid benefit period')
        return v

class FinancialAccount(BaseModel):
    kind: str
    name: str = Field(min_length=1,max_length=120)
    institution: str = Field(default='',max_length=120)
    owner: str = 'b'
    last_four: str = Field(default='',pattern=r'^(|\d{4})$')
    balance: int | None = Field(default=None,ge=0,le=9_000_000_000_000_000)
    credit_limit: int | None = Field(default=None,ge=0,le=9_000_000_000_000_000)
    billing_day: int | None = Field(default=None,ge=1,le=31)
    notes: str = Field(default='',max_length=1000)
    benefits: list[CardBenefit] = Field(default_factory=list,max_length=30)
    loan_principal: int | None = Field(default=None,gt=0,le=9_000_000_000_000_000)
    execution_date: date | None = None
    term_months: int | None = Field(default=None,ge=1,le=600)
    annual_rate_bps: int | None = Field(default=None,ge=0,le=100000)
    repayment_method: str | None = None
    payment_day: int | None = Field(default=None,ge=1,le=31)
    grace_months: int = Field(default=0,ge=0,le=600)
    principal_defer_start: date | None = None
    principal_defer_months: int = Field(default=0,ge=0,le=600)

    @field_validator('kind')
    @classmethod
    def valid_account_kind(cls,v):
        if v not in {'bank','loan','investment','card'}: raise ValueError('invalid account kind')
        return v

    @field_validator('owner')
    @classmethod
    def valid_account_owner(cls,v):
        if v not in {'j','m','b'}: raise ValueError('invalid owner')
        return v

    @model_validator(mode='after')
    def card_only_fields(self):
        if self.kind!='card' and (self.benefits or self.credit_limit is not None or self.billing_day is not None):
            raise ValueError('card details can only be set on a card account')
        loan_fields=(self.loan_principal,self.execution_date,self.term_months,self.annual_rate_bps,self.repayment_method,self.payment_day)
        if self.kind=='loan':
            if any(v is None for v in loan_fields): raise ValueError('loan terms are required')
            if self.repayment_method not in {'equal_payment','equal_principal','bullet','grace_equal_payment','principal_defer'}: raise ValueError('invalid repayment method')
            if self.repayment_method=='grace_equal_payment' and self.grace_months>=self.term_months: raise ValueError('grace period must be shorter than the loan term')
            if self.repayment_method=='principal_defer' and (self.principal_defer_start is None or self.principal_defer_months<1): raise ValueError('principal defer start and duration are required')
            if self.repayment_method=='principal_defer':
                if self.principal_defer_start<self.execution_date: raise ValueError('principal defer cannot start before the loan execution date')
                month_offset=(self.principal_defer_start.year-self.execution_date.year)*12+self.principal_defer_start.month-self.execution_date.month
                if month_offset+self.principal_defer_months>self.term_months: raise ValueError('principal defer extends beyond loan maturity')
            if self.balance is None: self.balance=self.loan_principal
        elif any(v is not None for v in loan_fields) or self.grace_months or self.principal_defer_start is not None or self.principal_defer_months:
            raise ValueError('loan terms can only be set on a loan account')
        return self

class AutomaticPayment(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    amount: int = Field(default=0,ge=0,le=9_000_000_000_000_000)
    cadence: str
    debit_day: int | None = Field(default=None,ge=1,le=31)
    category: str = Field(default='기타',max_length=80)
    source_account_id: str | None = None
    related_card_id: str | None = None
    notes: str = Field(default='',max_length=500)
    kind: str = 'autopay'

    @field_validator('cadence')
    @classmethod
    def valid_cadence(cls,v):
        if v not in {'monthly','quarterly','yearly','weekly'}: raise ValueError('invalid cadence')
        return v

    @field_validator('kind')
    @classmethod
    def valid_payment_kind(cls,v):
        if v not in {'autopay','subscription'}: raise ValueError('invalid payment kind')
        return v

class CardInstallmentPurchase(BaseModel):
    card_account_id: str
    name: str = Field(min_length=1,max_length=120)
    purchase_date: date
    total_amount: int = Field(gt=0,le=9_000_000_000_000_000)
    installment_count: int = Field(ge=2,le=120)
    first_billing_month: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    annual_rate_bps: int = Field(default=0,ge=0,le=100000)
    notes: str = Field(default='',max_length=500)

class SimulationEvent(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    amount: int = Field(gt=0, le=9_000_000_000_000_000)
    direction: str
    month: str = Field(pattern=r'^\d{4}-\d{2}$')
    owner: str = 'b'
    repeat: bool = False
    months: list[int] = Field(default_factory=list, max_length=12)
    start_year: int | None = Field(default=None, ge=2000, le=2200)
    end_year: int | None = Field(default=None, ge=2000, le=2200)

    @field_validator('direction')
    @classmethod
    def valid_event_direction(cls, v):
        if v not in {'income','expense'}: raise ValueError('invalid direction')
        return v

    @model_validator(mode='after')
    def valid_recurrence(self):
        if self.repeat:
            if not self.months or any(m < 1 or m > 12 for m in self.months) or len(set(self.months)) != len(self.months):
                raise ValueError('recurring events need unique months from 1 to 12')
            if self.start_year is None or self.end_year is None or self.end_year < self.start_year:
                raise ValueError('recurring events need a valid year range')
        return self

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
        rows=db.execute("SELECT direction,owner,category,payment_method,sum(amount)::bigint amount FROM transactions WHERE occurred_on >= %s AND occurred_on < %s GROUP BY direction,owner,category,payment_method",(start,end)).fetchall()
        fixed=db.execute('SELECT items FROM fixed_snapshots WHERE month=%s',(month,)).fetchone()
        anchor=db.execute('SELECT month,amount FROM carry_anchors WHERE month<=%s ORDER BY month DESC LIMIT 1',(month,)).fetchone()
        flow_start=anchor['month'] if anchor else f'{start.year}-01'
        fs_y,fs_m=map(int,flow_start.split('-')); history_start=f'{fs_y-1:04d}-12' if fs_m==1 else f'{fs_y:04d}-{fs_m-1:02d}'
        flow_rows=db.execute("SELECT to_char(occurred_on,'YYYY-MM') month,direction,category,payment_method,sum(amount)::bigint amount FROM transactions WHERE occurred_on >= %s::date AND occurred_on < %s GROUP BY 1,direction,category,payment_method",(history_start+'-01',end)).fetchall()
        flow_fixed=db.execute('SELECT month,items FROM fixed_snapshots WHERE month >= %s AND month <= %s',(history_start,month)).fetchall()
    income=sum(r['amount'] for r in rows if r['direction']=='income' and r['category'] not in PASSTHROUGH)
    expenses=sum(r['amount'] for r in rows if r['direction']=='expense' and r['category'] not in PASSTHROUGH)
    pass_net=sum(r['amount']*(1 if r['direction']=='income' else -1) for r in rows if r['category'] in PASSTHROUGH)
    by_category={}
    for r in rows:
        if r['direction']=='expense' and r['category'] not in PASSTHROUGH:
            by_category[r['category']]=by_category.get(r['category'],0)+r['amount']
    fixed_items=(fixed['items'] or []) if fixed else []
    fixed_total=sum(int(x.get('amt',x.get('amount',0))) for x in fixed_items if x.get('payMethod',x.get('payment_method','cash'))!='card')
    flow_by_month={}
    for row in flow_rows: flow_by_month.setdefault(row['month'],[]).append(row)
    fixed_by_month={r['month']:(r['items'] or []) for r in flow_fixed}
    def card_usage(ym):
        variable=sum(r['amount'] for r in flow_by_month.get(ym,[]) if r['direction']=='expense' and r['category'] not in PASSTHROUGH and r['payment_method']=='card')
        planned=sum(int(x.get('amt',x.get('amount',0))) for x in fixed_by_month.get(ym,[]) if x.get('payMethod',x.get('payment_method','cash'))=='card')
        return variable+planned
    def month_net(ym):
        txs=flow_by_month.get(ym,[])
        real_income=sum(r['amount'] for r in txs if r['direction']=='income' and r['category'] not in PASSTHROUGH)
        cash_expense=sum(r['amount'] for r in txs if r['direction']=='expense' and r['category'] not in PASSTHROUGH and r['payment_method']!='card')
        settlement=sum(r['amount'] for r in txs if r['direction']=='income' and r['category']=='정산')
        sidegig=sum(r['amount']*(1 if r['direction']=='income' else -1) for r in txs if r['category']=='부업')
        card_pay=sum(r['amount'] for r in txs if r['direction']=='expense' and r['category']=='카드대금')
        cash_fixed=sum(int(x.get('amt',x.get('amount',0))) for x in fixed_by_month.get(ym,[]) if x.get('payMethod',x.get('payment_method','cash'))!='card')
        prev_y,prev_m=map(int,ym.split('-')); prev_ym=f'{prev_y-1:04d}-12' if prev_m==1 else f'{prev_y:04d}-{prev_m-1:02d}'
        card_excess=max(0,card_usage(prev_ym)-card_pay)
        return real_income+settlement+sidegig-cash_expense-cash_fixed-card_pay-card_excess
    net=month_net(month)
    carry=int(anchor['amount']) if anchor else None
    if anchor:
        cy,cm=map(int,anchor['month'].split('-')); ty,tm=start.year,start.month
        for _ in range(120):
            if (cy,cm)>=(ty,tm): break
            ym=f'{cy:04d}-{cm:02d}'; carry+=month_net(ym)
            cm+=1
            if cm>12: cy+=1; cm=1
    current_card=sum(r['amount'] for r in rows if r['direction']=='expense' and r['category'] not in PASSTHROUGH and r['payment_method']=='card')+sum(int(x.get('amt',x.get('amount',0))) for x in fixed_items if x.get('payMethod',x.get('payment_method','cash'))=='card')
    prior_y,prior_m=map(int,month.split('-')); prior_month=f'{prior_y-1:04d}-12' if prior_m==1 else f'{prior_y:04d}-{prior_m-1:02d}'
    card_pay=sum(r['amount'] for r in rows if r['direction']=='expense' and r['category']=='카드대금')
    return {'month':month,'income':income,'expenses':expenses,'fixed_cash':fixed_total,'net_before_carry':net,'pass_net':pass_net,'carry_in':carry,'closing_balance':carry+net if carry is not None else None,'card_usage':current_card,'card_pay':card_pay,'card_unpaid_estimate':max(0,card_usage(prior_month)-card_pay),'categories':sorted([{'name':k,'amount':v} for k,v in by_category.items()],key=lambda x:-x['amount'])}

def add_months(day: date, count: int) -> date:
    serial=day.year*12+day.month-1+count; year,month=divmod(serial,12); month+=1
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))

def loan_schedule(account):
    principal=int(account['loan_principal']); term=int(account['term_months']); rate=int(account['annual_rate_bps'])/120000
    method=account['repayment_method']; grace=int(account.get('grace_months') or 0) if method=='grace_equal_payment' else 0
    amort_months=term-grace; balance=principal; monthly_payment=principal/amort_months if rate==0 else principal*rate/(1-(1+rate)**(-amort_months))
    defer_start=account.get('principal_defer_start') if method=='principal_defer' else None
    defer_months=int(account.get('principal_defer_months') or 0) if method=='principal_defer' else 0
    defer_end=add_months(defer_start,defer_months) if defer_start and defer_months else None
    schedule=[]; total_interest=0
    for index in range(1,term+1):
        due=add_months(account['execution_date'],index); due=date(due.year,due.month,min(int(account['payment_day']),calendar.monthrange(due.year,due.month)[1])); interest=int(balance*rate+0.5)
        deferred=bool(defer_start and due>=defer_start and due<defer_end)
        if deferred: principal_due=0
        elif method=='principal_defer' and defer_end and due>=defer_end:
            remaining=max(1,term-index+1); resumed_payment=balance/remaining if rate==0 else balance*rate/(1-(1+rate)**(-remaining)); principal_due=min(balance,max(0,int(resumed_payment+0.5)-interest))
        elif method=='bullet': principal_due=balance if index==term else 0
        elif method=='equal_principal' or (method=='grace_equal_payment' and index<=grace): principal_due=0 if index<=grace else min(balance,principal//amort_months+(1 if index-grace==amort_months else 0))
        else: principal_due=min(balance,max(0,int(monthly_payment+0.5)-interest))
        if index==term: principal_due=balance
        payment=principal_due+interest; balance=max(0,balance-principal_due); total_interest+=interest
        schedule.append({'number':index,'date':due.isoformat(),'month':due.strftime('%Y-%m'),'principal':principal_due,'interest':interest,'payment':payment,'remaining_balance':balance,'principal_deferred':deferred})
    return {'maturity_date':add_months(account['execution_date'],term).isoformat(),'total_interest':total_interest,'schedule':schedule}

def installment_schedule(purchase):
    total=int(purchase['total_amount']); count=int(purchase['installment_count']); rate=int(purchase['annual_rate_bps'])/120000
    payment=total/count if rate==0 else total*rate/(1-(1+rate)**(-count)); balance=total; first_year,first_month=map(int,purchase['first_billing_month'].split('-')); rows=[]
    for i in range(1,count+1):
        serial=first_year*12+first_month-1+i-1; year,month=divmod(serial,12); month+=1
        interest=int(balance*rate+0.5); principal=min(balance,max(0,int(payment+0.5)-interest))
        if i==count: principal=balance
        balance=max(0,balance-principal)
        rows.append({'month':f'{year:04d}-{month:02d}','number':i,'count':count,'amount':principal+interest,'principal':principal,'interest':interest,'remaining_balance':balance})
    return rows

@app.get('/api/finance')
def finance_hub(month: str, user=Depends(authenticated)):
    valid_month(month)
    year_start=month[:4]+'-01-01'; month_start=month+'-01'
    with database() as db:
        accounts=db.execute('SELECT * FROM financial_accounts ORDER BY CASE kind WHEN \'bank\' THEN 1 WHEN \'loan\' THEN 2 WHEN \'investment\' THEN 3 ELSE 4 END,name').fetchall()
        autopays=db.execute('SELECT * FROM automatic_payments ORDER BY active DESC,debit_day NULLS LAST,name').fetchall()
        installment_purchases=db.execute('SELECT p.*,a.name AS card_name FROM card_installments p JOIN financial_accounts a ON a.id=p.card_account_id ORDER BY first_billing_month,name').fetchall()
        usage=db.execute("SELECT payment_account_id,category,to_char(occurred_on,'YYYY-MM') month,sum(amount)::bigint amount FROM transactions WHERE payment_account_id IS NOT NULL AND payment_method='card' AND direction='expense' AND occurred_on >= %s::date AND occurred_on < (%s::date + interval '1 month') GROUP BY payment_account_id,category,3",(year_start,month_start)).fetchall()
    usage_map={}
    for row in usage: usage_map.setdefault(row['payment_account_id'],[]).append(row)
    rendered=[]
    for account in accounts:
        row=dict(account); benefits=[]
        if row['kind']=='loan' and row['loan_principal']:
            loan=loan_schedule(row); row.update({'maturity_date':loan['maturity_date'],'total_interest':loan['total_interest'],'loan_schedule':[x for x in loan['schedule'] if x['month']>=month]})
        if row['kind']=='card':
            txs=usage_map.get(row['id'],[]); row['month_spend']=sum(x['amount'] for x in txs if x['month']==month); row['year_spend']=sum(x['amount'] for x in txs)
            for raw in row['benefits'] or []:
                benefit=dict(raw); selected=[x for x in txs if benefit.get('category') in {'*',x['category']} and (benefit.get('period')=='yearly' or x['month']==month)]
                eligible=sum(x['amount'] for x in selected); total_spend=row['year_spend'] if benefit.get('period')=='yearly' else row['month_spend']
                estimate=(int(benefit.get('fixed_amount',0)) if benefit.get('reward_type')=='fixed' else eligible*int(benefit.get('rate_bps',0))//10000) if total_spend>=int(benefit.get('minimum_spend',0)) else 0
                cap=benefit.get('cap_amount'); benefit.update({'eligible_spend':eligible,'estimated_value':min(estimate,int(cap)) if cap is not None else estimate}); benefits.append(benefit)
        row['benefits']=benefits; rendered.append(row)
    installments=[]
    for purchase in installment_purchases:
        for due in installment_schedule(purchase):
            if due['month']>=month: installments.append({'id':purchase['id'],'name':purchase['name'],'card_account_id':purchase['card_account_id'],'card_name':purchase['card_name'],'notes':purchase['notes'],**due})
    installments.sort(key=lambda x:(x['month'],x['card_name'],x['name'],x['number']))
    return {'accounts':rendered,'automatic_payments':[p for p in autopays if p['kind']=='autopay'],'subscriptions':[p for p in autopays if p['kind']=='subscription'],'card_installment_schedule':installments,'month':month}

@app.post('/api/finance/accounts',status_code=201)
def add_financial_account(data: FinancialAccount,user=Depends(authenticated)):
    account_id=str(uuid.uuid4()); r=data.model_dump()
    with database() as db:
        db.execute('INSERT INTO financial_accounts(id,kind,name,institution,owner,last_four,balance,credit_limit,billing_day,notes,benefits,loan_principal,execution_date,term_months,annual_rate_bps,repayment_method,payment_day,grace_months,principal_defer_start,principal_defer_months) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(account_id,r['kind'],r['name'].strip(),r['institution'].strip(),r['owner'],r['last_four'],r['balance'],r['credit_limit'],r['billing_day'],r['notes'].strip(),Jsonb([b.model_dump() for b in data.benefits]),r['loan_principal'],r['execution_date'],r['term_months'],r['annual_rate_bps'],r['repayment_method'],r['payment_day'],r['grace_months'],r['principal_defer_start'],r['principal_defer_months']))
    return {'id':account_id,**r}

@app.put('/api/finance/accounts/{account_id}')
def update_financial_account(account_id: str,data: FinancialAccount,user=Depends(authenticated)):
    r=data.model_dump()
    with database() as db:
        existing=db.execute('SELECT benefits FROM financial_accounts WHERE id=%s',(account_id,)).fetchone()
        if not existing: raise HTTPException(404,'Account not found')
        benefits=[b.model_dump() for b in data.benefits] or (existing['benefits'] or [] if data.kind=='card' else [])
        saved=db.execute('UPDATE financial_accounts SET kind=%s,name=%s,institution=%s,owner=%s,last_four=%s,balance=%s,credit_limit=%s,billing_day=%s,notes=%s,benefits=%s,loan_principal=%s,execution_date=%s,term_months=%s,annual_rate_bps=%s,repayment_method=%s,payment_day=%s,grace_months=%s,principal_defer_start=%s,principal_defer_months=%s WHERE id=%s RETURNING id',(r['kind'],r['name'].strip(),r['institution'].strip(),r['owner'],r['last_four'],r['balance'],r['credit_limit'],r['billing_day'],r['notes'].strip(),Jsonb(benefits),r['loan_principal'],r['execution_date'],r['term_months'],r['annual_rate_bps'],r['repayment_method'],r['payment_day'],r['grace_months'],r['principal_defer_start'],r['principal_defer_months'],account_id)).fetchone()
    if not saved: raise HTTPException(404,'Account not found')
    return {'id':account_id,**r}

@app.post('/api/finance/accounts/{account_id}/benefits',status_code=201)
def add_card_benefit(account_id: str,data: CardBenefit,user=Depends(authenticated)):
    with database() as db:
        row=db.execute("SELECT benefits FROM financial_accounts WHERE id=%s AND kind='card' FOR UPDATE",(account_id,)).fetchone()
        if not row: raise HTTPException(404,'Card not found')
        benefits=row['benefits'] or []; benefits.append(data.model_dump())
        db.execute('UPDATE financial_accounts SET benefits=%s WHERE id=%s',(Jsonb(benefits),account_id))
    return data.model_dump()

@app.delete('/api/finance/accounts/{account_id}/benefits/{benefit_id}',status_code=204)
def delete_card_benefit(account_id: str,benefit_id: str,user=Depends(authenticated)):
    with database() as db:
        row=db.execute("SELECT benefits FROM financial_accounts WHERE id=%s AND kind='card' FOR UPDATE",(account_id,)).fetchone()
        if not row: raise HTTPException(404,'Card not found')
        benefits=row['benefits'] or []; updated=[b for b in benefits if b.get('id')!=benefit_id]
        if len(updated)==len(benefits): raise HTTPException(404,'Benefit not found')
        db.execute('UPDATE financial_accounts SET benefits=%s WHERE id=%s',(Jsonb(updated),account_id))

@app.post('/api/finance/installments',status_code=201)
def add_card_installment(data: CardInstallmentPurchase,user=Depends(authenticated)):
    purchase_id=str(uuid.uuid4()); r=data.model_dump()
    with database() as db:
        if not db.execute("SELECT id FROM financial_accounts WHERE id=%s AND kind='card'",(r['card_account_id'],)).fetchone(): raise HTTPException(422,'신용카드를 선택해 주세요.')
        db.execute('INSERT INTO card_installments(id,card_account_id,name,purchase_date,total_amount,installment_count,first_billing_month,annual_rate_bps,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',(purchase_id,r['card_account_id'],r['name'].strip(),r['purchase_date'],r['total_amount'],r['installment_count'],r['first_billing_month'],r['annual_rate_bps'],r['notes'].strip()))
    return {'id':purchase_id,**r}

@app.delete('/api/finance/installments/{purchase_id}',status_code=204)
def delete_card_installment(purchase_id: str,user=Depends(authenticated)):
    with database() as db: deleted=db.execute('DELETE FROM card_installments WHERE id=%s RETURNING id',(purchase_id,)).fetchone()
    if not deleted: raise HTTPException(404,'Installment purchase not found')

@app.delete('/api/finance/accounts/{account_id}',status_code=204)
def delete_financial_account(account_id: str,user=Depends(authenticated)):
    with database() as db: deleted=db.execute('DELETE FROM financial_accounts WHERE id=%s RETURNING id',(account_id,)).fetchone()
    if not deleted: raise HTTPException(404,'Account not found')

@app.post('/api/finance/automatic-payments',status_code=201)
def add_automatic_payment(data: AutomaticPayment,user=Depends(authenticated)):
    payment_id=str(uuid.uuid4()); r=data.model_dump()
    with database() as db:
        if r['source_account_id']:
            source=db.execute('SELECT kind FROM financial_accounts WHERE id=%s',(r['source_account_id'],)).fetchone()
            if not source or source['kind'] not in {'bank','card'}: raise HTTPException(422,'출금 계좌/카드를 찾을 수 없습니다.')
        if r['related_card_id'] and not db.execute("SELECT id FROM financial_accounts WHERE id=%s AND kind='card'",(r['related_card_id'],)).fetchone(): raise HTTPException(422,'연결 카드를 찾을 수 없습니다.')
        db.execute('INSERT INTO automatic_payments(id,name,amount,cadence,debit_day,category,source_account_id,related_card_id,notes,kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(payment_id,r['name'].strip(),r['amount'],r['cadence'],r['debit_day'],r['category'].strip(),r['source_account_id'],r['related_card_id'],r['notes'].strip(),r['kind']))
    return {'id':payment_id,**r,'active':True}

@app.delete('/api/finance/automatic-payments/{payment_id}',status_code=204)
def delete_automatic_payment(payment_id: str,user=Depends(authenticated)):
    with database() as db: deleted=db.execute('DELETE FROM automatic_payments WHERE id=%s RETURNING id',(payment_id,)).fetchone()
    if not deleted: raise HTTPException(404,'Automatic payment not found')

def validate_payment_account(db,method,account_id):
    if account_id is None: return
    row=db.execute('SELECT kind FROM financial_accounts WHERE id=%s',(account_id,)).fetchone()
    expected='card' if method=='card' else 'bank' if method=='cash' else None
    if not row or row['kind']!=expected: raise HTTPException(422,'선택한 결제 계좌와 결제수단이 맞지 않습니다.')

def valid_month(month):
    try:
        if len(month)!=7 or month[4]!='-': raise ValueError()
        date.fromisoformat(month+'-01')
    except ValueError: raise HTTPException(422,'Use YYYY-MM')

@app.get('/api/fixed')
def get_fixed(month: str, user=Depends(authenticated)):
    valid_month(month)
    with database() as db:
        row=db.execute('SELECT items FROM fixed_snapshots WHERE month=%s',(month,)).fetchone()
    return {'month':month,'items':row['items'] if row else []}

@app.put('/api/fixed/{month}')
def put_fixed(month: str, data: FixedSnapshot, user=Depends(authenticated)):
    valid_month(month)
    clean=[]
    for item in data.items:
        name=str(item.get('name','')).strip(); amount=item.get('amt',item.get('amount',0)); pay=item.get('payMethod',item.get('payment_method','cash'))
        if not name or len(name)>160 or type(amount) is not int or amount<0 or amount>9_000_000_000_000_000 or pay not in {'cash','card'}: raise HTTPException(422,'Invalid fixed expense')
        clean.append({**item,'name':name,'amt':amount,'payMethod':pay})
    with database() as db:
        db.execute('INSERT INTO fixed_snapshots(month,items) VALUES (%s,%s) ON CONFLICT(month) DO UPDATE SET items=EXCLUDED.items',(month,Jsonb(clean)))
    return {'month':month,'items':clean}

@app.get('/api/settings/salary')
def get_salary(user=Depends(authenticated)):
    with database() as db: row=db.execute("SELECT value FROM settings WHERE key='salary' ").fetchone()
    return row['value'] if row else {'enabled':False,'salary_j':0,'salary_m':0,'day_j':10,'day_m':17}

@app.put('/api/settings/salary')
def put_salary(data: SalarySettings, user=Depends(authenticated)):
    with database() as db: db.execute("INSERT INTO settings(key,value) VALUES ('salary',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",(Jsonb(data.model_dump()),))
    return data.model_dump()

@app.get('/api/carry/{month}')
def get_carry(month: str, user=Depends(authenticated)):
    valid_month(month)
    with database() as db: row=db.execute('SELECT amount FROM carry_anchors WHERE month=%s',(month,)).fetchone()
    return {'month':month,'amount':row['amount'] if row else None}

@app.put('/api/carry/{month}')
def put_carry(month: str, data: CarryAnchor, user=Depends(authenticated)):
    valid_month(month)
    with database() as db: db.execute('INSERT INTO carry_anchors(month,amount) VALUES (%s,%s) ON CONFLICT(month) DO UPDATE SET amount=EXCLUDED.amount',(month,data.amount))
    return {'month':month,'amount':data.amount}

@app.get('/api/simulation')
def get_simulation(user=Depends(authenticated)):
    with database() as db: rows=db.execute('SELECT id,value FROM simulation_events ORDER BY id').fetchall()
    return rows

@app.post('/api/simulation',status_code=201)
def add_simulation(data: SimulationEvent, user=Depends(authenticated)):
    valid_month(data.month)
    event={'name':data.name.strip(),'amount':data.amount,'direction':data.direction,'month':data.month,'owner':data.owner if data.owner in {'j','m','b'} else 'b', 'repeat':data.repeat}
    if data.repeat: event.update({'months':sorted(data.months),'start_year':data.start_year,'end_year':data.end_year})
    event_id=str(uuid.uuid4())
    with database() as db: db.execute('INSERT INTO simulation_events(id,value) VALUES (%s,%s)',(event_id,Jsonb(event)))
    return {'id':event_id,'value':event}

@app.delete('/api/simulation/{event_id}',status_code=204)
def delete_simulation(event_id: str,user=Depends(authenticated)):
    with database() as db: row=db.execute('DELETE FROM simulation_events WHERE id=%s RETURNING id',(event_id,)).fetchone()
    if not row: raise HTTPException(404,'Simulation event not found')

@app.get('/api/simulation/forecast')
def simulation_forecast(start: str, months: int=6, user=Depends(authenticated)):
    valid_month(start)
    if not 1<=months<=24: raise HTTPException(422,'months must be 1–24')
    sy,sm=map(int,start.split('-'))
    month_list=[f'{(sy*12+sm-1+i)//12:04d}-{(sy*12+sm-1+i)%12+1:02d}' for i in range(months)]
    with database() as db:
        events=db.execute('SELECT id,value FROM simulation_events').fetchall()
        fixed=db.execute('SELECT month,items FROM fixed_snapshots WHERE month=ANY(%s)',(month_list,)).fetchall()
        salary_row=db.execute("SELECT value FROM settings WHERE key='salary'").fetchone()
        tx=db.execute("SELECT to_char(occurred_on,'YYYY-MM') month,direction,sum(amount)::bigint amount,sum(CASE WHEN payment_method IS DISTINCT FROM 'card' THEN amount ELSE 0 END)::bigint cash_amount,category FROM transactions WHERE occurred_on >= %s::date AND occurred_on < (%s::date + (%s || ' months')::interval) GROUP BY 1,2,4,5",(start+'-01',start+'-01',months)).fetchall()
    salary=salary_row['value'] if salary_row else {'enabled':False,'salary_j':0,'salary_m':0}
    actual={m:{'income':0,'expense':0,'cash_expense':0} for m in month_list}
    for x in tx:
        if x['category'] not in PASSTHROUGH:
            actual[x['month']][x['direction']]+=x['amount']
            if x['direction']=='expense': actual[x['month']]['cash_expense']+=x['cash_amount']
    fixed_map={x['month']:(x['items'] or []) for x in fixed}; result=[]
    for m in month_list:
        yy,mm=map(int,m.split('-'))
        event_net=0
        for row in events:
            event=row['value']; occurs=(event.get('month')==m)
            if event.get('repeat'):
                occurs=(int(event.get('start_year',yy))<=yy<=int(event.get('end_year',yy)) and mm in event.get('months',[]))
            if occurs: event_net+=(1 if event.get('direction')=='income' else -1)*int(event.get('amount',0))
        fixed_total=sum(int(x.get('amt',x.get('amount',0))) for x in fixed_map.get(m,[]) if x.get('payMethod',x.get('payment_method','cash'))=='cash')
        expected_salary=(int(salary.get('salary_j',0))+int(salary.get('salary_m',0))) if salary.get('enabled') else 0
        result.append({'month':m,'recorded_income':actual[m]['income'],'expected_salary':expected_salary,'recorded_expense':actual[m]['expense'],'fixed_cash':fixed_total,'planned_net':event_net+expected_salary+actual[m]['income']-actual[m]['cash_expense']-fixed_total})
    return result

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
        return db.execute('SELECT id,occurred_on,name,category,amount,direction,owner,payment_method,payment_account_id,memo FROM transactions'+where+' ORDER BY occurred_on DESC,id LIMIT %s OFFSET %s',params+[limit,offset]).fetchall()

@app.post('/api/transactions',status_code=201)
def add_transaction(data: Transaction, user=Depends(authenticated)):
    row=data.model_dump()
    with database() as db:
        validate_payment_account(db,row['payment_method'],row['payment_account_id'])
        return db.execute('INSERT INTO transactions(id,source_month,occurred_on,name,category,amount,direction,owner,payment_method,payment_account_id,memo,legacy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,occurred_on,name,category,amount,direction,owner,payment_method,payment_account_id,memo',(str(uuid.uuid4()),row['occurred_on'].strftime('%Y-%m'),row['occurred_on'],row['name'].strip(),row['category'].strip(),row['amount'],row['direction'],row['owner'],row['payment_method'],row['payment_account_id'],row['memo'].strip(),Jsonb({}))).fetchone()

@app.put('/api/transactions/{tx_id}')
def edit_transaction(tx_id: str, data: Transaction, user=Depends(authenticated)):
    row=data.model_dump()
    with database() as db:
        validate_payment_account(db,row['payment_method'],row['payment_account_id'])
        saved=db.execute('UPDATE transactions SET source_month=%s,occurred_on=%s,name=%s,category=%s,amount=%s,direction=%s,owner=%s,payment_method=%s,payment_account_id=%s,memo=%s,updated_at=now() WHERE id=%s RETURNING id,occurred_on,name,category,amount,direction,owner,payment_method,payment_account_id,memo',(row['occurred_on'].strftime('%Y-%m'),row['occurred_on'],row['name'].strip(),row['category'].strip(),row['amount'],row['direction'],row['owner'],row['payment_method'],row['payment_account_id'],row['memo'].strip(),tx_id)).fetchone()
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
        for item in items: validate_payment_account(db,item.payment_method,item.payment_account_id)
        db.execute('INSERT INTO import_batches(id,report) VALUES (%s,%s)',(batch,Jsonb({'count':len(items),'source':'csv'})))
        for i,item in enumerate(items):
            r=item.model_dump()
            db.execute('INSERT INTO transactions(id,source_month,occurred_on,name,category,amount,direction,owner,payment_method,payment_account_id,memo,legacy,batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(f'csv/{batch}/{i}',r['occurred_on'].strftime('%Y-%m'),r['occurred_on'],r['name'].strip(),r['category'].strip(),r['amount'],r['direction'],r['owner'],r['payment_method'],r['payment_account_id'],r['memo'].strip(),Jsonb(r),batch))
    return {'count':len(items),'duplicate':False}

@app.get('/')
def index(): return FileResponse('/app/static/index.html')

app.mount('/static', StaticFiles(directory='/app/static'), name='static')

