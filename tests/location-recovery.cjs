const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/amap-app.js', 'utf8');
const timers = new Map();
const instances = [];
let timerId = 0;
const geolocation = {
  getCurrentPosition(success, error) {
    instances.push({callback: (status, result) => status === 'complete' ? success(result) : error(result)});
  },
  watchPosition(success) {
    instances.push({events: {complete: success}});
    return instances.length;
  },
  clearWatch(id) { instances[id-1].cleared = id; },
};
const context = vm.createContext({
  console, Date, Number, Math, Promise, Error,
  document: {hidden: false, addEventListener() {}},
  window: {
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() {}, addEventListener() {},
  },
  navigator: {geolocation},
});
vm.runInContext(`
  let geolocationReady, amapGeolocation, amapPollGeolocation, locationPollPromise;
  let cancelLocationPoll, positionWatchId, positionWatchCleanup;
  let positionWatchGeneration = 0, positionWatchStarting = false;
  let lastWatchStartedAt = 0, lastWatchFixAt = 0;
  let locationFixSerial = 0, lastLocationSourceTimestamp = 0, lastLocationFixAt = 0;
  let currentCoord = null;
  let pendingLocationRender = null;
  const locationFilter = {suspend() {}};
  const locationHealth = {};
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
  assert.equal(oldWatch.cleared, undefined, 'duplicate starts preserve active watch');
  run('stopPositionWatch()');
  await run('startPositionWatch()');
  assert.equal(oldWatch.cleared, instances.indexOf(oldWatch)+1);
  const serial = run('locationFixSerial');
  oldCallback({position: [8, 9]});
  assert.equal(run('locationFixSerial'), serial, 'ignore obsolete watch');
  const pending = run('locate()');
  await tick();
  await run('resumePosition()');
  await tick();
  instances.findLast(item => item.callback).callback('complete', {position: [5, 6]});
  await pending;
  await tick();
  assert.equal(run('locationPollPromise'), null, 'foreground recovery finishes');
  run('currentCoord = null; acceptLocation = () => { locationFixSerial++; return true; }');
  const initial = run('locate()');
  await tick();
  instances.findLast(item => item.callback).callback('complete', {position: [116, 40]});
  assert.equal(run('currentCoord'), null, 'raw coordinates do not become map coordinates');
  assert.equal(run('typeof pendingLocationRender'), 'function');
  run('currentCoord = [116.006, 40.001]; pendingLocationRender()');
  assert.equal((await initial)[0], 116.006, 'initial locate waits for conversion');
  run('locationFailed({code: 1})');
  const count = instances.length;
  await assert.rejects(run('locate()'), /权限/);
  await run('resumePosition()');
  assert.equal(instances.length, count, 'denied permission stops requests');
  run('locationPermissionBlocked = false; locationFailed({code: 3})');
  assert.ok(run('locationRetryAt > Date.now()'), 'timeout schedules backoff');
  console.log('PASS: single flight, hard timeout, late callback, watch replacement, foreground recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
