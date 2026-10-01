const $ = (selector) => document.querySelector(selector);
const won = new Intl.NumberFormat('ko-KR', {style:'currency',currency:'KRW',maximumFractionDigits:0});
const whoNames = {j:'진수',m:'미나',b:'공동'};
let csrf = '';
$('#theme-toggle').addEventListener('click',()=>window.walletTheme.toggle());
window.walletTheme.apply();
document.querySelectorAll('.nav-link[data-page]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();showPage(link.dataset.page);}));
window.addEventListener('popstate',syncPageFromLocation);window.addEventListener('hashchange',syncPageFromLocation);
$('#dashboard-all-transactions').addEventListener('click',()=>showPage('transactions'));
$('#recent-transactions').addEventListener('click',event=>{if(event.target.closest('[data-recent-id]'))showPage('transactions');});
const localToday=new Date();
let selectedMonth = `${localToday.getFullYear()}-${String(localToday.getMonth()+1).padStart(2,'0')}`;
let loadedTransactions = [];
let importRows = [];
let searchTimer;

async function api(path, options={}) {
  const headers = new Headers(options.headers || {});
  headers.set('Accept','application/json');
  if (options.body) headers.set('Content-Type','application/json');
  if (options.method && !['GET','HEAD'].includes(options.method)) headers.set('X-CSRF-Token',csrf);
  const response = await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  if (response.status===401 && !path.endsWith('/login')) { showLogin(); throw new Error('세션이 만료됐어요. 다시 로그인해 주세요.'); }
  const body = response.status===204 ? null : await response.json().catch(()=>null);
  if (!response.ok) throw new Error(body?.detail || `요청에 실패했어요 (${response.status})`);
  return body;
}
function showLogin(message='') { $('#app-view').hidden=true; $('#login-view').hidden=false; $('#login-error').textContent=message; }
function showApp(user) { $('#login-view').hidden=true; $('#app-view').hidden=false; $('#user-name').textContent=user; $('#month-picker').value=selectedMonth; showPage(location.hash.slice(1)||'overview',false); refresh(); loadPlanning(); }
function toast(message) { const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.remove('show'),2800); }
function escapeHtml(text) { return String(text ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function formatDate(value) { return new Date(`${value}T00:00:00`).toLocaleDateString('ko-KR',{month:'short',day:'numeric'}); }
const pageIds=new Set(['overview','transactions','planning','import']);
function showPage(page,addHistory=true){
  if(!pageIds.has(page))page='overview';
  document.querySelectorAll('[data-page-view]').forEach(section=>section.hidden=section.id!==page);
  document.querySelectorAll('.nav-link[data-page]').forEach(link=>{const active=link.dataset.page===page;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
  if(addHistory&&location.hash!==`#${page}`)history.pushState({page},'',`#${page}`);
  window.scrollTo({left:0,top:0,behavior:'auto'});
}
function syncPageFromLocation(){showPage(location.hash.slice(1)||'overview',false);}
function monthMove(delta) { const [y,m]=selectedMonth.split('-').map(Number); const d=new Date(y,m-1+delta,1); selectedMonth=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; $('#month-picker').value=selectedMonth; refresh(); }
async function refresh() { if ($('#app-view').hidden) return; await Promise.all([loadSummary(),loadTransactions(),loadRecentTransactions()]); }
async function loadSummary() {
  try {
    const s=await api(`/api/summary?month=${encodeURIComponent(selectedMonth)}`);
    $('#summary-income').textContent=won.format(s.income);
    $('#summary-expenses').textContent=won.format(s.expenses);
    $('#summary-fixed').textContent=won.format(s.fixed_cash);
    const net=$('#summary-net'); net.textContent=won.format(s.net_before_carry); net.classList.toggle('negative',s.net_before_carry<0);
    $('#summary-fixed').parentElement.querySelector('.summary-caption').textContent=`시작잔액 ${s.carry_in===null?'미설정':won.format(s.carry_in)} · 마감 ${s.closing_balance===null?'미설정':won.format(s.closing_balance)}`;
    const cats=$('#category-list');
    if (!s.categories.length) cats.innerHTML='<div class="category-empty">아직 기록된 지출이 없어요.</div>';
    else { const max=Math.max(...s.categories.map(x=>x.amount),1); cats.innerHTML=s.categories.slice(0,7).map((x,i)=>`<div class="category-row"><span class="category-name">${escapeHtml(x.name)}</span><span class="category-track"><span class="category-fill" style="display:block;width:${Math.max(4,Math.round(x.amount/max*100))}%;background:${['#6b9e7c','#d69864','#779bb0','#c17f8d','#a692bf','#8ca99e','#c0a75f'][i%7]}"></span></span><span class="category-amount">${won.format(x.amount)}</span></div>`).join(''); }
    const pass=$('#pass-summary'); pass.hidden=!s.pass_net; pass.textContent=s.pass_net?`대납·정산·부업 순액 ${s.pass_net>0?'+':''}${won.format(s.pass_net)} · 월 흐름에 반영됨`:'';
  } catch(e) { toast(e.message); }
}
async function loadTransactions() {
  const query=new URLSearchParams({month:selectedMonth}); const text=$('#search-input').value.trim(); if(text)query.set('q',text);
  try {
    loadedTransactions=await api(`/api/transactions?${query}`);
    const owner=$('#owner-filter').value;
    const visible=owner?loadedTransactions.filter(x=>x.owner===owner):loadedTransactions;
    const body=$('#transaction-rows'); $('#empty-state').hidden=visible.length>0;
    body.innerHTML=visible.map(t=>`<tr><td>${formatDate(t.occurred_on)}</td><td class="tx-name">${escapeHtml(t.name)}</td><td>${escapeHtml(t.category)}</td><td><span class="owner-badge owner-${t.owner}">${whoNames[t.owner]||'공동'}</span></td><td><span class="pay-badge">${t.payment_method==='card'?'카드':t.payment_method==='cash'?'현금·이체':'미정'}</span></td><td class="amount ${t.direction}">${t.direction==='income'?'+':'−'}${won.format(t.amount)}</td><td><button class="row-edit" data-id="${escapeHtml(t.id)}">보기</button></td></tr>`).join('');
  } catch(e) { toast(e.message); }
}
async function loadRecentTransactions(){
  try{const rows=await api(`/api/transactions?month=${encodeURIComponent(selectedMonth)}&limit=5`);const box=$('#recent-transactions');box.innerHTML=rows.length?rows.map(t=>`<button type="button" class="recent-row" data-recent-id="${escapeHtml(t.id)}"><span class="recent-date">${formatDate(t.occurred_on)}</span><span class="recent-main"><b>${escapeHtml(t.name)}</b><small>${escapeHtml(t.category)} · ${whoNames[t.owner]||'공동'} · ${t.payment_method==='card'?'카드':t.payment_method==='cash'?'현금·이체':'미정'}</small></span><strong class="amount ${t.direction}">${t.direction==='income'?'+':'−'}${won.format(t.amount)}</strong></button>`).join(''):'<div class="recent-empty">이 달에 기록된 거래가 없습니다.</div>';}
  catch(e){toast(e.message);}
}
function openTransaction(type='expense', transaction=null) {
  const form=$('#transaction-form'); form.reset(); form.elements.id.value=transaction?.id||''; form.elements.direction.value=transaction?.direction||type;
  form.elements.occurred_on.value=transaction?.occurred_on||`${selectedMonth}-${String(new Date().getDate()).padStart(2,'0')}`;
  form.elements.amount.value=transaction?.amount||''; form.elements.name.value=transaction?.name||'';
  const category=form.elements.category; const categoryValue=transaction?.category||'';
  if(categoryValue&&!Array.from(category.options).some(option=>option.value===categoryValue)){const option=document.createElement('option');option.value=categoryValue;option.textContent=`${categoryValue} · 기존`;category.append(option);}
  category.value=categoryValue;
  form.elements.owner.value=transaction?.owner||'j'; form.elements.payment_method.value=transaction?.payment_method??'cash'; form.elements.memo.value=transaction?.memo||'';
  $('#dialog-title').textContent=transaction?'거래 수정':'거래 추가'; $('#delete-transaction').hidden=!transaction; $('#transaction-error').textContent=''; setDirection(form.elements.direction.value); $('#transaction-dialog').showModal();
}
function setDirection(direction) { $('#transaction-form').elements.direction.value=direction; document.querySelectorAll('.type-toggle button').forEach(b=>b.classList.toggle('selected',b.dataset.direction===direction)); }
async function saveTransaction(event) {
  event.preventDefault(); const form=$('#transaction-form'); const id=form.elements.id.value;
  const payload={occurred_on:form.elements.occurred_on.value,name:form.elements.name.value.trim(),category:form.elements.category.value.trim(),amount:Number(form.elements.amount.value),direction:form.elements.direction.value,owner:form.elements.owner.value,payment_method:form.elements.payment_method.value||null,memo:form.elements.memo.value.trim()};
  try { await api(id?`/api/transactions/${encodeURIComponent(id)}`:'/api/transactions',{method:id?'PUT':'POST',body:JSON.stringify(payload)}); $('#transaction-dialog').close(); selectedMonth=payload.occurred_on.slice(0,7); $('#month-picker').value=selectedMonth; await refresh(); toast(id?'거래를 수정했어요.':'거래를 저장했어요.'); }
  catch(e) { $('#transaction-error').textContent=e.message; }
}
async function deleteTransaction() {
  const id=$('#transaction-form').elements.id.value; if(!id||!window.confirm('이 거래를 삭제할까요?'))return;
  try { await api(`/api/transactions/${encodeURIComponent(id)}`,{method:'DELETE'}); $('#transaction-dialog').close(); await refresh(); toast('거래를 삭제했어요.'); } catch(e) { $('#transaction-error').textContent=e.message; }
}

function parseCsv(text) {
  text=text.replace(/^\uFEFF/,''); const rows=[]; let row=[],field='',quoted=false;
  for(let i=0;i<text.length;i++){const c=text[i]; if(quoted){if(c==='"'&&text[i+1]==='"'){field+='"';i++;}else if(c==='"')quoted=false;else field+=c;}else if(c==='"')quoted=true;else if(c===','){row.push(field);field='';}else if(c==='\n'){row.push(field);rows.push(row);row=[];field='';}else if(c!=='\r')field+=c;}
  row.push(field); if(row.some(x=>x.trim()))rows.push(row); if(rows.length<2)throw new Error('헤더와 거래가 포함된 CSV를 선택해 주세요.');
  const headers=rows.shift().map(x=>x.trim().toLowerCase()); return rows.map(cols=>Object.fromEntries(headers.map((h,i)=>[h,(cols[i]||'').trim()])));
}
function pick(row,names) { for(const name of names){const k=Object.keys(row).find(x=>x.replace(/[\s_()-]/g,'')===name.replace(/[\s_()-]/g,'')); if(k&&row[k]!=='')return row[k];} return ''; }
function parseDate(raw) { const s=raw.replace(/[./]/g,'-').trim(); let m=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/); if(!m&&/^\d{8}$/.test(s))m=[s,s.slice(0,4),s.slice(4,6),s.slice(6,8)]; if(!m)return ''; const value=`${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}`; const d=new Date(`${value}T00:00:00`); return Number.isNaN(d.getTime())||d.toISOString().slice(0,10)!==value?'':value; }
function categorize(name) { const rules=[[/배민|요기요|쿠팡이츠|식당|음식|마트|이마트|홈플러스|식자재/,'식비'],[/스타벅스|커피|카페|투썸/,'카페'],[/택시|버스|지하철|교통|주유|하이패스|주차/,'교통'],[/병원|약국|의원|치과/,'의료'],[/쿠팡|다이소|편의점|올리브영|쇼핑/,'쇼핑'],[/넷플릭스|유튜브|영화|구독/,'문화/여가']]; return rules.find(([re])=>re.test(name))?.[1]||'기타'; }
function mapCsvRow(row) {
  const rawDate=pick(row,['날짜','거래일','이용일','승인일','date']); const dateValue=parseDate(rawDate);
  const name=pick(row,['가맹점명','사용처','내용','적요','상호','거래내용','name']);
  const rawAmount=pick(row,['이용금액','승인금액','거래금액','금액','amount']).replace(/[^0-9+-.]/g,''); const signed=Number(rawAmount);
  if(!dateValue||!name||!Number.isFinite(signed)||signed===0) return null;
  const rawDirection=pick(row,['구분','유형','type','direction']); const isIncome=/수입|입금|income/i.test(rawDirection)||signed<0;
  const rawOwner=pick(row,['담당','누가','owner']); const owner=/미나|mina|^m$/i.test(rawOwner)?'m':/진수|jinsu|^j$/i.test(rawOwner)?'j':'b';
  const rawPay=pick(row,['결제수단','결제','payment_method']); const payment=/카드|card/i.test(rawPay)?'card':/현금|이체|cash/i.test(rawPay)?'cash':null;
  const category=pick(row,['카테고리','분류','category'])||categorize(name);
  return {occurred_on:dateValue,name:name.slice(0,160),category:category.slice(0,80),amount:Math.round(Math.abs(signed)),direction:isIncome?'income':'expense',owner,payment_method:payment,memo:''};
}
function cancelImport() { importRows=[]; $('#import-preview').hidden=true; $('#csv-file').value=''; }
async function prepareImport(file) {
  try {
    const mapped=parseCsv(await file.text()).map(mapCsvRow); const invalid=mapped.filter(x=>!x).length; importRows=mapped.filter(Boolean);
    if(!importRows.length)throw new Error('가져올 수 있는 거래가 없어요. 날짜·내용·금액 열을 확인해 주세요.');
    $('#preview-title').textContent=file.name; $('#preview-count').textContent=`${importRows.length.toLocaleString()}건 등록 예정`;
    const warning=$('#preview-warnings'); warning.hidden=!invalid; warning.textContent=invalid?`${invalid}행은 날짜·거래처·금액을 읽지 못해 제외했어요.`:'';
    $('#preview-rows').innerHTML=importRows.slice(0,100).map(x=>`<div class="preview-row"><span>${formatDate(x.occurred_on)}</span><span>${escapeHtml(x.name)}</span><span>${escapeHtml(x.category)}</span><strong class="${x.direction}">${x.direction==='income'?'+':'−'}${won.format(x.amount)}</strong></div>`).join('')+(importRows.length>100?`<div class="preview-row">미리보기 100건 표시 · 전체 ${importRows.length.toLocaleString()}건</div>`:'');
    $('#import-preview').hidden=false;
  } catch(e) { toast(e.message); cancelImport(); }
}
async function confirmImport() {
  const button=$('#confirm-import'); button.disabled=true;
  try { const result=await api('/api/import/csv',{method:'POST',body:JSON.stringify(importRows)}); toast(result.duplicate?`같은 CSV를 이미 가져왔어요 (${result.count}건)`:`${result.count}건을 가져왔어요.`); cancelImport(); await refresh(); }
  catch(e) { toast(e.message); }
  finally { button.disabled=false; }
}

let fixedItems=[];
async function loadPlanning(){
  try {
    const [salary,fixed,carry,events,forecast]=await Promise.all([api('/api/settings/salary'),api(`/api/fixed?month=${selectedMonth}`),api(`/api/carry/${selectedMonth}`),api('/api/simulation'),api(`/api/simulation/forecast?start=${selectedMonth}&months=6`)]);
    const sf=$('#salary-form'); for(const k of ['salary_j','salary_m','day_j','day_m'])sf.elements[k].value=salary[k]??0; sf.elements.enabled.checked=salary.enabled;
    fixedItems=fixed.items||[]; $('#fixed-month-label').textContent=selectedMonth; renderFixed();
    $('#carry-form').elements.amount.value=carry.amount??'';
    $('#carry-result').textContent=carry.amount===null?'아직 이 달의 시작 잔액을 설정하지 않았어요.':`기준 잔액 ${won.format(carry.amount)}`;
    const shown=events.filter(e=>e.value.month===selectedMonth); $('#event-list').innerHTML=shown.length?shown.map(e=>`<div class="plan-row"><span>${escapeHtml(e.value.name)} · ${e.value.direction==='income'?'수입':'지출'} ${won.format(e.value.amount)}</span><button type="button" class="row-edit" data-event-id="${escapeHtml(e.id)}">삭제</button></div>`).join(''):'<p class="muted">이 달에 등록한 예정 항목이 없습니다.</p>';
    $('#forecast-list').innerHTML='<h4>향후 6개월 예상 순흐름 · 급여 예상 포함</h4>'+forecast.map(x=>`<div class="plan-row"><span>${x.month}<small>급여 예상 ${won.format(x.expected_salary)} · 고정 현금 ${won.format(x.fixed_cash)}</small></span><strong class="amount ${x.planned_net<0?'expense':'income'}">${won.format(x.planned_net)}</strong></div>`).join('');
  }catch(e){toast(e.message);}
}
function renderFixed(){
  const box=$('#fixed-list');
  box.innerHTML=fixedItems.length?fixedItems.map((x,i)=>`<div class="plan-row"><span>${escapeHtml(x.name)} <small>${x.payMethod==='card'?'카드':'현금·이체'} · ${whoNames[x.owner]||'공동'}</small></span><strong>${won.format(Number(x.amt??x.amount??0))}</strong><button type="button" class="row-edit" data-fixed-index="${i}">삭제</button></div>`).join(''):'<p class="muted">이 달에 등록한 고정지출이 없습니다.</p>';
}
$('#salary-form').addEventListener('submit',async e=>{e.preventDefault();const f=e.currentTarget;try{await api('/api/settings/salary',{method:'PUT',body:JSON.stringify({enabled:f.elements.enabled.checked,salary_j:Number(f.elements.salary_j.value||0),salary_m:Number(f.elements.salary_m.value||0),day_j:Number(f.elements.day_j.value||10),day_m:Number(f.elements.day_m.value||17)})});toast('급여 설정을 저장했어요.');await loadPlanning();}catch(err){toast(err.message);}});
$('#fixed-form').addEventListener('submit',e=>{e.preventDefault();const f=e.currentTarget;fixedItems.push({name:f.elements.name.value.trim(),amt:Number(f.elements.amount.value),payMethod:f.elements.payment_method.value,owner:f.elements.owner.value});f.reset();renderFixed();});
$('#save-fixed').addEventListener('click',async()=>{try{await api(`/api/fixed/${selectedMonth}`,{method:'PUT',body:JSON.stringify({items:fixedItems})});toast('이번 달 고정지출을 저장했어요.');await Promise.all([loadPlanning(),loadSummary()]);}catch(e){toast(e.message);}});
$('#fixed-list').addEventListener('click',e=>{const b=e.target.closest('[data-fixed-index]');if(!b)return;fixedItems.splice(Number(b.dataset.fixedIndex),1);renderFixed();});
$('#carry-form').addEventListener('submit',async e=>{e.preventDefault();const amount=Number(e.currentTarget.elements.amount.value);try{await api(`/api/carry/${selectedMonth}`,{method:'PUT',body:JSON.stringify({amount})});toast('이월 잔액 기준점을 저장했어요.');await Promise.all([loadPlanning(),loadSummary()]);}catch(err){toast(err.message);}});
$('#event-form').addEventListener('submit',async e=>{e.preventDefault();const f=e.currentTarget;try{await api('/api/simulation',{method:'POST',body:JSON.stringify({name:f.elements.name.value.trim(),amount:Number(f.elements.amount.value),month:f.elements.month.value,direction:f.elements.direction.value,owner:'b'})});f.reset();f.elements.month.value=selectedMonth;toast('예정 항목을 추가했어요.');await loadPlanning();}catch(err){toast(err.message);}});
$('#event-list').addEventListener('click',async e=>{const b=e.target.closest('[data-event-id]');if(!b)return;try{await api(`/api/simulation/${encodeURIComponent(b.dataset.eventId)}`,{method:'DELETE'});await loadPlanning();}catch(err){toast(err.message);}});

// iOS Safari zooms inputs whose text is smaller than 16px. CSS keeps mobile controls at
// 16px; this also clears any horizontal drift after the keyboard closes without disabling pinch zoom.
document.addEventListener('focusout',event=>{if(!event.target.matches('input,select,textarea'))return;setTimeout(()=>{if(document.activeElement.matches('input,select,textarea'))return;document.documentElement.scrollLeft=0;document.body.scrollLeft=0;window.scrollTo({left:0,top:window.scrollY,behavior:'auto'});},80);});

$('#login-form').addEventListener('submit',async event=>{event.preventDefault();const data=new FormData(event.currentTarget);const button=event.currentTarget.querySelector('button');button.disabled=true;try{const result=await api('/api/login',{method:'POST',body:JSON.stringify({username:data.get('username'),password:data.get('password')})});csrf=result.csrf;showApp(String(data.get('username')));}catch(e){$('#login-error').textContent=e.message==='Invalid credentials'?'아이디 또는 비밀번호를 확인해 주세요.':e.message;}finally{button.disabled=false;}});
$('#logout-button').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST',body:'{}'});}catch{}csrf='';showLogin();});
$('#add-button').addEventListener('click',()=>openTransaction()); $('#empty-add').addEventListener('click',()=>openTransaction());
document.querySelectorAll('.quick-action').forEach(b=>b.addEventListener('click',()=>openTransaction(b.dataset.type)));
document.querySelectorAll('.type-toggle button').forEach(b=>b.addEventListener('click',()=>setDirection(b.dataset.direction)));
$('#transaction-form').addEventListener('submit',saveTransaction); $('#delete-transaction').addEventListener('click',deleteTransaction);
$('#close-dialog').addEventListener('click',()=>$('#transaction-dialog').close()); $('#cancel-dialog').addEventListener('click',()=>$('#transaction-dialog').close());
$('#transaction-rows').addEventListener('click',event=>{const button=event.target.closest('[data-id]');if(!button)return;const tx=loadedTransactions.find(x=>x.id===button.dataset.id);if(tx)openTransaction(tx.direction,tx);});
$('#prev-month').addEventListener('click',()=>monthMove(-1)); $('#next-month').addEventListener('click',()=>monthMove(1)); $('#month-picker').addEventListener('change',event=>{if(event.target.value){selectedMonth=event.target.value;refresh();loadPlanning();}});
$('#owner-filter').addEventListener('change',loadTransactions); $('#search-input').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadTransactions,250);});
$('#csv-file').addEventListener('change',event=>{const file=event.target.files?.[0];if(file)prepareImport(file);}); $('#cancel-import').addEventListener('click',cancelImport); $('#confirm-import').addEventListener('click',confirmImport);

(async()=>{try{const user=await api('/api/me');csrf=user.csrf;showApp(user.username);}catch{showLogin();}})();
