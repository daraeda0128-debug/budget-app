"""Offline export -> deterministic review plan -> atomic, idempotent import.
Never connects to Firebase or deletes source data. Run inside the API container.
"""
import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

MONTH = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
PRIVATE = {'pin_master', 'pin_guest'}

def entries(value):
    if isinstance(value, dict):
        return value.items()
    if isinstance(value, list):
        return ((str(i), v) for i,v in enumerate(value) if v is not None)
    raise ValueError('Expected object or array')

def money(value):
    if isinstance(value,bool) or not isinstance(value,(int,str)) or not re.fullmatch(r'-?\d+', str(value)):
        raise ValueError('Amount must be integer KRW')
    return int(value)

def normalize_fixed(items):
    if not isinstance(items, list):
        raise ValueError('Fixed expenses must be an array')
    normalized=[]
    for item in items:
        if not isinstance(item,dict):
            raise ValueError('Fixed expense must be an object')
        amount=money(item.get('amt',item.get('amount',0)))
        if amount<0:
            raise ValueError('Fixed expense cannot be negative')
        payment=item.get('payMethod',item.get('payment_method',item.get('pay','cash')))
        if payment not in {'cash','card'}:
            raise ValueError('Unknown fixed-expense payment method')
        normalized.append({**item,'amt':amount,'payMethod':payment})
    return normalized

def normalize_event(event, key):
    if not isinstance(event,dict):
        raise ValueError('Simulation event must be an object')
    repeat=bool(event.get('repeat',False))
    month=event.get('month',event.get('ym'))
    if not repeat and (not isinstance(month,str) or not MONTH.fullmatch(month)):
        raise ValueError('Simulation event has an invalid month')
    amount=money(event.get('amount',event.get('amt')))
    direction=event.get('direction',event.get('type'))
    if amount<=0 or direction not in {'income','expense'}:
        raise ValueError('Simulation event has an invalid amount or direction')
    normalized={'id':str(event.get('id',key)),'name':str(event.get('name','')).strip(),'amount':amount,'direction':direction,'month':month,'owner':event.get('owner',event.get('who','b')),'repeat':repeat}
    if not normalized['name'] or len(normalized['name'])>160 or normalized['owner'] not in {'j','m','b'}:
        raise ValueError('Simulation event has invalid name or owner')
    if repeat:
        months=event.get('months') or []
        start_year=event.get('start_year',event.get('startYear'))
        end_year=event.get('end_year',event.get('endYear'))
        if not months or any(type(m) is not int or m<1 or m>12 for m in months) or len(set(months))!=len(months):
            raise ValueError('Recurring simulation event has invalid months')
        if type(start_year) is not int or type(end_year) is not int or start_year<2000 or end_year<start_year or end_year>2200:
            raise ValueError('Recurring simulation event has invalid years')
        normalized.update({'months':sorted(months),'start_year':start_year,'end_year':end_year})
    return normalized

def plan(firebase, local):
    config = dict(firebase.get('config') or {})
    conflicts = []
    fixed = dict(firebase.get('fixed') or {})
    for key, value in local.items():
        if key in PRIVATE:
            continue
        if key.startswith('fixedItems_'):
            month = key.removeprefix('fixedItems_')
            parsed = json.loads(value) if isinstance(value,str) else value
            if month in fixed and fixed[month] != parsed:
                conflicts.append('fixed/'+month)
            else:
                fixed[month] = parsed
        elif key in {'salaryJ','salaryM','avgSpend','carryover','carryoverBaseYm','carryAnchors','simEvents','autoSalary','cardList'}:
            parsed = value
            if isinstance(value,str) and key in {'carryAnchors','simEvents','cardList','salaryJ','salaryM','avgSpend','carryover'}:
                parsed = json.loads(value)
            if key in config and config[key] != parsed:
                conflicts.append('config/'+key)
            else:
                config[key] = parsed
    config = {k:v for k,v in config.items() if k not in PRIVATE}
    rows, totals = [], {}
    for month, txs in (firebase.get('tx') or {}).items():
        if not MONTH.fullmatch(month):
            raise ValueError('Invalid source month')
        for key, t in entries(txs):
            date.fromisoformat(t['date'])
            if t['date'][:7] != month:
                raise ValueError('Transaction date differs from source month')
            if 'amt' in t and 'amount' in t and money(t['amt']) != money(t['amount']):
                raise ValueError('amt/amount conflict')
            amount = money(t.get('amt',t.get('amount')))
            if amount<=0 or t['type'] not in {'income','expense'} or t['who'] not in {'j','m','b'}:
                raise ValueError('Invalid transaction')
            payment = t.get('payMethod')
            if payment not in {None,'cash','card'}:
                raise ValueError('Unknown payment method')
            rows.append({'id':f'firebase/tx/{month}/{key}', 'month':month, 'date':t['date'], 'name':t['name'], 'category':t['cat'], 'amount':amount,'direction':t['type'],'owner':t['who'],'payment':payment,'memo':t.get('memo',''),'legacy':t})
            group = '/'.join((month,t['who'],t['type'],payment or 'unknown',t['cat']))
            totals[group] = totals.get(group,0)+amount
    fixed={month:normalize_fixed(items) for month,items in fixed.items()}
    for month in fixed:
        if not MONTH.fullmatch(month):
            raise ValueError('Invalid fixed month')
    anchors = config.get('carryAnchors') or {}
    if not anchors and config.get('carryoverBaseYm'):
        anchors={config['carryoverBaseYm']:config.get('carryover',0)}
    for month, amount in anchors.items():
        if not MONTH.fullmatch(month):
            raise ValueError('Invalid carry anchor month')
        money(amount)
    config['carryAnchors']=anchors
    config['salary']={'enabled':config.get('autoSalary','on')!='off','salary_j':money(config.get('salaryJ',0)),'salary_m':money(config.get('salaryM',0)),'day_j':10,'day_m':17}
    raw_events=config.get('simEvents') or []
    config['simEvents']=[normalize_event(event,key) for key,event in entries(raw_events)]
    report = {'count':len(rows), 'totals':dict(sorted(totals.items())), 'conflicts':sorted(conflicts), 'missing_payment':sum(r['payment'] is None for r in rows), 'fixed_months':sorted(fixed), 'config_keys':sorted(config)}
    return {'transactions':rows,'fixed':fixed,'config':config,'report':report}

def apply(result, expected):
    from app import database
    from psycopg.types.json import Jsonb
    canonical = json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    batch = hashlib.sha256(canonical.encode()).hexdigest()
    if batch != expected:
        raise ValueError('Reviewed plan hash mismatch')
    if result['report']['conflicts']:
        raise ValueError('Resolve source conflicts before importing')
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('budget-migration'))")
        if db.execute('SELECT 1 FROM import_batches WHERE id=%s',(batch,)).fetchone():
            return batch
        if db.execute('SELECT 1 FROM transactions LIMIT 1').fetchone() or db.execute('SELECT 1 FROM import_batches LIMIT 1').fetchone():
            raise ValueError('Import requires an empty target or the identical batch; never overwrites live data')
        db.execute('INSERT INTO import_batches(id,report) VALUES (%s,%s)',(batch,Jsonb(result['report'])))
        for r in result['transactions']:
            db.execute('INSERT INTO transactions(id,source_month,occurred_on,name,category,amount,direction,owner,payment_method,memo,legacy,batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(r['id'],r['month'],r['date'],r['name'],r['category'],r['amount'],r['direction'],r['owner'],r['payment'],r['memo'],Jsonb(r['legacy']),batch))
        for month, items in result['fixed'].items():
            db.execute('INSERT INTO fixed_snapshots VALUES (%s,%s,%s)',(month,Jsonb(items),batch))
        for key,value in result['config'].items():
            db.execute('INSERT INTO settings VALUES (%s,%s)',(key,Jsonb(value)))
        for month,amount in (result['config'].get('carryAnchors') or {}).items():
            db.execute('INSERT INTO carry_anchors VALUES (%s,%s)',(month,money(amount)))
        for key,event in entries(result['config'].get('simEvents') or []):
            db.execute('INSERT INTO simulation_events VALUES (%s,%s)',(str(event.get('id',key)),Jsonb(event)))
        # Keep sanitized original shape for field-level reconciliation.
        for key,value in {'config':result['config'],'fixed':result['fixed']}.items():
            db.execute('INSERT INTO legacy_documents VALUES (%s,%s,%s)',(key,Jsonb(value),batch))
        counts = db.execute('SELECT count(*) AS n FROM transactions WHERE batch_id=%s',(batch,)).fetchone()['n']
        actual = db.execute("SELECT concat_ws('/',source_month,owner,direction,coalesce(payment_method,'unknown'),category) AS g,sum(amount) AS total FROM transactions WHERE batch_id=%s GROUP BY g",(batch,)).fetchall()
        if counts != result['report']['count'] or {r['g']:r['total'] for r in actual} != result['report']['totals']:
            raise ValueError('Reconciliation failed; transaction rolled back')
    return batch

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('firebase_export')
    parser.add_argument('--local-export')
    parser.add_argument('--apply-reviewed-sha256')
    args = parser.parse_args()
    result = plan(json.loads(Path(args.firebase_export).read_text()), json.loads(Path(args.local_export).read_text()) if args.local_export else {})
    canonical = json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    sha = hashlib.sha256(canonical.encode()).hexdigest()
    print(json.dumps({'sha256':sha,**result['report']},ensure_ascii=False,indent=2))
    if args.apply_reviewed_sha256:
        print('Imported batch:', apply(result,args.apply_reviewed_sha256))

