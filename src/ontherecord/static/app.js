// On the Record: the GUI. Plain JavaScript, no build step, so it runs wherever Python does.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUS_TEXT = { cached: "reused", ran: "ran", run: "will run", "run-if-changed": "maybe", running: "running",
  queued: "queued", pending: "waiting", failed: "failed", blocked: "blocked", cancelled: "cancelled", done: "done" };

let STAGES = [], PROMPTS = [], DEFS = {};
let form = { name: "", description: "", urls: "", files: "", stages: [] };
let currentRun = null, selectedJob = null, runTimer = null, armedCancel = null;

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts = { ...opts, method: opts.method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.json) };
  }
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof body === "string" ? body : (body.detail || JSON.stringify(body)));
  return body;
}

function fmtDur(s) {
  if (s == null) return "";
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min` : `${m} min ${s % 60} s`;
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const secsBetween = (a, b) => (a && b ? (new Date(b) - new Date(a)) / 1000 : null);

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "jobs") refreshJobs();
  if (name === "results") refreshRuns();
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- build
function settingWidget(stageIdx, key, spec, value) {
  const id = `s${stageIdx}-${key}`;
  const changed = JSON.stringify(value) !== JSON.stringify(spec.default);
  const help = `<div class="help">${esc(spec.help)}${spec.in_key ? "" : " (does not change outputs)"}</div>`;
  let input;
  if (spec.kind === "file") {
    const opts = PROMPTS.map((p) => `<option ${p.file === value ? "selected" : ""}>${esc(p.file)}</option>`).join("");
    const known = PROMPTS.some((p) => p.file === value);
    input = `<select id="${id}">${known ? "" : `<option selected>${esc(value)}</option>`}${opts}</select>
      <button class="small secondary" data-edit-prompt="${stageIdx}:${key}">View or edit</button>`;
    return `<label class="wide ${changed ? "changed" : ""}">${esc(key)}${input}${help}</label>`;
  }
  if (spec.choices) {
    input = `<select id="${id}">${spec.choices.map((c, i) => `<option value="${i}" ${JSON.stringify(c) === JSON.stringify(value) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  } else if (typeof spec.default === "boolean") {
    return `<label class="${changed ? "changed" : ""}"><input type="checkbox" id="${id}" ${value ? "checked" : ""}>${esc(key)}${help}</label>`;
  } else if (typeof spec.default === "number") {
    input = `<input type="number" step="any" id="${id}" value="${esc(value)}">`;
  } else {
    input = `<input id="${id}" value="${esc(value ?? "")}" placeholder="${spec.default === null ? "not set" : ""}">`;
  }
  const wide = typeof value === "string" && value.length > 40 ? "wide" : "";
  return `<label class="${wide} ${changed ? "changed" : ""}">${esc(key)}${input}${help}</label>`;
}

function readWidget(stageIdx, key, spec) {
  const el = $(`#s${stageIdx}-${key}`);
  if (!el) return spec.default;
  if (spec.kind === "file") return el.value;
  if (spec.choices) return spec.choices[+el.value];
  if (typeof spec.default === "boolean") return el.checked;
  if (typeof spec.default === "number") return el.value === "" ? spec.default : Number(el.value);
  if (spec.default === null) {
    if (el.value.trim() === "") return null;
    const n = Number(el.value);
    return Number.isNaN(n) ? el.value : n;
  }
  return el.value;
}

function renderStages() {
  const made = new Set();
  $("#stages").innerHTML = form.stages.map((st, i) => {
    const d = DEFS[st.name];
    const missing = d.needs.filter((n) => !made.has(n));
    d.makes.forEach((m) => made.add(m));
    const rows = Object.entries(d.settings).map(([k, spec]) => settingWidget(i, k, spec, k in st.settings ? st.settings[k] : spec.default)).join("");
    return `<div class="stage">
      <div class="stage-head"><span class="num">${i + 1}</span><span class="title">${esc(d.label)}</span>
        <span class="flow">${d.needs.length ? esc(d.needs.join(", ")) + " → " : ""}${esc(d.makes.join(", "))}${d.gpu ? " · GPU" : ""}</span>
        <span class="tools">
          <button class="small secondary" data-move="${i}:-1" title="Move up">↑</button>
          <button class="small secondary" data-move="${i}:1" title="Move down">↓</button>
          <button class="small danger" data-remove="${i}" title="Remove">✕</button></span></div>
      ${missing.length ? `<div class="stage-doc" style="color:var(--fail)">Needs ${esc(missing.join(", "))} from an earlier stage.</div>` : ""}
      <div class="stage-doc">${esc(d.doc)}</div>
      <div class="settings">${rows}
        <label class="wide"><input type="checkbox" id="onfail-${i}" ${st.on_fail === "continue" ? "checked" : ""}>Run later stages even when one of this stage's checks fails (to measure the damage)</label>
      </div></div>`;
  }).join("") || `<p class="muted">No stages yet. Add one below.</p>`;
  updateYaml();
}

function readForm() {
  form.name = $("#r-name").value.trim();
  form.description = $("#r-desc").value.trim();
  form.urls = $("#r-urls").value;
  form.files = $("#r-files").value;
  form.stages.forEach((st, i) => {
    const d = DEFS[st.name];
    const s = {};
    for (const [k, spec] of Object.entries(d.settings)) {
      const v = readWidget(i, k, spec);
      if (JSON.stringify(v) !== JSON.stringify(spec.default)) s[k] = v;
    }
    st.settings = s;
    st.on_fail = $(`#onfail-${i}`)?.checked ? "continue" : "stop";
  });
}

// The recipe is written as YAML whose values are JSON, which YAML reads natively, so no YAML library is needed.
function toYaml() {
  const lines = [`name: ${JSON.stringify(form.name || "untitled")}`];
  if (form.description) lines.push(`description: ${JSON.stringify(form.description)}`);
  const urls = form.urls.split("\n").map((s) => s.trim()).filter(Boolean);
  const files = form.files.split("\n").map((s) => s.trim()).filter(Boolean);
  lines.push("source:");
  if (urls.length) lines.push("  urls:", ...urls.map((u) => `    - ${JSON.stringify(u)}`));
  if (files.length) lines.push("  files:", ...files.map((f) => `    - ${JSON.stringify(f)}`));
  lines.push("stages:");
  for (const st of form.stages) {
    lines.push(`  - ${st.name}: ${JSON.stringify(st.settings).replace(/":/g, '": ')}`);
    if (st.on_fail === "continue") lines.push("    on_fail: continue");
  }
  return lines.join("\n") + "\n";
}

function updateYaml() { $("#yaml").textContent = toYaml(); }

function msg(text, kind = "") { const m = $("#build-msg"); m.textContent = text; m.className = `msg ${kind}`; }

$("#stages").addEventListener("change", () => { readForm(); renderStages(); });
$("#stages").addEventListener("input", () => { readForm(); updateYaml(); });
["#r-name", "#r-desc", "#r-urls", "#r-files"].forEach((s) => $(s).addEventListener("input", () => { readForm(); updateYaml(); }));
$("#stages").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault();
  readForm();
  if (b.dataset.move) {
    const [i, dir] = b.dataset.move.split(":").map(Number);
    const j = i + dir;
    if (j >= 0 && j < form.stages.length) [form.stages[i], form.stages[j]] = [form.stages[j], form.stages[i]];
  } else if (b.dataset.remove) {
    form.stages.splice(+b.dataset.remove, 1);
  } else if (b.dataset.editPrompt) {
    const [i, key] = b.dataset.editPrompt.split(":");
    return editPrompt(+i, key);
  }
  renderStages();
});
$("#add-btn").addEventListener("click", () => {
  readForm();
  form.stages.push({ name: $("#add-stage").value, settings: {}, on_fail: "stop" });
  renderStages();
});

function editPrompt(i, key) {
  const st = form.stages[i];
  const file = st.settings[key] ?? DEFS[st.name].settings[key].default;
  const p = PROMPTS.find((x) => x.file === file);
  openModal(`<h3>${esc(key)} <span class="muted">${esc(file)}</span></h3>
    <p class="muted">Prompts are never changed in place. Saving makes a new file named after its contents, so every
    receipt that names a prompt can be read back as exactly the text that run used.</p>
    <textarea id="prompt-text" rows="22">${esc(p ? p.text : "")}</textarea>
    <div class="actions" style="margin-top:10px"><button id="prompt-save">Save as a new prompt and use it</button></div>`);
  $("#prompt-save").addEventListener("click", async () => {
    try {
      const r = await api("/api/prompts", { json: { name: file.split("/").pop(), text: $("#prompt-text").value } });
      PROMPTS = await api("/api/prompts");
      st.settings[key] = r.file;
      closeModal();
      renderStages();
      msg(`Saved ${r.file}`, "ok");
    } catch (err) { alert(err.message); }
  });
}

async function loadRecipe(file) {
  try {
    const text = await api(`/api/recipes/${encodeURIComponent(file)}`);
    const raw = await api("/api/raw", { json: { text } });
    const src = raw.source || {};
    form = { name: raw.name || "", description: (raw.description || "").trim(), urls: (src.urls || []).join("\n"),
      files: (src.files || []).join("\n"), stages: [] };
    for (const entry of raw.stages || []) {
      const e = typeof entry === "string" ? { [entry]: {} } : entry;
      const name = Object.keys(e).find((k) => DEFS[k]);
      if (name) form.stages.push({ name, settings: e[name] || {}, on_fail: e.on_fail || "stop" });
    }
    $("#r-name").value = form.name; $("#r-desc").value = form.description;
    $("#r-urls").value = form.urls; $("#r-files").value = form.files;
    renderStages();
    $("#plan").innerHTML = "";
    msg(`Loaded ${file}`, "ok");
  } catch (err) { msg(err.message, "err"); }
}
$("#recipe-pick").addEventListener("change", (e) => e.target.value && loadRecipe(e.target.value));

$("#plan-btn").addEventListener("click", async () => {
  readForm();
  try {
    const p = await api("/api/plan", { json: { text: toYaml() } });
    $("#plan").innerHTML = gridHtml(p.stages, p.cells, false) + legendHtml(true);
    msg("");
  } catch (err) { msg(err.message, "err"); }
});
$("#save-btn").addEventListener("click", async () => {
  readForm();
  try { const r = await api("/api/recipes", { json: { text: toYaml(), name: form.name } }); msg(`Saved as ${r.file}`, "ok"); await loadRecipeList(); }
  catch (err) { msg(err.message, "err"); }
});
$("#submit-btn").addEventListener("click", async () => {
  readForm();
  try {
    const r = await api("/api/jobs", { json: { text: toYaml() } });
    msg(`Submitted ${r.run_id}`, "ok");
    selectedJob = r.run_id;
    showTab("jobs");
  } catch (err) { msg(err.message, "err"); }
});

async function loadRecipeList() {
  const list = await api("/api/recipes");
  $("#recipe-pick").innerHTML = `<option value="">— a saved recipe —</option>` +
    list.map((r) => `<option value="${esc(r.file)}">${esc(r.name)}${r.builtin ? " (example)" : ""}</option>`).join("");
}

// ---------------------------------------------------------------- grid (plan and results)
function gridHtml(stages, cells, clickable) {
  const head = `<tr><th>item</th>${stages.map((s) => `<th>${esc(DEFS[s]?.label || s)}</th>`).join("")}</tr>`;
  const rows = Object.entries(cells).map(([item, row]) => `<tr><td class="item">${esc(item)}</td>${stages.map((s) => {
    const c = row[s] || {};
    const chk = c.checks && c.checks !== "ok" ? ` <span class="chk-${c.checks}">● ${c.checks}</span>` : "";
    const t = c.elapsed_s != null ? fmtDur(c.elapsed_s) : (c.status === "running" ? "…" : "");
    const attrs = clickable ? `data-item="${esc(item)}" data-stage="${esc(s)}"` : "";
    return `<td><span class="cell c-${esc(c.status)}" ${attrs}><span class="s">${esc(STATUS_TEXT[c.status] || c.status)}</span>${chk}<br><span class="t">${esc(t)}</span></span></td>`;
  }).join("")}</tr>`).join("");
  return `<table class="grid">${head}${rows}</table>`;
}
function legendHtml(plan) {
  return plan
    ? `<div class="legend"><span><b>reused</b>: same settings and same input bytes as an earlier output</span><span><b>will run</b>: something it depends on changed</span><span><b>maybe</b>: runs unless the stage before it produces byte-identical output</span><span><b>blocked</b>: an earlier check failed</span></div>`
    : `<div class="legend"><span>Click any cell for its receipt.</span><span><b>reused</b>: taken from an earlier run</span><span><b>ran</b>: computed in this run</span><span>● warn / fail: a check on that output</span></div>`;
}

// ---------------------------------------------------------------- jobs
async function refreshJobs() {
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch { return; }
  $("#jobs tbody").innerHTML = jobs.map((j) => {
    const counts = j.cells ? Object.entries(j.cells).map(([k, v]) => `${v} ${STATUS_TEXT[k] || k}`).join(", ") : "";
    const took = secsBetween(j.started_utc, j.finished_utc || (j.status === "running" ? new Date().toISOString() : null));
    const canCancel = j.status === "queued" || j.status === "running";
    return `<tr data-run="${esc(j.run_id)}" class="${j.run_id === selectedJob ? "sel" : ""}">
      <td><b>${esc(j.name)}</b><div class="hash">${esc(j.run_id)}</div></td>
      <td><span class="badge b-${esc(j.status)}">${esc(j.status)}</span>${j.cancel_requested && j.status === "running" ? ' <span class="muted">stopping…</span>' : ""}</td>
      <td class="muted">${esc(counts)}</td><td>${esc(fmtTime(j.created_utc))}</td><td>${esc(fmtDur(took))}</td>
      <td>${canCancel ? `<button class="small danger" data-cancel="${esc(j.run_id)}" ${armedCancel === j.run_id ? 'data-armed="1"' : ""}>${armedCancel === j.run_id ? "Click again to cancel" : "Cancel"}</button>` : `<button class="small secondary" data-open="${esc(j.run_id)}">Results</button>`}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No jobs yet. Build a run and submit it.</td></tr>`;
  if (selectedJob) refreshLog();
}
$("#jobs").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (b?.dataset.cancel) {
    // Two clicks rather than a browser dialog: the first arms the button, the second cancels.
    if (!b.dataset.armed) {
      b.dataset.armed = "1";
      b.textContent = "Click again to cancel";
      armedCancel = b.dataset.cancel;
      setTimeout(() => { if (armedCancel === b.dataset.cancel) armedCancel = null; }, 5000);
      return;
    }
    armedCancel = null;
    await api(`/api/jobs/${encodeURIComponent(b.dataset.cancel)}/cancel`, { method: "POST" });
    return refreshJobs();
  }
  if (b?.dataset.open) { currentRun = b.dataset.open; return showTab("results"); }
  const tr = e.target.closest("tr[data-run]");
  if (tr) { selectedJob = tr.dataset.run; refreshJobs(); }
});
async function refreshLog() {
  $("#job-log-wrap").hidden = false;
  $("#job-log-id").textContent = selectedJob;
  const pre = $("#job-log");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = await api(`/api/runs/${encodeURIComponent(selectedJob)}/log`).catch(() => "");
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------- results
async function refreshRuns() {
  const runs = await api("/api/runs").catch(() => []);
  if (!currentRun && runs.length) currentRun = runs[0].run_id;
  $("#runs").innerHTML = runs.map((r) => `<li data-run="${esc(r.run_id)}" class="${r.run_id === currentRun ? "sel" : ""}">
    <div class="n">${esc(r.name)} <span class="badge b-${esc(r.status)}">${esc(r.status)}</span></div>
    <div class="d">${esc(fmtTime(r.created_utc))} · ${r.items} item(s) · ${esc(r.host)}</div></li>`).join("") || `<li class="muted">No runs yet.</li>`;
  if (currentRun) showRun(currentRun);
}
$("#runs").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-run]");
  if (li) { currentRun = li.dataset.run; $("#receipt").innerHTML = ""; refreshRuns(); }
});

async function showRun(id) {
  const r = await api(`/api/runs/${encodeURIComponent(id)}`).catch(() => null);
  if (!r) return;
  const stageNames = r.stages.map((s) => s.name);
  $("#run-head").innerHTML = `<h3>${esc(r.name)} <span class="badge b-${esc(r.status)}">${esc(r.status)}</span></h3>
    <div class="muted">${esc(r.description || "")}</div>
    <div class="muted" style="font-size:12px;margin:4px 0 10px">${esc(r.run_id)} · ${esc(r.host)}${r.gpu ? " · " + esc(r.gpu.name) : ""} · started ${esc(fmtTime(r.created_utc))}${r.finished_utc ? " · took " + esc(fmtDur(secsBetween(r.created_utc, r.finished_utc))) : ""}</div>`;
  $("#grid").innerHTML = gridHtml(stageNames, r.cells, true) + legendHtml(false);
  clearTimeout(runTimer);
  if (r.status === "running" && $("#tab-results").classList.contains("active")) runTimer = setTimeout(() => showRun(id), 3000);
}

$("#grid").addEventListener("click", async (e) => {
  const c = e.target.closest(".cell[data-item]");
  if (!c) return;
  document.querySelectorAll(".cell.sel").forEach((x) => x.classList.remove("sel"));
  c.classList.add("sel");
  const run = await api(`/api/runs/${encodeURIComponent(currentRun)}`);
  const cell = run.cells[c.dataset.item][c.dataset.stage];
  if (!cell.receipt) {
    $("#receipt").innerHTML = `<div class="card"><b>${esc(c.dataset.stage)} · ${esc(c.dataset.item)}</b>: ${esc(STATUS_TEXT[cell.status] || cell.status)}
      ${cell.reason ? `<p>${esc(cell.reason)}</p>` : ""}${cell.error ? `<pre>${esc(cell.error)}</pre>` : ""}</div>`;
    return;
  }
  renderReceipt(await api(`/api/receipt?path=${encodeURIComponent(cell.receipt)}`), cell);
});

function kvRows(obj, defaults = {}) {
  return Object.entries(obj || {}).map(([k, v]) => {
    let shown = v;
    let extra = "";
    if (v && typeof v === "object" && "text" in v && "sha256" in v) {
      shown = `${v.file}  (sha256 ${v.sha256.slice(0, 12)}…)`;
      extra = `<details><summary>show text</summary><pre>${esc(v.text)}</pre></details>`;
    } else if (typeof v === "object") shown = JSON.stringify(v);
    const changed = k in defaults && JSON.stringify(defaults[k]) !== JSON.stringify(v && v.file ? v.file : v);
    return `<tr class="${changed ? "changed" : ""}"><td>${esc(k)}</td><td class="v">${esc(shown)}${extra}</td></tr>`;
  }).join("");
}

function renderReceipt(r, cell) {
  const d = DEFS[r.stage] || { settings: {} };
  const defaults = Object.fromEntries(Object.entries(d.settings).map(([k, s]) => [k, s.default]));
  const env = r.environment || {};
  const checks = (r.checks || []).map((c) => `<li><span class="badge b-${esc(c.status)}">${esc(c.status)}</span> <b>${esc(c.name)}</b>: ${esc(c.message)}</li>`).join("") || `<li class="muted">This stage has no checks.</li>`;
  const inputs = Object.entries(r.inputs || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="v">from ${esc(v.from_stage)} <span class="hash">${esc((v.sha256 || "").slice(0, 16))}</span></td></tr>`).join("") || `<tr><td colspan="2" class="muted">${esc(r.item.url || r.item.path || "")}</td></tr>`;
  const outputs = Object.entries(r.outputs || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="v">${esc(v.file.split(/[\\/]/).pop())} · ${(v.bytes / 1024).toFixed(0)} KB <span class="hash">${esc(v.sha256.slice(0, 16))}</span>
    <button class="small secondary" data-preview="${esc(k)}">Open</button></td></tr>`).join("");
  const pk = Object.entries(env.packages || {}).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(", ");
  $("#receipt").innerHTML = `<div class="card" data-receipt="${esc(cell.receipt)}">
    <h3>${esc(r.stage_label || r.stage)} · <span style="font-family:var(--mono)">${esc(r.item.id)}</span>
      <span class="badge b-${esc(cell.status)}">${esc(STATUS_TEXT[cell.status] || cell.status)}</span></h3>
    <div class="muted" style="font-size:12px">${cell.status === "cached" ? `Reused from run ${esc(r.run_id)}. ` : ""}Took ${esc(fmtDur(r.timing.elapsed_s))} on ${esc(env.hostname)}${r.device ? " (" + esc(r.device) + ")" : ""}, finished ${esc(fmtTime(r.timing.finished_utc))}. Stage version ${esc(r.stage_version)}.</div>
    <h4>Checks</h4><ul class="checks">${checks}</ul>
    <h4>Settings <span class="muted" style="font-weight:400">(blue = changed from the default)</span></h4><table class="kv">${kvRows(r.settings, defaults)}</table>
    <h4>Inputs</h4><table class="kv">${inputs}</table>
    <h4>Outputs</h4><table class="kv">${outputs}</table>
    <h4>Software and models</h4><table class="kv">${kvRows(r.tools)}
      <tr><td>packages</td><td class="v">${esc(pk)}</td></tr>
      <tr><td>python</td><td class="v">${esc(env.python)} · ${esc(env.os)}</td></tr>
      <tr><td>gpu</td><td class="v">${esc(env.gpu ? `${env.gpu.name} · ${env.gpu.memory} · driver ${env.gpu.driver}` : "none")}</td></tr>
      <tr><td>ontherecord</td><td class="v">${esc(env.ontherecord ? `${env.ontherecord.version} ${env.ontherecord.commit || ""}${env.ontherecord.dirty ? " (uncommitted changes)" : ""}` : "")}</td></tr></table>
    <h4>Measurements</h4><pre>${esc(JSON.stringify(r.metrics, null, 1))}</pre>
    <details><summary>The whole receipt (JSON)</summary><pre>${esc(JSON.stringify(r, null, 1))}</pre></details></div>`;
}

$("#receipt").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-preview]");
  if (!b) return;
  const receipt = b.closest("[data-receipt]").dataset.receipt;
  const name = b.dataset.preview;
  const url = `/api/output?receipt=${encodeURIComponent(receipt)}&name=${encodeURIComponent(name)}`;
  if (["audio", "media"].includes(name)) {
    return openModal(`<h3>${esc(name)}</h3><audio controls preload="metadata" style="width:100%" src="${url}"></audio>`);
  }
  const data = await api(url).catch((err) => `Could not open: ${err.message}`);
  openModal(`<h3>${esc(name)}</h3>${previewHtml(name, data)}`);
});

function table(rows, cols) {
  return `<table class="data"><tr>${cols.map(([h]) => `<th>${esc(h)}</th>`).join("")}</tr>${rows.map((r) =>
    `<tr>${cols.map(([, f]) => `<td>${esc(f(r))}</td>`).join("")}</tr>`).join("")}</table>`;
}
const hms = (s) => { s = Math.floor(s); return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
function previewHtml(name, d) {
  if (typeof d === "string") return `<pre>${esc(d)}</pre>`;
  if (name === "blocks") return `<p class="muted">${esc(d.title)} · ${d.blocks.length} blocks</p>` + table(d.blocks, [["#", (b) => b.block_id], ["time", (b) => `${hms(b.start)}–${hms(b.end)}`], ["speaker", (b) => b.speaker], ["category", (b) => b.category], ["text", (b) => b.text]]);
  if (name === "transcript") return table(d.segments, [["start", (s) => hms(s.start)], ["end", (s) => hms(s.end)], ["text", (s) => s.text], ["logprob", (s) => s.avg_logprob]]);
  if (name === "aligned") return table(d.segments, [["start", (s) => hms(s.start)], ["speaker", (s) => s.speaker], ["text", (s) => s.text]]);
  if (name === "comments") return `<p class="muted">${d.comments.length} comments · ${d.windows} window(s) · ${d.failed_windows} unparsed</p>` + table(d.comments, [["#", (c) => c.comment_number], ["time", (c) => c.timestamp], ["speaker", (c) => c.speaker_name], ["position", (c) => c.position], ["agenda item", (c) => c.agenda_item], ["summary", (c) => c.summary]]);
  return `<pre>${esc(JSON.stringify(d, null, 1))}</pre>`;
}

function openModal(html) { $("#modal-body").innerHTML = html; $("#modal").hidden = false; }
function closeModal() { $("#modal").hidden = true; $("#modal-body").innerHTML = ""; }
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

// ---------------------------------------------------------------- start
(async function init() {
  const [stages, prompts, machine] = await Promise.all([api("/api/stages"), api("/api/prompts"), api("/api/machine")]);
  STAGES = stages; PROMPTS = prompts;
  DEFS = Object.fromEntries(stages.map((s) => [s.name, s]));
  $("#add-stage").innerHTML = stages.map((s) => `<option value="${esc(s.name)}">${esc(s.label)}</option>`).join("");
  $("#machine").textContent = `${machine.gpu ? machine.gpu.name : "no GPU"} · workspace ${machine.workspace}`;
  await loadRecipeList();
  renderStages();
  setInterval(() => { if ($("#tab-jobs").classList.contains("active")) refreshJobs(); }, 2000);
})();
