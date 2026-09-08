const assert = require('node:assert/strict');
const LocationFilter = require('../web/location-filter.js');
function fixture() {
  let time = 100000, id = 0;
  const calls = [], positions = [], fixes = [], timers = new Map();
  const filter = new LocationFilter({
    now: () => time,
    schedule: (fn, delay) => { timers.set(++id, {fn, due: time+delay}); return id; },
    cancel: key => timers.delete(key),
    distance: (a, b) => Math.abs(a[0]-b[0])*100000,
    convert: (point, done) => calls.push({point, done}),
    onFix: sample => fixes.push(sample),
    onPosition: (sample, point) => positions.push({sample, point}),
  });
  const advance = delta => {
    time += delta;
    for (const [key, timer] of [...timers]) if (timer.due <= time) { timers.delete(key); timer.fn(); }
  };
  return {filter, calls, positions, fixes, advance,
    sample: (x = 116) => ({timestamp: time, coords: {longitude: x, latitude: 40, heading: 90}})};
}
{
  const f = fixture();
  assert(f.filter.submit(f.sample()));
  assert.equal(f.calls.length, 1);
  f.calls[0].done(null, [116.006, 40.001]);
  assert(!f.filter.submit(f.sample()), 'duplicate timestamp rejected');
  for (let i = 0; i < 30; i++) { f.advance(1000); assert(f.filter.submit(f.sample())); }
  assert.equal(f.calls.length, 1, 'stationary fixes reuse conversion indefinitely');
  assert.equal(f.fixes.length, 31, 'fresh stationary timestamps keep location healthy');
  assert.deepEqual(f.positions.at(-1).point, [116.006, 40.001], 'raw GPS never rendered');
  assert(!f.filter.submit({...f.sample(), timestamp: 1}));
  assert(!f.filter.submit({...f.sample(), timestamp: Infinity}));
  assert(!f.filter.submit({...f.sample(), coords: {longitude: 181, latitude: 40}}));
}
{
  const f = fixture();
  f.filter.submit(f.sample()); f.calls[0].done(null, [116.006, 40]);
  f.advance(1000); f.filter.submit(f.sample(116.000009));
  f.advance(1000); f.filter.submit(f.sample(116.000018));
  assert.equal(f.calls.length, 1);
  f.advance(500); f.filter.submit(f.sample(116.000027));
  assert.equal(f.calls.length, 2, 'accumulate movement from last converted point');
}
{
  const f = fixture();
  f.filter.submit(f.sample()); f.calls[0].done(null, [116.006, 40]);
  f.advance(3000); f.filter.submit(f.sample(116.000001));
  assert.equal(f.calls.length, 2, 'slow movement refreshes after 3 seconds');
}
{
  const f = fixture();
  f.filter.submit(f.sample());
  for (let i = 1; i <= 5; i++) { f.advance(100); f.filter.submit(f.sample(116+i*.001)); }
  assert.equal(f.calls.length, 1, 'only one conversion active');
  f.calls[0].done(null, [116.006, 40]);
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.calls[1].point, [116.005, 40], 'only newest pending position converted');
  f.calls[1].done(null, [116.011, 40]);
  f.calls[0].done(null, [1, 1]);
  assert.deepEqual(f.positions.at(-1).point, [116.011, 40], 'late callbacks ignored');
}
{
  const f = fixture();
  f.filter.submit(f.sample()); f.advance(8000);
  assert.equal(f.filter.stats.errors, 1);
  f.advance(1000);
  assert.equal(f.calls.length, 2, 'timeout releases slot and retries');
  f.calls[0].done(null, [1, 1]);
  assert.equal(f.positions.length, 0);
  f.filter.suspend(); f.calls[1].done(null, [1, 1]);
  assert.equal(f.positions.length, 0, 'suspended request ignored');
  f.advance(20000); f.filter.pump();
  assert.equal(f.calls.length, 2, 'expired position is not retried');
}
console.log('PASS: freshness, stationary reuse, accumulated movement, slow movement, latest-only queue, timeout and suspension');
