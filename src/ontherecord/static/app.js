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
  if (name === "library") refreshLibrary();
  if (name === "import") refreshImport();
  if (name === "ask") $("#ask-input").focus();
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

const stepName = (st) => st.as || st.name;

function renderStages() {
  const producers = {};  // artifact -> names of the earlier stages that make it, in order
  const typeCount = {};
  form.stages.forEach((st) => { typeCount[st.name] = (typeCount[st.name] || 0) + 1; });
  $("#stages").innerHTML = form.stages.map((st, i) => {
    const d = DEFS[st.name];
    const from = st.from || {};
    const missing = d.needs.filter((n) => !(producers[n] || []).length);
    const wiring = d.needs.filter((n) => (producers[n] || []).length > 1 || from[n]).map((n) => {
      const opts = producers[n] || [];
      const latest = opts[opts.length - 1];
      const bad = from[n] && !opts.includes(from[n]);
      return `<label>${esc(n)} from <select id="from-${i}-${esc(n)}">
          <option value="">the latest (${esc(latest || "none")})</option>
          ${opts.map((o) => `<option ${from[n] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
          ${bad ? `<option selected value="${esc(from[n])}">${esc(from[n])} (not an earlier stage)</option>` : ""}</select></label>`;
    }).join("");
    const named = typeCount[st.name] > 1 || (st.as && st.as !== st.name);
    d.makes.forEach((m) => (producers[m] = producers[m] || []).push(stepName(st)));
    const rows = Object.entries(d.settings).map(([k, spec]) => settingWidget(i, k, spec, k in st.settings ? st.settings[k] : spec.default)).join("");
    return `<div class="stage">
      <div class="stage-head"><span class="num">${i + 1}</span><span class="title">${esc(d.label)}</span>
        ${named ? `<input class="step-name" id="as-${i}" value="${esc(stepName(st))}" title="This stage's name in the run, so later stages can take their input from it">` : ""}
        <span class="flow">${d.needs.length ? esc(d.needs.join(", ")) + " → " : ""}${esc(d.makes.join(", "))}${d.gpu ? " · GPU" : ""}</span>
        <span class="tools">
          <button class="small secondary" data-move="${i}:-1" title="Move up">↑</button>
          <button class="small secondary" data-move="${i}:1" title="Move down">↓</button>
          <button class="small danger" data-remove="${i}" title="Remove">✕</button></span></div>
      ${missing.length ? `<div class="stage-doc" style="color:var(--fail)">Needs ${esc(missing.join(", "))} from an earlier stage.</div>` : ""}
      ${wiring ? `<div class="wiring">${wiring}</div>` : ""}
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
    const as = $(`#as-${i}`);
    if (as) st.as = as.value.trim() === st.name ? "" : as.value.trim();
    st.from = {};
    for (const n of d.needs) { const sel = $(`#from-${i}-${n}`); if (sel && sel.value) st.from[n] = sel.value; }
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
    if (st.as && st.as !== st.name) lines.push(`    as: ${JSON.stringify(st.as)}`);
    if (Object.keys(st.from || {}).length) lines.push(`    from: ${JSON.stringify(st.from).replace(/":/g, '": ')}`);
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
  const type = $("#add-stage").value;
  const taken = new Set(form.stages.map(stepName));
  let as = "";
  for (let k = 2; taken.has(as || type); k++) as = `${type}_${k}`;
  form.stages.push({ name: type, settings: {}, on_fail: "stop", as, from: {} });
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
      if (name) form.stages.push({ name, settings: e[name] || {}, on_fail: e.on_fail || "stop", as: e.as || "", from: e.from || {} });
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
function colHead(s) {
  const type = s.stage || s.name;
  const label = s.label || DEFS[type]?.label || s.name;
  const wired = Object.entries(s.from || {}).filter(([n, src]) => src !== n && !(DEFS[src] && DEFS[src].makes.includes(n)));
  return esc(label) + (s.name !== type ? `<br><span class="step">${esc(s.name)}</span>` : "")
    + (wired.length ? `<br><span class="step">${wired.map(([n, src]) => `${esc(n)} ← ${esc(src)}`).join(", ")}</span>` : "");
}
function gridHtml(stages, cells, clickable) {
  const head = `<tr><th>item</th>${stages.map((s) => `<th>${colHead(s)}</th>`).join("")}</tr>`;
  const rows = Object.entries(cells).map(([item, row]) => `<tr><td class="item">${esc(item)}</td>${stages.map(({ name: s }) => {
    const c = row[s] || {};
    const mixed = c.mixed ? Object.entries(c.mixed).map(([a, st]) => `${a} from ${st.join(" and ")}`).join("; ") : "";
    const chk = (c.checks && c.checks !== "ok" ? ` <span class="chk-${c.checks}">● ${c.checks}</span>` : "")
      + (mixed && c.checks !== "fail" ? ` <span class="chk-warn" title="Its inputs trace back to ${esc(mixed)}; if those files differ, its same_source check fails">⚠ mixed</span>` : "");
    const t = c.elapsed_s != null ? fmtDur(c.elapsed_s) : (c.status === "running" ? "…" : "");
    const attrs = clickable ? `data-item="${esc(item)}" data-stage="${esc(s)}"` : "";
    return `<td><span class="cell c-${esc(c.status)}" ${attrs}><span class="s">${esc(STATUS_TEXT[c.status] || c.status)}</span>${chk}<br><span class="t">${esc(t)}</span></span></td>`;
  }).join("")}</tr>`).join("");
  return `<table class="grid">${head}${rows}</table>`;
}
function legendHtml(plan) {
  return plan
    ? `<div class="legend"><span><b>reused</b>: same settings and same input bytes as an earlier output</span><span><b>will run</b>: something it depends on changed</span><span><b>maybe</b>: runs unless the stage before it produces byte-identical output</span><span><b>blocked</b>: an earlier check failed</span><span><b>⚠ mixed</b>: its inputs come from two different stages that make the same thing</span></div>`
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
  const stageCols = r.stages;
  $("#run-head").innerHTML = `<h3>${esc(r.name)} <span class="badge b-${esc(r.status)}">${esc(r.status)}</span></h3>
    <div class="muted">${esc(r.description || "")}</div>
    <div class="muted" style="font-size:12px;margin:4px 0 10px">${esc(r.run_id)} · ${esc(r.host)}${r.gpu ? " · " + esc(r.gpu.name) : ""} · started ${esc(fmtTime(r.created_utc))}${r.finished_utc ? " · took " + esc(fmtDur(secsBetween(r.created_utc, r.finished_utc))) : ""}</div>`;
  $("#grid").innerHTML = gridHtml(stageCols, r.cells, true) + legendHtml(false);
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
  const li = (c) => `<li><span class="badge b-${esc(c.status)}">${esc(c.status)}</span> <b>${esc(c.name)}</b>: ${esc(c.message)}</li>`;
  const checks = [...(cell.run_checks || []), ...(r.checks || [])].map(li).join("") || `<li class="muted">This stage has no checks.</li>`;
  const inputs = Object.entries(r.inputs || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="v">from ${esc(v.from_step || v.from_stage)} <span class="hash">${esc((v.sha256 || "").slice(0, 16))}</span></td></tr>`).join("") || `<tr><td colspan="2" class="muted">${esc(r.item.url || r.item.path || "")}</td></tr>`;
  const outputs = Object.entries(r.outputs || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="v">${esc(v.file.split(/[\\/]/).pop())} · ${(v.bytes / 1024).toFixed(0)} KB <span class="hash">${esc(v.sha256.slice(0, 16))}</span>
    <button class="small secondary" data-preview="${esc(k)}">Open</button></td></tr>`).join("");
  const pk = Object.entries(env.packages || {}).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(", ");
  $("#receipt").innerHTML = `<div class="card" data-receipt="${esc(cell.receipt)}">
    <h3>${esc(r.stage_label || r.stage)} · <span style="font-family:var(--mono)">${esc(r.item.id)}</span>
      <span class="badge b-${esc(cell.status)}">${esc(STATUS_TEXT[cell.status] || cell.status)}</span></h3>
    <div><button class="small secondary" data-player="${esc(currentRun)}|${esc(r.item.id)}">Open in the player</button></div>
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
  const pl = e.target.closest("button[data-player]");
  if (pl) { const [run, item] = pl.dataset.player.split("|"); return openPlayer(run, item); }
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


// ---------------------------------------------------------------- library and player
// A recording is played from the audio its transcript was made from (what the models heard), so the transcript lines
// up with it exactly; the original download is one click away. Deep links: #play/RUN/ITEM/SECONDS.
let LIB = [], playing = null, lineIdx = -1, searchTimer = null;
const PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#7f7f7f",
  "#bcbd22", "#3949ab", "#00897b", "#c2185b", "#5d4037", "#689f38", "#f9a825"];
const hm = (s) => (s == null ? "" : s >= 3600 ? hms(s) : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
const outputUrl = (o) => `/api/output?receipt=${encodeURIComponent(o.receipt)}&name=${encodeURIComponent(o.name)}`;
const marked = (text, words) => words.reduce((t, w) => t.replace(new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"), "<mark>$1</mark>"), esc(text));

async function refreshLibrary() {
  LIB = await api("/api/library").catch(() => []);
  $("#recordings").innerHTML = LIB.map((r) => `<li data-rec="${esc(r.id)}" class="${playing && playing.item.id === r.id ? "sel" : ""}">
    <div class="t">${esc(r.title)}</div>
    <div class="d">${esc(r.date || "")}${r.duration_s ? " · " + esc(fmtDur(r.duration_s)) : ""} · ${r.views.length} run${r.views.length > 1 ? "s" : ""}</div></li>`).join("")
    || `<li class="muted">Nothing here yet. Build a run from video links or files, and its recordings appear here.</li>`;
}
$("#recordings").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-rec]");
  if (!li) return;
  const r = LIB.find((x) => x.id === li.dataset.rec);
  if (r) openPlayer(r.default_run, r.id);
});

async function openPlayer(run, item, t = null) {
  showTab("library");
  const d = await api(`/api/recording?run=${encodeURIComponent(run)}&item=${encodeURIComponent(item)}`).catch((err) => ({ error: err.message }));
  if (d.error) { $("#player").innerHTML = `<p class="muted">Could not open it: ${esc(d.error)}</p>`; return; }
  playing = d; lineIdx = -1;
  history.replaceState(null, "", `#play/${encodeURIComponent(run)}/${encodeURIComponent(item)}`);
  const rec = LIB.find((x) => x.id === item);
  const spk = Object.keys(d.speakers);
  const color = (s) => (s == null ? "#9e9e9e" : PALETTE[spk.indexOf(s) % PALETTE.length]);
  d.color = color;
  const views = rec ? rec.views.map((v) => `<option value="${esc(v.run_id)}" ${v.run_id === run ? "selected" : ""}>${esc(v.run_name)} · ${esc(fmtTime(v.created_utc))}${v.words != null ? ` · ${v.words.toLocaleString()} words` : ""}${v.failed_checks ? " · ⚠ " + v.failed_checks + " failed check" + (v.failed_checks > 1 ? "s" : "") : ""}</option>`).join("") : "";
  const fails = d.problems.filter((p) => p.status === "fail");
  const ch = d.audio?.settings?.channel;
  const audioLabel = d.audio ? `What the transcript heard${ch ? ` (${ch === "average" ? "both channels averaged" : ch + " channel"}, ${d.audio.settings.sample_rate / 1000} kHz)` : ""}` : "";
  const dur = d.duration_s || (d.segments.length ? d.segments[d.segments.length - 1].end : 0);
  $("#player").innerHTML = `<h3>${esc(d.title)}</h3>
    <div class="meta">${esc(d.date || "")}${d.duration_s ? " · " + esc(fmtDur(d.duration_s)) : ""}${d.channel ? " · " + esc(d.channel) : ""}
      ${d.url ? `· <a href="${esc(d.url)}" target="_blank" rel="noopener">source</a>` : ""}
      ${views ? `<label>Run <select id="view-pick">${views}</select></label>` : ""}</div>
    ${fails.length ? `<div class="problems"><b>Failed checks in this run.</b> ${fails.map((p) => `${esc(p.stage)}: ${esc(p.message)}`).join("<br>")}</div>` : ""}
    <div class="meta">${d.audio ? `<label><input type="radio" name="src" value="audio" checked> ${esc(audioLabel)}</label>` : ""}
      ${d.media ? `<label><input type="radio" name="src" value="media" ${d.audio ? "" : "checked"}> The original download${d.media.has_video ? " (video)" : ""}</label>` : ""}</div>
    <div id="media-wrap"></div>
    <div class="timeline" id="timeline" title="Speakers over time: click to jump">${d.segments.map((s) => `<div class="seg" style="left:${(100 * s.start / dur).toFixed(3)}%;width:${(100 * (s.end - s.start) / dur).toFixed(3)}%;background:${color(s.speaker)}"></div>`).join("")}<div class="head" id="head"></div></div>
    <div class="legend-spk">${spk.length ? spk.slice(0, 16).map((s) => `<span><span class="chip" style="background:${color(s)}"></span>${esc(s)} · ${esc(fmtDur(d.speakers[s]))}</span>`).join("") + (spk.length > 16 ? `<span class="muted">+${spk.length - 16} more</span>` : "")
      : `<span class="muted">${d.segments.length ? "No speaker labels in this run." : "No transcript in this run."}</span>`}</div>
    <div class="ptabs"><button class="small" data-ptab="lines">Transcript (${d.segments.length})</button>
      <button class="small secondary" data-ptab="comments">Comments (${d.comments.length})</button>
      <span class="grow"></span><input id="line-filter" type="search" placeholder="Find in this recording" style="width:200px">
      <label class="muted" style="font-size:12px"><input type="checkbox" id="follow" checked> Follow playback</label></div>
    <div class="lines" id="lines"></div>`;
  setSource(d.audio ? "audio" : "media", t);
  renderLines("lines");
  refreshLibrary();
}

function setSource(kind, t = null) {
  const d = playing;
  const src = kind === "audio" ? d.audio : d.media;
  if (!src) return;
  const el = kind === "media" && d.media.has_video ? "video" : "audio";
  const old = $("#media-wrap media, #media-wrap audio, #media-wrap video");
  const at = t ?? (old ? old.currentTime : 0);
  $("#media-wrap").innerHTML = `<${el} id="media" controls preload="metadata" src="${outputUrl(src)}"></${el}>`;
  const m = $("#media");
  if (at) m.addEventListener("loadedmetadata", () => { m.currentTime = at; if (t != null) m.play().catch(() => {}); }, { once: true });
  m.addEventListener("timeupdate", onTime);
}

function renderLines(which, filter = "") {
  const d = playing, words = filter.toLowerCase().split(/\s+/).filter(Boolean);
  document.querySelectorAll("[data-ptab]").forEach((b) => b.classList.toggle("secondary", b.dataset.ptab !== which));
  if (which === "comments") {
    $("#lines").innerHTML = d.comments.map((c) => `<div class="cmt" data-t="${c.start_s}">
      <b>${esc(c.timestamp)}</b> · ${esc(c.speaker_name || "Unidentified")}${c.residence_or_affiliation ? `, ${esc(c.residence_or_affiliation)}` : ""}
      · <span class="badge">${esc(c.position || "")}</span><div class="muted">${esc(c.agenda_item || "")}</div><div>${esc(c.summary || "")}</div></div>`).join("")
      || `<p class="muted" style="padding:8px">No comments were extracted in this run.</p>`;
    return;
  }
  $("#lines").innerHTML = d.segments.map((s, i) => (words.length && !words.every((w) => s.text.toLowerCase().includes(w))) ? ""
    : `<div class="line" data-i="${i}" data-t="${s.start}"><span class="tm">${hm(s.start)}</span><span class="sp"><span class="chip" style="background:${d.color(s.speaker)}"></span>${esc(s.speaker || "")}</span><span>${words.length ? marked(s.text, words) : esc(s.text)}</span></div>`).join("")
    || `<p class="muted" style="padding:8px">${d.segments.length ? "No line matches." : "This run has no transcript for this recording."}</p>`;
}

function onTime() {
  const m = $("#media"), d = playing;
  if (!m || !d) return;
  const dur = d.duration_s || m.duration || 1;
  $("#head").style.left = `${(100 * m.currentTime / dur).toFixed(3)}%`;
  let lo = 0, hi = d.segments.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (d.segments[mid].start <= m.currentTime) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  if (idx === lineIdx) return;
  document.querySelector(".line.now")?.classList.remove("now");
  lineIdx = idx;
  const el = document.querySelector(`.line[data-i="${idx}"]`);
  if (el) { el.classList.add("now"); if ($("#follow")?.checked) el.scrollIntoView({ block: "nearest" }); }
}

function seek(t) { const m = $("#media"); if (m) { m.currentTime = t; m.play().catch(() => {}); } }
$("#player").addEventListener("click", (e) => {
  const row = e.target.closest("[data-t]");
  if (row) return seek(+row.dataset.t);
  const tab = e.target.closest("[data-ptab]");
  if (tab) return renderLines(tab.dataset.ptab, $("#line-filter").value);
  const tl = e.target.closest("#timeline");
  if (tl) { const r = tl.getBoundingClientRect(), m = $("#media"); seek((e.clientX - r.left) / r.width * (playing.duration_s || m.duration)); }
});
$("#player").addEventListener("change", (e) => {
  if (e.target.id === "view-pick") openPlayer(e.target.value, playing.item.id, $("#media")?.currentTime || null);
  if (e.target.name === "src") setSource(e.target.value);
});
$("#player").addEventListener("input", (e) => { if (e.target.id === "line-filter") renderLines("lines", e.target.value); });

$("#lib-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#search-hits").hidden = true; return; }
  searchTimer = setTimeout(async () => {
    const hits = await api(`/api/search?q=${encodeURIComponent(q)}&limit=200`).catch(() => []);
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const by = {};
    hits.forEach((h) => (by[h.id] = by[h.id] || []).push(h));
    $("#search-hits").hidden = false;
    $("#search-hits").innerHTML = `<div class="muted" style="margin-bottom:4px">${hits.length}${hits.length >= 200 ? "+" : ""} line${hits.length === 1 ? "" : "s"} in ${Object.keys(by).length} recording${Object.keys(by).length === 1 ? "" : "s"}</div>`
      + Object.values(by).map((hs) => `<div><b>${esc(hs[0].title)}</b>${hs.slice(0, 8).map((h) => `<div class="hit" data-run="${esc(h.run_id)}" data-item="${esc(h.id)}" data-t="${h.start}"><span class="muted">${hm(h.start)}</span> ${marked(h.text, words)}</div>`).join("")}${hs.length > 8 ? `<div class="muted">+${hs.length - 8} more</div>` : ""}</div>`).join("");
  }, 300);
});
$("#search-hits").addEventListener("click", (e) => {
  const h = e.target.closest(".hit");
  if (h) openPlayer(h.dataset.run, h.dataset.item, +h.dataset.t);
});

// ---------------------------------------------------------------- import from cameras and drives
let impSource = null, armedClear = null, MACHINE = {}, impTimer = null;
const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${(b / 1e6).toFixed(0)} MB`);
const LS = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };
const DEFAULT_TEMPLATE = "{project}/{date:%Y}/{date:%Y-%m-%d}_{device}/{date:%Y-%m-%d_%H%M%S}_{device}_{name}";

function impForm() {
  return { source: impSource, archive: $("#imp-archive").value.trim() || null, project: $("#imp-project").value.trim() || "Project",
    template: $("#imp-template").value.trim() || null, kinds: [...document.querySelectorAll(".kinds input:checked")].map((x) => x.value) };
}
function impMsg(text, kind = "") { const m = $("#imp-msg"); m.textContent = text; m.className = `msg ${kind}`; }

async function refreshImport() {
  if (!$("#imp-archive").value) $("#imp-archive").value = LS.get("archive") || MACHINE.config?.archive || "";
  if (!$("#imp-template").value) $("#imp-template").value = LS.get("template") || DEFAULT_TEMPLATE;
  if (LS.get("project") && $("#imp-project").value === "Project") $("#imp-project").value = LS.get("project");
  const devs = await api("/api/devices").catch(() => []);
  $("#devices").innerHTML = devs.map((d) => `<button class="device ${impSource === d.mount ? "sel" : ""}" data-mount="${esc(d.mount)}">
      <b>${esc(d.label)}</b>${d.camera ? " · camera card" : ""}<div class="muted">${esc(d.mount)} · ${gb(d.used)} used</div></button>`).join("")
    || `<p class="muted">No camera card or USB drive is plugged in. Plug one in and press “Look again”, or choose a folder.</p>`;
  const [list, jobs] = await Promise.all([api("/api/ingests").catch(() => []), api("/api/jobs").catch(() => [])]);
  const io = jobs.filter((j) => j.backend === "io" && (j.status === "queued" || j.status === "running"));
  const waiting = io.filter((j) => !list.some((r) => r.run_id === j.run_id) && !list.some((r) => r.clear?.run_id === j.run_id));
  $("#imports").innerHTML = waiting.map((j) => `<div class="imp-card"><b>${esc(j.name)}</b> <span class="badge b-${esc(j.status)}">${esc(j.status)}</span>
      <span class="muted">${esc(fmtTime(j.created_utc))} · starting…</span></div>`).join("") + list.map((r) => {
    const n = r.counts, i = r.ingest || {};
    const counts = [n.ran && `${n.ran} copied`, n.cached && `${n.cached} already in the archive`, n.failed && `${n.failed} failed`,
      i.skipped && `${i.skipped} not imported`, (n.pending || n.running) && `${(n.pending || 0) + (n.running || 0)} to go`].filter(Boolean).join(" · ");
    const total = Object.values(n).reduce((a, b) => a + b, 0);
    const cleared = r.clear ? `<div><span class="badge b-${esc(r.clear.status)}">clear ${esc(r.clear.status)}</span> ${r.clear.deleted != null ? `${r.clear.deleted} deleted from the device${r.clear.kept ? `, ${r.clear.kept} kept (see Jobs)` : ""}` : ""}</div>` : "";
    const canClear = r.clearable && (!r.clear || r.clear.status === "failed");
    return `<div class="imp-card"><b>${esc(r.name)}</b> <span class="badge b-${esc(r.status)}">${esc(r.status)}</span>
      <span class="muted">${esc(fmtTime(r.created_utc))}</span>
      <div class="muted">${esc(i.source || "")} → ${esc(i.archive || "")}${i.project ? ` / ${esc(i.project)}` : ""}</div>
      <div>${esc(counts)}${total ? ` of ${total}` : ""}</div>${cleared}
      <div class="muted" style="font-size:12px">${r.clearable ? "✓ " : ""}${esc(r.why)}</div>
      <div class="btns">${r.manifest ? `<a class="button small secondary" href="/api/ingest/${encodeURIComponent(r.run_id)}/manifest.csv">Manifest (opens in Excel)</a>` : ""}
        ${r.media.length ? `<button class="small secondary" data-transcribe="${esc(r.run_id)}">Transcribe the ${r.media.length} recording${r.media.length > 1 ? "s" : ""}</button>` : ""}
        ${canClear ? `<button class="small danger" data-clear="${esc(r.run_id)}" ${armedClear === r.run_id ? 'data-armed="1"' : ""}>${armedClear === r.run_id ? `Click again: delete ${total - (i.skipped || 0)} files from ${esc(i.volume || "the device")}` : "Clear the device…"}</button>` : ""}
        <button class="small secondary" data-open="${esc(r.run_id)}">Details</button></div></div>`;
  }).join("") || `<p class="muted">No imports yet.</p>`;
  clearTimeout(impTimer);
  if (io.length || list.some((r) => r.status === "running" || r.clear?.status === "running")) impTimer = setTimeout(() => $("#tab-import").classList.contains("active") && refreshImport(), 2000);
}

$("#devices").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mount]");
  if (b) { impSource = b.dataset.mount; $("#imp-source").textContent = impSource; refreshImport(); }
});
$("#refresh-devices").addEventListener("click", refreshImport);
$("#pick-source").addEventListener("click", () => pickFolder(impSource, (p) => { impSource = p; $("#imp-source").textContent = p; refreshImport(); }));
$("#pick-archive").addEventListener("click", () => pickFolder($("#imp-archive").value, (p) => { $("#imp-archive").value = p; }));
$("#imp-preview-btn").addEventListener("click", async () => {
  const f = impForm();
  if (!f.source) return impMsg("Choose a card, drive or folder first.", "err");
  impMsg("Reading the files…");
  try {
    const p = await api("/api/ingest/preview", { json: f });
    impMsg("");
    const kinds = Object.entries(p.by_kind).map(([k, [n, b]]) => `${n} ${k} (${gb(b)})`).join(", ");
    $("#imp-preview").innerHTML = `<div class="imp-card"><b>${p.files} files, ${gb(p.bytes)}</b> to copy${p.skipped ? `; ${p.skipped} other file(s) stay on the device` : ""}.
      <div class="muted">${esc(kinds)} · taken ${esc((p.first || "").replace("T", " "))} to ${esc((p.last || "").replace("T", " "))} · devices: ${esc(p.devices.join(", "))}
      · times from ${esc(Object.entries(p.time_from).map(([k, v]) => `${k}: ${v}`).join(", "))}</div>
      ${table(p.sample, [["on the device", (r) => r.rel], ["will be saved as", (r) => r.dest], ["time from", (r) => r.taken_from]])}
      ${p.files > p.sample.length ? `<div class="muted">… and ${p.files - p.sample.length} more</div>` : ""}</div>`;
  } catch (err) { impMsg(err.message, "err"); }
});
$("#imp-go").addEventListener("click", async () => {
  const f = impForm();
  if (!f.source) return impMsg("Choose a card, drive or folder first.", "err");
  LS.set("archive", $("#imp-archive").value); LS.set("project", $("#imp-project").value); LS.set("template", $("#imp-template").value);
  try {
    const r = await api("/api/ingest", { json: f });
    impMsg(`Copying and verifying (${r.run_id}). You can keep working; the card stays untouched.`, "ok");
    $("#imp-preview").innerHTML = "";
    refreshImport();
  } catch (err) { impMsg(err.message, "err"); }
});
$("#imports").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.clear) {
    if (!b.dataset.armed) { armedClear = b.dataset.clear; setTimeout(() => { if (armedClear === b.dataset.clear) { armedClear = null; refreshImport(); } }, 6000); return refreshImport(); }
    armedClear = null;
    try { const r = await api(`/api/ingest/${encodeURIComponent(b.dataset.clear)}/clear`, { method: "POST" }); impMsg(`Clearing: each file is read once more and deleted only if it still matches its archived copy (${r.run_id}).`, "ok"); }
    catch (err) { impMsg(err.message, "err"); }
    return refreshImport();
  }
  if (b.dataset.open) { currentRun = b.dataset.open; return showTab("results"); }
  if (b.dataset.transcribe) {
    const list = await api("/api/ingests");
    const r = list.find((x) => x.run_id === b.dataset.transcribe);
    form = { name: `transcribe ${r.name.replace(/^import /, "")}`, description: `The recordings from import ${r.run_id}`, urls: "",
      files: r.media.join("\n"), stages: ["download", "audio", "diarize", "transcribe", "align", "blocks"].map((name) => ({ name, settings: {}, on_fail: "stop", as: "", from: {} })) };
    $("#r-name").value = form.name; $("#r-desc").value = form.description; $("#r-urls").value = ""; $("#r-files").value = form.files;
    renderStages();
    showTab("build");
    msg("Check the plan, then submit. The audio stage picks the channels automatically and records why.", "ok");
  }
});

// A folder browser on this machine (folders only), for the source and the archive.
async function pickFolder(start, done) {
  async function show(path) {
    let d;
    try { d = await api(`/api/fs${path ? `?path=${encodeURIComponent(path)}` : ""}`); } catch (err) { d = await api("/api/fs"); }
    openModal(`<h3>Choose a folder</h3><div class="muted" style="font-family:var(--mono)">${esc(d.path)}</div>
      <div class="row" style="display:flex;gap:6px;margin-top:6px">${d.parent ? `<button class="small secondary" data-go="${esc(d.parent)}">↑ Up</button>` : ""}
        <button class="small secondary" data-go="${esc(d.home)}">Home</button><button class="small secondary" data-go="${esc(d.archive)}">Archive</button></div>
      <div class="fs-list">${d.dirs.map((n) => `<div data-go="${esc(d.path.replace(/[\\/]$/, "") + (d.path.includes("\\") ? "\\" : "/") + n)}">📁 ${esc(n)}</div>`).join("") || `<p class="muted" style="padding:8px">No folders inside.</p>`}</div>
      <button data-use="${esc(d.path)}">Use this folder</button>`);
  }
  $("#modal-body").onclick = (e) => {
    const g = e.target.closest("[data-go]");
    if (g) return show(g.dataset.go);
    const u = e.target.closest("[data-use]");
    if (u) { closeModal(); $("#modal-body").onclick = null; done(u.dataset.use); }
  };
  show(start);
}

// ---------------------------------------------------------------- ask (the assistant)
let chatHistory = [], chatBusy = false, chatNotes = [];  // notes ride along with the next question (roles alternate)
// Links like #play/RUN/ITEM/SECONDS (in answers and in what the assistant looked at) open the player at that moment.
const linkify = (text) => esc(text).replace(/\(?#play\/([^\s/)]+)\/([^\s/)]+)\/(\d+)\)?/g,
  (m, run, item, t) => ` <a class="play" href="#play/${run}/${item}/${t}">▶ ${hm(+t)}</a>`);
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#play\/([^/]+)\/([^/]+)(?:\/([\d.]+))?/);
  if (m) openPlayer(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3] ? +m[3] : null);
});

function addBubble(cls, html) {
  const d = document.createElement("div");
  d.className = `bubble ${cls}`;
  d.innerHTML = html;
  $("#chat").appendChild(d);
  d.scrollIntoView({ block: "end" });
  return d;
}

async function ask(text) {
  if (chatBusy || !text.trim()) return;
  chatBusy = true;
  $("#ask-send").disabled = true;
  addBubble("user", esc(text));
  chatHistory.push({ role: "user", content: chatNotes.length ? `${chatNotes.join(" ")}\n\n${text}` : text });
  chatNotes = [];
  const wait = addBubble("bot thinking", "Thinking… (on this computer's processor, this can take half a minute)");
  try {
    const r = await api("/api/assistant", { json: { messages: chatHistory } });
    chatHistory = r.messages;
    wait.remove();
    const steps = r.steps.length ? `<details><summary>What I looked at (${r.steps.length})</summary>${r.steps.map((s) =>
      `<div><b>${esc(s.tool)}</b> ${esc(JSON.stringify(s.args))}</div><pre>${linkify(s.result)}</pre>`).join("")}</details>` : "";
    const pend = r.pending.map((p, i) => `<div class="pending" data-i="${i}"><b>${esc(p.description)}</b><div class="muted">${esc(p.tool)} ${esc(JSON.stringify(p.args))}</div>
      <button class="small" data-go='${esc(JSON.stringify(p))}'>Go ahead</button> <button class="small secondary" data-no="1">No</button></div>`).join("");
    const reply = Object.entries(r.links || {}).sort((a, b) => b[0].length - a[0].length).reduce((html, [tm, url]) =>
      html.replace(new RegExp(`(^|[^\\d:])(${tm.replace(/[:]/g, ":")})(?![\\d:]|</a>)`), `$1<a class="play" href="${esc(url)}">▶ $2</a>`), linkify(r.reply || "(no answer)"));
    addBubble("bot", reply + pend + steps + `<div class="muted" style="font-size:11px">${r.seconds} s</div>`);
  } catch (err) {
    wait.remove();
    addBubble("bot", `Something went wrong: ${esc(err.message)}`);
    chatHistory.pop();
  }
  chatBusy = false;
  $("#ask-send").disabled = false;
}
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#ask-input").value; $("#ask-input").value = ""; ask(t); });
$("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
$("#examples").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ask(b.textContent); });
$("#ask-clear").addEventListener("click", (e) => { e.preventDefault(); chatHistory = []; chatNotes = []; $("#chat").innerHTML = ""; });
$("#chat").addEventListener("click", async (e) => {
  const go = e.target.closest("[data-go]"), no = e.target.closest("[data-no]");
  if (no) { no.closest(".pending").innerHTML = `<span class="muted">Not done.</span>`; chatNotes.push("(I chose not to do that.)"); return; }
  if (!go) return;
  const p = JSON.parse(go.dataset.go);
  const box = go.closest(".pending");
  box.innerHTML = `<span class="muted">Working…</span>`;
  const r = await api("/api/assistant/confirm", { json: { tool: p.tool, args: p.args } }).catch((err) => ({ result: err.message }));
  box.innerHTML = linkify(r.result);
  chatNotes.push(`(I pressed the button. Result: ${r.result})`);
});

// ---------------------------------------------------------------- start
(async function init() {
  const [stages, prompts, machine] = await Promise.all([api("/api/stages"), api("/api/prompts"), api("/api/machine")]);
  STAGES = stages; PROMPTS = prompts;
  DEFS = Object.fromEntries(stages.map((s) => [s.name, s]));
  $("#add-stage").innerHTML = stages.map((s) => `<option value="${esc(s.name)}">${esc(s.label)}</option>`).join("");
  MACHINE = machine;
  $("#machine").textContent = `${machine.gpu ? machine.gpu.name : "no GPU"} · workspace ${machine.workspace}`;
  await loadRecipeList();
  renderStages();
  await refreshLibrary();
  const m = location.hash.match(/^#play\/([^/]+)\/([^/]+)(?:\/([\d.]+))?/);
  if (m) openPlayer(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3] ? +m[3] : null);
  setInterval(() => { if ($("#tab-jobs").classList.contains("active")) refreshJobs(); }, 2000);
})();
