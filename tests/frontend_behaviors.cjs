const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = require('node:path');

function setup() {
  const nodes = new Map();
  const context = vm.createContext({
    setTimeout, clearTimeout, console,
    window: {},
    document: {getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {className: '', textContent: '', hidden: false});
      return nodes.get(id);
    }},
    EventSource: class {
      constructor() { this.listeners = {}; this.closed = false; }
      addEventListener(type, fn) { this.listeners[type] = fn; }
      close() { this.closed = true; }
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  vm.runInContext(source.replace(/\ninitialize\(\);\s*$/, '\n'), context);
  vm.runInContext('renderRuns = renderLoadingDetail = renderAll = renderEvents = scheduleDetailRefresh = () => {};', context);
  return context;
}

test('completed history reads all event pages without duplicates', async () => {
  const context = setup();
  await vm.runInContext(`
    api = async path => {
      if (path.includes('events-log')) {
        const after = Number(new URL(path, 'http://fixture').searchParams.get('after') || 0);
        return Array.from({length: Math.min(500, 501 - after)}, (_, i) => ({id: after + i + 1, event: 'node_finished'}));
      }
      return path.endsWith('history') ? [] : {status: 'completed'};
    };
    selectRun('completed');
  `.replace('new URL', 'new globalURL'), Object.assign(context, {globalURL: URL}));
  assert.equal(vm.runInContext('ui.events.length', context), 501);
  assert.equal(vm.runInContext('ui.eventIds.size', context), 501);
  assert.equal(vm.runInContext('ui.lastEventId', context), 501);
});

test('native SSE transport errors do not become application error events', () => {
  const context = setup();
  vm.runInContext(`ui.currentRunId = 'live'; ui.current = {status:'running'}; connectEvents(); ui.source.listeners.error({type:'error'});`, context);
  assert.equal(vm.runInContext('ui.events.length', context), 0);
});

test('an old detail refresh cannot close a newly selected stream', async () => {
  const context = setup();
  await vm.runInContext(`
    ui.currentRunId = 'old'; ui.current = {status:'running'};
    api = async path => path.endsWith('history') || path.includes('events-log') ? [] : {status:'completed'};
    refreshRuns = async () => { ui.currentRunId = 'new'; ui.current = {status:'running'}; connectEvents(); };
    refreshSelectedDetail();
  `, context);
  assert.equal(vm.runInContext('ui.source !== null && !ui.source.closed', context), true);
});

test('terminal refresh drains persisted events before closing the stream', async () => {
  const context = setup();
  await vm.runInContext(`
    ui.currentRunId = 'live'; ui.current = {status:'running'}; connectEvents();
    api = async path => path.includes('events-log') ? [{id: 42, event:'run_finished'}] : path.endsWith('history') ? [] : {status:'completed'};
    refreshRuns = async () => {};
    refreshSelectedDetail();
  `, context);
  assert.equal(vm.runInContext('ui.lastEventId', context), 42);
  assert.equal(vm.runInContext('ui.source', context), null);
});
