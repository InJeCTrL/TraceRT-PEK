const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/amap-app.js', 'utf8');
let received;
const context = vm.createContext({Date, recordLocationEvent() {}, locationFixSerial: 0,
  renderLocation(result) { received = result; }});
vm.runInContext(source.slice(source.indexOf('function acceptLocation('), source.indexOf('function renderLocation(')), context);
assert.equal(vm.runInContext("acceptLocation({position:{lng:116,lat:40},isConverted:true,location_type:'html5'})", context), true);
assert.equal(received.position[0], 116);
assert.equal(received.position[1], 40);
assert.equal(vm.runInContext("acceptLocation({position:{lng:116,lat:40},isConverted:false,location_type:'ip'})", context), true);
assert.ok(!source.includes('AMap.convertFrom('), 'no application coordinate conversion');
console.log('PASS: official results used directly without conversion');
