/* Resy SF Busyness — plain JS over the FastAPI endpoints. */
const $ = (id) => document.getElementById(id);
const state = { tab: 'scored', results: null, run: null, poll: null };

function route() {
  const screen = location.hash === '#run' ? 'run' : 'results';
  $('screen-run').hidden = screen !== 'run';
  $('screen-results').hidden = screen !== 'results';
  $('nav-run').toggleAttribute('aria-current', screen === 'run');
  $('nav-results').toggleAttribute('aria-current', screen === 'results');
  if (screen === 'results') loadResults(); else loadLatestRun();
}
window.addEventListener('hashchange', route);

const fmtTs = (iso) => new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ---------------- Run screen ---------------- */
async function loadLatestRun() {
  const runs = await (await fetch('/api/runs?limit=1')).json();
  if (!runs.length) { $('run-status').textContent = 'no runs yet'; return; }
  await showRun(runs[0].id);
}

async function showRun(id) {
  const r = await (await fetch(`/api/runs/${id}?log_limit=40`)).json();
  state.run = r;
  const pct = r.requests_total ? Math.round((100 * r.requests_done) / r.requests_total) : 0;
  $('run-kicker').textContent = `Run ${String(r.id).padStart(3, '0')} · ${r.status}`;
  $('pct').textContent = `${pct}%`;
  $('pct-bar').style.width = `${pct}%`;
  $('req').textContent = `${r.requests_done} of ${r.requests_total} requests`;
  $('run-status').textContent = r.status + (r.error ? ` · ${r.error}` : '');
  $('c-venues').textContent = r.venues_seen;
  $('c-new').textContent = r.states_new;
  $('c-same').textContent = r.states_unchanged;
  $('c-scored').textContent = `${r.scored} / ${r.excluded}`;
  $('party').value = r.params.party_size; $('days').value = r.params.days; $('verify').value = r.params.verify_offset_days;
  const lines = r.log.map((l) => {
    const color = l.level === 'error' ? 'var(--color-accent-700)' : l.tag.startsWith('verify') ? 'var(--color-neutral-600)' : l.tag === 'done' ? 'var(--color-accent)' : 'var(--color-text)';
    return `<div class="log-line"><span class="text-muted">${new Date(l.ts).toLocaleTimeString(undefined, { hour12: false })}</span><span title="${esc(l.msg)}">${esc(l.msg)}</span><span class="tag-col" style="color:${color}">${esc(l.tag)}</span></div>`;
  });
  if (r.status === 'running' || r.status === 'queued') lines.unshift(`<div class="log-line"><span class="text-muted">now</span><span><span class="cursor"></span></span><span></span></div>`);
  $('log').innerHTML = lines.join('');
  $('start').disabled = r.status === 'running' || r.status === 'queued';
  if (r.finished_at) $('snapshot').textContent = `Snapshot · run ${r.id} · ${fmtTs(r.finished_at)}`;
  clearTimeout(state.poll);
  if (r.status === 'running' || r.status === 'queued') state.poll = setTimeout(() => showRun(id), 1500);
  else if (r.status === 'done') state.results = null;
}

$('start').addEventListener('click', async () => {
  $('run-error').textContent = '';
  const body = { party_size: +$('party').value, days: +$('days').value, verify_offset_days: +$('verify').value };
  const res = await fetch('/api/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) { $('run-error').textContent = (await res.json()).detail || `HTTP ${res.status}`; return; }
  const r = await res.json();
  showRun(r.id);
});
$('refresh-runs').addEventListener('click', loadLatestRun);

/* ---------------- Results screen ---------------- */
async function loadResults() {
  if (!state.results) {
    const res = await fetch('/api/results');
    if (!res.ok) { $('results-body').innerHTML = `<p class="text-muted empty">${esc((await res.json()).detail)} <a href="#run">Go to Run</a>.</p>`; return; }
    state.results = await res.json();
  }
  const R = state.results;
  $('snapshot').textContent = `Snapshot · run ${R.run_id} · ${fmtTs(R.computed_at)}`;
  $('n-listed').textContent = R.listed; $('n-scored').textContent = R.scored; $('n-excl').textContent = R.excluded;
  $('t-scored').textContent = R.scored; $('t-excl').textContent = R.excluded;
  $('csv').href = `/api/results.csv?run_id=${R.run_id}`;
  const party = R.rows[0] ? state.run?.params?.party_size ?? 2 : 2;
  $('res-kicker').textContent = `Results · party of ${party} · ${R.rows[0]?.nights?.length ?? 7} nights`;
  renderTable();
}

function nightCell(n) {
  if (!n.boxes) return `<div class="na" title="${n.day}: no dinner window for two"></div>`;
  const pct = Math.round(n.ratio * 100);
  const bg = `color-mix(in srgb, var(--color-accent) ${pct}%, var(--color-surface))`;
  const open = n.open_times.length ? `open: ${n.open_times.join(' ')}` : 'nothing left';
  return `<div style="background:${bg}" title="${n.day} · ${n.window} · ${n.taken}/${n.boxes} taken · ${open}"></div>`;
}

function renderTable() {
  const R = state.results; if (!R) return;
  const q = $('q').value.trim().toLowerCase();
  let rows = R.rows.filter((r) => (state.tab === 'scored' ? !r.excluded : r.excluded));
  if (q) rows = rows.filter((r) => [r.name, r.neighborhood, r.cuisine, r.reason].some((v) => (v || '').toLowerCase().includes(q)));
  document.querySelectorAll('.tabs a').forEach((a) => a.classList.toggle('on', a.dataset.tab === state.tab));
  const who = (r) => `<td><div style="font-weight:600">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${esc(r.name)}</a>` : esc(r.name)}</div><div class="text-muted sub">${esc(r.neighborhood || '—')} · ${esc(r.cuisine || '—')}</div></td>`;
  if (state.tab === 'scored') {
    const top = rows.length ? rows[0].score : 1;
    $('results-body').innerHTML = `
      <table class="table"><thead><tr>
        <th style="width:44px">#</th><th>Restaurant</th><th style="width:170px">Score ↓</th><th style="width:200px">Nights · taken share</th>
        <th class="num">Taken / boxes</th><th class="num">Nights</th><th class="num">3 wks out</th><th class="num">Reviews</th><th class="num">Price</th>
      </tr></thead><tbody>
      ${rows.map((r) => `<tr>
        <td class="text-muted rank">${r.rank}</td>${who(r)}
        <td><div class="score-cell"><div class="track"><div class="fill ${r.score >= 0.9 ? 'top' : ''}" style="width:${Math.round(r.score * 100)}%"></div></div><span>${r.score.toFixed(2)}</span></div>
            ${r.evidence ? `<div class="text-muted sub" style="margin-top:4px">${esc(r.evidence)}</div>` : ''}</td>
        <td><div class="nights">${r.nights.map(nightCell).join('')}</div><div class="text-muted sub" style="margin-top:3px">${r.nights[0]?.day.slice(5)} → ${r.nights[r.nights.length - 1]?.day.slice(5)}</div></td>
        <td class="num">${r.taken_total} / ${r.boxes_total}</td>
        <td class="num">${r.days_scored}</td>
        <td class="num">${r.verified_far_out_open ?? '—'} open</td>
        <td class="num text-muted">${r.rating_count ?? '—'}</td>
        <td class="num text-muted">${esc(r.price || '—')}</td>
      </tr>`).join('')}
      </tbody></table>
      <div class="legend text-muted">
        <span><i style="background:var(--color-accent)"></i>All half-hours taken</span>
        <span><i style="background:color-mix(in srgb, var(--color-accent) 50%, var(--color-surface))"></i>Half taken</span>
        <span><i style="background:var(--color-surface)"></i>Wide open</span>
        <span><i class="na" style="background:repeating-linear-gradient(135deg,var(--color-neutral-300) 0 3px,transparent 3px 6px)"></i>No dinner window that night</span>
        <span style="margin-left:auto">Showing ${rows.length} of ${R.scored} · hover a night for open times</span>
      </div>`;
  } else {
    const label = { closed: 'Closed', other_platform: 'Other platform', events_only: 'Events only', no_dinner_service: 'No dinner service', no_inventory: 'No inventory', insufficient_data: 'Insufficient data' };
    $('results-body').innerHTML = `
      <p class="text-muted" style="max-width:70ch;margin-bottom:var(--space-4)">These venues show no availability for reasons other than demand. They get no score and no rank, so they cannot pass for sold-out.</p>
      <table class="table"><thead><tr><th style="width:44px">#</th><th>Restaurant</th><th style="width:160px">Reason</th><th>Evidence</th><th style="width:200px">Nights</th><th class="num">Price</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr>
        <td class="text-muted rank">${i + 1}</td>${who(r)}
        <td><span class="tag tag-neutral">${label[r.reason] || esc(r.reason)}</span></td>
        <td class="text-muted" style="font-size:13px">${esc(r.evidence)}</td>
        <td><div class="nights">${r.nights.map(nightCell).join('')}</div></td>
        <td class="num text-muted">${esc(r.price || '—')}</td>
      </tr>`).join('')}
      </tbody></table>
      <div class="legend text-muted"><span style="margin-left:auto">Showing ${rows.length} of ${R.excluded}</span></div>`;
  }
}
document.querySelectorAll('.tabs a').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); state.tab = a.dataset.tab; renderTable(); }));
$('q').addEventListener('input', renderTable);

route();
