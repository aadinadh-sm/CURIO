const $ = id => document.getElementById(id);
let view = 'diagnose', pollTimer = null, sessionId = null;
const labels = { cpu_saturation: 'CPU saturation', memory_pressure: 'Memory pressure', disk_io_bottleneck: 'Disk I/O bottleneck', thermal_stress: 'Thermal stress', network_activity: 'High network activity', normal: 'No sustained pressure observed' };
const names = { cpu_mean:'CPU use', cpu_max:'CPU peak', cpu_std:'CPU variation', cpu_trend:'CPU change', cpu_freq_mean_mhz:'CPU frequency', cpu_freq_trend_mhz:'CPU frequency change', memory_mean:'Memory use', memory_max:'Memory peak', available_min_gb:'Available RAM', available_pct_min:'Available RAM share', memory_trend:'Memory change', swap_used_gb:'Page file used', swap_pct_mean:'Page file use', disk_read_mbps:'Disk read rate', disk_write_mbps:'Disk write rate', disk_busy_pct:'Disk busy time', disk_latency_ms:'Disk response time', network_rx_mbps:'Network receive', network_tx_mbps:'Network send', temperature_max_c:'Peak temperature', process_count:'Process count', process_growth_mb_per_sec:'Process memory growth' };
function esc(s) { const e=document.createElement('span'); e.textContent=s??''; return e.innerHTML; }
function when(s) { return s ? new Date(s).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : ''; }
function dateOnly(s) { return new Date(s||Date.now()).toLocaleDateString([], {weekday:'long',month:'long',day:'numeric'}); }
function go(next) {
  view=next; document.querySelectorAll('.view').forEach(e=>e.classList.remove('active'));
  $(`view-${next}`)?.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===next));
  $('crumb').textContent=({diagnose:'Diagnose',running:'Quick diagnosis',result:'Diagnosis result',history:'History',discover:'Discoveries',datasets:'Dataset Lab',how:'How it works'})[next]||'Diagnose';
  window.scrollTo({top:0,behavior:'smooth'}); if(next==='history')loadHistory(); if(next==='discover')loadDiscoveries();
}
async function getJson(url,options) { const r=await fetch(url,options); const d=await r.json(); if(!r.ok)throw Error(d.error||'Request failed.'); return d; }
document.querySelectorAll('.nav-item').forEach(b=>b.onclick=()=>go(b.dataset.view));
document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>go(b.dataset.go));
$('how-button').onclick=()=>go('how'); $('model-details').onclick=()=>go('how');
$('cancel-view').onclick=()=>go('diagnose'); $('new-check').onclick=()=>go('diagnose'); $('start-diagnosis').onclick=startDiagnosis;

async function boot() {
  try {
    const d=await getJson('/api/state'); $('device-name').textContent=`${d.system.name} · ${d.system.os} ${d.system.release}`;
    $('footer-system').textContent=`${d.system.os.toUpperCase()} · PRIVATE SESSION STORAGE`;
    const note=$('model-note').querySelector('span:nth-child(2)');
    if(d.model.available){note.textContent='A trained local classifier is installed. Its held-out evaluation metrics are available in technical details. CURIO never uploads telemetry.';$('model-note').classList.add('model-available');}
    renderRecent(d.history,d.sessionCount);
    if(d.diagnosis?.running){sessionId=d.diagnosis.id;go('running');pollDiagnosis();}
    else if(d.diagnosis?.phase==='complete'&&d.diagnosis.result){renderResult(d.diagnosis.result,d.diagnosis.endedAt);go('result');}
  } catch(e) { $('device-name').textContent='Local monitor unavailable'; console.error(e); }
}
async function startDiagnosis() {
  const button=$('start-diagnosis'); button.disabled=true; button.innerHTML='<span>Starting local check…</span><i>·</i>';
  try { const d=await getJson('/api/diagnosis',{method:'POST'});sessionId=d.id;go('running');updateProgress({progress:0,phase:'collecting',windowCount:0,samples:0});pollDiagnosis(); }
  catch(e){alert(e.message);button.disabled=false;button.innerHTML='<span>Diagnose my computer</span><i>↗</i>';}
}
function phaseName(progress,phase) {
  if(phase==='analyzing')return 'Comparing the six feature windows…';
  if(progress<26)return 'Collecting processor and memory behavior…';
  if(progress<51)return 'Measuring disk activity and response time…';
  if(progress<76)return 'Checking network rates and process context…';
  return 'Building the final comparison windows…';
}
function updateProgress(d) {
  const p=d.progress||0; $('progress-fill').style.width=`${p}%`;document.querySelector('.progress-dot').style.left=`${p}%`;
  $('run-phase').textContent=phaseName(p,d.phase);$('run-seconds').textContent=d.phase==='analyzing'?'…':Math.max(0,30-Math.floor(d.samples||0));
  const steps=[['cpu',p>=15],['disk',p>=42],['network',p>=68],['analysis',d.phase==='analyzing'||d.phase==='complete']];
  steps.forEach(([id,done])=>{const e=document.querySelector(`[data-collect="${id}"]`);e.classList.toggle('done',done);e.classList.toggle('current',!done&&p>=({cpu:0,disk:25,network:50,analysis:75})[id]);});
  if(d.liveSummary){const x=d.liveSummary;$('live-reading').innerHTML=`<span class="live-reading-dot"></span><span>Live sample &nbsp; CPU ${x.cpu.toFixed(0)}% · RAM ${x.memory.toFixed(0)}% · Disk read/write ${x.readMbps.toFixed(1)} / ${x.writeMbps.toFixed(1)} MB/s · ${d.windowCount||0}/6 windows</span>`;}
  else $('live-reading').innerHTML=`<span class="live-reading-dot"></span><span>Samples ${d.samples||0}/30 · ${d.windowCount||0}/6 five-second windows</span>`;
}
async function pollDiagnosis() {
  if(!sessionId)return;
  try { const d=await getJson('/api/diagnosis'); if(d.id!==sessionId)return; updateProgress(d);
    if(d.phase==='complete'&&d.result){clearTimeout(pollTimer);renderResult(d.result,d.endedAt);await loadHistory(true);go('result');return;}
    if(d.phase==='error'){clearTimeout(pollTimer);$('run-phase').textContent=`Collection stopped: ${d.error}`;$('run-seconds').textContent='!';return;}
    pollTimer=setTimeout(pollDiagnosis,850);
  } catch(e) { $('run-phase').textContent='Local service paused. Reconnecting…';pollTimer=setTimeout(pollDiagnosis,1600); }
}
function formatFeature(k,v) {
  const n=Number(v); if(!Number.isFinite(n))return 'Unavailable';
  if(k.includes('gb'))return `${n.toFixed(1)} GB`;if(k.includes('latency'))return `${n.toFixed(1)} ms`;
  if(k.includes('mbps'))return `${n.toFixed(2)} Mbps`;if(k.includes('temperature'))return `${n.toFixed(1)} °C`;
  if(k.includes('freq'))return `${n.toFixed(0)} MHz`;if(k.includes('growth'))return `${n.toFixed(1)} MB/s`;
  if(k.includes('process_count'))return `${Math.round(n)}`;
  if(k.includes('pct')||k.includes('mean')&&(/cpu|memory/.test(k))||k.includes('max')&&(/cpu|memory/.test(k))||k.includes('std')||k.includes('trend'))return `${n.toFixed(1)}%`;
  return `${n.toFixed(2)}`;
}
function renderResult(r,endedAt) {
  $('result-date').textContent=dateOnly(endedAt||r.windows?.at(-1)?.end);const normal=r.label==='normal',source=r.source==='trained_model';
  const windows=r.windows||[],features=r.features||{};const cpuMax=Math.max(10,...windows.map(w=>w.cpu_max||0));
  const bars=windows.map((w,i)=>`<div class="window-col"><div class="window-bar ${(w.cpu_mean||0)>80||(w.memory_mean||0)>90?'warn':''}" style="height:${Math.max(5,(w.cpu_mean||0)/cpuMax*78)}%" title="CPU ${Math.round(w.cpu_mean||0)}%, RAM ${Math.round(w.memory_mean||0)}%"></div><small>${i*5}–${i*5+5}s</small></div>`).join('');
  const featureHtml=Object.keys(names).filter(k=>features[k]!==null&&features[k]!==undefined).map(k=>`<div class="feature-chip"><span>${names[k]}</span><b>${formatFeature(k,features[k])}</b></div>`).join('');
  const ruleRows=Object.entries(r.ruleScores||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class="feature-chip"><span>${esc(k)} · screening score, not probability</span><b>${Math.round(v*100)}/100</b></div>`).join('');
  const anomaly=r.anomaly?`<div class="model-caveat">Isolation Forest flagged ${r.anomaly.outlier_windows} of ${r.anomaly.total_windows} windows outside the controlled normal-state distribution. This is an anomaly signal, not a cause.</div>`:'';
  const metrics=r.model?.training;const metricLine=metrics?.accuracy!==undefined?` Held-out accuracy ${(metrics.accuracy*100).toFixed(0)}%; macro F1 ${(metrics.macro_f1*100).toFixed(0)}% across the saved evaluation split.`:'';
  const tag=source?'<span class="result-badge">TRAINED CLASSIFIER</span>':'<span class="result-badge">SIGNAL-RULE SCREEN · NOT ML CLASSIFICATION</span>';
  const summary=normal?(r.evidence[0]||'No strong pressure signal was observed.'):r.evidence.join(' ');
  $('result-content').innerHTML=`<div class="result-hero"><div><div class="result-kicker">${normal?'CHECK COMPLETE · NO STRONG PRESSURE SIGNAL':'CHECK COMPLETE · PRESSURE SIGNALS FOUND'}</div><h1 class="result-title">${esc(labels[r.label]||'Unclassified operating state')}</h1><p class="result-summary">${esc(summary)}</p><div class="result-badges"><span class="result-badge">30-SECOND CHECK</span><span class="result-badge">${windows.length} FIVE-SECOND WINDOWS</span>${tag}${r.anomaly?'<span class="result-badge">ISOLATION FOREST ACTIVE</span>':''}</div></div><div class="result-score"><strong>${normal?'SCREENING RESULT':'MOST SUPPORTED SIGNAL'}</strong><small>${source?'The trained classifier selected this condition. Review the held-out evaluation before relying on it.':'This screening compares multiple measured signals. It does not estimate a calibrated diagnosis probability.'}</small></div></div>
    <div class="result-layout"><article class="result-card"><div class="result-card-heading"><div><span class="card-label">WHY CURIO THINKS THIS</span><p>Evidence measured during this check</p></div><span class="signal-seal">01</span></div><div class="evidence-list">${(r.evidence||[]).map((x,i)=>`<div class="evidence-item"><span class="evidence-num">0${i+1}</span><span>${esc(x)}</span></div>`).join('')}</div><details class="technical-details"><summary>Show all measured features and screening scores</summary><div class="technical-body"><div class="feature-grid">${featureHtml}</div><div class="feature-grid" style="margin-top:10px">${ruleRows}</div></div></details></article>
    <article class="result-card"><div class="result-card-heading"><div><span class="card-label">SUGGESTED CHECKS</span><p>Low-risk next steps based on the evidence</p></div><span class="signal-seal">↗</span></div><div class="action-list">${(r.actions||[]).map(a=>`<div class="action-row"><span>↳</span><span>${esc(a)}</span></div>`).join('')}</div><div class="model-caveat"><b>Read this result carefully.</b> CURIO identifies patterns consistent with observable operating conditions. It cannot confirm damaged hardware or prove what caused a slowdown. ${source?'This result uses a classifier trained on labeled controlled sessions.'+metricLine:'A trained diagnosis model is not installed; this result uses transparent screening rules.'}</div></article></div>
    <article class="window-card"><div class="result-card-heading"><div><span class="card-label">SIGNALS OVER TIME</span><p>Average CPU load in each five-second window · hover for CPU and RAM</p></div><span class="signal-seal">⌁</span></div><div class="window-chart">${bars}</div><div class="window-legend"><i class="legend-swatch"></i> CPU average <span>·</span> Amber marks higher CPU or memory windows</div>${anomaly}</article><div class="model-caveat">${(r.limitations||[]).map(esc).join(' ')}</div>`;
  $('start-diagnosis').disabled=false;$('start-diagnosis').innerHTML='<span>Diagnose my computer</span><i>↗</i>';
}
function renderRecent(rows,count=rows.length) {
  $('history-count').textContent=count;const el=$('recent-list');if(!rows.length){el.innerHTML='<div class="no-history"><span>◷</span> Your first check will appear here.</div>';return;}
  el.innerHTML=rows.slice(0,3).map(row=>`<div class="recent-row" data-session="${esc(row.id)}"><i class="recent-dot ${row.label==='normal'?'':'warning'}"></i><span>${esc(labels[row.label]||row.label)}</span><time>${when(row.started_at)}</time></div>`).join('');
  el.querySelectorAll('[data-session]').forEach(e=>e.onclick=()=>openSession(e.dataset.session));
}
async function loadHistory(silent=false) {
  try { const d=await getJson('/api/history');$('history-count').textContent=d.sessions.length;$('history-summary').textContent=d.sessions.length?`${d.sessions.length} saved check${d.sessions.length===1?'':'s'} · stored on this device`:'No checks yet';
    $('history-empty').classList.toggle('visible',!d.sessions.length);const list=$('history-list');
    list.innerHTML=d.sessions.map(s=>`<div class="history-item" data-session="${esc(s.id)}"><span class="history-icon">${s.label==='normal'?'◉':'⌁'}</span><span><span class="history-title">${esc(labels[s.label]||s.label)}</span><span class="history-description">${esc(s.summary)}</span></span><time class="history-time">${when(s.started_at)}</time><span class="history-chevron">↗</span></div>`).join('');
    list.querySelectorAll('[data-session]').forEach(e=>e.onclick=()=>openSession(e.dataset.session));renderRecent(d.sessions.slice(0,5),d.sessions.length);
  } catch(e) {if(!silent)console.error(e);}
}
async function openSession(id) {try{const r=await getJson(`/api/session?id=${encodeURIComponent(id)}`);renderResult(r,r.windows?.at(-1)?.end);go('result');}catch(e){alert(e.message);}}
async function loadDiscoveries() {
  const root=$('discovery-content');root.innerHTML='<div class="discovery-panel"><div class="discovery-empty"><span class="empty-orbit">✳</span><b>Comparing your diagnosis sessions…</b><p>Discovery checks run locally across saved sessions.</p></div></div>';
  try {
    const d=await getJson('/api/discovery');let html=`<div class="discovery-overview"><div class="discovery-count"><strong>${d.abnormalSessions}</strong><span>PRESSURE SESSIONS</span></div><div class="discovery-count"><strong>${d.normalSessions}</strong><span>NO-PRESSURE SESSIONS</span></div><div class="discovery-count"><strong>${d.patterns.length}</strong><span>RECURRING PATTERNS</span></div></div>`;
    if(d.ready&&d.patterns.length)html+=`<div class="discovery-panel">${d.patterns.map(p=>`<div class="pattern-row"><span class="pattern-icon">↗</span><span><b>${esc(p.feature)} ${esc(p.direction)} in the lead-in</b><p>${esc(p.text)}</p></span><span class="pattern-lift">${p.lift}×<small>RELATIVE RATE</small></span></div>`).join('')}</div>`;
    else html+=`<div class="discovery-panel"><div class="discovery-empty"><span class="empty-orbit">✳</span><b>Not enough independent sessions yet</b><p>${esc(d.required)} CURIO needs repeated outcomes to distinguish a recurring lead-in from normal variation. Run checks during normal use and while the issue is present.</p><span class="session-counts">${d.abnormalSessions} / 3 PRESSURE · ${d.normalSessions} / 3 NO-PRESSURE</span></div></div>`;
    root.innerHTML=html;
  } catch(e) {root.innerHTML='<div class="discovery-panel">Discovery data is not available yet.</div>';}
}
let datasetText='', datasetFilename='', datasetColumns=[];
const fileInput=$('dataset-file'), drop=$('dataset-drop');
fileInput.onchange=()=>{if(fileInput.files[0])loadDatasetFile(fileInput.files[0]);};
for(const ev of ['dragenter','dragover'])drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('dragging');});
for(const ev of ['dragleave','drop'])drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('dragging');});
drop.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f)loadDatasetFile(f);});
$('dataset-target').onchange=()=>{const selected=$('dataset-target').value;const group=$('dataset-group');const prev=group.value;group.innerHTML='<option value="">No grouping selected</option>'+datasetColumns.map(c=>`<option value="${esc(c.name)}">${esc(c.name)} · ${esc(c.kind)}</option>`).join('');group.value=datasetColumns.some(c=>c.name===prev)?prev:'';};
$('dataset-analyze').onclick=analyzeDataset;
$('dataset-reset').onclick=()=>{datasetText='';datasetFilename='';datasetColumns=[];fileInput.value='';$('dataset-file-name').textContent='No dataset selected';$('dataset-preview').innerHTML='';$('dataset-results').innerHTML='';$('dataset-error').textContent='';$('dataset-analyze').disabled=true;$('dataset-target').disabled=true;$('dataset-group').disabled=true;$('dataset-target').innerHTML='<option value="">Profile only · no model evaluation</option>';$('dataset-group').innerHTML='<option value="">No grouping selected</option>';};
async function loadDatasetFile(file){
  $('dataset-error').textContent='';$('dataset-results').innerHTML='';
  if(!file.name.toLowerCase().endsWith('.csv')){$('dataset-error').textContent='Choose a .csv file.';return;}
  if(file.size>10*1024*1024){$('dataset-error').textContent='This analyzer accepts CSV files up to 10 MB.';return;}
  try{datasetText=await file.text();datasetFilename=file.name;$('dataset-file-name').textContent=`${file.name} · ${(file.size/1024).toFixed(1)} KB`;$('dataset-preview').innerHTML='<div class="dataset-panel">Reading columns and sample rows…</div>';
    const d=await getJson('/api/dataset/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:datasetText,filename:file.name})});datasetColumns=d.columns;
    const target=$('dataset-target'),group=$('dataset-group');target.innerHTML='<option value="">Profile only · no model evaluation</option>'+d.headers.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');group.innerHTML='<option value="">No grouping selected</option>'+d.headers.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');
    if(d.targetGuess)target.value=d.targetGuess;if(d.groupGuess)group.value=d.groupGuess;target.disabled=false;group.disabled=false;$('dataset-analyze').disabled=false;renderDatasetPreview(d);
  }catch(e){$('dataset-preview').innerHTML='';$('dataset-error').textContent=e.message;}
}
function renderDatasetPreview(d){
  const cols=d.columns.map(c=>`<div class="dataset-column"><b>${esc(c.name)}</b><span>${esc(c.kind)} · ${c.unique} unique · ${c.missingPct}% missing</span></div>`).join('');
  const heads=d.headers.map(h=>`<th>${esc(h)}</th>`).join('');const rows=d.preview.map(r=>`<tr>${d.headers.map(h=>`<td>${esc(r[h])||'—'}</td>`).join('')}</tr>`).join('');
  $('dataset-preview').innerHTML=`<article class="dataset-panel"><div class="card-label">FILE PROFILE</div><h2>${esc(d.filename)}</h2><div class="dataset-meta"><span class="dataset-pill">${d.rows.toLocaleString()} rows</span><span class="dataset-pill">${d.headers.length} columns</span><span class="dataset-pill">${d.note.includes('not saved')?'LOCAL · NOT SAVED':'LOCAL'}</span></div><div class="dataset-columns">${cols}</div><details class="technical-details"><summary>Preview first rows</summary><div class="technical-body dataset-table-wrap"><table class="dataset-table"><thead><tr>${heads}</tr></thead><tbody>${rows}</tbody></table></div></details></article>`;
}
async function analyzeDataset(){
  $('dataset-error').textContent='';$('dataset-analyze').disabled=true;$('dataset-results').innerHTML='<div class="dataset-panel">Running CURIO diagnosis on each telemetry run and preparing the evidence…</div>';
  try{const d=await getJson('/api/dataset/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:datasetText,filename:datasetFilename,target:$('dataset-target').value,group:$('dataset-group').value})});renderDatasetAnalysis(d);}
  catch(e){$('dataset-results').innerHTML='';$('dataset-error').textContent=e.message;}
  finally{$('dataset-analyze').disabled=false;}
}
function renderDatasetAnalysis(d){
  const stats=d.featureStats||[];let html=`<article class="dataset-panel"><div class="card-label">ANALYSIS · IN MEMORY</div><h2>${esc(d.filename)}</h2><div class="dataset-meta"><span class="dataset-pill">${d.rows.toLocaleString()} rows</span><span class="dataset-pill">${d.featureCount} eligible numeric features</span><span class="dataset-pill">Target: ${esc(d.target||'none')}</span><span class="dataset-pill">Group: ${esc(d.group||'none')}</span></div>`;
  if(d.classCounts&&Object.keys(d.classCounts).length)html+=`<div class="dataset-metrics">${Object.entries(d.classCounts).map(([k,v])=>`<div class="dataset-metric"><strong>${v}</strong><span>${esc(k)}</span></div>`).join('')}</div>`;
  const diagnosed=(d.diagnoses||[]).filter(x=>x.result);
  if(diagnosed.length)html+=`<div class="dataset-diagnoses"><div class="card-label">CURIO DIAGNOSIS · ${diagnosed.length} RUN${diagnosed.length===1?'':'S'}</div><p class="dataset-copy">Same signal rules and evidence explanations as Quick Diagnosis. The CSV label column is not used to choose these results.</p>${diagnosed.map((run,i)=>renderDatasetRun(run,i)).join('')}</div>`;
  else html+=`<div class="dataset-warnings">CURIO could not produce a diagnosis from these columns. Use CURIO feature columns such as cpu_mean, memory_mean and disk_busy_pct, or common CPU/RAM percentage fields. Check the data and column summary below.</div>`;
  html+=`<div class="card-label">FEATURE SUMMARY</div><div class="dataset-table-wrap"><table class="dataset-table"><thead><tr><th>Feature</th><th>Mean</th><th>Median</th><th>Min</th><th>Max</th><th>Missing</th></tr></thead><tbody>${stats.map(s=>`<tr><td>${esc(s.name)}</td><td>${s.mean}</td><td>${s.median}</td><td>${s.min}</td><td>${s.max}</td><td>${s.missingPct}%</td></tr>`).join('')}</tbody></table></div>`;
  if(d.evaluation){const e=d.evaluation;const imp=e.importance||[];const noUniqueSignal=imp.length&&imp.every(x=>x.score<=0);html+=`<div class="dataset-panel"><div class="card-label">SESSION-HELD-OUT CLASSIFIER EVALUATION</div><div class="dataset-metrics"><div class="dataset-metric"><strong>${(e.accuracy*100).toFixed(1)}%</strong><span>ACCURACY</span></div><div class="dataset-metric"><strong>${(e.macroF1*100).toFixed(1)}%</strong><span>MACRO F1</span></div><div class="dataset-metric"><strong>${e.trainSessions}</strong><span>TRAIN SESSIONS</span></div><div class="dataset-metric"><strong>${e.testSessions}</strong><span>TEST SESSIONS</span></div></div><p class="dataset-copy">${esc(e.split)} One split is an estimate, not a validated production model.</p><div class="card-label">PERMUTATION IMPORTANCE · HELD-OUT DATA</div>${noUniqueSignal?'<p class="dataset-copy">No single feature changed the score when shuffled. Correlated features can share the same signal, so zero here does not mean the features are useless.</p>':''}<div class="dataset-bars">${imp.map(x=>`<div class="dataset-bar-row"><span>${esc(x.name)}</span><div class="dataset-bar"><i style="width:${Math.min(100,Math.max(0,x.score*100))}%"></i></div><span>${x.score.toFixed(3)}</span></div>`).join('')}</div><div class="card-label" style="margin-top:15px">CONFUSION MATRIX · ACTUAL ROW / PREDICTED COLUMN</div><div class="dataset-confusion">${e.confusion.map((row,i)=>`<div class="dataset-confusion-row">${row.map((n,j)=>`<div class="dataset-confusion-cell ${i===j?'':'miss'}" title="Actual ${esc(e.classes[i])}, predicted ${esc(e.classes[j])}">${n}</div>`).join('')}</div>`).join('')}</div><div class="dataset-meta">${e.classes.map(c=>`<span class="dataset-pill">${esc(c)}</span>`).join('')}</div></div>`;}
  else html+=`<div class="dataset-warnings">No classifier evaluation was produced. CURIO requires numeric features, a categorical target, and enough independent sessions per class to keep related rows together.</div>`;
  if(d.warnings?.length)html+=`<div class="dataset-warnings">${d.warnings.map(w=>`<div>· ${esc(w)}</div>`).join('')}</div>`;
  html+=`<p class="dataset-copy">${esc(d.privacy)} Synthetic sample files are for demonstration only; they are not real computer measurements.</p></article>`;$('dataset-results').innerHTML=html;
}
function renderDatasetRun(run,index){
  const r=run.result;const normal=r.label==='normal';const rows=r.windows||[];const shown=rows.length>36?rows.filter((_,i)=>i%Math.ceil(rows.length/36)===0).slice(0,36):rows;const peak=Math.max(10,...shown.map(w=>w.cpu_max||w.cpu_mean||0));
  const chart=shown.map((w,i)=>`<div class="window-col"><div class="window-bar ${(w.cpu_mean||0)>80||(w.memory_mean||0)>90?'warn':''}" style="height:${Math.max(4,(w.cpu_mean||0)/peak*75)}%" title="CPU ${Math.round(w.cpu_mean||0)}%, RAM ${Math.round(w.memory_mean||0)}%"></div><small>${esc(String(i+1))}</small></div>`).join('');
  const classifier=r.source==='trained_model';
  return `<details class="dataset-run" ${index===0?'open':''}><summary><span class="run-summary-group">${esc(run.group)}</span><b class="${normal?'run-normal':'run-pressure'}">${esc(r.labelTitle)}</b><small>${run.windowCount} windows · ${(run.signalGroups||[]).length}/4 signal groups</small></summary><div class="dataset-run-body"><div class="result-hero"><div><div class="result-kicker">${normal?'NO STRONG PRESSURE SIGNAL':'MOST SUPPORTED SIGNAL'}</div><h2 class="result-title">${esc(r.labelTitle)}</h2><p class="result-summary">${esc((r.evidence||[]).join(' '))}</p><div class="result-badges"><span class="result-badge">${run.windowCount} DATA WINDOWS</span><span class="result-badge">${(run.signalGroups||[]).length}/4 SIGNAL GROUPS</span><span class="result-badge">${classifier?'TRAINED CLASSIFIER':'TRANSPARENT SIGNAL RULES'}</span><span class="result-badge">NOT A PROBABILITY</span></div></div><div class="result-score"><strong>${normal?'SCREENING RESULT':classifier?'MODEL-SELECTED CONDITION':'RULE STRENGTH'}</strong><small>${normal?'No dominant pressure signal crossed the screening threshold.':classifier?'The trained model selected this condition; this is not calibrated confidence.':'Signal score '+Math.round((r.ruleStrength||0)*100)+'/100 · a screening score, not calibrated confidence.'}</small></div></div><div class="result-layout"><article class="result-card"><div class="card-label">MEASURED EVIDENCE</div><div class="evidence-list">${(r.evidence||[]).map((x,j)=>`<div class="evidence-item"><span class="evidence-num">0${j+1}</span><span>${esc(x)}</span></div>`).join('')}</div></article><article class="result-card"><div class="card-label">SUGGESTED CHECKS</div><div class="action-list">${(r.actions||[]).map(a=>`<div class="action-row"><span>↳</span><span>${esc(a)}</span></div>`).join('')}</div></article></div><article class="window-card"><div class="card-label">SIGNALS IN THIS RUN</div><p class="dataset-copy">CPU average by uploaded row/window; hover for CPU and RAM. ${rows.length>shown.length?'Chart samples up to 36 points from the run.':''}</p><div class="window-chart">${chart}</div></article><details class="technical-details"><summary>Show measured feature values and screening scores</summary><div class="technical-body"><div class="feature-grid">${Object.entries(names).filter(([k])=>r.features?.[k]!==null&&r.features?.[k]!==undefined).map(([k,n])=>`<div class="feature-chip"><span>${esc(n)}</span><b>${formatFeature(k,r.features[k])}</b></div>`).join('')}</div></div></details></div></details>`;
}
boot();
