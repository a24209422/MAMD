const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(api) {
  const handlers = {};
  const container = { html: '', addEventListener: (kind, fn) => { handlers[kind] = fn; } };
  const context = vm.createContext({
    app: { token: 'token', page: 'models', state: { roles: {
      writer: { cli: 'claude', model: 'opus', reasoningEffort: 'high' },
      reviewer: { cli: 'codex', model: null, reasoningEffort: null },
    } } },
    drafts: {}, roleInfo: { writer: { role: '寫程式', accent: 'green' }, reviewer: { role: '審查', accent: 'blue' } },
    cliNames: { claude: 'Claude', codex: 'Codex' }, cliLabel: cli => cli,
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    icon: () => '', writeDisabled: () => '', $: () => container,
    rerender: (element, html) => { element.html = html; },
    onStateChange: () => {}, onPageChange: () => {}, pollState: () => {},
    apiPost: api, Date, console,
  });
  context.refresh = render => render(context.app.state);
  const source = fs.readFileSync(path.join(__dirname, '../web/js/models.js'), 'utf8');
  vm.runInContext(source + '\nglobalThis.modelUi = { modelsUi, roleChoice, renderModels, loadRoleModels, saveRole };', context);
  return { context, container, handlers, ...context.modelUi };
}

const models = [{ id: 'opus', displayName: 'Opus New', resolvedModel: 'claude-opus-new',
  description: '<script>bad</script>', efforts: [{ value: 'high', description: 'Deep' }, { value: 'max', description: '' }] }];

test('CLI models, resolved versions and supported effort options are rendered safely', async () => {
  const ui = setup(async () => ({ ok: true, models }));
  await ui.loadRoleModels('writer');
  ui.renderModels(ui.context.app.state);
  assert.match(ui.container.html, /<select[^>]*data-role-model="writer"/);
  assert.match(ui.container.html, /Opus New/);
  assert.match(ui.container.html, /claude-opus-new/);
  assert.match(ui.container.html, /value="max"/);
  assert.doesNotMatch(ui.container.html, /value="ultra"/);
  assert.doesNotMatch(ui.container.html, /<script>bad/);
  assert.match(ui.container.html, /&lt;script&gt;/);
});

test('saving includes effort; changing CLI or model clears incompatible choices', async () => {
  const calls = [];
  const ui = setup(async (url, body) => { calls.push([url, body]); return { ok: true, models }; });
  await ui.loadRoleModels('writer');
  await ui.saveRole('writer');
  assert.equal(calls.at(-1)[0], '/api/roles/writer');
  assert.equal(calls.at(-1)[1].reasoningEffort, 'high');
  function change(dataset, value) { ui.handlers.change({ target: { dataset, value } }); }
  change({ roleModel: 'writer' }, 'opus');
  assert.equal(ui.roleChoice(ui.context.app.state, 'writer').reasoningEffort, '');
  change({ roleCli: 'writer' }, 'codex');
  assert.equal(ui.roleChoice(ui.context.app.state, 'writer').model, '');
  assert.equal(ui.roleChoice(ui.context.app.state, 'writer').reasoningEffort, '');
});

test('catalog errors are visible, preserve settings, and can be refreshed', async () => {
  let fail = true;
  const calls = [];
  const ui = setup(async (url, body) => {
    calls.push(body);
    return fail ? { ok: false, message: '請先登入', models: [] } : { ok: true, models };
  });
  await ui.loadRoleModels('writer');
  assert.match(ui.container.html, /請先登入/);
  assert.match(ui.container.html, /opus/);
  assert.equal(ui.context.app.state.roles.writer.model, 'opus');
  fail = false;
  await ui.loadRoleModels('writer', true);
  assert.equal(calls.at(-1).refresh, true);
  assert.match(ui.container.html, /Opus New/);
});

test('late catalog response cannot replace a newly selected CLI', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const ui = setup(() => pending);
  const loading = ui.loadRoleModels('writer');
  ui.context.drafts['role-cli:writer'] = 'codex';
  ui.context.drafts['role-model:writer'] = '';
  resolve({ ok: true, models });
  await loading;
  assert.equal(ui.roleChoice(ui.context.app.state, 'writer').cli, 'codex');
  assert.doesNotMatch(ui.container.html, /Opus New/);
});
