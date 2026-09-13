const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({
  setTimeout: function () {
    'use strict';
    assert.equal(this, undefined, 'native timer must not receive LocationFilter as receiver');
    return 1;
  },
  clearTimeout: function () {
    'use strict';
    assert.equal(this, undefined);
  },
});
vm.runInContext(fs.readFileSync('web/location-filter.js', 'utf8'), context);
vm.runInContext(`
  const filter = new LocationFilter({convert: (p, done) => done(null, p),
    distance: () => 0, onFix: () => {}, onPosition: () => {}});
  filter.submit({coords: {longitude: 116, latitude: 40}, timestamp: Date.now()});
`, context);
console.log('PASS: browser timer receiver binding');
