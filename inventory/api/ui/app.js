// Inventory UI: a thin client over the /v1 API (docs/specs/07-api-service.md §9).
// Everything that comes from the API is rendered with textContent: resource names, tags and
// data originate in the scanned accounts and are untrusted.
'use strict';

const TERMINAL_JOB = new Set(['succeeded', 'partial', 'failed', 'timed_out']);
const JOB_STATUSES = ['queued', 'running', 'succeeded', 'partial', 'failed', 'timed_out'];
const POLL_MS = 2000;
const PAGE_SIZE = 50;
// Scheduled runs have one task per account and region; listing thousands every poll is
// pointless and heavy, so tasks are only fetched for small jobs.
const TASK_LIST_MAX = 200;
const BADGE = {
  succeeded: 'ok', partial: 'warn', failed: 'bad', auth_denied: 'bad', timed_out: 'bad',
  running: 'info', retrying: 'info', queued: 'muted', never: 'warn',
  created: 'ok', updated: 'ok', unchanged: 'muted', deleted: 'warn', not_found: 'warn',
  unsupported_type: 'bad', error: 'bad', pending: 'info',
};

// --------------------------------------------------------------------------- DOM helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'class') el.className = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

// Like el.replaceChildren(), which would render null/undefined as the text "null".
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function badge(status) {
  return h('span', { class: `badge ${BADGE[status] || 'muted'}` }, status || '—');
}

function time(value) {
  if (!value) return h('span', { class: 'muted' }, '—');
  return h('span', { title: value }, new Date(value).toLocaleString());
}

function shortType(type) {
  return type ? type.replace(/^AWS::/, '') : '';
}

function kv(pairs) {
  return h('dl', { class: 'kv' },
    pairs.filter(Boolean).flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v ?? '—')]));
}

// A header ending in '#' is a numeric column. `rows` is a list of <tr> or a <tbody> that the
// caller keeps filling (paged lists).
function table(headers, rows) {
  const body = rows instanceof HTMLTableSectionElement ? rows : h('tbody', null, rows);
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, headers.map((x) => h('th', { class: x.endsWith('#') ? 'num' : null }, x.replace(/#$/, ''))))),
    body));
}

function field(label, input) {
  return h('label', null, label, input);
}

function select(name, options, value) {
  return h('select', { name },
    options.map(([v, text]) => h('option', { value: v, selected: v === value }, text)));
}

function splitList(value) {
  return value.split(',').map((s) => s.trim()).filter(Boolean);
}

function scopeText(scope) {
  if (!scope) return '—';
  if (scope.all) return 'all accounts';
  if (scope.arn) return scope.arn;
  if (scope.arns) return scope.arns.length === 1 ? scope.arns[0] : `${scope.arns.length} ARNs`;
  return [scope.account_id, scope.region, scope.resource_type].filter(Boolean).join(' · ');
}

// --------------------------------------------------------------------------- API client

class ApiError extends Error {
  constructor(status, body, requestId) {
    const error = body && body.error;
    super(error ? error.message : `HTTP ${status}`);
    this.status = status;
    this.code = error ? error.code : `HTTP_${status}`;
    this.requestId = (error && error.request_id) || requestId;
    this.details = (error && error.details) || [];
  }
}

async function api(method, path, { query, body } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null && item !== '') url.searchParams.append(key, String(item));
    }
  }
  const init = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    throw new ApiError(0, { error: { code: 'NETWORK_ERROR', message: String(err.message || err) } }, null);
  }
  const requestId = response.headers.get('X-Request-ID');
  let data = null;
  try {
    data = await response.json();
  } catch {
    // non-JSON body (e.g. a proxy error page); the status code is reported instead
  }
  if (!response.ok) throw new ApiError(response.status, data, requestId);
  return { status: response.status, data, requestId };
}

function errorBox(err) {
  if (!(err instanceof ApiError)) {
    console.error(err);
    return h('div', { class: 'error-box' }, h('strong', null, 'UI error'), ': ', String(err.message || err));
  }
  return h('div', { class: 'error-box' },
    h('strong', null, err.code), ': ', err.message,
    err.details.length
      ? h('ul', null, err.details.map((d) => h('li', null, `${(d.loc || []).join('.')}: ${d.msg || ''}`)))
      : null,
    h('div', { class: 'meta' },
      `HTTP ${err.status || '—'}`, err.requestId ? ` · request_id ${err.requestId}` : ''));
}

let typesPromise = null;
function resourceTypes() {
  typesPromise = typesPromise || api('GET', '/v1/resource-types').then((r) => r.data.items);
  typesPromise.catch(() => { typesPromise = null; });
  return typesPromise;
}

function regionsOf(types) {
  return [...new Set(types.flatMap((t) => t.enabled_regions))].sort();
}

function typesFor(types, region) {
  if (!region) return types;
  const enabled = types.filter((t) => t.enabled_regions.includes(region));
  return enabled.length ? enabled : types.filter((t) => t.is_global === (region === 'global'));
}

// --------------------------------------------------------------------------- routing

let leaving = [];
let pendingFlash = null;

function onLeave(fn) {
  leaving.push(fn);
}

function flash(message, kind = 'info') {
  pendingFlash = { message, kind };
}

function go(hash) {
  if (window.location.hash === hash) route();
  else window.location.hash = hash;
}

function hashFor(page, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '' && value !== false) query.set(key, String(value));
  }
  const qs = query.toString();
  return `#/${page}${qs ? `?${qs}` : ''}`;
}

async function route() {
  leaving.splice(0).forEach((fn) => fn());
  const [path, qs] = window.location.hash.replace(/^#\/?/, '').split('?');
  const [page, arg] = path.split('/').map((p) => {
    try {
      return decodeURIComponent(p || '');
    } catch {
      return ''; // malformed escape: fall back to the default page
    }
  });
  const params = new URLSearchParams(qs || '');
  const section = page === 'coverage' ? 'scans' : (page || 'resources');
  document.querySelectorAll('[data-nav]').forEach((a) => {
    a.classList.toggle('active', a.dataset.nav === section);
  });

  const box = document.getElementById('flash');
  box.hidden = !pendingFlash;
  if (pendingFlash) {
    box.className = `flash ${pendingFlash.kind}`;
    box.textContent = pendingFlash.message;
    pendingFlash = null;
  }

  // Each view renders into its own root; a slow response for a view the user already left
  // lands in a detached element instead of the current page.
  const root = h('div');
  document.getElementById('view').replaceChildren(root);
  try {
    if (page === 'overview') await overviewPage(root);
    else if (page === 'scans' && arg) await jobPage(root, arg);
    else if (page === 'scans') await scansPage(root, params);
    else if (page === 'coverage' && arg) await coveragePage(root, arg);
    else await resourcesPage(root, params);
  } catch (err) {
    root.append(errorBox(err));
  }
}

// --------------------------------------------------------------------------- scans (shared)

async function startScan(scope, waitSeconds = 0) {
  const { data: job } = await api('POST', '/v1/scans', { body: { scope, wait_seconds: waitSeconds } });
  if (job.deduplicated) flash('A job for the same scope is already running; showing that job.');
  go(`#/scans/${encodeURIComponent(job.job_id)}`);
}

async function startRefresh(arns) {
  const { data } = await api('POST', '/v1/resources/refresh', { body: { arns, wait_seconds: 0 } });
  if (data.job.deduplicated) flash('A refresh for the same ARNs is already running; showing that job.');
  go(`#/scans/${encodeURIComponent(data.job.job_id)}`);
}

function actionButton(label, action, errorArea, attrs = {}) {
  const button = h('button', { type: 'button', ...attrs }, label);
  button.addEventListener('click', async () => {
    button.disabled = true;
    fill(errorArea);
    try {
      await action();
    } catch (err) {
      fill(errorArea, errorBox(err));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// --------------------------------------------------------------------------- resources

async function resourcesPage(root, params) {
  const types = await resourceTypes();
  const regions = regionsOf(types);
  const form = h('form', { class: 'card' },
    h('h2', null, 'Resources'),
    h('div', { class: 'grid' },
      field('Account IDs (comma separated)', h('input', { name: 'account_id', value: params.get('account_id') || '', placeholder: '123456789012' })),
      field('Region', select('region', [['', 'Any'], ...regions.map((r) => [r, r])], params.get('region') || '')),
      field('Resource type', select('resource_type', [['', 'Any'], ...types.map((t) => [t.resource_type, t.resource_type])], params.get('resource_type') || '')),
      field('Name prefix', h('input', { name: 'name_prefix', value: params.get('name_prefix') || '' })),
      field('Tags (Key=Value or Key, comma separated)', h('input', { name: 'tag', value: params.get('tag') || '', placeholder: 'env=prod' }))),
    h('div', { class: 'row' },
      h('label', { class: 'inline' }, h('input', { type: 'checkbox', name: 'include_deleted', checked: params.get('include_deleted') === 'true' }), 'Include deleted'),
      h('button', { type: 'submit', class: 'primary' }, 'Search'),
      h('button', { type: 'button', onclick: () => go('#/resources') }, 'Clear')));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const f = form.elements;
    go(hashFor('resources', {
      account_id: f.account_id.value.trim(),
      region: f.region.value,
      resource_type: f.resource_type.value,
      name_prefix: f.name_prefix.value.trim(),
      tag: f.tag.value.trim(),
      include_deleted: f.include_deleted.checked ? 'true' : '',
    }));
  });

  const detail = h('div', { class: 'card', hidden: true });
  const summary = h('p', { class: 'muted' }, 'Loading…');
  const body = h('tbody');
  const more = h('button', { type: 'button', hidden: true }, 'Load more');
  const errors = h('div');
  root.append(form, detail, h('div', { class: 'card' }, summary,
    table(['Type', 'Name', 'Resource ID', 'Account', 'Region', 'Last changed', ''], body), errors, h('div', { class: 'row' }, more)));

  const query = {
    account_id: splitList(params.get('account_id') || ''),
    region: params.get('region') || undefined,
    resource_type: params.get('resource_type') || undefined,
    name_prefix: params.get('name_prefix') || undefined,
    tag: splitList(params.get('tag') || ''),
    include_deleted: params.get('include_deleted') === 'true' ? 'true' : undefined,
    limit: PAGE_SIZE,
  };
  let shown = 0;
  let total = null;
  let selected = null;

  async function load(cursor) {
    more.disabled = true;
    fill(errors);
    try {
      const { data } = await api('GET', '/v1/resources', {
        query: { ...query, cursor, with_total: cursor ? undefined : 'true' },
      });
      for (const r of data.items) {
        const row = h('tr', { class: 'clickable' },
          h('td', null, shortType(r.resource_type)),
          h('td', null, r.name || h('span', { class: 'muted' }, '—')),
          h('td', { class: 'mono wrap' }, r.resource_id),
          h('td', { class: 'mono' }, r.account_id),
          h('td', null, r.region),
          h('td', null, time(r.last_changed_at)),
          h('td', null, r.deleted_at ? badge('deleted') : null));
        row.addEventListener('click', () => {
          if (selected) selected.classList.remove('selected');
          selected = row;
          row.classList.add('selected');
          showResource(detail, r.arn);
        });
        body.append(row);
      }
      shown += data.items.length;
      if (!cursor) total = data.total;
      summary.textContent = total != null ? `${shown} of ${total} resources` : `${shown} resources`;
      more.hidden = !data.next_cursor;
      more.onclick = () => load(data.next_cursor);
    } catch (err) {
      fill(errors, errorBox(err));
    } finally {
      more.disabled = false;
    }
  }
  await load(undefined);
}

async function showResource(panel, arn, note = null) {
  const seq = String(Number(panel.dataset.seq || 0) + 1);
  panel.dataset.seq = seq; // a slower response for an earlier click must not overwrite this one
  panel.hidden = false;
  fill(panel, h('p', { class: 'muted' }, 'Loading…'));
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  let r;
  try {
    ({ data: r } = await api('GET', '/v1/resources/by-arn', { query: { arn, include_deleted: 'true' } }));
  } catch (err) {
    if (panel.dataset.seq === seq) fill(panel, errorBox(err));
    return;
  }
  if (panel.dataset.seq !== seq) return;
  const status = h('div', null, note);
  const errors = h('div');
  const tags = Object.entries(r.tags || {});
  const scanScope = { account_id: r.account_id, region: r.region, resource_type: r.resource_type };
  fill(panel,
    h('div', { class: 'row' },
      h('h2', null, r.name || r.resource_id), r.deleted_at ? badge('deleted') : null,
      h('span', { class: 'muted' }, shortType(r.resource_type))),
    kv([
      ['ARN', h('span', { class: 'mono' }, r.arn)],
      ['Resource ID', h('span', { class: 'mono' }, r.resource_id)],
      ['Account / region', `${r.account_id} · ${r.region}`],
      ['Tags', tags.length ? tags.map(([k, v]) => `${k}=${v}`).join(', ') : null],
      ['First seen', time(r.first_seen_at)],
      ['Last changed', time(r.last_changed_at)],
      ['Observed', time(r.observed_at)],
      ['Scope last verified', time(r.verified_at)],
      r.deleted_at ? ['Deleted', time(r.deleted_at)] : null,
    ]),
    h('div', { class: 'row' },
      actionButton('Refresh from AWS', async () => {
        fill(status, h('p', { class: 'muted' }, 'Refreshing (up to 20 s)…'));
        const { data } = await api('POST', '/v1/resources/refresh', { body: { arns: [arn], wait_seconds: 20 } });
        const result = (data.results || [])[0];
        const jobLink = h('a', { href: `#/scans/${encodeURIComponent(data.job.job_id)}` }, 'job');
        if (!result || result.status === 'pending') {
          fill(status, h('p', null, 'Still running in the background — follow the ', jobLink, '.'));
          return;
        }
        const outcome = h('p', null, 'Refresh result: ', badge(result.status), ' (', jobLink, ')',
          result.error ? ` — ${result.error.code}: ${result.error.message}` : '');
        if (['created', 'updated', 'deleted'].includes(result.status)) await showResource(panel, arn, outcome);
        else fill(status, outcome);
      }, errors, { class: 'primary' }),
      h('a', { href: hashFor('scans', scanScope) }, `Scan ${shortType(r.resource_type)} in this account and region`),
      h('a', { href: `#/coverage/${encodeURIComponent(r.account_id)}` }, 'Account coverage'),
      h('button', { type: 'button', onclick: () => { panel.hidden = true; } }, 'Close')),
    status, errors,
    h('h3', null, 'data'),
    h('pre', null, JSON.stringify(r.data, null, 2)),
    Object.keys(r.relations || {}).length ? [h('h3', null, 'relations'), h('pre', null, JSON.stringify(r.relations, null, 2))] : null);
}

// --------------------------------------------------------------------------- overview

async function overviewPage(root) {
  const [byType, byRegion, types] = await Promise.all([
    api('GET', '/v1/resources/stats', { query: { group_by: 'resource_type' } }),
    api('GET', '/v1/resources/stats', { query: { group_by: 'region' } }),
    resourceTypes(),
  ]);
  const countRows = (groups, key) => groups.map((g) => h('tr', null,
    h('td', null, key === 'resource_type'
      ? h('a', { href: hashFor('resources', { resource_type: g[key] }) }, g[key])
      : h('a', { href: hashFor('resources', { region: g[key] }) }, g[key])),
    h('td', { class: 'num' }, Number(g.count).toLocaleString())));
  root.append(
    h('div', { class: 'card' },
      h('h2', null, 'Overview'),
      h('p', null, `${Number(byType.data.total).toLocaleString()} resources in the inventory.`),
      h('div', { class: 'split' },
        h('div', null, h('h3', null, 'By resource type'), table(['Resource type', 'Count#'], countRows(byType.data.groups, 'resource_type'))),
        h('div', null, h('h3', null, 'By region'), table(['Region', 'Count#'], countRows(byRegion.data.groups, 'region'))))),
    h('div', { class: 'card' },
      h('h2', null, 'Resource types'),
      table(['Resource type', 'Service', 'Scope', 'Depends on', 'Scannable in'], types.map((t) => h('tr', null,
        h('td', null, t.resource_type),
        h('td', null, t.service),
        h('td', null, t.is_global ? 'global' : 'regional'),
        h('td', null, t.depends_on.map(shortType).join(', ') || '—'),
        h('td', null, t.enabled_regions.join(', ')))))));
}

// --------------------------------------------------------------------------- scans

async function scansPage(root, params) {
  const types = await resourceTypes();
  const regions = regionsOf(types);
  const initialLevel = params.get('arn') ? 'arn'
    : params.get('resource_type') ? 'type'
      : params.get('region') ? 'region' : 'account';

  const levels = [['account', 'Account'], ['region', 'Account + region'], ['type', 'Account + type'], ['arn', 'Single ARN']];
  const typeSelect = h('select', { name: 'resource_type' });
  const regionSelect = select('region', [['', 'Select a region'], ...regions.map((r) => [r, r])], params.get('region') || '');
  const regionHint = h('span', { class: 'muted' }, 'Leave the region empty to scan every region the account has enabled and a worker covers.');
  const fillTypes = (keep) => {
    fill(typeSelect, h('option', { value: '' }, 'Select a type'),
      ...typesFor(types, regionSelect.value).map((t) => h('option', { value: t.resource_type, selected: t.resource_type === keep }, t.resource_type)));
  };
  fillTypes(params.get('resource_type'));
  regionSelect.addEventListener('change', () => fillTypes(typeSelect.value));

  const fields = {
    account: field('Account ID', h('input', { name: 'account_id', value: params.get('account_id') || '', placeholder: '123456789012', inputmode: 'numeric' })),
    region: field('Region', regionSelect),
    type: field('Resource type', typeSelect),
    arn: field('ARN', h('input', { name: 'arn', value: params.get('arn') || '', placeholder: 'arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0abc', class: 'mono' })),
  };
  const errors = h('div');
  const form = h('form', { class: 'card' },
    h('h2', null, 'Trigger a scan'),
    h('div', { class: 'segmented' }, levels.map(([value, text]) => h('label', { class: 'inline' },
      h('input', { type: 'radio', name: 'level', value, checked: value === initialLevel }), text))),
    h('div', { class: 'grid' }, fields.account, fields.region, fields.type, fields.arn,
      field('Wait (seconds, 0–25)', h('input', { name: 'wait_seconds', type: 'number', min: 0, max: 25, value: 0 }))),
    regionHint,
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'primary' }, 'Start scan'),
      h('span', { class: 'muted' }, 'You will be taken to the job page, which updates until the scan finishes.')),
    errors);
  const showFields = () => {
    const level = form.elements.level.value;
    fields.account.hidden = level === 'arn';
    fields.region.hidden = level === 'account' || level === 'arn';
    fields.type.hidden = level !== 'type';
    fields.arn.hidden = level !== 'arn';
    regionHint.hidden = level !== 'type';
    regionSelect.options[0].textContent = level === 'type' ? 'All enabled regions' : 'Select a region';
  };
  form.addEventListener('change', showFields);
  showFields();

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const f = form.elements;
    const level = f.level.value;
    const scope = level === 'arn' ? { arn: f.arn.value.trim() } : {
      account_id: f.account_id.value.trim(),
      region: level === 'account' ? undefined : f.region.value || undefined,
      resource_type: level === 'type' ? f.resource_type.value || undefined : undefined,
    };
    const missing = level === 'arn' ? (!scope.arn && 'ARN')
      : !scope.account_id ? 'Account ID'
        : level === 'region' && !scope.region ? 'Region'
          : level === 'type' && !scope.resource_type ? 'Resource type' : null;
    fill(errors);
    if (missing) {
      errors.append(h('p', { class: 'error' }, `${missing} is required.`));
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      await startScan(scope, Number(f.wait_seconds.value) || 0);
    } catch (err) {
      fill(errors, errorBox(err));
    } finally {
      button.disabled = false;
    }
  });

  const coverageForm = h('form', { class: 'card' },
    h('h2', null, 'Account coverage'),
    h('p', { class: 'muted' }, 'See which regions and types of an account were scanned, when, and with what result; start a scan for any of them.'),
    h('div', { class: 'row' }, h('input', { name: 'account', placeholder: '123456789012', inputmode: 'numeric' }),
      h('button', { type: 'submit' }, 'Show coverage')));
  coverageForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const account = coverageForm.elements.account.value.trim();
    if (account) go(`#/coverage/${encodeURIComponent(account)}`);
  });

  root.append(form, coverageForm, jobList());
}

function jobList() {
  const filters = h('form', { class: 'row' },
    select('status', [['', 'Any status'], ...JOB_STATUSES.map((s) => [s, s])], ''),
    select('trigger', [['', 'Any trigger'], ['api', 'api'], ['schedule', 'schedule']], ''),
    h('input', { name: 'account_id', placeholder: 'Account ID' }),
    h('button', { type: 'submit' }, 'Filter'));
  const body = h('tbody');
  const errors = h('div');
  const more = h('button', { type: 'button', hidden: true }, 'Load more');
  const card = h('div', { class: 'card' }, h('h2', null, 'Recent scan jobs'), filters,
    table(['Job', 'Trigger', 'Scope', 'Status', 'Tasks#', 'Created', 'Finished'], body), errors, h('div', { class: 'row' }, more));

  // The cursor is bound to the filters it was issued for (a mismatch is a 400), so "Load more"
  // reuses the query captured when Filter was submitted, not the current form values.
  let query = {};
  let seq = 0;
  async function load(cursor) {
    const mine = ++seq;
    fill(errors);
    more.disabled = true;
    try {
      const { data } = await api('GET', '/v1/scans', { query: { ...query, limit: PAGE_SIZE, cursor } });
      if (mine !== seq) return;
      if (!cursor) fill(body);
      for (const job of data.items) {
        const stats = job.stats || {};
        body.append(h('tr', null,
          h('td', { class: 'mono' }, h('a', { href: `#/scans/${encodeURIComponent(job.job_id)}` }, job.job_id)),
          h('td', null, job.trigger),
          h('td', { class: 'wrap' }, scopeText(job.scope)),
          h('td', null, badge(job.status)),
          h('td', { class: 'num' }, stats.tasks_total != null ? `${stats.tasks_done ?? 0} / ${stats.tasks_total}` : '—'),
          h('td', null, time(job.created_at)),
          h('td', null, time(job.finished_at))));
      }
      if (!body.children.length) body.append(h('tr', null, h('td', { colspan: 7, class: 'muted' }, 'No jobs match.')));
      more.hidden = !data.next_cursor;
      more.onclick = () => load(data.next_cursor);
    } catch (err) {
      if (mine === seq) fill(errors, errorBox(err));
    } finally {
      if (mine === seq) more.disabled = false;
    }
  }
  filters.addEventListener('submit', (event) => {
    event.preventDefault();
    const f = filters.elements;
    query = { status: f.status.value, trigger: f.trigger.value, account_id: f.account_id.value.trim() };
    load(undefined);
  });
  load(undefined);
  return card;
}

async function jobPage(root, jobId) {
  const summary = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  // built once from the first response (the scope never changes) so a rerun error or a
  // disabled button is not wiped by the next poll
  const controls = h('div');
  const header = h('div', { class: 'card' }, summary, controls);
  const tasksCard = h('div', { class: 'card', hidden: true });
  const errors = h('div');
  root.append(h('p', null, h('a', { href: '#/scans' }, '← All scans')), header, errors, tasksCard);

  let timer = null;
  let stopped = false;
  let taskTotal = null;
  const expanded = new Set();
  onLeave(() => { stopped = true; clearTimeout(timer); });

  async function load() {
    timer = null;
    let job;
    try {
      const withTasks = taskTotal !== null && taskTotal <= TASK_LIST_MAX;
      ({ data: job } = await api('GET', `/v1/scans/${encodeURIComponent(jobId)}`, {
        query: { include_tasks: withTasks ? 'true' : undefined },
      }));
      fill(errors);
    } catch (err) {
      fill(errors, errorBox(err));
      if (err.status !== 404 && !stopped) timer = setTimeout(load, POLL_MS * 3);
      return;
    }
    if (stopped) return;
    const known = taskTotal;
    taskTotal = (job.stats || {}).tasks_total ?? 0;
    renderJob(job);
    // first response tells us the job size; fetch the task list right away when it is small
    if (known === null && taskTotal <= TASK_LIST_MAX) load();
    else if (!TERMINAL_JOB.has(job.status)) timer = setTimeout(load, POLL_MS);
  }

  function renderJob(job) {
    const stats = job.stats || {};
    const total = stats.tasks_total || 0;
    const done = stats.tasks_done || 0;
    const live = !TERMINAL_JOB.has(job.status);
    const counters = Object.entries(stats).filter(([k, v]) => typeof v === 'number' && !['tasks_total', 'tasks_done'].includes(k));
    fill(summary,
      h('div', { class: 'row' }, h('h2', null, `Scan job ${job.job_id}`), badge(job.status),
        h('span', { class: 'muted' }, live ? `updating every ${POLL_MS / 1000} s` : 'finished')),
      h('div', { class: 'progress', title: `${done} of ${total} tasks done` }, h('div')),
      kv([
        ['Scope', h('span', { class: 'wrap' }, scopeText(job.scope))],
        ['Trigger', job.trigger],
        ['Tasks', `${done} of ${total} done`],
        ['Created', time(job.created_at)],
        ['Started', time(job.started_at)],
        ['Finished', time(job.finished_at)],
        ['request_id', job.request_id ? h('span', { class: 'mono' }, job.request_id) : null],
        job.error ? ['Error', h('span', { class: 'error' }, job.error)] : null,
      ]),
      counters.length ? table(counters.map(([k]) => `${k}#`), [h('tr', null, counters.map(([, v]) => h('td', { class: 'num' }, v.toLocaleString())))]) : null);
    if (!controls.hasChildNodes()) {
      const controlErrors = h('div');
      fill(controls,
        h('div', { class: 'row' }, rerunButton(job, controlErrors),
          job.scope && job.scope.account_id ? h('a', { href: `#/coverage/${encodeURIComponent(job.scope.account_id)}` }, 'Account coverage') : null),
        controlErrors);
    }
    // width via CSSOM: inline style attributes are blocked by the CSP
    summary.querySelector('.progress > div').style.width = `${total ? Math.round((done / total) * 100) : (live ? 0 : 100)}%`;

    tasksCard.hidden = false;
    if (!job.tasks) {
      fill(tasksCard, h('h2', null, 'Tasks'), h('p', { class: 'muted' },
        total > TASK_LIST_MAX ? `${total} tasks — too many to list here; use GET /v1/scans/{job_id}?include_tasks=true.` : 'Loading…'));
      return;
    }
    const rows = [];
    for (const task of job.tasks) {
      const results = Object.entries(task.result || {}).filter(([, v]) => v && typeof v === 'object');
      const open = expanded.has(task.task_id);
      const row = h('tr', { class: results.length ? 'clickable' : null },
        h('td', null, results.length ? (open ? '▾' : '▸') : ''),
        h('td', { class: 'mono' }, task.task_id),
        h('td', { class: 'mono' }, task.account_id),
        h('td', null, task.region),
        h('td', null, task.resource_types ? task.resource_types.map(shortType).join(', ') : 'all'),
        h('td', null, badge(task.status)),
        h('td', { class: 'num' }, task.attempts),
        h('td', null, time(task.started_at)),
        h('td', null, time(task.finished_at)),
        h('td', { class: 'error' }, task.error_code ? `${task.error_code}: ${task.error_message || ''}` : ''));
      if (results.length) {
        row.addEventListener('click', () => {
          if (expanded.has(task.task_id)) expanded.delete(task.task_id); else expanded.add(task.task_id);
          renderJob(job);
        });
      }
      rows.push(row);
      if (open) {
        rows.push(h('tr', null, h('td', null), h('td', { colspan: 9 },
          table(['Resource type', 'Status', 'Created#', 'Updated#', 'Deleted#', 'Unchanged#', 'Duration (ms)#', 'Error'], results.map(([type, r]) => h('tr', null,
            h('td', null, type),
            h('td', null, badge(r.status)),
            h('td', { class: 'num' }, r.created ?? '—'),
            h('td', { class: 'num' }, r.updated ?? '—'),
            h('td', { class: 'num' }, r.deleted ?? '—'),
            h('td', { class: 'num' }, r.unchanged ?? '—'),
            h('td', { class: 'num' }, r.duration_ms ?? '—'),
            h('td', { class: 'error' }, r.error_code ? `${r.error_code}: ${r.error_message || ''}` : '')))))));
      }
    }
    fill(tasksCard, h('h2', null, `Tasks (${job.tasks.length})`),
      table(['', 'Task', 'Account', 'Region', 'Types', 'Status', 'Attempts#', 'Started', 'Finished', 'Error'], rows));
  }

  await load();
}

function rerunButton(job, errorArea) {
  const scope = job.scope || {};
  if (scope.all) return null;
  if (scope.arn || scope.arns) {
    return actionButton('Refresh the same ARNs again', () => startRefresh(scope.arns || [scope.arn]), errorArea);
  }
  const again = { account_id: scope.account_id, region: scope.region, resource_type: scope.resource_type };
  return actionButton('Scan the same scope again', () => startScan(again), errorArea);
}

// --------------------------------------------------------------------------- coverage

async function coveragePage(root, accountId) {
  const { data } = await api('GET', `/v1/accounts/${encodeURIComponent(accountId)}/coverage`);
  const errors = h('div');
  const rows = data.scopes.map((s) => h('tr', null,
    h('td', null, s.region),
    h('td', null, h('a', { href: hashFor('resources', { account_id: accountId, region: s.region, resource_type: s.resource_type }) }, s.resource_type)),
    h('td', null, badge(s.status)),
    h('td', { class: 'num' }, s.resource_count ?? '—'),
    h('td', null, time(s.last_attempt_at)),
    h('td', null, time(s.last_success_at)),
    h('td', { class: 'error' }, s.error_code ? `${s.error_code}: ${s.error_message || ''}` : ''),
    h('td', null, actionButton('Scan', () => startScan({ account_id: accountId, region: s.region, resource_type: s.resource_type }), errors))));
  root.append(
    h('p', null, h('a', { href: '#/scans' }, '← Scans')),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', null, `Coverage for ${data.account_id}`),
        actionButton('Scan whole account', () => startScan({ account_id: accountId }), errors, { class: 'primary' })),
      kv([
        ['Enabled regions', data.enabled_regions.length ? data.enabled_regions.join(', ') : 'unknown (not checked yet)'],
        ['Regions checked', time(data.regions_checked_at)],
      ]),
      errors,
      rows.length
        ? table(['Region', 'Resource type', 'Status', 'Resources#', 'Last attempt', 'Last success', 'Error', ''], rows)
        : h('p', { class: 'muted' }, 'Nothing scanned for this account yet. Start with "Scan whole account".')));
}

window.addEventListener('hashchange', route);
route();
