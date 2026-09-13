const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/amap-app.js', 'utf8');
let received;
const context = vm.createContext({Date, recordLocationEvent() {},
  locationFilter: {submit(result) { received = result; return true; }}});
vm.runInContext(source.slice(source.indexOf('function acceptLocation('), source.indexOf('function renderLocation(')), context);
assert.equal(vm.runInContext("acceptLocation({position:{lng:116,lat:40},isConverted:true,location_type:'html5'})", context), true);
assert.equal(received.coords.longitude, 116);
assert.equal(received.coords.latitude, 40);
assert.equal(vm.runInContext("acceptLocation({location_type:'ip'})", context), false);
assert.ok(source.includes('result.isConverted === false'), 'unconverted results use explicit fallback');
console.log('PASS: official GCJ-02 input, IP and unconverted rejection');
