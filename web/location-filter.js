class LocationFilter {
  constructor({convert, distance, onFix, onPosition, onEvent = () => {}, onError = () => {},
    now = Date.now, schedule = setTimeout, cancel = clearTimeout, canRun = () => true}) {
    Object.assign(this, {convert, distance, onFix, onPosition, onEvent, onError, now, schedule, cancel, canRun});
    this.latest = null;
    this.converted = null;
    this.active = null;
    this.retry = null;
    this.stats = {received: 0, accepted: 0, rejected: 0, reused: 0, conversions: 0, errors: 0};
  }

  submit(result) {
    this.stats.received++;
    const coords = result?.coords;
    const sample = {point: [coords?.longitude, coords?.latitude], timestamp: result?.timestamp,
      heading: coords?.heading, accuracy: coords?.accuracy};
    if (!sample.point.every(Number.isFinite) || Math.abs(sample.point[0]) > 180 ||
        Math.abs(sample.point[1]) > 90 || !Number.isFinite(sample.timestamp) ||
        this.now()-sample.timestamp > 15000 || sample.timestamp-this.now() > 1000 ||
        (this.latest && sample.timestamp <= this.latest.timestamp)) {
      this.stats.rejected++;
      this.onEvent('raw-rejected');
      return false;
    }
    this.latest = sample;
    this.stats.accepted++;
    this.onFix(sample);
    this.pump();
    return true;
  }

  pump() {
    const sample = this.latest;
    if (!sample || this.active || this.retry || !this.canRun() || this.now()-sample.timestamp > 15000) return;
    if (this.converted) {
      const moved = this.distance(this.converted.raw, sample.point);
      if (moved === 0 || (moved < 2 && this.now()-this.converted.time < 3000)) {
        this.stats.reused++;
        this.onPosition(sample, this.converted.point);
        return;
      }
    }
    const request = {sample, timer: null};
    this.active = request;
    this.stats.conversions++;
    this.onEvent('conversion-start');
    const finish = (error, point) => {
      if (this.active !== request) return;
      this.cancel(request.timer);
      this.active = null;
      if (!error && (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite) ||
          Math.abs(point[0]) > 180 || Math.abs(point[1]) > 90)) error = new Error('坐标转换返回无效结果');
      if (error) {
        this.stats.errors++;
        this.onEvent('conversion-error', {message: error.message});
        this.onError(error);
        this.retry = this.schedule(() => { this.retry = null; this.pump(); }, 1000);
        return;
      }
      if (this.now()-sample.timestamp <= 15000) {
        this.converted = {raw: sample.point, point, time: this.now()};
        this.onPosition(sample, point);
      }
      this.onEvent('conversion-complete');
      // Only the newest pending sample is kept; old requests cannot overwrite a newer conversion.
      if (this.latest !== sample) this.pump();
    };
    request.timer = this.schedule(() => finish(new Error('坐标转换超时')), 8000);
    try { this.convert(sample.point, finish); } catch (error) { finish(error); }
  }

  suspend() {
    if (this.active) this.cancel(this.active.timer);
    this.active = null;
    if (this.retry) this.cancel(this.retry);
    this.retry = null;
  }
}

if (typeof module !== 'undefined') module.exports = LocationFilter;
