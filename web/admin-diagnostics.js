let records = [];
function render() {
  const root = document.getElementById('rows'); root.replaceChildren();
  const query = document.getElementById('query').value.trim();
  for (const record of records.filter(r => r.session.includes(query) || r.device.includes(query))) {
    const article = document.createElement('article');
    const title = document.createElement('h3'); title.textContent = record.session;
    const info = document.createElement('small');
    info.textContent = `设备 ${record.device} · ${record.browser} · 最近上传 ${new Date(record.received).toLocaleString()}`;
    const detail = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = `${record.events.length} 条事件`;
    const pre = document.createElement('pre');
    pre.textContent = record.events.map(e => `${new Date(e.time).toLocaleString()} ${JSON.stringify(e)}`).join('\n');
    detail.append(summary, pre); article.append(title, info, detail); root.append(article);
  }
  if (!root.childElementCount) root.textContent = '暂无匹配记录';
}
async function refresh() {
  try {
    const response = await fetch('/api/admin/location-diagnostics');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    records = await response.json(); render();
  } catch (error) { document.getElementById('rows').textContent = `加载失败：${error.message}`; }
}
document.getElementById('query').oninput = render;
document.getElementById('refresh').onclick = refresh;
refresh();
