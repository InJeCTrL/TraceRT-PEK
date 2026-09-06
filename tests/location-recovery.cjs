const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/amap-app.js', 'utf8');
const timers = new Map();
const instances = [];
let timerId = 0;
class Geolocation {
  constructor() { this.events = {}; instances.push(this); }
  getCurrentPosition(callback) { this.callback = callback; }
  on(name, fn) { this.events[name] = fn; }
  off(name) { delete this.events[name]; }
  watchPosition() { return 42; }
  clearWatch(id) { this.cleared = id; }
}
const context = vm.createContext({
  console, Date, Number, Math, Promise, Error,
  document: {hidden: false, addEventListener() {}},
  window: {
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() {}, addEventListener() {},
  },
  AMap: {Geolocation, plugin(name, callback) { callback(); }},
});
vm.runInContext(`
  let geolocationReady, amapGeolocation, amapPollGeolocation, locationPollPromise;
  let cancelLocationPoll, positionWatchId, positionWatchCleanup;
  let positionWatchGeneration = 0, positionWatchStarting = false;
  let lastWatchStartedAt = 0, lastWatchFixAt = 0;
  let locationFixSerial = 0, lastLocationSourceTimestamp = 0, lastLocationFixAt = 0;
  let currentCoord = null;
  function recordLocationEvent() {}
  function updateLocationHealth() {}
  function acceptLocation(result) {
    locationFixSerial++; currentCoord = result.position; return true;
  }
`, context);
vm.runInContext(source.slice(source.indexOf('function createGeolocation()'),
  source.indexOf('function useCurrent(')), context);
const run = code => vm.runInContext(code, context);
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
(async () => {
  const first = run('locate()');
  const failed = assert.rejects(first, /超时/);
  await tick();
  const hung = instances[0];
  const same = run('locate()');
  const sameFailed = assert.rejects(same, /超时/);
  await tick();
  assert.equal(instances.length, 1, 'one in-flight poll');
  for (const fn of [...timers.values()]) fn();
  await failed; await sameFailed;
  assert.equal(run('locationPollPromise'), null);
  hung.callback('complete', {position: [1, 2]});
  assert.equal(run('locationFixSerial'), 0, 'ignore late timeout result');
  const second = run('locate()');
  await tick();
  instances.at(-1).callback('complete', {position: [3, 4]});
  assert.deepEqual(await second, [3, 4]);
  await run('startPositionWatch()');
  const oldWatch = instances.at(-1);
  const oldCallback = oldWatch.events.complete;
  await run('startPositionWatch()');
  assert.equal(oldWatch.cleared, 42);
  const serial = run('locationFixSerial');
  oldCallback({position: [8, 9]});
  assert.equal(run('locationFixSerial'), serial, 'ignore obsolete watch');
  const pending = run('locate()');
  const cancelled = assert.rejects(pending, /取消/);
  await tick();
  await run('resumePosition()');
  await cancelled; await tick();
  instances.findLast(item => item.callback).callback('complete', {position: [5, 6]});
  await tick();
  assert.equal(run('locationPollPromise'), null, 'foreground recovery finishes');
  const validation = source.slice(source.indexOf('function acceptLocation(result)'),
    source.indexOf('  const point = [lon, lat];', source.indexOf('function acceptLocation(result)')));
  vm.runInContext(`${validation}\n return true; }`, context);
  run('lastLocationSourceTimestamp = 0');
  assert.equal(run('acceptLocation({position:[116,40], timestamp:Date.now()-60000})'), false);
  run('const fixTime = Date.now()');
  assert.equal(run('acceptLocation({position:[116,40], timestamp:fixTime})'), true);
  assert.equal(run('acceptLocation({position:[116,40], timestamp:fixTime})'), false);
  console.log('PASS: single flight, hard timeout, late callback, watch replacement, foreground recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
