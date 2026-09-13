(() => {
  const key = 'location-diagnostics-v1';
  const id = () => crypto.randomUUID();
  let saved;
  try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) {}
  const state = saved && Date.now()-saved.updated < 86400000 ? saved :
    {device: saved?.device || id(), session: id(), events: []};
  let busy = false;
  const persist = () => {
    state.updated = Date.now();
    try { localStorage.setItem(key, JSON.stringify(state)); } catch (_) {}
  };
  async function flush() {
    if (busy || !state.events.length || !navigator.onLine) return;
    busy = true;
    const events = state.events.slice(0, 60);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/location-diagnostics', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, signal: controller.signal,
        body: JSON.stringify({device: state.device, session: state.session, browser: navigator.userAgent, events})});
      if (response.ok && (await response.json()).ok) {
        state.events.splice(0, events.length); persist();
      }
    } catch (_) {} finally { clearTimeout(timer); busy = false; }
  }
  let lastFix = 0;
  window.uploadLocationDiagnostic = event => {
    if (event.type === 'fix') {
      if (Date.now()-lastFix < 30000) return;
      lastFix = Date.now();
    }
    const clean = {};
    for (const k of ['time', 'type', 'visibility', 'code', 'state', 'elapsed_ms', 'accuracy', 'sourceTimestamp']) {
      if (event[k] !== undefined) clean[k] = event[k];
    }
    state.events.push(clean);
    if (state.events.length > 300) state.events.shift();
    persist();
    if (event.type === 'permission' || event.code === 1) flush();
  };
  const button = document.createElement('button');
  button.textContent = `定位诊断：${state.session.slice(0, 8)}`;
  button.title = '点击复制完整诊断编号；仅上传技术事件，不上传位置。';
  button.style.cssText = 'border:0;background:transparent;color:#768397;font-size:11px;padding:4px;cursor:pointer';
  button.onclick = async () => {
    try { await navigator.clipboard.writeText(state.session); button.textContent = '诊断编号已复制'; }
    catch (_) { window.prompt('诊断编号', state.session); }
  };
  document.getElementById('app').prepend(button);
  navigator.permissions?.query({name: 'geolocation'}).then(permission => {
    const changed = () => window.uploadLocationDiagnostic({time: new Date().toISOString(), type: 'permission', state: permission.state});
    permission.addEventListener('change', changed); changed();
  }).catch(() => {});
  setInterval(flush, 15000);
  window.addEventListener('online', flush);
  persist();
})();
