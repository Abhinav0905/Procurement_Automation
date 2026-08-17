/* ProcureGuard control surface.
 *
 * Vanilla JS, no build step, no external assets. The identity switcher sends
 * X-Actor-Id / X-Actor-Roles headers, which the API trusts only when
 * AUTH_MODE=dev; in production the same screens run behind SSO and the switcher
 * is inert.
 */
'use strict';

const API = '/api/v1';

const IDENTITIES = [
  { id: 'dana.buyer',     label: 'Dana — Buyer',             roles: 'BUYER' },
  { id: 'sam.senior',     label: 'Sam — Senior buyer',       roles: 'SENIOR_BUYER' },
  { id: 'priya.engineer', label: 'Priya — Engineer',         roles: 'ENGINEER' },
  { id: 'quinn.quality',  label: 'Quinn — Quality',          roles: 'QUALITY' },
  { id: 'alex.category',  label: 'Alex — Category manager',  roles: 'CATEGORY_MANAGER' },
  { id: 'jordan.head',    label: 'Jordan — Procurement head',roles: 'PROCUREMENT_HEAD' },
  { id: 'morgan.finance', label: 'Morgan — Finance',         roles: 'FINANCE' },
  { id: 'admin',          label: 'Admin (all permissions)',  roles: 'ADMIN' },
];

const PIPELINE = [
  ['RECEIVED', 'Received'],
  ['VALIDATING_PR', 'Validate PR'],
  ['WAITING_FOR_ENGINEERING', 'Engineering'],
  ['SOURCING_STRATEGY', 'Sourcing'],
  ['READY_FOR_RFQ', 'RFQ ready'],
  ['WAITING_FOR_QUOTES', 'Quotes'],
  ['TECHNICAL_EVALUATION', 'Tech eval'],
  ['WAITING_FOR_TECHNICAL_APPROVAL', 'Tech approval'],
  ['COMMERCIAL_EVALUATION', 'Commercial'],
  ['NEGOTIATION', 'Negotiation'],
  ['WAITING_FOR_AWARD_APPROVAL', 'Award'],
  ['PO_RECOMMENDATION', 'PO draft'],
  ['ORDER_PLACED', 'Ordered'],
  ['COMPLETED', 'Done'],
];
const HUMAN_GATES = new Set([
  'WAITING_FOR_ENGINEERING',
  'WAITING_FOR_TECHNICAL_APPROVAL',
  'WAITING_FOR_AWARD_APPROVAL',
]);

let identity = IDENTITIES[0];
let currentCaseId = null;

// ── plumbing ────────────────────────────────────────────────────────────────

function headers() {
  return {
    'Content-Type': 'application/json',
    'X-Actor-Id': identity.id,
    'X-Actor-Roles': identity.roles,
  };
}

async function api(path, options = {}) {
  const response = await fetch(API + path, { headers: headers(), ...options });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { message: text }; }
  if (!response.ok) {
    const error = new Error(body.message || `HTTP ${response.status}`);
    error.payload = body;
    error.status = response.status;
    throw error;
  }
  return body;
}

function toast(message, kind = '') {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = 'toast ' + kind;
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 8000 : 4000);
}

const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function money(value, currency = '') {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return esc(value);
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    + (currency ? ' ' + currency : '');
}

function pct(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) + '%' : esc(value);
}

const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined,
  { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—');

function statePill(state) {
  const kind = state === 'COMPLETED' ? 'ok'
    : state === 'CANCELLED' || state === 'FAILED' ? 'danger'
    : HUMAN_GATES.has(state) ? 'warn' : 'accent';
  return `<span class="pill ${kind}">${esc(state.replace(/_/g, ' '))}</span>`;
}

function show(viewName) {
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  document.getElementById('view-' + viewName)?.classList.add('active');
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.view === viewName));
}

// ── dashboard ───────────────────────────────────────────────────────────────

async function renderDashboard() {
  const el = document.getElementById('view-dashboard');
  el.innerHTML = '<div class="empty"><span class="spinner"></span> Loading…</div>';
  try {
    const [summary, cases, pending, spend] = await Promise.all([
      api('/cases/summary'),
      api('/cases?limit=100'),
      api('/mail/pending').catch(() => ({ messages: [] })),
      api('/data/spend?months=12').catch(() => null),
    ]);

    const awaiting = cases.items.filter((c) => HUMAN_GATES.has(c.state));
    const savings = cases.items.reduce((sum, c) => sum + Number(c.savings_base || 0), 0);

    el.innerHTML = `
      <div class="grid cols-4">
        <div class="stat"><div class="value">${summary.total}</div><div class="label">Total cases</div></div>
        <div class="stat"><div class="value">${summary.in_flight}</div><div class="label">In flight</div></div>
        <div class="stat ${awaiting.length ? 'attention' : ''}">
          <div class="value">${awaiting.length}</div><div class="label">Awaiting you</div></div>
        <div class="stat"><div class="value">${money(savings)}</div><div class="label">Recorded savings</div></div>
      </div>

      ${awaiting.length ? `
      <div class="card actions-needed">
        <header><h2>Waiting on a human decision</h2></header>
        ${awaiting.map((c) => `
          <div class="action-item">
            <div>
              <strong>${esc(c.case_id)}</strong> · ${esc(c.title || c.pr_number)}<br>
              <span class="muted">${statePill(c.state)} · updated ${when(c.updated_at)}</span>
            </div>
            <button class="btn primary small" onclick="openCase('${esc(c.case_id)}')">Review</button>
          </div>`).join('')}
      </div>` : '<div class="card"><p class="muted">Nothing is waiting on a human decision.</p></div>'}

      ${pending.messages?.length ? `
      <div class="card actions-needed">
        <header>
          <h2>Outbound mail held for release</h2>
          <span class="pill warn">${pending.messages.length} held</span>
        </header>
        <p class="muted">Automated external email is disabled, so the agent drafted these and stopped.</p>
        <button class="btn" onclick="switchTab('outbox')">Open outbox</button>
      </div>` : ''}

      <div class="grid cols-2">
        <div class="card">
          <header><h2>Cases by state</h2></header>
          <div class="table-wrap"><table><tbody>
            ${Object.entries(summary.counts_by_state).sort((a, b) => b[1] - a[1]).map(
              ([state, count]) => `<tr><td>${statePill(state)}</td><td class="num">${count}</td></tr>`).join('')}
          </tbody></table></div>
        </div>
        ${spend ? `
        <div class="card">
          <header><h2>Top suppliers by spend</h2><span class="muted">last 12 months</span></header>
          <div class="table-wrap"><table>
            <thead><tr><th>Supplier</th><th class="num">Spend</th></tr></thead>
            <tbody>${(spend.top_vendors || []).slice(0, 8).map((v) => `
              <tr><td>${esc(v.vendor_name || v.vendor_id)}</td>
                  <td class="num">${money(v.spend_base)}</td></tr>`).join('')}
            </tbody></table></div>
        </div>` : ''}
      </div>`;
  } catch (error) {
    el.innerHTML = `<div class="card"><p class="pill danger">${esc(error.message)}</p></div>`;
  }
}

// ── case list ───────────────────────────────────────────────────────────────

async function renderCases() {
  const el = document.getElementById('view-cases');
  el.innerHTML = '<div class="empty"><span class="spinner"></span> Loading…</div>';
  try {
    const data = await api('/cases?limit=200');
    el.innerHTML = `
      <div class="card">
        <header>
          <h2>Open a case from a requisition</h2>
          <div class="row">
            <label class="muted" for="upload-plant">Plant</label>
            <input id="upload-plant" value="1000" style="width:5.5rem" />
            <label class="muted"><input type="checkbox" id="upload-start" checked /> start workflow</label>
          </div>
        </header>
        <div id="dropzone" class="dropzone">
          <p><strong>Drop a requisition here</strong> — CSV, JSON or a saved e-mail</p>
          <p class="muted">Columns are matched by alias, so SAP names work directly:
             <span class="mono">matnr, werks, lgort, menge, meins, eddat</span>.</p>
          <input type="file" id="upload-file" accept=".csv,.tsv,.json,.eml,.txt,text/csv,application/json,message/rfc822" />
        </div>
        <div id="upload-status"></div>
      </div>

      <div class="card">
        <header>
          <h2>Sourcing cases</h2>
          <div class="row">
            <input id="case-filter" placeholder="Filter by id, PR or title" />
            <button class="btn" onclick="renderCases()">Refresh</button>
          </div>
        </header>
        <div class="table-wrap">
          <table id="case-table">
            <thead><tr>
              <th>Case</th><th>PR</th><th>Title</th><th>State</th>
              <th class="num">Estimated</th><th class="num">Awarded</th>
              <th class="num">Savings</th><th>Updated</th>
            </tr></thead>
            <tbody>
              ${data.items.map((c) => `
                <tr class="clickable" onclick="openCase('${esc(c.case_id)}')">
                  <td class="mono">${esc(c.case_id)}</td>
                  <td class="mono">${esc(c.pr_number)}</td>
                  <td>${esc((c.title || '').slice(0, 60))}</td>
                  <td>${statePill(c.state)}</td>
                  <td class="num">${money(c.estimated_value_base)}</td>
                  <td class="num">${Number(c.awarded_value_base) ? money(c.awarded_value_base) : '—'}</td>
                  <td class="num">${Number(c.savings_base) ? money(c.savings_base) : '—'}</td>
                  <td class="nowrap muted">${when(c.updated_at)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
        ${data.items.length ? '' : '<div class="empty">No cases yet. Seed demo data or POST a requisition.</div>'}
      </div>`;

    document.getElementById('case-filter')?.addEventListener('input', (event) => {
      const needle = event.target.value.toLowerCase();
      document.querySelectorAll('#case-table tbody tr').forEach((row) => {
        row.style.display = row.textContent.toLowerCase().includes(needle) ? '' : 'none';
      });
    });

    const zone = document.getElementById('dropzone');
    const picker = document.getElementById('upload-file');
    picker?.addEventListener('change', (event) => uploadRequisition(event.target.files[0]));
    ['dragenter', 'dragover'].forEach((name) =>
      zone?.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.add('dragging');
      }));
    ['dragleave', 'drop'].forEach((name) =>
      zone?.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.remove('dragging');
      }));
    zone?.addEventListener('drop', (event) =>
      uploadRequisition(event.dataTransfer?.files?.[0]));
  } catch (error) {
    el.innerHTML = `<div class="card"><p class="pill danger">${esc(error.message)}</p></div>`;
  }
}

async function uploadRequisition(file) {
  if (!file) return;
  const status = document.getElementById('upload-status');
  const plant = document.getElementById('upload-plant')?.value.trim() || '';
  const startWorkflow = document.getElementById('upload-start')?.checked ? 'true' : 'false';
  status.innerHTML = `<p><span class="spinner"></span> Parsing ${esc(file.name)}…</p>`;

  const form = new FormData();
  form.append('file', file);
  form.append('plant_code', plant);
  form.append('source_channel', 'UPLOAD');
  form.append('start_workflow', startWorkflow);

  try {
    // Content-Type is deliberately omitted: the browser must set the multipart
    // boundary itself, so headers() cannot be reused here.
    const response = await fetch(`${API}/cases/upload`, {
      method: 'POST',
      headers: { 'X-Actor-Id': identity.id, 'X-Actor-Roles': identity.roles },
      body: form,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`);

    const warnings = body.warnings || [];
    const workflow = body.workflow || {};
    status.innerHTML = `
      <div class="card sub">
        <p><strong>Case ${esc(body.case_id)}</strong> opened from ${esc(file.name)} —
           ${esc(String(body.line_count))} line(s), parsed as ${esc(body.source_format)}
           at confidence ${esc(String(body.parse_confidence))}.</p>
        ${workflow.workflow_id
          ? `<p class="pill ok">Workflow ${esc(workflow.workflow_id)} started</p>`
          : (workflow.error
              ? `<p class="pill warn">Workflow not started: ${esc(String(workflow.error).slice(0, 120))}</p>`
              : '')}
        ${warnings.length
          ? `<p class="muted">Parser notes:</p><ul>${warnings
              .map((w) => `<li class="muted">${esc(w)}</li>`).join('')}</ul>`
          : ''}
        <button class="btn primary small" onclick="openCase('${esc(body.case_id)}')">Open case</button>
      </div>`;
    toast(`Case ${body.case_id} opened`, 'ok');
  } catch (error) {
    status.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
    toast(error.message, 'danger');
  }
}

// ── case detail ─────────────────────────────────────────────────────────────

async function openCase(caseId) {
  currentCaseId = caseId;
  show('case');
  const el = document.getElementById('view-case');
  el.innerHTML = '<div class="empty"><span class="spinner"></span> Loading case…</div>';
  try {
    const detail = await api(`/cases/${encodeURIComponent(caseId)}`);
    el.innerHTML = renderCaseDetail(detail);
  } catch (error) {
    el.innerHTML = `<div class="card"><p class="pill danger">${esc(error.message)}</p>
      <button class="btn" onclick="switchTab('cases')">Back to cases</button></div>`;
  }
}

function renderCaseDetail(d) {
  const c = d.case;
  const currentIndex = PIPELINE.findIndex(([state]) => state === c.state);

  const pipeline = PIPELINE.map(([state, label], index) => {
    let cls = 'step';
    if (index < currentIndex) cls += ' done';
    else if (index === currentIndex) cls += HUMAN_GATES.has(state) ? ' blocked' : ' current';
    return `<div class="${cls}" title="${esc(state)}">${esc(label)}</div>`;
  }).join('');

  return `
    <div class="card">
      <header>
        <div>
          <h2>${esc(c.case_id)} · ${esc(c.title || c.pr_number)}</h2>
          <p class="muted">PR ${esc(c.pr_number)} · plant ${esc(c.plant_code || '—')} ·
             ${statePill(c.state)}
             ${c.commercial_unlocked
               ? '<span class="pill ok">commercial unlocked</span>'
               : '<span class="pill sealed">bids sealed</span>'}</p>
        </div>
        <div class="row">
          <button class="btn" onclick="openCase('${esc(c.case_id)}')">Refresh</button>
          <button class="btn ghost" onclick="switchTab('cases')">Back</button>
        </div>
      </header>
      <div class="pipeline">${pipeline}</div>
      <div class="grid cols-4">
        <div class="stat"><div class="value">${money(c.estimated_value_base)}</div><div class="label">Estimated ${esc(c.base_currency)}</div></div>
        <div class="stat"><div class="value">${Number(c.awarded_value_base) ? money(c.awarded_value_base) : '—'}</div><div class="label">Awarded</div></div>
        <div class="stat"><div class="value">${Number(c.savings_base) ? money(c.savings_base) : '—'}</div><div class="label">Savings vs benchmark</div></div>
        <div class="stat"><div class="value">${c.negotiation_round}</div><div class="label">Negotiation rounds</div></div>
      </div>
    </div>

    ${renderPendingActions(d)}
    ${renderSecurityFindings(d)}
    ${renderRequisition(d)}
    ${renderRequirements(d)}
    ${renderShortlist(d)}
    ${renderRfq(d)}
    ${renderQuotations(d)}
    ${renderMatrix(d)}
    ${renderRanking(d)}
    ${renderNegotiations(d)}
    ${renderRecommendation(d)}
    ${renderApprovals(d)}
    <div class="card">
      <header><h2>Explainability</h2></header>
      <div class="row">
        <button class="btn small" onclick="loadDecisions('${esc(c.case_id)}')">Agent decisions &amp; evidence</button>
        <button class="btn small" onclick="loadAudit('${esc(c.case_id)}')">Audit trail</button>
        <button class="btn small" onclick="loadCorrespondence('${esc(c.case_id)}')">Correspondence</button>
      </div>
      <div id="explain-panel"></div>
    </div>`;
}

function renderPendingActions(d) {
  if (!d.pending_actions?.length) return '';
  return `
    <div class="card actions-needed">
      <header><h2>Your decision is required</h2></header>
      ${d.pending_actions.map((a) => `
        <div class="action-item">
          <div>
            <strong>${esc(a.action.replace(/_/g, ' '))}</strong><br>
            <span class="muted">${esc(a.description)}</span>
            ${a.blocked ? '<br><span class="pill warn">blocked: approval chain incomplete</span>' : ''}
          </div>
          ${renderActionButton(d.case.case_id, a)}
        </div>`).join('')}
    </div>`;
}

function renderActionButton(caseId, action) {
  const handlers = {
    RFQ_RELEASE: `approve('${caseId}','rfq-release','Release the RFQ to the invited suppliers')`,
    TECHNICAL_APPROVAL: `approve('${caseId}','technical','Approving unseals every supplier\\'s commercial bid. This cannot be undone.')`,
    AWARD_APPROVAL: `approveAward('${caseId}')`,
    PO_RELEASE: `releasePo('${caseId}')`,
    ENGINEERING_INPUT: `engineeringReady('${caseId}')`,
    RELEASE_EMAIL: `switchTab('outbox')`,
  };
  const handler = handlers[action.action];
  if (!handler) return '';
  return `<button class="btn primary small" ${action.blocked ? 'disabled' : ''} onclick="${handler}">Act</button>`;
}

function renderSecurityFindings(d) {
  const findings = d.security_findings || [];
  if (!findings.length) return '';
  const critical = findings.filter((f) => f.severity === 'CRITICAL' || f.severity === 'HIGH');
  return `
    <div class="card" style="border-left:3px solid var(--danger)">
      <header>
        <h2>Document firewall findings</h2>
        <span class="pill ${critical.length ? 'danger' : 'warn'}">${findings.length} finding(s)</span>
      </header>
      <p class="muted">Supplier-controlled content that tried to influence the evaluation, or that
         needs independent verification.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Type</th><th>Severity</th><th>Detail</th><th>Disposition</th></tr></thead>
        <tbody>${findings.map((f) => `
          <tr>
            <td class="mono">${esc(f.finding_type)}</td>
            <td><span class="pill ${f.severity === 'CRITICAL' || f.severity === 'HIGH' ? 'danger' : 'warn'}">${esc(f.severity)}</span></td>
            <td>${esc(f.detail)}</td>
            <td class="muted">${esc(f.disposition)}</td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderRequisition(d) {
  const r = d.requisition;
  if (!r) return '';
  return `
    <div class="card">
      <header>
        <h2>Requisition ${esc(r.pr_number)}</h2>
        <span class="muted">${esc(r.requester)} · ${esc(r.source_channel)} ·
          parse confidence ${pct(Number(r.parse_confidence) * 100)}</span>
      </header>
      ${(r.parse_warnings || []).length ? `<p class="pill warn">${esc(r.parse_warnings[0])}</p>` : ''}
      <div class="table-wrap"><table>
        <thead><tr><th>#</th><th>Material</th><th>Description</th><th class="num">Qty</th>
          <th>UOM</th><th>Required</th><th>Validation</th></tr></thead>
        <tbody>${r.lines.map((line) => `
          <tr>
            <td>${line.line_number}</td>
            <td class="mono">${esc(line.resolved_material_code || line.material_code || '—')}</td>
            <td>${esc(line.description)}</td>
            <td class="num">${esc(line.quantity)}</td>
            <td>${esc(line.uom)}</td>
            <td class="nowrap">${line.required_date ? when(line.required_date).split(',')[0] : '—'}</td>
            <td>
              <span class="pill ${line.validation_status === 'VALID' || line.validation_status === 'RESOLVED' ? 'ok'
                : line.validation_status === 'BLOCKED' || line.validation_status === 'NOT_FOUND' ? 'danger' : 'warn'}">
                ${esc(line.validation_status)}</span>
              ${(line.validation_messages || []).length
                ? `<details><summary>${line.validation_messages.length} note(s)</summary>
                     <ul>${line.validation_messages.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></details>`
                : ''}
            </td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderRequirements(d) {
  if (!d.requirements?.length) return '';
  const mandatory = d.requirements.filter((r) => r.obligation === 'MANDATORY');
  return `
    <div class="card">
      <header>
        <h2>Technical requirements</h2>
        <span class="muted">${d.requirements.length} extracted · ${mandatory.length} mandatory</span>
      </header>
      <details>
        <summary>Show requirements</summary>
        <div class="table-wrap"><table>
          <thead><tr><th>Ref</th><th>Attribute</th><th>Requirement</th><th>Obligation</th>
            <th>Kind</th><th>Source</th><th class="num">Conf.</th></tr></thead>
          <tbody>${d.requirements.map((r) => `
            <tr>
              <td class="mono">${esc(r.requirement_key)}</td>
              <td>${esc(r.attribute)}</td>
              <td class="mono">${esc(describeRequirement(r))}</td>
              <td><span class="pill ${r.obligation === 'MANDATORY' ? 'danger' : ''}">${esc(r.obligation)}</span></td>
              <td class="muted">${esc(r.kind)}</td>
              <td class="muted">${esc(r.source_location || '—')}</td>
              <td class="num">${pct(Number(r.extraction_confidence) * 100)}</td>
            </tr>`).join('')}</tbody>
        </table></div>
      </details>
    </div>`;
}

function describeRequirement(r) {
  const unit = r.uom ? ' ' + r.uom : '';
  switch (r.operator) {
    case 'GTE': return `>= ${r.target_numeric}${unit}`;
    case 'LTE': return `<= ${r.target_numeric}${unit}`;
    case 'RANGE': return `[${r.lower_numeric}, ${r.upper_numeric}]${unit}`;
    case 'TOLERANCE': return `${r.target_numeric} +${r.tolerance_plus}/-${r.tolerance_minus}${unit}`;
    case 'ONE_OF': return `one of ${(r.allowed_values || []).join(' | ')}`;
    case 'BOOLEAN': return 'required';
    case 'PRESENT': return 'must be stated';
    default: return `${r.target_value}${unit}`;
  }
}

function renderShortlist(d) {
  if (!d.candidates?.length) return '';
  return `
    <div class="card">
      <header>
        <h2>Supplier shortlist</h2>
        <span class="muted">${d.candidates.filter((c) => c.selected).length} of ${d.candidates.length} selected</span>
      </header>
      <div class="table-wrap"><table>
        <thead><tr><th></th><th>#</th><th>Supplier</th><th class="num">Score</th>
          <th>Source</th><th>Rationale</th></tr></thead>
        <tbody>${d.candidates.map((c) => `
          <tr>
            <td>${c.selected ? '<span class="pill ok">invited</span>'
                 : c.excluded_reason ? '<span class="pill danger">excluded</span>' : ''}</td>
            <td>${c.rank}</td>
            <td><strong>${esc(c.vendor_name || c.vendor_id)}</strong><br><span class="mono muted">${esc(c.vendor_id)}</span></td>
            <td class="num">${(Number(c.total_score) * 100).toFixed(1)}</td>
            <td class="muted">${esc(c.selection_source)}</td>
            <td class="muted">${esc(c.excluded_reason || c.rationale || '')}</td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderRfq(d) {
  const rfq = d.rfq;
  if (!rfq) return '';
  return `
    <div class="card">
      <header>
        <h2>RFQ ${esc(rfq.rfq_number)}</h2>
        <span class="row">
          <span class="pill ${rfq.status === 'ISSUED' ? 'ok' : 'warn'}">${esc(rfq.status)}</span>
          ${rfq.sealed_bid ? '<span class="pill sealed">sealed bid</span>' : ''}
        </span>
      </header>
      <p class="muted">Deadline ${when(rfq.response_deadline)} ·
        ${esc(rfq.required_incoterm)} · target terms ${esc(rfq.payment_terms_target)}
        ${rfq.released_by ? `· released by ${esc(rfq.released_by)}` : ''}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Supplier</th><th>Contact</th><th>Status</th>
          <th class="num">Reminders</th><th>Sent</th><th>Responded</th></tr></thead>
        <tbody>${(rfq.invitations || []).map((i) => `
          <tr>
            <td>${esc(i.vendor_name || i.vendor_id)}</td>
            <td class="mono muted">${esc(i.contact_email)}</td>
            <td><span class="pill ${i.status === 'QUOTED' ? 'ok'
              : i.status === 'DECLINED' || i.status === 'NO_RESPONSE' ? 'danger' : 'warn'}">${esc(i.status)}</span></td>
            <td class="num">${i.reminders_sent}</td>
            <td class="nowrap muted">${when(i.sent_at)}</td>
            <td class="nowrap muted">${when(i.responded_at)}</td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderQuotations(d) {
  if (!d.quotations?.length) return '';
  const sealed = d.quotations.some((q) => q.is_sealed);
  return `
    <div class="card">
      <header>
        <h2>Quotations</h2>
        ${sealed ? '<span class="pill sealed">commercial data sealed until technical approval</span>' : ''}
      </header>
      <div class="table-wrap"><table>
        <thead><tr><th>Supplier</th><th>Status</th><th class="num">Tech score</th>
          <th>Qualified</th><th class="num">Total</th><th>Incoterm</th>
          <th class="num">Lead time</th><th>Received</th></tr></thead>
        <tbody>${d.quotations.map((q) => `
          <tr>
            <td><strong>${esc(q.vendor_name || q.vendor_id)}</strong><br>
                <span class="mono muted">${esc(q.vendor_id)} rev ${q.revision}</span></td>
            <td><span class="pill">${esc(q.status)}</span></td>
            <td class="num">${q.technical_score ?? '—'}</td>
            <td>${q.technically_qualified === null ? '—'
              : q.technically_qualified ? '<span class="pill ok">yes</span>'
              : `<span class="pill danger">no</span>`}</td>
            <td class="num">${q.is_sealed ? '<span class="pill sealed">sealed</span>' : money(q.total_amount, q.currency)}</td>
            <td>${esc(q.incoterm || '—')}</td>
            <td class="num">${q.lead_time_days || '—'}</td>
            <td class="nowrap muted">${when(q.received_at)}</td>
          </tr>
          ${(q.disqualification_reasons || []).length ? `
          <tr><td colspan="8" class="muted">
            <details><summary>${q.disqualification_reasons.length} blocking issue(s)</summary>
              <ul>${q.disqualification_reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
            </details></td></tr>` : ''}`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderMatrix(d) {
  if (!d.requirements?.length || !d.quotations?.length) return '';
  return `
    <div class="card">
      <header>
        <h2>Technical comparison</h2>
        <button class="btn small" onclick="loadMatrix('${esc(d.case.case_id)}')">Load matrix</button>
      </header>
      <div id="matrix-panel"><p class="muted">Load the requirement × supplier compliance matrix.</p></div>
    </div>`;
}

async function loadMatrix(caseId) {
  const panel = document.getElementById('matrix-panel');
  panel.innerHTML = '<span class="spinner"></span> Loading…';
  try {
    const m = await api(`/cases/${encodeURIComponent(caseId)}/comparison`);
    const vendors = m.evaluations.map((e) => e.vendor_id);
    if (!vendors.length) { panel.innerHTML = '<p class="muted">No evaluations yet.</p>'; return; }

    panel.innerHTML = `
      <div class="table-wrap"><table class="matrix">
        <thead><tr>
          <th>Requirement</th><th>Obligation</th>
          ${m.evaluations.map((e) => `<th>${esc(e.vendor_name || e.vendor_id)}<br>
            ${e.qualified ? '<span class="pill ok">qualified</span>' : '<span class="pill danger">disqualified</span>'}
            <br><span class="muted">score ${e.technical_score ?? '—'}</span></th>`).join('')}
        </tr></thead>
        <tbody>${m.requirements.map((r) => `
          <tr>
            <td><strong>${esc(r.requirement_key)}</strong> ${esc(r.attribute)}<br>
                <span class="mono muted">${esc(describeRequirement(r))}</span></td>
            <td><span class="pill ${r.obligation === 'MANDATORY' ? 'danger' : ''}">${esc(r.obligation.slice(0, 4))}</span></td>
            ${vendors.map((vendorId) => {
              const cell = (m.cells[r.requirement_id] || {})[vendorId];
              if (!cell) return '<td class="cell muted">—</td>';
              return `<td class="cell cell-${esc(cell.status)}" title="${esc(cell.rationale)}">
                <strong>${esc(cell.status.replace(/_/g, ' '))}</strong>
                ${cell.deviation_accepted ? '<br><span class="pill ok">deviation accepted</span>' : ''}
                ${cell.offered_value ? `<br><span class="muted">${esc(String(cell.offered_value).slice(0, 60))}</span>` : ''}
                ${cell.status === 'DEVIATION' && !cell.deviation_accepted && cell.assessment_id
                  ? `<br><button class="btn small" onclick="acceptDeviation('${esc(caseId)}','${esc(cell.assessment_id)}')">Accept deviation</button>`
                  : ''}
              </td>`;
            }).join('')}
          </tr>`).join('')}</tbody>
      </table></div>`;
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

function renderRanking(d) {
  if (!d.ranking?.length) return '';
  return `
    <div class="card">
      <header><h2>Bid tabulation — L1 / L2 / L3</h2>
        <span class="muted">total cost of ownership basis</span></header>
      <div class="table-wrap"><table>
        <thead><tr><th>Pos</th><th>Supplier</th><th class="num">Landed cost</th>
          <th class="num">TCO</th><th class="num">Δ vs L1</th><th class="num">Δ vs history</th>
          <th class="num">Tech</th><th class="num">Value score</th><th>Flags</th></tr></thead>
        <tbody>${d.ranking.map((r) => `
          <tr>
            <td><span class="pill ${r.position === 1 ? 'ok' : r.technically_qualified ? 'accent' : 'danger'}">
              ${esc(r.position_label || 'DQ')}</span></td>
            <td><strong>${esc(r.vendor_name || r.vendor_id)}</strong></td>
            <td class="num">${money(r.landed_cost_base)}</td>
            <td class="num"><strong>${money(r.tco_base)}</strong></td>
            <td class="num">${Number(r.delta_vs_l1_base) ? money(r.delta_vs_l1_base) + ' (' + pct(r.delta_vs_l1_pct) + ')' : '—'}</td>
            <td class="num">${pct(r.delta_vs_benchmark_pct)}</td>
            <td class="num">${r.technical_score ?? '—'}</td>
            <td class="num">${r.weighted_value_score ?? '—'}</td>
            <td>${(r.flags || []).slice(0, 3).map((f) => `<span class="pill warn">${esc(f.slice(0, 26))}</span>`).join(' ')}
                ${r.partial_offer ? '<span class="pill warn">partial</span>' : ''}</td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

function renderNegotiations(d) {
  if (!d.negotiations?.length) return '';
  return `
    <div class="card">
      <header><h2>Negotiation history</h2>
        <span class="muted">${d.negotiations.length} round(s)</span></header>
      ${d.negotiations.map((r) => `
        <details ${r.status !== 'CLOSED' ? 'open' : ''}>
          <summary>Round ${r.round_number} · ${esc(r.strategy)} ·
            <span class="pill ${r.status === 'CLOSED' ? 'ok' : 'warn'}">${esc(r.status)}</span>
            ${Number(r.savings_base) ? ` · saved ${money(r.savings_base)} (${pct(r.savings_pct)})` : ''}
          </summary>
          <p class="muted">${esc(r.rationale)}</p>
          <div class="table-wrap"><table>
            <thead><tr><th>Supplier</th><th class="num">Before</th><th class="num">Target</th>
              <th class="num">Achieved</th><th class="num">Reduction</th><th>Leverage used</th></tr></thead>
            <tbody>${r.targets.map((t) => `
              <tr>
                <td>${esc(t.vendor_id)}</td>
                <td class="num">${money(t.current_total_base)}</td>
                <td class="num">${money(t.target_total_base)}</td>
                <td class="num">${t.achieved_total_base ? money(t.achieved_total_base) : '—'}</td>
                <td class="num">${pct(t.achieved_reduction_pct)}</td>
                <td class="muted">${(t.leverage_points || []).map((p) => esc(p)).join('; ')}</td>
              </tr>`).join('')}</tbody>
          </table></div>
        </details>`).join('')}
    </div>`;
}

function renderRecommendation(d) {
  const r = d.po_recommendation;
  if (!r) return '';
  return `
    <div class="card">
      <header>
        <h2>PO recommendation ${esc(r.recommendation_number)}</h2>
        <span class="row">
          <span class="pill ${r.status === 'RELEASED' ? 'ok' : 'warn'}">${esc(r.status)}</span>
          ${r.approval_chain_satisfied
            ? '<span class="pill ok">approval chain satisfied</span>'
            : '<span class="pill danger">approval chain incomplete</span>'}
        </span>
      </header>
      <p><strong>${esc(r.vendor_name || r.vendor_id)}</strong>
        ${r.total_amount ? ` · ${money(r.total_amount, r.currency)}` : ''}
        ${r.savings_vs_benchmark_base ? ` · saving vs history ${money(r.savings_vs_benchmark_base)}` : ''}</p>

      ${r.lines ? `<div class="table-wrap"><table>
        <thead><tr><th>#</th><th>Material</th><th>Description</th><th class="num">Qty</th>
          <th>UOM</th><th class="num">Unit price</th><th class="num">Line total</th>
          <th class="num">vs history</th></tr></thead>
        <tbody>${r.lines.map((l) => `
          <tr>
            <td>${l.line_number}</td>
            <td class="mono">${esc(l.material_code)}</td>
            <td>${esc((l.description || '').slice(0, 50))}</td>
            <td class="num">${esc(l.quantity)}</td>
            <td>${esc(l.uom)}</td>
            <td class="num">${money(l.unit_price, l.currency)}</td>
            <td class="num">${money(l.line_total, l.currency)}</td>
            <td class="num">${pct(l.price_variance_pct)}</td>
          </tr>`).join('')}</tbody>
      </table></div>` : ''}

      <details><summary>Award justification</summary>
        <div class="justification">${esc(r.justification || '')}</div></details>
      ${r.sap_payload ? `<details><summary>SAP-ready payload</summary>
        <pre class="payload">${esc(JSON.stringify(r.sap_payload, null, 2))}</pre></details>` : ''}
      <details><summary>Approval chain</summary>
        <ul>${(r.approval_chain || []).map((a) => `<li><strong>${esc(a.approval_type)}</strong>
          — ${a.minimum_approvers} from ${esc((a.eligible_roles || []).join(', '))}
          <span class="muted">(${esc(a.reason)})</span></li>`).join('')}</ul></details>
      <div class="row">
        <button class="btn small" onclick="loadInfoRecords('${esc(d.case.case_id)}')">Info-record proposals</button>
      </div>
      <div id="info-record-panel"></div>
    </div>`;
}

function renderApprovals(d) {
  if (!d.approvals?.length) return '';
  return `
    <div class="card">
      <header><h2>Recorded approvals</h2></header>
      <div class="table-wrap"><table>
        <thead><tr><th>Type</th><th>Decision</th><th>Actor</th><th>Reason</th><th>When</th></tr></thead>
        <tbody>${d.approvals.map((a) => `
          <tr>
            <td class="mono">${esc(a.approval_type)}</td>
            <td><span class="pill ${a.decision.startsWith('APPROVED') ? 'ok' : 'danger'}">${esc(a.decision)}</span></td>
            <td>${esc(a.actor_id)}</td>
            <td class="muted">${esc(a.reason)}</td>
            <td class="nowrap muted">${when(a.created_at)}</td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`;
}

// ── approval flows ──────────────────────────────────────────────────────────

function askApproval({ title, description, extraHtml = '' }) {
  return new Promise((resolve) => {
    const dialog = document.getElementById('approval-dialog');
    document.getElementById('approval-title').textContent = title;
    document.getElementById('approval-description').textContent = description;
    document.getElementById('approval-extra').innerHTML = extraHtml;
    document.getElementById('approval-reason').value = '';
    document.getElementById('approval-reject').checked = false;

    const form = document.getElementById('approval-form');
    const cancel = document.getElementById('approval-cancel');

    const cleanup = () => {
      form.onsubmit = null;
      cancel.onclick = null;
      dialog.close();
    };
    form.onsubmit = (event) => {
      event.preventDefault();
      const reason = document.getElementById('approval-reason').value.trim();
      if (reason.length < 3) { toast('A reason is required', 'error'); return; }
      const extra = {};
      dialog.querySelectorAll('#approval-extra [name]').forEach((input) => {
        extra[input.name] = input.value;
      });
      cleanup();
      resolve({
        reason,
        decision: document.getElementById('approval-reject').checked ? 'REJECTED' : 'APPROVED',
        extra,
      });
    };
    cancel.onclick = () => { cleanup(); resolve(null); };
    dialog.showModal();
  });
}

async function approve(caseId, kind, description) {
  const answer = await askApproval({ title: 'Record your decision', description });
  if (!answer) return;
  try {
    const result = await api(`/cases/${encodeURIComponent(caseId)}/approvals/${kind}`, {
      method: 'POST',
      body: JSON.stringify({ reason: answer.reason, decision: answer.decision }),
    });
    toast(`${result.status}: ${result.detail || ''}`, 'success');
    openCase(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function approveAward(caseId) {
  let options = '';
  try {
    const detail = await api(`/cases/${encodeURIComponent(caseId)}`);
    options = (detail.ranking || []).filter((r) => r.technically_qualified)
      .map((r) => `<option value="${esc(r.vendor_id)}">${esc(r.position_label)} — ${esc(r.vendor_name || r.vendor_id)} — ${money(r.tco_base)}</option>`).join('');
  } catch { /* fall through to free-text entry */ }

  const answer = await askApproval({
    title: 'Approve award',
    description: 'Approving records your authorisation. Additional approvers may still be required by value.',
    extraHtml: `<label><span>Award to supplier</span>
      ${options ? `<select name="supplier_id">${options}</select>`
                : '<input name="supplier_id" placeholder="Vendor id" required />'}</label>`,
  });
  if (!answer) return;
  try {
    const result = await api(`/cases/${encodeURIComponent(caseId)}/approvals/award`, {
      method: 'POST',
      body: JSON.stringify({
        reason: answer.reason,
        decision: answer.decision,
        supplier_id: answer.extra.supplier_id,
      }),
    });
    toast(`${result.status}: ${result.detail || ''}`, result.status === 'APPROVED' ? 'success' : '');
    openCase(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function acceptDeviation(caseId, assessmentId) {
  const answer = await askApproval({
    title: 'Accept technical deviation',
    description: 'The supplier does not meet this requirement as written. Accepting it makes them eligible to win.',
  });
  if (!answer) return;
  try {
    const result = await api(`/cases/${encodeURIComponent(caseId)}/approvals/deviation`, {
      method: 'POST',
      body: JSON.stringify({
        reason: answer.reason, decision: answer.decision, assessment_id: assessmentId,
      }),
    });
    toast(result.detail || result.status, 'success');
    loadMatrix(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function releasePo(caseId) {
  const answer = await askApproval({
    title: 'Release purchase order',
    description: 'This records the PO as released for ERP creation. ProcureGuard never writes to SAP itself.',
    extraHtml: '<label><span>ERP purchase order number (optional)</span><input name="erp_po_number" placeholder="4500001234" /></label>',
  });
  if (!answer) return;
  try {
    const result = await api(`/cases/${encodeURIComponent(caseId)}/po/release`, {
      method: 'POST',
      body: JSON.stringify({ reason: answer.reason, erp_po_number: answer.extra.erp_po_number || '' }),
    });
    toast(`Released — case ${result.case_state}`, 'success');
    openCase(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function engineeringReady(caseId) {
  const answer = await askApproval({
    title: 'Confirm engineering input',
    description: 'Confirms the specification gap is closed and sourcing may continue.',
  });
  if (!answer) return;
  try {
    await api(`/cases/${encodeURIComponent(caseId)}/engineering-ready?note=${encodeURIComponent(answer.reason)}`,
      { method: 'POST' });
    toast('Engineering input recorded', 'success');
    openCase(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── explainability panels ───────────────────────────────────────────────────

async function loadDecisions(caseId) {
  const panel = document.getElementById('explain-panel');
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api(`/cases/${encodeURIComponent(caseId)}/decisions`);
    panel.innerHTML = data.decisions.map((d) => `
      <details>
        <summary>${esc(d.decision_type)} #${d.sequence}
          <span class="muted">· confidence ${pct(Number(d.confidence) * 100)} · ${when(d.created_at)}</span></summary>
        <p>${esc(d.rationale)}</p>
        <p class="mono muted">${esc(JSON.stringify(d.model_metadata))}</p>
        ${d.evidence.length ? `<h4>Evidence</h4>${d.evidence.map((e) => `
          <div class="evidence"><strong>${esc(e.evidence_type)}</strong>
            <span class="mono muted">${esc(e.evidence_id)}</span>
            <span class="pill">${esc(e.role)}</span>
            ${e.excerpt ? `<br>${esc(e.excerpt.slice(0, 240))}` : ''}</div>`).join('')}` : ''}
      </details>`).join('') || '<p class="muted">No decisions recorded yet.</p>';
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

async function loadAudit(caseId) {
  const panel = document.getElementById('explain-panel');
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api(`/cases/${encodeURIComponent(caseId)}/audit`);
    panel.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>When</th><th>Action</th><th>Actor</th><th>Entity</th><th>Detail</th></tr></thead>
      <tbody>${data.entries.map((e) => `
        <tr>
          <td class="nowrap muted">${when(e.created_at)}</td>
          <td class="mono">${esc(e.action)}</td>
          <td>${esc(e.actor_id)} <span class="pill">${esc(e.actor_type)}</span></td>
          <td class="muted">${esc(e.entity_type)}</td>
          <td class="muted">${esc((e.detail || '').slice(0, 140))}</td>
        </tr>`).join('')}</tbody></table></div>`;
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

async function loadCorrespondence(caseId) {
  const panel = document.getElementById('explain-panel');
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api(`/mail/case/${encodeURIComponent(caseId)}`);
    panel.innerHTML = data.messages.map((m) => `
      <details>
        <summary>${m.direction === 'OUTBOUND' ? '→' : '←'} ${esc(m.type)}
          <span class="muted">· ${esc(m.vendor_id || '')} · ${esc(m.status)} ·
          ${when(m.sent_at || m.received_at)}</span></summary>
        <p class="mono muted">${esc(m.subject)}</p>
        <pre class="payload">${esc(m.body_preview)}</pre>
      </details>`).join('') || '<p class="muted">No correspondence yet.</p>';
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

async function loadInfoRecords(caseId) {
  const panel = document.getElementById('info-record-panel');
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api(`/cases/${encodeURIComponent(caseId)}/po/info-records`);
    if (!data.proposals.length) { panel.innerHTML = '<p class="muted">No proposals.</p>'; return; }
    panel.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Material</th><th>Supplier</th><th>Action</th><th class="num">Net price</th>
        <th class="num">Previous</th><th class="num">Change</th><th>Status</th><th></th></tr></thead>
      <tbody>${data.proposals.map((p) => `
        <tr>
          <td class="mono">${esc(p.material_code)}</td>
          <td>${esc(p.vendor_id)}</td>
          <td><span class="pill">${esc(p.action)}</span></td>
          <td class="num">${money(p.net_price, p.currency)}</td>
          <td class="num">${p.previous_net_price ? money(p.previous_net_price, p.currency) : '—'}</td>
          <td class="num">${pct(p.price_change_pct)}</td>
          <td><span class="pill ${p.status === 'APPLIED' ? 'ok' : 'warn'}">${esc(p.status)}</span></td>
          <td>${p.status === 'APPLIED' ? '' :
            `<button class="btn small primary" onclick="applyInfoRecord('${esc(caseId)}','${esc(p.proposal_id)}')">Apply</button>`}</td>
        </tr>`).join('')}</tbody></table></div>`;
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

async function applyInfoRecord(caseId, proposalId) {
  try {
    const result = await api(`/cases/${encodeURIComponent(caseId)}/po/info-records/${proposalId}/apply`,
      { method: 'POST' });
    toast(`Info record ${result.info_record_number} maintained`, 'success');
    loadInfoRecords(caseId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── outbox ──────────────────────────────────────────────────────────────────

async function renderOutbox() {
  const el = document.getElementById('view-outbox');
  el.innerHTML = '<div class="empty"><span class="spinner"></span> Loading…</div>';
  try {
    const data = await api('/mail/pending');
    el.innerHTML = `
      <div class="card">
        <header>
          <h2>Outbound mail awaiting human release</h2>
          <div class="row">
            <button class="btn" onclick="pollInbox()">Poll inbox now</button>
            <button class="btn" onclick="renderOutbox()">Refresh</button>
          </div>
        </header>
        <p class="muted">Automated external email is disabled by default. The agent drafts the
           message in full; a human with EMAIL_SEND releases it.</p>
        ${data.messages.length ? data.messages.map((m) => `
          <details>
            <summary>${esc(m.type)} → ${esc((m.to || []).join(', '))}
              <span class="pill warn">${esc(m.status)}</span></summary>
            <p class="mono muted">${esc(m.subject)}</p>
            <pre class="payload">${esc(m.body_preview)}</pre>
            ${(m.attachments || []).length ? `<p class="muted">Attachments: ${m.attachments.map(esc).join(', ')}</p>` : ''}
            <button class="btn primary small" onclick="releaseMail('${esc(m.communication_id)}')">Release and send</button>
          </details>`).join('') : '<div class="empty">Nothing held.</div>'}
      </div>`;
  } catch (error) {
    el.innerHTML = `<div class="card"><p class="pill danger">${esc(error.message)}</p></div>`;
  }
}

async function releaseMail(communicationId) {
  try {
    const result = await api(`/mail/${communicationId}/release`, {
      method: 'POST', body: JSON.stringify({ reason: 'Reviewed and released' }),
    });
    toast(result.transmitted ? 'Message sent' : 'Not transmitted', result.transmitted ? 'success' : 'error');
    renderOutbox();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function pollInbox() {
  try {
    const result = await api('/mail/poll', { method: 'POST' });
    toast(`Processed ${result.processed} inbound message(s)`, 'success');
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── master data ─────────────────────────────────────────────────────────────

async function renderData() {
  const el = document.getElementById('view-data');
  el.innerHTML = `
    <div class="card">
      <header><h2>Material search</h2><span class="muted">lexical + vector over the SAP mirror</span></header>
      <div class="row">
        <input id="material-q" placeholder="e.g. gate valve DN50 stainless" style="min-width:320px" />
        <button class="btn primary" onclick="searchMaterials()">Search</button>
      </div>
      <div id="material-results"></div>
    </div>
    <div class="card">
      <header><h2>Price benchmark</h2><span class="muted">stage 3 historical purchasing tools</span></header>
      <div class="row">
        <input id="benchmark-code" placeholder="Material code" />
        <input id="benchmark-qty" type="number" value="100" min="1" style="width:110px" />
        <button class="btn primary" onclick="loadBenchmark()">Build benchmark</button>
      </div>
      <div id="benchmark-results"></div>
    </div>`;
  document.getElementById('material-q')?.addEventListener('keydown',
    (e) => { if (e.key === 'Enter') searchMaterials(); });
}

async function searchMaterials() {
  const q = document.getElementById('material-q').value.trim();
  const panel = document.getElementById('material-results');
  if (q.length < 2) { panel.innerHTML = '<p class="muted">Enter at least two characters.</p>'; return; }
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api(`/data/materials/search?q=${encodeURIComponent(q)}`);
    panel.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Code</th><th>Description</th><th>Group</th><th>UOM</th>
        <th>Status</th><th>Match</th><th class="num">Similarity</th><th></th></tr></thead>
      <tbody>${data.materials.map((m) => `
        <tr>
          <td class="mono">${esc(m.material_code)}</td>
          <td>${esc(m.description)}</td>
          <td class="muted">${esc(m.material_group_text || m.material_group)}</td>
          <td>${esc(m.base_uom)}</td>
          <td><span class="pill ${m.status === 'ACTIVE' ? 'ok' : 'warn'}">${esc(m.status)}</span></td>
          <td class="muted">${esc(m.match)}</td>
          <td class="num">${m.similarity ? m.similarity.toFixed(3) : '—'}</td>
          <td><button class="btn small" onclick="quickBenchmark('${esc(m.material_code)}')">Benchmark</button></td>
        </tr>`).join('')}</tbody></table></div>
      <p class="muted">${data.count} result(s)</p>`;
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

function quickBenchmark(code) {
  document.getElementById('benchmark-code').value = code;
  loadBenchmark();
  document.getElementById('benchmark-results').scrollIntoView({ behavior: 'smooth' });
}

async function loadBenchmark() {
  const code = document.getElementById('benchmark-code').value.trim();
  const qty = document.getElementById('benchmark-qty').value || 1;
  const panel = document.getElementById('benchmark-results');
  if (!code) { panel.innerHTML = '<p class="muted">Enter a material code.</p>'; return; }
  panel.innerHTML = '<span class="spinner"></span>';
  try {
    const b = await api(`/data/materials/${encodeURIComponent(code)}/benchmark?quantity=${qty}`);
    if (!b.has_history) {
      panel.innerHTML = `<p class="pill warn">No purchase history for ${esc(code)} — this would be a first buy.</p>`;
      return;
    }
    panel.innerHTML = `
      <div class="grid cols-4">
        <div class="stat"><div class="value">${money(b.benchmark_unit_price)}</div><div class="label">Benchmark unit price</div></div>
        <div class="stat"><div class="value">${money(b.should_cost)}</div><div class="label">Should-cost floor</div></div>
        <div class="stat"><div class="value">${money(b.target_price)}</div><div class="label">Negotiation target</div></div>
        <div class="stat"><div class="value">${b.order_count}</div><div class="label">Historical orders</div></div>
      </div>
      <p class="muted">Range ${money(b.min_unit_price)} – ${money(b.max_unit_price)} ·
        median ${money(b.median_unit_price)} · trend ${pct(b.price_trend_pct_per_year)}/yr ·
        volatility ${pct(b.volatility_pct)}</p>
      ${(b.notes || []).length ? `<ul>${b.notes.map((n) => `<li class="muted">${esc(n)}</li>`).join('')}</ul>` : ''}
      ${(b.vendors || []).length ? `<div class="table-wrap"><table>
        <thead><tr><th>Supplier</th><th class="num">Orders</th><th class="num">Avg price</th>
          <th class="num">On time</th><th class="num">Defect ppm</th><th>Last order</th></tr></thead>
        <tbody>${b.vendors.map((v) => `
          <tr>
            <td>${esc(v.vendor_name || v.vendor_id)}</td>
            <td class="num">${v.order_count}</td>
            <td class="num">${money(v.weighted_avg_unit_price)}</td>
            <td class="num">${v.on_time_pct !== null ? v.on_time_pct + '%' : '—'}</td>
            <td class="num">${v.defect_ppm ?? '—'}</td>
            <td class="nowrap muted">${when(v.last_order_date)}</td>
          </tr>`).join('')}</tbody></table></div>` : ''}`;
  } catch (error) {
    panel.innerHTML = `<p class="pill danger">${esc(error.message)}</p>`;
  }
}

// ── bootstrap ───────────────────────────────────────────────────────────────

function switchTab(view) {
  show(view);
  if (view === 'dashboard') renderDashboard();
  if (view === 'cases') renderCases();
  if (view === 'outbox') renderOutbox();
  if (view === 'data') renderData();
}

async function loadBackendBadges() {
  try {
    const health = await api('/health/ready');
    document.getElementById('backend-badges').innerHTML = `
      <span class="pill ${health.status === 'ok' ? 'ok' : 'warn'}">${esc(health.status)}</span>
      <span class="pill">db: ${esc(health.backends.vector)} vectors</span>
      <span class="pill">llm: ${esc(health.backends.llm)}</span>
      <span class="pill">mail: ${esc(health.backends.email)}</span>
      <span class="pill ${health.temporal.status === 'ok' ? 'ok' : 'warn'}">temporal: ${esc(health.temporal.status)}</span>`;
  } catch {
    document.getElementById('backend-badges').innerHTML = '<span class="pill danger">API unreachable</span>';
  }
}

function init() {
  const select = document.getElementById('actor-select');
  select.innerHTML = IDENTITIES.map(
    (i, index) => `<option value="${index}">${esc(i.label)}</option>`).join('');
  select.addEventListener('change', (event) => {
    identity = IDENTITIES[Number(event.target.value)];
    toast(`Now acting as ${identity.label}`);
    const active = document.querySelector('.tab.active')?.dataset.view || 'dashboard';
    if (currentCaseId && document.getElementById('view-case').classList.contains('active')) {
      openCase(currentCaseId);
    } else {
      switchTab(active);
    }
  });

  document.querySelectorAll('.tab').forEach((tab) =>
    tab.addEventListener('click', () => switchTab(tab.dataset.view)));

  loadBackendBadges();
  switchTab('dashboard');
}

document.addEventListener('DOMContentLoaded', init);

// Exposed for inline handlers.
Object.assign(window, {
  openCase, switchTab, approve, approveAward, acceptDeviation, releasePo,
  engineeringReady, loadMatrix, loadDecisions, loadAudit, loadCorrespondence,
  loadInfoRecords, applyInfoRecord, releaseMail, pollInbox, renderCases,
  renderOutbox, searchMaterials, loadBenchmark, quickBenchmark, uploadRequisition,
});
