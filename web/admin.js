const state = {offset: 0, limit: 50, total: 0, map: null, overlays: []};
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character]);
const formatTime = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '—';
const formatDistance = value => value == null ? '—' : value >= 1000 ? `${(value/1000).toFixed(2)} km` : `${Math.round(value)} m`;
const formatDuration = value => value == null ? '—' : `${Math.round(value/60)} 分钟`;
const coord = value => value?.[0] != null ? `${Number(value[0]).toFixed(6)}, ${Number(value[1]).toFixed(6)}` : '—';
const routeTitle = item => `${item.start_label || coord(item.start)} → ${item.mode === 'outside' ? '规避区域外安全点' : item.end_label || coord(item.end)}`;
function providerLabel(value) { return ({
  'amap-avoidance':'高德规避路线', 'amap-transition-outbound':'驶出规避区域',
  'amap-transition-inbound':'驶入规避区域', 'amap-buffer-entry':'缓冲区直接规避',
  'amap-handoff':'规避区域外提示',
})[value] || value || '—'; }
async function request(url, options) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data; }

async function loadRecords(reset = false) {
  if (reset) state.offset = 0;
  const params = new URLSearchParams({limit: state.limit, offset: state.offset,
    status: $('status-filter').value, q: $('search').value.trim()});
  $('records').innerHTML = '<tr><td colspan="7">正在加载…</td></tr>';
  try {
    const data = await request(`/api/admin/navigation-records?${params}`); state.total = data.total;
    $('total-count').textContent = data.summary.total; $('success-count').textContent = data.summary.succeeded;
    $('failed-count').textContent = data.summary.failed; renderRows(data.items);
    const start = data.total ? state.offset+1 : 0; const end = Math.min(state.offset+state.limit, data.total);
    $('page-info').textContent = `第 ${start}–${end} 条，共 ${data.total} 条`;
    $('previous').disabled = state.offset === 0; $('next').disabled = end >= data.total;
  } catch (error) { $('records').innerHTML = `<tr><td colspan="7">${escapeHtml(error.message)}</td></tr>`; }
}

function renderRows(items) {
  $('empty').hidden = items.length > 0;
  $('records').innerHTML = items.map(item => `<tr data-id="${escapeHtml(item.id)}"><td>${formatTime(item.created_at)}</td><td><span class="badge ${item.success?'ok':'bad'}">${item.success?'成功':'失败'}</span></td><td><div class="route-name"><b title="${escapeHtml(routeTitle(item))}">${escapeHtml(routeTitle(item))}</b><small>${escapeHtml(item.id)}</small></div></td><td>${escapeHtml(providerLabel(item.provider))}</td><td>${formatDistance(item.distance_m)}<br><small>${formatDuration(item.duration_s)}</small></td><td>${item.amap_api_calls ?? 0} 次</td><td>${(item.elapsed_ms/1000).toFixed(1)} 秒</td></tr>`).join('');
  $('records').querySelectorAll('tr[data-id]').forEach(row => row.onclick = () => openDetail(row.dataset.id));
}

function detailItem(label, value, wide = false) { return `<div class="detail-item ${wide?'wide':''}"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`; }
async function openDetail(id) {
  $('drawer').hidden = false; $('detail').innerHTML = '<div class="detail-card">正在加载…</div>';
  try {
    const item = await request(`/api/admin/navigation-records/${encodeURIComponent(id)}`); const result = item.result || {};
    $('detail').innerHTML = `<section class="detail-card"><div class="detail-status"><span class="badge ${item.success?'ok':'bad'}">${item.success?'算路成功':'算路失败'}</span><span class="record-id">${escapeHtml(item.id)}</span></div>${item.error?`<div class="error-box" style="margin-top:12px">${escapeHtml(item.error)}</div>`:''}</section><section class="detail-card"><div id="detail-map"></div></section><section class="detail-card"><h3>算路概要</h3><div class="detail-grid">${detailItem('创建时间',formatTime(item.created_at))}${detailItem('完成时间',formatTime(item.completed_at))}${detailItem('算路耗时',(item.elapsed_ms/1000).toFixed(1)+' 秒')}${detailItem('起点',item.start_label||coord(item.start),true)}${detailItem('终点',item.mode==='outside'?'规避区域外安全点':item.end_label||coord(item.end),true)}${detailItem('计算方式',providerLabel(item.provider))}${detailItem('路线距离',formatDistance(item.distance_m))}${detailItem('预估行驶时间',formatDuration(item.duration_s))}${detailItem('计算次数',(item.amap_api_calls??0)+' 次')}${detailItem('手动规避点',item.manual_avoid_count+' 个')}${detailItem('客户端 IP',item.client_ip||'—')}${detailItem('浏览器',item.user_agent||'—',true)}</div></section><section class="detail-card"><h3>原始结果摘要</h3><div class="raw">${escapeHtml(JSON.stringify({...result,coordinates:undefined},null,2))}</div></section>`;
    requestAnimationFrame(() => renderMap(item));
  } catch (error) { $('detail').innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`; }
}

function renderMap(item) {
  state.overlays.forEach(overlay => overlay.setMap?.(null)); state.overlays = []; state.map?.destroy();
  const result = item.result || {}; const coordinates = result.coordinates || []; const center = item.start?.[0] != null ? item.start : [116.4,39.9];
  state.map = new AMap.Map('detail-map',{zoom:12,center,viewMode:'2D',pitch:0,pitchEnable:false,rotateEnable:true});
  if (coordinates.length > 1) state.overlays.push(new AMap.Polyline({map:state.map,path:coordinates,strokeColor:'#1677ff',strokeWeight:7,strokeOpacity:.9,lineJoin:'round',showDir:true,zIndex:20}));
  const transition = result.area_transition;
  if (transition?.connector) state.overlays.push(new AMap.Polyline({map:state.map,path:transition.connector,strokeColor:'#16a464',strokeWeight:7,strokeStyle:'dashed',zIndex:18}));
  for (const [label,point,color] of [['起',item.start,'#16a464'],['终',item.end||coordinates.at(-1),'#7c4dff']]) if (point?.[0] != null) state.overlays.push(new AMap.Marker({map:state.map,position:point,anchor:'center',content:`<div style="width:30px;height:30px;display:grid;place-items:center;border:3px solid white;border-radius:50%;background:${color};color:white;font-weight:800;box-shadow:0 2px 7px #0005">${label}</div>`,zIndex:35}));
  if (state.overlays.length) state.map.setFitView(state.overlays,false,[45,45,45,45]);
}

$('apply-filter').onclick = () => loadRecords(true); $('refresh').onclick = () => loadRecords(false);
$('search').onkeydown = event => { if (event.key === 'Enter') loadRecords(true); };
$('previous').onclick = () => { state.offset = Math.max(0,state.offset-state.limit); loadRecords(); };
$('next').onclick = () => { if (state.offset+state.limit < state.total) { state.offset += state.limit; loadRecords(); } };
$('close-detail').onclick = () => $('drawer').hidden = true; $('drawer').onclick = event => { if (event.target === $('drawer')) $('drawer').hidden = true; };
$('clear-all').onclick = async () => { if (!confirm('确定清空全部算路记录？')) return; await request('/api/admin/navigation-records',{method:'DELETE'}); $('drawer').hidden=true; loadRecords(true); };
loadRecords(true);
