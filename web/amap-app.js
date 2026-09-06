const panel = document.getElementById('panel');
const statusEl = document.getElementById('status');
const map = new AMap.Map('map', {
  center: [116.506, 40.076], zoom: 12, viewMode: '3D', pitch: 0,
  rotateEnable: true, pitchEnable: false, lang: 'zh_cn',
  animateEnable: false, keyboardEnable: false,
});

const readPreference = (key, fallback) => {
  try { return window.localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
};
const writePreference = (key, value) => {
  try { window.localStorage.setItem(key, value); } catch (_) {}
};

let startCoord = null;
let startAreaSerial = 0;
let endCoord = null;
let currentCoord = null;
let vehicleHeading = null;
let headingAnchor = null;
let startMarker = null;
let endMarker = null;
let currentMarker = null;
let currentPulseMarker = null;
let routeLine = null;
let connectorLine = null;
let gatewayMarker = null;
let lastRoute = null;
let manualAvoid = [];
let manualMarkers = [];
let infoWindow = null;
let cameraMass = null;
let cameraData = [];
let cameraDataLoaded = false;
let cameraEdit = null;
let camerasVisible = readPreference('tracert-camera-layer', '0') !== '0';
let simpleMap = readPreference('tracert-simple-map', '1') === '1';
let amapGeolocation = null;
let amapPollGeolocation = null;
let geolocationReady = null;
let locationPollPromise = null;
let lastLocationFixAt = 0;
let lastLocationSourceTimestamp = 0;
let locationFixSerial = 0;
let cancelLocationPoll = null;
let positionWatchId = null;
let positionWatchGeneration = 0;
let positionWatchCleanup = null;
let positionWatchStarting = false;
let lastWatchStartedAt = 0;
let lastWatchFixAt = 0;
const locationStartedAt = Date.now();
const locationDiagnostics = [];
const locationHealth = document.createElement('div');
locationHealth.className = 'location-health';
locationHealth.hidden = true;
document.getElementById('app').append(locationHealth);
window.getLocationDiagnostics = () => locationDiagnostics.map(entry => ({...entry}));

function recordLocationEvent(type, details = {}) {
  locationDiagnostics.push({time: new Date().toISOString(), type,
    visibility: document.visibilityState, ...details});
  if (locationDiagnostics.length > 240) locationDiagnostics.shift();
}

function updateLocationHealth() {
  const age = Date.now()-(lastLocationFixAt || locationStartedAt);
  locationHealth.hidden = age < 10000;
  locationHealth.textContent = lastLocationFixAt
    ? `定位暂未更新 · ${Math.floor(age/1000)} 秒`
    : '暂未取得定位，请检查定位权限与信号';
}
let following = false;
let orientationMode = 'north';
let followBaseZoom = null;
let junctionZoomed = false;
let activeRouteController = null;
let recenterRotationAnchor = null;
let internalViewChange = false;
let internalViewTimer = 0;

const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[character]);

function distanceMeters(a, b) {
  const radians = value => value*Math.PI/180;
  const dLat = radians(b[1]-a[1]);
  const dLon = radians(b[0]-a[0]);
  const lat1 = radians(a[1]);
  const lat2 = radians(b[1]);
  const value = Math.sin(dLat/2)**2+Math.cos(lat1)*Math.cos(lat2)*Math.sin(dLon/2)**2;
  return 6371000*2*Math.atan2(Math.sqrt(value), Math.sqrt(1-value));
}

function bearingDegrees(a, b) {
  const radians = value => value*Math.PI/180;
  const degrees = value => value*180/Math.PI;
  const delta = radians(b[0]-a[0]);
  const lat1 = radians(a[1]);
  const lat2 = radians(b[1]);
  return (degrees(Math.atan2(Math.sin(delta)*Math.cos(lat2),
    Math.cos(lat1)*Math.sin(lat2)-Math.sin(lat1)*Math.cos(lat2)*Math.cos(delta)))+360)%360;
}

function smoothHeading(next) {
  if (!Number.isFinite(next)) return;
  if (vehicleHeading === null) vehicleHeading = next;
  else vehicleHeading = (vehicleHeading+(((next-vehicleHeading+540)%360)-180)*0.45+360)%360;
  updateVehicleVisual();
}

function setStatus(text, error = false) {
  statusEl.hidden = false;
  statusEl.textContent = text;
  statusEl.className = `status${error ? ' error' : ''}`;
}

function setPanelCollapsed(value) {
  panel.classList.toggle('collapsed', value);
  document.getElementById('panel-toggle').textContent = value ? '展开路线栏 ▼' : '收起路线栏 ▲';
  requestAnimationFrame(() => map.resize());
}
document.getElementById('panel-toggle').onclick = () => setPanelCollapsed(!panel.classList.contains('collapsed'));

function svgIcon(svg, width, height) {
  return new AMap.Icon({size: new AMap.Size(width, height), imageSize: new AMap.Size(width, height),
    image: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`});
}

function pinIcon(color) {
  return svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="36" height="46" viewBox="0 0 36 46"><defs><filter id="s"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-opacity=".4"/></filter></defs><path filter="url(#s)" fill="${color}" stroke="white" stroke-width="2.5" d="M18 1C8.6 1 2 7.8 2 17c0 12 16 28 16 28s16-16 16-28C34 7.8 27.4 1 18 1z"/><circle cx="18" cy="17" r="5" fill="white"/></svg>`, 36, 46);
}

function vehicleIcon() {
  return svgIcon('<svg xmlns="http://www.w3.org/2000/svg" width="46" height="46" viewBox="0 0 46 46"><defs><filter id="s"><feDropShadow dx="0" dy="3" stdDeviation="2" flood-opacity=".5"/></filter></defs><path filter="url(#s)" fill="#12b166" stroke="white" stroke-width="3.5" stroke-linejoin="round" d="M23 6 33.5 36 23 30.5 12.5 36Z"/></svg>', 46, 46);
}

function setPoint(mode, lon, lat, label = '') {
  const id = mode === 'start' ? 'start' : 'end';
  const point = [Number(lon), Number(lat)];
  if (mode === 'start') {
    startCoord = point;
    startMarker?.setMap(null);
    startMarker = new AMap.Marker({map, position: point, anchor: 'bottom-center', icon: pinIcon('#16a464'),
      zIndex: 130, draggable: false, clickable: false, noSelect: true, cursor: 'default'});
  } else {
    endCoord = point;
    endMarker?.setMap(null);
    endMarker = new AMap.Marker({map, position: point, anchor: 'bottom-center', icon: pinIcon('#7c4dff'),
      zIndex: 130, draggable: false, clickable: false, noSelect: true, cursor: 'default'});
  }
  document.getElementById(id).value = label || `${point[0].toFixed(6)},${point[1].toFixed(6)}`;
  document.getElementById(`${id}-selected`).textContent = '✓ 已选择坐标';
  document.getElementById(`${id}-suggestions`).classList.remove('open');
  if (mode === 'start') updateExitAvailability();
}

async function updateExitAvailability() {
  const serial = ++startAreaSerial;
  const button = document.getElementById('exit');
  button.disabled = true;
  button.title = startCoord ? '正在检查起点所在区域' : '请先选择起点';
  if (!startCoord) return;
  const [lon, lat] = startCoord;
  try {
    const response = await fetch(`/api/area-status?lon=${lon}&lat=${lat}`);
    if (!response.ok) throw new Error('区域检查失败');
    const area = await response.json();
    if (serial !== startAreaSerial) return;
    button.disabled = !area.inside;
    button.title = area.inside ? '从起点离开规避区域' : '起点已在规避区域外，无需一键出六环';
  } catch (_) {
    if (serial !== startAreaSerial) return;
    // The route endpoint still validates the area if this advisory request fails.
    button.disabled = false;
    button.title = '点击检查起点并计算出区路线';
  }
}

function updateVehicleVisual() {
  if (!currentMarker) return;
  const heading = vehicleHeading ?? 0;
  // Marker angle 是屏幕坐标角度；地图 rotation 是底图顺时针旋转角。
  // 只在航向或 rotation 变化时计算，平移/缩放绝不能改动箭头角度。
  currentMarker.setAngle((heading+Number(map.getRotation() || 0)+360)%360);
}

function updateViewButtons() {
  const follow = document.getElementById('recenter');
  const orientation = document.getElementById('orientation');
  if (!follow || !orientation) return;
  follow.querySelector('.control-label').textContent = following ? '跟随中' : '车位';
  follow.title = following ? '正在跟随车辆位置' : '回到车位并跟随';
  orientation.querySelector('.control-label').textContent = orientationMode === 'north' ? 'N朝上' : '车头朝上';
  orientation.title = orientationMode === 'north' ? '切换为车头朝上' : '切换为正北朝上';
  orientation.classList.toggle('head-up', orientationMode === 'heading');
}

function headingUpRotation() {
  return vehicleHeading === null ? 0 : (360-vehicleHeading)%360;
}

function selectedRotation() {
  return orientationMode === 'heading' ? headingUpRotation() : 0;
}

function changeView(action) {
  internalViewChange = true;
  window.clearTimeout(internalViewTimer);
  action();
  internalViewTimer = window.setTimeout(() => {
    internalViewChange = false;
  }, 80);
}

function updateMapOrientation() {
  recenterRotationAnchor = null;
  changeView(() => map.setRotation(selectedRotation(), true));
  updateVehicleVisual();
}

function applyFollowView(updateOrientation = true) {
  if (!following || !currentCoord) return;
  changeView(() => {
    map.setCenter(currentCoord, true);
    if (updateOrientation) map.setRotation(selectedRotation(), true);
  });
  if (updateOrientation) updateVehicleVisual();
}

function stopFollowing(restoreZoom = true) {
  if (!following) return;
  following = false;
  if (restoreZoom && junctionZoomed && followBaseZoom !== null) {
    changeView(() => map.setZoom(followBaseZoom));
  }
  junctionZoomed = false;
  followBaseZoom = null;
  updateViewButtons();
}

function updateJunctionZoom() {
  if (!following || !currentCoord || !lastRoute?.maneuvers?.length) return;
  const nearest = Math.min(...lastRoute.maneuvers.map(item => distanceMeters(currentCoord, item.coordinate)));
  if (!junctionZoomed && nearest <= 180) {
    followBaseZoom = followBaseZoom ?? map.getZoom();
    junctionZoomed = true;
    changeView(() => map.setZoom(Math.max(17, map.getZoom())));
  } else if (junctionZoomed && nearest >= 350) {
    changeView(() => map.setZoom(followBaseZoom ?? 15));
    junctionZoomed = false;
  }
}

function acceptLocation(result) {
  const position = result?.position;
  if (!position) return false;
  const lon = Number(position.lng ?? position.getLng?.() ?? position[0]);
  const lat = Number(position.lat ?? position.getLat?.() ?? position[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return false;
  const sourceTimestamp = Number(result.timestamp ?? result.coords?.timestamp);
  if (Number.isFinite(sourceTimestamp) && sourceTimestamp > 0 &&
      (sourceTimestamp <= lastLocationSourceTimestamp || Date.now()-sourceTimestamp > 15000)) {
    recordLocationEvent('stale-result', {sourceTimestamp});
    return false;
  }
  if (Number.isFinite(sourceTimestamp) && sourceTimestamp > 0) {
    lastLocationSourceTimestamp = Math.max(lastLocationSourceTimestamp, sourceTimestamp);
  }
  lastLocationFixAt = Number.isFinite(sourceTimestamp) && sourceTimestamp > 0
    ? Math.min(Date.now(), sourceTimestamp) : Date.now();
  locationFixSerial += 1;
  recordLocationEvent('fix', {sourceTimestamp: sourceTimestamp || null,
    accuracy: result.accuracy ?? null, source: result.location_type ?? 'unknown'});
  updateLocationHealth();
  const point = [lon, lat];
  const now = Date.now();
  const rawHeading = result.heading ?? result.coords?.heading;
  const reported = rawHeading == null ? NaN : Number(rawHeading);
  if (Number.isFinite(reported) && reported >= 0) smoothHeading(reported%360);
  else if (headingAnchor) {
    const moved = distanceMeters(headingAnchor.point, point);
    if (moved >= 4 && moved <= 250 && now-headingAnchor.time <= 30000) smoothHeading(bearingDegrees(headingAnchor.point, point));
    if (moved >= 4 || now-headingAnchor.time > 30000) headingAnchor = {point, time: now};
  } else headingAnchor = {point, time: now};
  currentCoord = point;
  if (!currentMarker) {
    currentPulseMarker = new AMap.Marker({map, position: point, anchor: 'center',
      content: '<div class="current-position-pulse"></div>', zIndex: 139,
      draggable: false, clickable: false, bubble: false, noSelect: true, cursor: 'default'});
    currentMarker = new AMap.Marker({map, position: point, anchor: 'center', icon: vehicleIcon(),
      title: '当前位置及车头方向', zIndex: 140, angle: 0,
      draggable: false, clickable: false, bubble: false, noSelect: true, cursor: 'default'});
  } else {
    currentMarker.setPosition(point);
    currentPulseMarker?.setPosition(point);
  }
  updateVehicleVisual();
  if (recenterRotationAnchor && distanceMeters(recenterRotationAnchor, point) >= 8) {
    recenterRotationAnchor = null;
  }
  applyFollowView(!recenterRotationAnchor);
  updateJunctionZoom();
  return true;
}

function createGeolocation() {
  return new AMap.Geolocation({enableHighAccuracy: true, maximumAge: 0, timeout: 10000, convert: true,
    GeoLocationFirst: true, noIpLocate: 3, getCityWhenFail: false, needAddress: false,
    showButton: false, showMarker: false, showCircle: false, panToLocation: false, zoomToAccuracy: false});
}

function ensureGeolocation() {
  if (geolocationReady) return geolocationReady;
  geolocationReady = new Promise((resolve, reject) => {
    let finished = false;
    const timer = window.setTimeout(() => {
      finished = true;
      reject(new Error('定位插件加载超时'));
    }, 12000);
    AMap.plugin('AMap.Geolocation', () => {
      if (finished) return;
      finished = true;
      window.clearTimeout(timer);
      try {
        amapPollGeolocation = createGeolocation();
        resolve();
      } catch (error) { reject(error); }
    });
  }).catch(error => { geolocationReady = null; throw error; });
  return geolocationReady;
}

async function locate() {
  await ensureGeolocation();
  if (locationPollPromise) return locationPollPromise;
  const serialAtStart = locationFixSerial;
  const started = Date.now();
  const request = new Promise((resolve, reject) => {
    let settled = false;
    const finish = error => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      recordLocationEvent(error ? 'poll-error' : 'poll-complete', {
        elapsedMs: Date.now()-started, message: error?.message ?? ''});
      if (error) reject(error); else resolve(currentCoord);
    };
    const timer = window.setTimeout(() => {
      amapPollGeolocation = createGeolocation();
      finish(new Error('定位补采超时'));
    }, 12000);
    cancelLocationPoll = () => finish(new Error('定位补采已取消'));
    try {
      amapPollGeolocation.getCurrentPosition((status, result) => {
        if (settled) return;
        if (status !== 'complete') {
          finish(new Error(result?.message || result?.info || '无法获取当前位置'));
          return;
        }
        try {
          const sourceTimestamp = Number(result.timestamp ?? result.coords?.timestamp);
          const newerThanWatch = Number.isFinite(sourceTimestamp) && sourceTimestamp > lastLocationSourceTimestamp;
          const accepted = (locationFixSerial === serialAtStart || newerThanWatch) && acceptLocation(result);
          finish(accepted || locationFixSerial > serialAtStart ? null : new Error('未取得新的定位结果'));
        } catch (error) { finish(error); }
      });
    } catch (error) { finish(error); }
  });
  locationPollPromise = request;
  try {
    return await request;
  } finally {
    if (locationPollPromise === request) {
      locationPollPromise = null;
      cancelLocationPoll = null;
    }
  }
}

function stopPositionWatch() {
  positionWatchGeneration += 1;
  positionWatchCleanup?.();
  positionWatchCleanup = null;
  if (positionWatchId != null) {
    try { amapGeolocation?.clearWatch(positionWatchId); } catch (_) {}
  }
  positionWatchId = null;
}

async function startPositionWatch() {
  if (positionWatchStarting || document.hidden) return;
  positionWatchStarting = true;
  lastWatchStartedAt = Date.now();
  try {
    await ensureGeolocation();
    if (document.hidden) return;
    stopPositionWatch();
    const generation = positionWatchGeneration;
    const geolocation = amapGeolocation = createGeolocation();
    const complete = result => {
      if (generation !== positionWatchGeneration) return;
      if (acceptLocation(result)) lastWatchFixAt = Date.now();
    };
    const error = result => {
      if (generation === positionWatchGeneration) recordLocationEvent('watch-error', {
        message: result?.message || result?.info || 'unknown'});
    };
    if (typeof geolocation.watchPosition !== 'function') {
      recordLocationEvent('watch-unavailable');
      return;
    }
    geolocation.on('complete', complete);
    geolocation.on('error', error);
    positionWatchCleanup = () => {
      geolocation.off('complete', complete);
      geolocation.off('error', error);
    };
    positionWatchId = geolocation.watchPosition();
    recordLocationEvent('watch-start');
  } catch (error) { recordLocationEvent('watch-error', {message: error.message}); }
  finally { positionWatchStarting = false; }
}

window.setInterval(() => {
  if (document.hidden) return;
  updateLocationHealth();
  if (Date.now()-Math.max(lastWatchFixAt, lastWatchStartedAt) >= 30000) startPositionWatch();
  if (!lastLocationFixAt || Date.now()-lastLocationFixAt > 1500) {
    locate().catch(error => recordLocationEvent('recovery-error', {message: error.message}));
  }
}, 1000);

async function resumePosition() {
  if (document.hidden) return;
  recordLocationEvent('resume');
  cancelLocationPoll?.();
  if (locationPollPromise) await locationPollPromise.catch(() => {});
  startPositionWatch();
  locate().catch(error => recordLocationEvent('resume-error', {message: error.message}));
}
document.addEventListener('visibilitychange', () => {
  recordLocationEvent('visibility');
  if (document.hidden) { stopPositionWatch(); cancelLocationPoll?.(); }
  else resumePosition();
});
window.addEventListener('pageshow', resumePosition);
window.addEventListener('online', resumePosition);
window.addEventListener('pagehide', () => { stopPositionWatch(); cancelLocationPoll?.(); });

function useCurrent(mode) {
  (currentCoord ? Promise.resolve(currentCoord) : locate())
    .then(point => setPoint(mode, ...point, '当前位置'))
    .catch(error => setStatus(error.message, true));
}
document.getElementById('use-current-start').onclick = () => useCurrent('start');
document.getElementById('use-current-end').onclick = () => useCurrent('end');

const controls = document.createElement('div');
controls.className = 'amap-local-controls';
controls.innerHTML = '<button id="zoom-in" title="放大">＋</button><button id="zoom-out" title="缩小">−</button><button id="recenter" class="follow-control"><span class="control-icon">⌖</span><span class="control-label">车位</span></button><button id="orientation" class="orientation-control"><span class="control-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path class="compass-north" d="M12 4 15.3 12 12 10.3 8.7 12Z"/><path class="compass-south" d="M12 20 8.7 12 12 13.7 15.3 12Z"/><path d="M12 1v2M12 21v2M1 12h2M21 12h2"/></svg></span><span class="control-label">N朝上</span></button>';
document.getElementById('app').append(controls);
const displayControls = document.createElement('div');
displayControls.className = 'map-display-controls';
displayControls.innerHTML = '<div class="map-display-option single"><span class="left" id="camera-layer-label">隐藏摄像头</span><button class="toggle-switch" id="toggle-cameras" type="button" role="switch"></button></div><div class="map-display-option"><span class="left">详细地图</span><button class="toggle-switch" id="toggle-map-detail" type="button" role="switch"></button><span class="right">简洁地图</span></div>';
document.getElementById('app').append(displayControls);

function updateDisplayControls() {
  const cameraButton = document.getElementById('toggle-cameras');
  const detailButton = document.getElementById('toggle-map-detail');
  document.getElementById('camera-layer-label').textContent = camerasVisible ? '显示摄像头' : '隐藏摄像头';
  cameraButton.classList.toggle('on', camerasVisible);
  cameraButton.setAttribute('aria-checked', String(camerasVisible));
  cameraButton.title = camerasVisible ? '隐藏地图上的摄像头' : '显示地图上的摄像头';
  detailButton.classList.toggle('on', simpleMap);
  detailButton.setAttribute('aria-checked', String(simpleMap));
  detailButton.title = simpleMap ? '显示兴趣点和建筑物' : '隐藏兴趣点和建筑物';
}

function applyMapDetail() {
  map.setFeatures(simpleMap ? ['bg', 'road'] : ['bg', 'point', 'road', 'building']);
}

document.getElementById('toggle-cameras').onclick = event => {
  event.stopPropagation();
  camerasVisible = !camerasVisible;
  writePreference('tracert-camera-layer', camerasVisible ? '1' : '0');
  if (!camerasVisible && cameraEdit) finishCameraCorrection(false);
  if (camerasVisible) loadCameras().catch(error => setStatus(`摄像头加载失败：${error.message}`, true));
  else destroyCameraMass();
  closeInfo();
  updateDisplayControls();
};
document.getElementById('toggle-map-detail').onclick = event => {
  event.stopPropagation();
  simpleMap = !simpleMap;
  writePreference('tracert-simple-map', simpleMap ? '1' : '0');
  applyMapDetail();
  updateDisplayControls();
};
applyMapDetail();
updateDisplayControls();
document.getElementById('zoom-in').onclick = () => map.zoomIn();
document.getElementById('zoom-out').onclick = () => map.zoomOut();
document.getElementById('recenter').onclick = async () => {
  try {
    if (!currentCoord) await locate();
    if (!following) followBaseZoom = map.getZoom();
    following = true;
    recenterRotationAnchor = currentCoord && [...currentCoord];
    updateViewButtons();
    applyFollowView(false);
    updateJunctionZoom();
  } catch (error) { following = false; updateViewButtons(); setStatus(error.message, true); }
};
document.getElementById('orientation').onclick = () => {
  orientationMode = orientationMode === 'north' ? 'heading' : 'north';
  updateViewButtons();
  updateMapOrientation();
};
updateViewButtons();
const mapElement = document.getElementById('map');

fetch('/api/update-status').then(response => response.json()).then(status => {
  const box = document.getElementById('camera-updated-at');
  const formatted = status.cameras_updated_at ? new Date(status.cameras_updated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '未知';
  box.textContent = `摄像头数据更新时间：${formatted}`;
}).catch(() => { document.getElementById('camera-updated-at').textContent = '摄像头数据更新时间：未知'; });

function closeInfo() { infoWindow?.close(); infoWindow = null; }

function openInfo(content, point, approximateHeight = 150) {
  closeInfo();
  const pixel = map.lngLatToContainer(point);
  const size = map.getSize();
  const width = Number(size.width ?? size.getWidth?.() ?? 0);
  const below = Number(pixel.y) < approximateHeight + 18;
  const horizontal = Number(pixel.x) < 105 ? 'left' :
    (Number(pixel.x) > width-105 ? 'right' : 'center');
  const anchor = `${below ? 'top' : 'bottom'}-${horizontal}`;
  const offsetX = horizontal === 'left' ? 8 : (horizontal === 'right' ? -8 : 0);
  infoWindow = new AMap.InfoWindow({content, isCustom: true, autoMove: false, anchor,
    offset: new AMap.Pixel(offsetX, below ? 12 : -12)});
  infoWindow.open(map, point);
  return infoWindow;
}

function showMenu(point) {
  const box = document.createElement('div');
  box.className = 'point-menu';
  box.innerHTML = `<small>${point[0].toFixed(6)}, ${point[1].toFixed(6)}</small><button>设为起点</button><button>设为终点</button><button>手动规避</button>`;
  const buttons = box.querySelectorAll('button');
  buttons[0].onclick = () => { setPoint('start', ...point); closeInfo(); };
  buttons[1].onclick = () => { setPoint('end', ...point); closeInfo(); };
  buttons[2].onclick = () => { manualAvoid.push(point); drawManual(); closeInfo(); };
  openInfo(box, point, 170);
}
map.on('click', event => {
  stopFollowing();
  if (cameraEdit) return;
  if (infoWindow) closeInfo(); else showMenu([event.lnglat.lng, event.lnglat.lat]);
});
const stopFollowingForMapGesture = () => {
  if (!internalViewChange) stopFollowing();
};
map.on('dragstart', stopFollowingForMapGesture);
map.on('zoomend', () => {
  if (!following || internalViewChange) return;
  followBaseZoom = map.getZoom();
  junctionZoomed = false;
  applyFollowView(false);
});
map.on('rotatestart', stopFollowingForMapGesture);
map.on('rotatechange', updateVehicleVisual);

function drawManual() {
  manualMarkers.forEach(marker => marker.setMap(null));
  manualMarkers = manualAvoid.map((point, index) => {
    const marker = new AMap.CircleMarker({map, center: point, radius: 8.5, zIndex: 125,
      strokeColor: '#fff', strokeWeight: 3, strokeOpacity: 1,
      fillColor: '#ff9800', fillOpacity: 1, bubble: false});
    marker.on('click', event => {
      event.originEvent?.stopPropagation();
      const box = document.createElement('div'); box.className = 'point-menu';
      box.innerHTML = `<small>手动规避点 ${index+1} · 30米</small><button>删除此规避点</button>`;
      box.querySelector('button').onclick = () => { manualAvoid.splice(index, 1); drawManual(); closeInfo(); };
      openInfo(box, point, 76);
    });
    return marker;
  });
}

function cameraMassItems(excludedId = null) {
  return cameraData
    .filter(camera => camera.id !== excludedId)
    .map(camera => ({lnglat: [camera.lon, camera.lat], ...camera}));
}

function destroyCameraMass() {
  cameraMass?.setMap(null);
  cameraMass = null;
}

function renderCameraMass(excludedId = cameraEdit?.camera.id ?? null) {
  destroyCameraMass();
  if (!camerasVisible || !cameraDataLoaded) return;
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18"><circle cx="9" cy="9" r="7" fill="#e5484d" stroke="white" stroke-width="2"/></svg>';
  const mass = new AMap.MassMarks(cameraMassItems(excludedId), {
    zIndex: 110, zoom: 12, style: {url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`, anchor: new AMap.Pixel(9, 9), size: new AMap.Size(18, 18)}});
  mass.on('click', event => {
    event.originEvent?.stopPropagation();
    if (cameraEdit) return;
    showCameraInfo(event.data);
  });
  mass.setMap(map);
  cameraMass = mass;
}

function setCameraMassData(excludedId = null) {
  if (cameraMass && typeof cameraMass.setData === 'function') {
    cameraMass.setData(cameraMassItems(excludedId));
  } else if (camerasVisible) {
    renderCameraMass(excludedId);
  }
}

function finishCameraCorrection(restoreCamera = true) {
  if (!cameraEdit) return;
  cameraEdit.stopDragging?.();
  cameraEdit.cleanupView?.();
  cameraEdit.point.setMap(null);
  cameraEdit.handle.setMap(null);
  cameraEdit.line.setMap(null);
  cameraEdit.actions.setMap(null);
  cameraEdit = null;
  if (restoreCamera) setCameraMassData();
  closeInfo();
}

function showCameraInfo(camera) {
  const point = [camera.lon, camera.lat];
  const box = document.createElement('div');
  box.className = 'point-menu camera-menu';
  box.innerHTML = `<small><b>${escapeHtml(camera.name)}</b><br>${camera.lon.toFixed(6)}, ${camera.lat.toFixed(6)}<br>规避半径：30米${camera.corrected ? '<br><em>已使用本地修正位置</em>' : ''}</small><button>修正位置</button>`;
  box.querySelector('button').onclick = event => {
    event.preventDefault();
    event.stopPropagation();
    closeInfo();
    beginCameraCorrection(camera);
  };
  openInfo(box, point, camera.corrected ? 150 : 130);
}

function beginCameraCorrection(camera) {
  finishCameraCorrection();
  closeInfo();
  stopFollowing();
  const origin = [camera.lon, camera.lat];
  const pixel = map.lngLatToContainer(origin);
  const size = map.getSize();
  setCameraMassData(camera.id);

  const pointContent = document.createElement('div');
  pointContent.className = 'camera-correction-edit-point';
  const point = new AMap.Marker({map, position: origin, anchor: 'center', content: pointContent,
    draggable: false, clickable: false, bubble: false, cursor: 'default', zIndex: 200});
  const pedalOffset = 92;
  const initialHandle = map.containerToLngLat(new AMap.Pixel(Number(pixel.x), Number(pixel.y)+pedalOffset));
  const line = new AMap.Polyline({map, path: [origin, initialHandle], strokeColor: '#e2a500',
    strokeWeight: 7, strokeOpacity: 1, isOutline: true, outlineColor: '#fff', borderWeight: 2,
    lineCap: 'round', zIndex: 198});
  const handleContent = document.createElement('div');
  handleContent.className = 'camera-correction-pedal';
  handleContent.innerHTML = '<span class="pedal-grip"></span><b>按住拖动</b>';
  const handle = new AMap.Marker({map, position: initialHandle, anchor: 'center', content: handleContent,
    draggable: false, clickable: true, bubble: false, cursor: 'grab', zIndex: 200});
  const actionContent = document.createElement('div');
  actionContent.className = 'camera-correction-actions';
  actionContent.innerHTML = '<button class="lock-correction"><span class="lock-icon">&#128274;</span>锁定修正</button><button class="cancel-correction" title="取消修正">×</button>';
  const rightSide = Number(pixel.x) < Number(size.width ?? size.getWidth?.() ?? 0)-190;
  const actions = new AMap.Marker({map, position: origin,
    anchor: rightSide ? 'middle-left' : 'middle-right',
    offset: new AMap.Pixel(rightSide ? 18 : -18, 0), content: actionContent,
    draggable: false, clickable: true, bubble: false, zIndex: 201});
  cameraEdit = {camera, point, handle, line, actions};

  const layoutHandle = () => {
    const candidate = point.getPosition();
    const candidatePixel = map.lngLatToContainer(candidate);
    const handlePosition = map.containerToLngLat(new AMap.Pixel(
      Number(candidatePixel.x), Number(candidatePixel.y)+pedalOffset));
    handle.setPosition(handlePosition);
    line.setPath([candidate, handlePosition]);
    actions.setPosition(candidate);
  };
  map.on('zoomchange', layoutHandle);
  map.on('rotatechange', layoutHandle);
  cameraEdit.cleanupView = () => {
    map.off('zoomchange', layoutHandle);
    map.off('rotatechange', layoutHandle);
  };

  let activePointer = null;
  let dragOrigin = null;
  const movePedal = event => {
    if (event.pointerId !== activePointer || !dragOrigin) return;
    event.preventDefault();
    event.stopPropagation();
    const candidatePixel = new AMap.Pixel(
      dragOrigin.pointX+event.clientX-dragOrigin.clientX,
      dragOrigin.pointY+event.clientY-dragOrigin.clientY);
    point.setPosition(map.containerToLngLat(candidatePixel));
    layoutHandle();
  };
  const stopDragging = event => {
    if (event && activePointer !== null && event.pointerId !== activePointer) return;
    if (activePointer === null) return;
    activePointer = null;
    dragOrigin = null;
    handleContent.classList.remove('dragging');
    map.setStatus({dragEnable: true, rotateEnable: true});
    window.removeEventListener('pointermove', movePedal, true);
    window.removeEventListener('pointerup', stopDragging, true);
    window.removeEventListener('pointercancel', stopDragging, true);
  };
  cameraEdit.stopDragging = stopDragging;
  handleContent.addEventListener('pointerdown', event => {
    if (activePointer !== null) return;
    event.preventDefault();
    event.stopPropagation();
    const candidatePixel = map.lngLatToContainer(point.getPosition());
    activePointer = event.pointerId;
    dragOrigin = {clientX: event.clientX, clientY: event.clientY,
      pointX: Number(candidatePixel.x), pointY: Number(candidatePixel.y)};
    handleContent.classList.add('dragging');
    handleContent.setPointerCapture?.(event.pointerId);
    map.setStatus({dragEnable: false, rotateEnable: false});
    window.addEventListener('pointermove', movePedal, {capture: true, passive: false});
    window.addEventListener('pointerup', stopDragging, true);
    window.addEventListener('pointercancel', stopDragging, true);
  });

  actionContent.querySelector('.cancel-correction').onclick = event => { event.stopPropagation(); finishCameraCorrection(); };
  actionContent.querySelector('.lock-correction').onclick = async event => {
    event.stopPropagation();
    const button = event.currentTarget;
    const position = point.getPosition();
    button.disabled = true;
    button.innerHTML = '<span class="lock-icon">&#128274;</span>正在保存…';
    try {
      const response = await fetch(`/api/cameras/${encodeURIComponent(camera.id)}/correction`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lon: position.lng, lat: position.lat}),
      });
      const updated = await response.json();
      if (!response.ok) throw new Error(updated.error || '修正保存失败');
      finishCameraCorrection(false);
      await loadCameras({force: true});
      showCameraInfo(updated);
    } catch (error) {
      button.disabled = false;
      button.innerHTML = '<span class="lock-icon">&#128274;</span>锁定修正';
      setStatus(error.message, true);
    }
  };
}

async function loadCameras({force = false} = {}) {
  if (!cameraDataLoaded || force) {
    const response = await fetch('/api/cameras');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    cameraData = await response.json();
    cameraDataLoaded = true;
  }
  renderCameraMass();
}

function setupSearch(id, mode) {
  const input = document.getElementById(id); const box = document.getElementById(`${id}-suggestions`);
  let timer; let serial = 0;
  input.oninput = () => {
    if (mode === 'start') startCoord = null; else endCoord = null;
    if (mode === 'start') updateExitAvailability();
    document.getElementById(`${id}-selected`).textContent = ''; clearTimeout(timer);
    const query = input.value.trim(); if (query.length < 2) { box.classList.remove('open'); return; }
    const current = ++serial; box.innerHTML = '<div class="suggestion">正在查询…</div>'; box.classList.add('open');
    timer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`); const data = await response.json();
        if (current !== serial) return; if (!response.ok) throw new Error(data.error || '查询失败');
        box.innerHTML = data.length ? data.map((point, index) => `<div class="suggestion" data-i="${index}"><b>${escapeHtml(point.name.split('，')[0])}</b><small>${escapeHtml(point.name)}<br>${point.lon.toFixed(6)}, ${point.lat.toFixed(6)}</small></div>`).join('') : '<div class="suggestion">没有找到匹配位置</div>';
        box.querySelectorAll('[data-i]').forEach(item => item.onclick = () => {
          const point = data[Number(item.dataset.i)]; setPoint(mode, point.lon, point.lat, point.name.split('，')[0]);
          map.setZoomAndCenter(15, [point.lon, point.lat]);
        });
      } catch (error) { box.innerHTML = `<div class="suggestion">${escapeHtml(error.message)}</div>`; }
    }, 350);
  };
}
setupSearch('start', 'start'); setupSearch('end', 'end');
document.addEventListener('click', event => { if (!event.target.closest('.search-box')) document.querySelectorAll('.suggestions').forEach(box => box.classList.remove('open')); });

function clearPoint(mode) {
  const id = mode === 'start' ? 'start' : 'end';
  if (mode === 'start') { startCoord = null; startMarker?.setMap(null); startMarker = null; }
  else { endCoord = null; endMarker?.setMap(null); endMarker = null; }
  document.getElementById(id).value = ''; document.getElementById(`${id}-selected`).textContent = '';
  if (mode === 'start') updateExitAvailability();
}
document.getElementById('swap-points').onclick = () => {
  const start = {point: startCoord && [...startCoord], label: document.getElementById('start').value};
  const end = {point: endCoord && [...endCoord], label: document.getElementById('end').value};
  if (end.point) setPoint('start', ...end.point, end.label); else clearPoint('start');
  if (start.point) setPoint('end', ...start.point, start.label); else clearPoint('end');
};

function clearCalculatedRoute() {
  [routeLine, connectorLine, gatewayMarker].forEach(item => item?.setMap(null));
  routeLine = connectorLine = gatewayMarker = null; lastRoute = null;
  statusEl.hidden = true;
  document.getElementById('result-modal').classList.remove('open');
}

function abortActiveCalculation() {
  activeRouteController?.abort();
  activeRouteController = null;
  panel.classList.remove('loading');
}

function showResult(data, elapsed) {
  document.getElementById('result-message').textContent = data.notice;
  const metrics = document.getElementById('result-metrics');
  metrics.hidden = !data.coordinates?.length;
  if (!metrics.hidden) {
    document.getElementById('result-distance').textContent = `${(data.distance_m/1000).toFixed(2)} km`;
    document.getElementById('result-duration').textContent = `${Math.round(data.duration_s/60)} 分钟`;
    document.getElementById('result-query').textContent = `${data.amap_api_calls ?? 0} 次`;
    document.getElementById('result-elapsed').textContent = `${elapsed.toFixed(1)} 秒`;
  }
  const transition = data.area_transition;
  const info = document.getElementById('gateway-info'); const copy = document.getElementById('copy-gateway');
  info.hidden = !transition; copy.hidden = !transition;
  if (transition) {
    document.getElementById('gateway-label').textContent = transition.gateway_label || '规避区域外安全点';
    document.getElementById('gateway-coordinate').textContent = `GCJ-02：${transition.gateway[0].toFixed(6)}, ${transition.gateway[1].toFixed(6)}`;
    copy.onclick = async () => {
      const text = `${transition.gateway_label || '规避区域外安全点'} ${transition.gateway[0].toFixed(6)},${transition.gateway[1].toFixed(6)}`;
      await navigator.clipboard.writeText(text); copy.textContent = '已复制'; setTimeout(() => copy.textContent = '复制接驳点', 1500);
    };
  }
  document.getElementById('result-modal').classList.add('open');
}

async function calculate(mode) {
  abortActiveCalculation();
  clearCalculatedRoute();
  const controller = new AbortController();
  activeRouteController = controller;
  panel.classList.add('loading'); setStatus('正在使用高德在线路网计算并校验规避点…');
  const started = performance.now();
  try {
    if (!startCoord) throw new Error('请选择起点坐标');
    if (mode === 'point' && !endCoord) throw new Error('请选择终点坐标');
    await new Promise(resolve => requestAnimationFrame(resolve));
    const response = await fetch('/api/route', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start: startCoord, end: mode === 'point' ? endCoord : null, mode,
        avoid_points: manualAvoid, start_label: document.getElementById('start').value.trim(),
        end_label: mode === 'point' ? document.getElementById('end').value.trim() : ''}), signal: controller.signal});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || '算路失败');
    lastRoute = data; const overlays = [];
    if (mode === 'outside' && data.coordinates?.length) {
      const endpoint = data.coordinates[data.coordinates.length-1]; setPoint('end', ...endpoint, '规避区域外安全点');
    }
    if (data.coordinates?.length) {
      routeLine = new AMap.Polyline({map, path: data.coordinates, strokeColor: '#1677ff', strokeWeight: 7,
        strokeOpacity: .95, lineJoin: 'round', showDir: true, zIndex: 120}); overlays.push(routeLine);
    }
    if (data.area_transition) {
      connectorLine = new AMap.Polyline({map, path: data.area_transition.connector, strokeColor: '#16a464',
        strokeWeight: 8, strokeOpacity: .8, strokeStyle: 'dashed', strokeDasharray: [16, 12], zIndex: 115});
      gatewayMarker = new AMap.CircleMarker({map, center: data.area_transition.gateway, radius: 13,
        strokeColor: '#fff', strokeWeight: 4, strokeOpacity: 1,
        fillColor: '#15a263', fillOpacity: 1, zIndex: 132});
      overlays.push(connectorLine, gatewayMarker);
    }
    statusEl.hidden = true;
    showResult(data, (performance.now()-started)/1000);
    if (overlays.length) requestAnimationFrame(() => { map.resize(); map.setFitView(overlays, false, [45, 45, 45, 45]); });
  } catch (error) {
    if (error.name !== 'AbortError') setStatus(error.message, true);
  } finally {
    if (activeRouteController === controller) {
      activeRouteController = null;
      panel.classList.remove('loading');
    }
  }
}
document.getElementById('route').onclick = () => calculate('point');
document.getElementById('exit').onclick = () => calculate('outside');
document.getElementById('result-close').onclick = () => document.getElementById('result-modal').classList.remove('open');
document.getElementById('result-modal').onclick = event => { if (event.target.id === 'result-modal') event.currentTarget.classList.remove('open'); };
document.getElementById('clear').onclick = () => {
  abortActiveCalculation();
  startMarker?.setMap(null); endMarker?.setMap(null); clearCalculatedRoute();
  startMarker = endMarker = null; startCoord = endCoord = null; manualAvoid = []; drawManual();
  ['start', 'end'].forEach(clearPoint); if (currentCoord) useCurrent('start');
};

AMap.plugin('AMap.MassMarks', () => {
  if (camerasVisible) loadCameras().catch(error => setStatus(`摄像头加载失败：${error.message}`, true));
});
updateExitAvailability();
locate().then(() => {
  useCurrent('start');
  followBaseZoom = map.getZoom();
  following = true;
  recenterRotationAnchor = currentCoord && [...currentCoord];
  updateViewButtons();
  applyFollowView(false);
}).catch(() => { document.getElementById('start').placeholder = '输入地名或点按地图设置起点'; });
startPositionWatch().catch(() => {});
