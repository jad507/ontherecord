// On the Record: the GUI. Plain JavaScript, no build step, so it runs wherever Python does.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUS_TEXT = { cached: "reused", ran: "ran", run: "will run", "run-if-changed": "maybe", running: "running",
  queued: "queued", pending: "waiting", failed: "failed", blocked: "blocked", cancelled: "cancelled", done: "done" };

// Per-browser conveniences (the last tab, recording and draft); everything works without them.
const LS = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  json: (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } } };
let STAGES = [], PROMPTS = [], DEFS = {};
let form = { name: "", description: "", urls: "", files: "", stages: [] };
let currentRun = null, selectedJob = null, runTimer = null, armedCancel = null, compareWith = null, CMP = null;

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

// ---------------------------------------------------------------- theme: follows the system unless picked here
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const b = $("#theme"); if (b) b.textContent = `Theme: ${t}`;
}
applyTheme(LS.get("theme") || "auto");
$("#theme")?.addEventListener("click", () => {
  const t = THEMES[(THEMES.indexOf(LS.get("theme") || "auto") + 1) % THEMES.length];
  LS.set("theme", t); applyTheme(t);
});

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "jobs") { refreshJobs(); refreshUsage(); }
  if (name === "results") refreshRuns();
  if (name === "library") refreshLibrary();
  if (name === "import") refreshImport();
  if (name === "ask") { $("#ask-input").focus(); refreshAskBusy(); }
  if (name === "setup") refreshSetup();
  if (name === "review" && !RV) reviewList();
  LS.set("tab", name);
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

function updateYaml() { $("#yaml").textContent = toYaml(); LS.set("draft", JSON.stringify(form)); }

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
    } catch (err) { $("#modal-body").insertAdjacentHTML("beforeend", `<p class="msg err">${esc(err.message)}</p>`); }
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
    $("#plan").innerHTML = (p.estimate && p.estimate.text ? `<p class="msg ok">Time: ${esc(p.estimate.text)}.</p>` : "") + gridHtml(p.stages, p.cells, false) + legendHtml(true);
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
// How long is left: from this computer's own pace per stage (usage.py), shown in its yardsticks.
const canLeft = (j) => j.status === "running" || j.status === "queued";
function leftText(s, status) {
  if (!s) return "";
  const d = s < 60 ? "under a minute" : `about ${fmtDur(Math.round(s / 60) * 60).replace(/ 0 s$/, "")}`;
  return status === "queued" ? `${d} once it starts` : `${d} left`;
}
async function refreshUsage() {
  const u = await api("/api/usage").catch(() => null);
  if (!u || !u.receipts) { $("#usage-line").innerHTML = ""; return; }
  const a = u.api_equivalent, P = a.prices;
  $("#usage-line").innerHTML = `This computer has done <b>${u.compute.hours} h</b> of work since ${esc(u.since)}
    · <b>${(u.tokens.input + u.tokens.output).toLocaleString()}</b> tokens through local models, which would have cost about
    ${a.headline.map((k) => `<b>$${a.usd[k].toFixed(2)}</b> at ${esc(P[k].name)}`).join(" or ")} prices (read ${esc(a.prices_read)})
    · <a href="#" id="usage-more">details</a>`;
}
$("#usage-line").addEventListener("click", async (e) => {
  if (e.target.id !== "usage-more") return;
  e.preventDefault();
  const u = await api("/api/usage"), a = u.api_equivalent;
  const dev = { cuda: "graphics card", server: "language-model server", cpu: "processor", mps: "Apple GPU" };
  openModal(`<h3>Work on this computer since ${esc(u.since)}</h3>
    <p>${u.compute.hours} h in all: ${Object.entries(u.compute.by_device_hours).map(([k, h]) => `${h} h on the ${esc(dev[k] || k)}`).join(", ")}, and ${u.compute.setup_hours} h loading models.
      Each receipt is one stage done for one recording; a reused result is not counted again.${u.compute.assistant_hours ? ` The assistant: ${u.compute.assistant_hours} h.` : ""}</p>
    ${table(Object.entries(u.stages).filter(([, v]) => v.receipts).map(([k, v]) => ({ k, ...v })), [["stage", (r) => r.k], ["runs", (r) => r.receipts], ["hours", (r) => r.hours],
      ["tokens in", (r) => (r.input_tokens || "").toLocaleString()], ["tokens out", (r) => (r.output_tokens || "").toLocaleString()], ["not counted", (r) => r.uncounted || ""]])}
    <h3 style="margin-top:12px">What the language-model work would have cost through an API</h3>
    ${table(Object.entries(a.prices).map(([k, p]) => ({ k, ...p })), [["model", (p) => p.name], ["tier", (p) => p.tier], ["$ / M in", (p) => p.input.toFixed(2)], ["$ / M out", (p) => p.output.toFixed(2)],
      ["this computer's tokens", (p) => `$${a.usd[p.k].toFixed(2)}`], ["", (p) => p.note || ""]])}
    <p class="muted" style="font-size:12px">${u.tokens.input.toLocaleString()} tokens in and ${u.tokens.output.toLocaleString()} out, counted by the local models. Prices read ${esc(a.prices_read)} from
      <a href="${esc(a.prices["claude-sonnet-5-5"].source)}" target="_blank" rel="noopener">Anthropic's</a> and <a href="${esc(a.prices["gpt-5.6-terra"].source)}" target="_blank" rel="noopener">OpenAI's</a> pricing pages:
      standard rates, no caching or batch discounts. On the Record never looks prices up itself.${a.uncounted_receipts ? ` ${a.uncounted_receipts} language-model runs from before token counting are not included.` : ""}</p>
    <h3 style="margin-top:12px">Speeds, by software version</h3>
    ${u.speeds.audio.length ? table(u.speeds.audio, [["stage", (s) => s.stage], ["setting", (s) => s.setting], ["× real time", (s) => s.times_real_time], ["software", (s) => s.software], ["graphics", (s) => s.gpu], ["runs", (s) => s.runs]]) : ""}
    ${u.speeds.llm.length ? table(u.speeds.llm, [["stage", (s) => s.stage], ["model", (s) => s.model], ["generation tok/s", (s) => s.gen_tokens_per_s], ["prompt tok/s", (s) => s.prompt_tokens_per_s], ["llama.cpp", (s) => s.llama_build], ["graphics", (s) => s.gpu], ["runs", (s) => s.runs]])
      : `<p class="muted">Language-model speeds appear after the next comment extraction or stress reader run (they were not recorded before Oct 23).</p>`}`);
});

async function refreshJobs() {
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch { return; }
  $("#jobs tbody").innerHTML = jobs.map((j) => {
    const counts = (j.cells ? Object.entries(j.cells).map(([k, v]) => `${v} ${STATUS_TEXT[k] || k}`).join(", ") : "")
      + (j.left_s != null && canLeft(j) ? `${j.cells ? " · " : ""}${leftText(j.left_s, j.status)}` : "");
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
    <div id="compare-slot"></div>
    <div class="muted" style="font-size:12px;margin:4px 0 10px">${esc(r.run_id)} · ${esc(r.host)}${r.gpu ? " · " + esc(r.gpu.name) : ""} · started ${esc(fmtTime(r.created_utc))}${r.finished_utc ? " · took " + esc(fmtDur(secsBetween(r.created_utc, r.finished_utc))) : ""}</div>`;
  const runs = await api("/api/runs").catch(() => []);
  if (currentRun !== id) return;  // another run was chosen while this one loaded
  $("#compare-slot").innerHTML = (`<label class="muted" style="display:inline-flex;gap:6px;align-items:center">Compare with
    <select id="compare-pick" style="width:auto"><option value="">—</option>${runs.filter((x) => x.run_id !== id).map((x) => `<option value="${esc(x.run_id)}" ${x.run_id === compareWith ? "selected" : ""}>${esc(x.name)} · ${esc(fmtTime(x.created_utc))}</option>`).join("")}</select></label>`);
  if (compareWith) return showCompare(compareWith, id);
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
// the theme's ten accent colors (style.css), so speakers read in both themes
const PALETTE = Array.from({ length: 10 }, (_, i) => `var(--c${i + 1})`);
const hm = (s) => (s == null ? "" : s >= 3600 ? hms(s) : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
const outputUrl = (o) => `/api/output?receipt=${encodeURIComponent(o.receipt)}&name=${encodeURIComponent(o.name)}`;
const marked = (text, words) => words.reduce((t, w) => t.replace(new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"), "<mark>$1</mark>"), esc(text));

async function refreshLibrary() {
  LIB = await api("/api/library").catch(() => []);
  $("#recordings").innerHTML = LIB.map((r) => `<li data-rec="${esc(r.id)}" class="${playing && playing.item.id === r.id ? "sel" : ""}">
    <div class="t">${esc(r.title)}</div>
    <div class="d">${esc(r.date || "")}${r.duration_s ? " · " + esc(fmtDur(r.duration_s)) : ""} · ${r.views.length} run${r.views.length > 1 ? "s" : ""}</div></li>`).join("")
    || `<li class="muted">Nothing here yet. “Transcribe a recording” above adds one; so does a run from the Build tab.</li>`;
}
$("#recordings").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-rec]");
  if (!li) return;
  const r = LIB.find((x) => x.id === li.dataset.rec);
  if (r) openPlayer(r.default_run, r.id);
});

async function openPlayer(run, item, t = null, { switchTab = true, autoplay = true } = {}) {
  if (switchTab) showTab("library");
  const d = await api(`/api/recording?run=${encodeURIComponent(run)}&item=${encodeURIComponent(item)}`).catch((err) => ({ error: err.message }));
  if (d.error) { $("#player").innerHTML = `<p class="muted">Could not open it: ${esc(d.error)}</p>`; return; }
  playing = d; lineIdx = -1; d.run = run;
  LS.set("play", JSON.stringify({ run, item, t: t || 0 }));
  history.replaceState(null, "", `#play/${encodeURIComponent(run)}/${encodeURIComponent(item)}`);
  const rec = LIB.find((x) => x.id === item);
  const spk = Object.keys(d.speakers);
  const color = (s) => (s == null ? "var(--gone)" : PALETTE[spk.indexOf(s) % PALETTE.length]);
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
      <span class="muted" style="font-size:12px">Save: <a href="#" data-save="txt">.txt</a> · <a href="#" data-save="srt">.srt</a> · <a href="#" data-save="vtt">.vtt</a></span>
      <span class="grow"></span><input id="line-filter" type="search" placeholder="Find in this recording" style="width:200px">
      <label class="muted" style="font-size:12px"><input type="checkbox" id="follow" checked> Follow playback</label></div>
    <div class="lines" id="lines"></div>`;
  setSource(d.audio ? "audio" : "media", t, autoplay);
  renderLines("lines");
  refreshLibrary();
}

function setSource(kind, t = null, autoplay = true) {
  const d = playing;
  const src = kind === "audio" ? d.audio : d.media;
  if (!src) return;
  const el = kind === "media" && d.media.has_video ? "video" : "audio";
  const old = $("#media-wrap media, #media-wrap audio, #media-wrap video");
  const at = t ?? (old ? old.currentTime : 0);
  $("#media-wrap").innerHTML = `<${el} id="media" controls preload="metadata" src="${outputUrl(src)}"></${el}>`;
  const m = $("#media");
  if (at) m.addEventListener("loadedmetadata", () => { m.currentTime = at; if (t != null && autoplay) m.play().catch(() => {}); }, { once: true });
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
  if (Math.abs(m.currentTime - (onTime.saved ?? -99)) > 5) {
    onTime.saved = m.currentTime;
    LS.set("play", JSON.stringify({ run: d.run, item: d.item.id, t: m.currentTime }));
  }
  let lo = 0, hi = d.segments.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (d.segments[mid].start <= m.currentTime) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  if (idx === lineIdx) return;
  document.querySelector(".line.now")?.classList.remove("now");
  lineIdx = idx;
  const el = document.querySelector(`.line[data-i="${idx}"]`);
  if (el) { el.classList.add("now"); if ($("#follow")?.checked) el.scrollIntoView({ block: "nearest" }); }
}

function seek(t) { const m = $("#media"); if (m) { m.currentTime = t; m.play().catch(() => {}); } }
// The transcript as a plain file: text with times, or subtitles (SubRip, WebVTT) with speakers.
function saveTranscript(kind) {
  const d = playing, pad = (n, w = 2) => String(Math.floor(n)).padStart(w, "0");
  const ts = (s, sep) => `${pad(s / 3600)}:${pad((s % 3600) / 60)}:${pad(s % 60)}${sep}${pad((s % 1) * 1000, 3)}`;
  const who = (s) => (s.speaker ? `${s.speaker}: ` : "");
  const text = kind === "txt" ? [d.title, d.date || "", d.url || "", `run ${d.run}`, "", ...d.segments.map((s) => `[${hms(s.start)}] ${who(s)}${s.text}`)].join("\n") + "\n"
    : kind === "srt" ? d.segments.map((s, i) => `${i + 1}\n${ts(s.start, ",")} --> ${ts(s.end, ",")}\n${who(s)}${s.text}\n`).join("\n")
    : "WEBVTT\n\n" + d.segments.map((s) => `${ts(s.start, ".")} --> ${ts(s.end, ".")}\n${s.speaker ? `<v ${s.speaker}>` : ""}${s.text}\n`).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  a.download = `${(d.title || d.item.id).replace(/[^\w.-]+/g, "_").slice(0, 80)}.${kind}`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

$("#player").addEventListener("click", (e) => {
  const sv = e.target.closest("[data-save]");
  if (sv) { e.preventDefault(); return saveTranscript(sv.dataset.save); }
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
// One thing runs at a time: while a job runs or waits, the assistant is off, and the tab says so up front.
async function refreshAskBusy() {
  const jobs = await api("/api/jobs").catch(() => []);
  const j = jobs.find((x) => x.status === "running") || jobs.find((x) => x.status === "queued");
  $("#ask-busy").className = j ? "msg warn" : "msg";
  $("#ask-busy").textContent = j ? `The computer is ${j.status === "running" ? "working on" : "about to start"} "${j.name}", so the assistant is switched off until it finishes: that way the job gets all of the memory. The Jobs tab shows progress.` : "";
}
setInterval(() => { if ($("#tab-ask").classList.contains("active")) refreshAskBusy(); }, 10000);

let chatHistory = [], chatBusy = false, chatNotes = [];  // notes ride along with the next question (roles alternate)
// Links like #play/RUN/ITEM/SECONDS (in answers and in what the assistant looked at) open the player at that moment.
const linkify = (text) => esc(text).replace(/\(?#play\/([^\s/)]+)\/([^\s/)]+)\/(\d+)\)?/g,
  (m, run, item, t) => ` <a class="play" href="#play/${run}/${item}/${t}">▶ ${hm(+t)}</a>`);
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#play\/([^/]+)\/([^/]+)(?:\/([\d.]+))?/);
  if (m) openPlayer(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3] ? +m[3] : null);
  const r = location.hash.match(/^#review\/([a-z0-9-]+)/);
  if (r && (!RV || RV.id !== r[1])) { RV = { id: r[1] }; rvItem = null; showTab("review"); refreshReview(r[1]); }
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
  const wait = addBubble("bot thinking", "Thinking… (on a laptop this can take half a minute)");
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
  saveChat();
}
function saveChat() { LS.set("chat", JSON.stringify({ history: chatHistory.slice(-40), notes: chatNotes, html: $("#chat").innerHTML.slice(-300000) })); }
function restoreChat() {
  const c = LS.json("chat");
  if (!c || !c.html) return;
  chatHistory = c.history || []; chatNotes = c.notes || [];
  $("#chat").innerHTML = c.html;
  document.querySelectorAll("#chat .thinking").forEach((b) => b.remove());
  document.querySelectorAll("#chat .pending").forEach((p) => { if (p.querySelector("button")) p.innerHTML = `<span class="muted">This was waiting for a button when you left; ask again if you still want it.</span>`; });
}
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#ask-input").value; $("#ask-input").value = ""; ask(t); });
$("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
$("#examples").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ask(b.textContent); });
$("#ask-clear").addEventListener("click", (e) => { e.preventDefault(); chatHistory = []; chatNotes = []; $("#chat").innerHTML = ""; saveChat(); });
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
  saveChat();
});

// ---------------------------------------------------------------- setup: the guided first run
// Five steps from a fresh install to a first transcript, each with its status. The tab opens by itself until the
// first recording is in the Library, and its label counts what is left. Only the token's Check button goes online.
let setupTimer = null, SETUP = null;
const FOLDERS = {
  workspace: ["Recordings and results", "Downloads, transcripts, receipts and the job list. On the Record looks after this folder: read it through the Library and Results tabs, and do not change files in it by hand."],
  archive: ["Archive", "Imports from cameras and cards, filed by project. An external drive is fine."],
  models: ["Models", "The language models, several GB each. The speech models live in the Hugging Face cache."],
  settings: ["Settings", "machine.yaml (folders, programs, the assistant's model) and secrets.env (the Hugging Face token)."],
};

function setupDone(d, libCount) {
  return [d.accel.driver?.level !== "needed", Object.values(d.tools).every(Boolean),
    d.models.every((m) => !m.recommended || m.status === true),
    d.has_token || d.models.some((m) => m.gated && m.status === true), libCount > 0];
}
function markSetupTab(done) {
  const left = done.filter((x) => !x).length;
  $('nav button[data-tab="setup"]').innerHTML = left ? `Setup <span class="badge b-run">${left} to do</span>` : "Setup";
}

async function refreshSetup() {
  const [d, jobs, lib] = await Promise.all([api("/api/setup"), api("/api/jobs").catch(() => []), api("/api/library").catch(() => [])]);
  SETUP = d;
  const hw = d.hardware, acc = d.accel, f = d.fit, done = setupDone(d, lib.length);
  markSetupTab(done);
  const dl = jobs.filter((j) => j.name === "download models" && (j.status === "queued" || j.status === "running"));
  const st = (s) => (s === true ? `<span class="badge b-done">downloaded</span>` : s ? `<span class="badge b-failed">${esc(s)}</span>` : `<span class="muted">not downloaded</span>`);
  const step = (n, title, body) => `<div class="imp-card step ${done[n - 1] ? "done" : ""}"><h3><span class="stepnum">${done[n - 1] ? "✓" : n}</span>${title}</h3>${body}</div>`;
  const nv = hw.gpus.filter((g) => g.vendor === "nvidia");
  const cards = hw.gpus.map((g) => g.vendor === "nvidia"
    ? `<div><b>${esc(g.name)}</b> · ${g.memory_gb} GB · compute capability ${esc(g.compute_cap || "unknown")} · driver ${esc(g.driver)}, which supports CUDA ${esc(g.cuda || "older than 12")}</div>`
    : `<div><b>${esc(g.name)}</b></div>`).join("")
    || (hw.gpu?.vendor === "apple" ? `<div><b>${esc(hw.gpu.name)}</b></div>` : `<div>No graphics card found: everything runs on the processor, several times slower.</div>`);
  const busy = nv.filter((g) => g.memory_gb - g.free_gb > Math.max(2, g.memory_gb / 4)).map((g) =>
    `<div class="problems warn">Another program is using ${(g.memory_gb - g.free_gb).toFixed(1)} GB of the ${esc(g.name)}'s ${g.memory_gb} GB right now. On the Record cannot see it, so close it before a long job, or the job may run out of memory.</div>`).join("");
  const onCard = hw.gpu?.vendor === "nvidia" ? "the graphics card" : "the processor";
  const torchOn = hw.os === "macos" ? "the Apple GPU" : acc.torch.want === "cpu" ? "the processor" : "the graphics card";
  const drv = acc.driver ? `<div class="${acc.driver.level === "optional" ? "muted" : acc.driver.level === "needed" ? "problems" : "problems warn"}" style="font-size:13px">${acc.driver.level === "optional" ? "Optional: " : ""}${esc(acc.driver.text)}</div>` : "";
  const torchFix = acc.torch.ok ? "" : `<div class="problems warn">The PyTorch here (${esc(acc.torch.installed)}, ${esc(acc.torch.installed_build)}) is not the build this computer suits (${esc(acc.torch.want)}: ${esc(acc.torch.why)}). Run the installer again, and it installs the right one.</div>`;
  const tokenOk = d.has_token ? `<span class="badge b-done">saved</span>` : "";
  $("#setup").innerHTML = `
    <p class="lede">Five steps take a fresh install to its first transcript. Each turns green when it is done, and everything stays on this computer.</p>
    ${step(1, "This computer", `
      <div>${esc({ windows: "Windows", macos: "macOS", linux: "Linux" }[hw.os] || hw.os)}${hw.wsl ? " (WSL)" : ""} · ${hw.cpus} processor threads · ${hw.ram_gb} GB memory · ${hw.disk_free_gb} GB free disk</div>
      ${cards}${busy}
      <div class="muted" style="margin-top:6px">What it will use:</div>
      <ul class="uses"><li>Speech to text: Whisper ${esc(f.whisper)} (${esc(f.whisper_compute)}) on ${onCard}</li>
        <li>Who speaks when: pyannote on ${torchOn} (PyTorch ${esc(hw.os === "macos" ? "for macOS" : acc.torch.want)})</li>
        <li>Questions and comment lists: ${esc(f.assistant)} and ${esc(f.extraction)} with llama.cpp (${esc(acc.llama.want)} build)</li></ul>
      ${drv}${torchFix}`)}
    ${step(2, "Programs", Object.entries(d.tools).map(([k, v]) => `<div>${v ? "✓" : "✗"} <b>${esc(k)}</b> <span class="muted">${esc(v || "missing: run the installer again, or `ontherecord setup`")}</span></div>`).join(""))}
    ${step(3, "Models", `<p class="muted" style="margin-top:0">The checked ones suit this computer. Each file is checked against its published fingerprint before it is used.</p>
      ${table(d.models, [["", (m) => ""], ["model", (m) => m.name], ["size", (m) => gb(m.bytes)], ["for", (m) => m.about], ["", (m) => ""]])}
      ${dl.length ? `<p class="msg ok">Downloading (${esc(dl[0].run_id)})… progress is also on the Jobs tab.</p>` : ""}
      <div class="btns" style="margin-top:8px"><button id="dl-btn" ${dl.length ? "disabled" : ""}>Download the checked models</button></div>`)}
    ${step(4, `Hugging Face, for the speaker model ${tokenOk}`, `
      <p style="margin-top:0">pyannote's speaker model is free, but its makers ask each user to accept their terms. This takes a few minutes, once:</p>
      <ol><li>Make a free account at <a href="https://huggingface.co/join" target="_blank" rel="noopener">huggingface.co</a>, or sign in.</li>
        <li>Open <a href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noopener">pyannote/speaker-diarization-community-1</a> and accept the terms.</li>
        <li>Open <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener">huggingface.co/settings/tokens</a>, make a token with <b>Read</b> access, and copy it.</li>
        <li>Paste it here, press Save, then Check. Then download the speaker model in step 3.</li></ol>
      <div class="row" style="display:flex;gap:6px"><input id="hf-token" type="password" placeholder="hf_..." autocomplete="off" style="margin:0"><button id="token-btn" class="secondary">Save</button><button id="token-check" class="secondary">Check</button></div>
      <div class="muted" style="font-size:12px">Kept in ${esc(d.secrets_file)}, readable only by you, and never written into a receipt. Check asks Hugging Face whether the token works and whether your account accepted the terms; nothing else goes online.</div>
      <div id="token-msg" class="msg"></div>`)}
    ${step(5, "Your first recording", `<div id="tx-first"></div>`)}
    <div class="imp-card"><h3>Where your files are</h3>
      <table class="data folders">${d.folders.map((x) => `<tr><td><b>${esc(FOLDERS[x.key][0])}</b><div class="muted">${esc(FOLDERS[x.key][1])}</div></td>
        <td><code>${esc(x.path)}</code>${x.free_gb != null ? `<div class="muted">${x.free_gb} GB free</div>` : ""}</td>
        <td>${x.exists ? `<button class="small secondary" data-reveal="${esc(x.key)}">Open</button>` : `<span class="muted">not made yet</span>`}</td></tr>`).join("")}</table>
      <div class="muted" style="font-size:12px;margin-top:6px">${d.config_file ? `To move one, change its line in <code>${esc(d.config_file)}</code> and restart On the Record. Moving the recordings folder does not move what is in it.` : "These are the built-in defaults: `ontherecord setup` writes a settings file you can change."}</div></div>`;
  const rows = document.querySelectorAll("#setup table.data:not(.folders) tr");
  d.models.forEach((m, i) => {
    const cells = rows[i + 1].children;
    cells[0].innerHTML = m.status === true ? "" : `<input type="checkbox" data-model="${esc(m.name)}" ${m.recommended ? "checked" : ""}>`;
    cells[1].innerHTML = `<b>${esc(m.name)}</b>${m.recommended ? ' <span class="badge">suits this computer</span>' : ""}${m.gated ? ' <span class="muted">(needs step 4)</span>' : ""}`;
    cells[4].innerHTML = st(m.status);
  });
  transcribeForm($("#tx-first"), d);
  clearTimeout(setupTimer);
  if (dl.length && $("#tab-setup").classList.contains("active")) setupTimer = setTimeout(refreshSetup, 3000);
}
$("#setup").addEventListener("click", async (e) => {
  const tmsg = (text, kind) => { $("#token-msg").textContent = text; $("#token-msg").className = `msg ${kind}`; };
  if (e.target.id === "dl-btn") {
    const models = [...document.querySelectorAll("#setup [data-model]:checked")].map((x) => x.dataset.model);
    try { await api("/api/setup/models", { json: { models } }); return refreshSetup(); }
    catch (err) { $("#dl-btn").insertAdjacentHTML("afterend", ` <span class="msg err">${esc(err.message)}</span>`); }
    return;
  }
  if (e.target.id === "token-btn") {
    try { const r = await api("/api/setup/token", { json: { token: $("#hf-token").value } }); tmsg(`Saved in ${r.saved}. Now press Check.`, "ok"); }
    catch (err) { tmsg(err.message, "err"); }
  }
  if (e.target.id === "token-check") {
    tmsg("Asking Hugging Face…", "");
    try { const r = await api("/api/setup/check-token", { json: { token: $("#hf-token").value || null } }); tmsg(r.message, r.access ? "ok" : "warn"); }
    catch (err) { tmsg(err.message, "err"); }
  }
  const rv = e.target.closest("[data-reveal]");
  if (rv) api("/api/reveal", { json: { which: rv.dataset.reveal } }).catch((err) => rv.insertAdjacentHTML("afterend", ` <span class="msg err">${esc(err.message)}</span>`));
});

// ---------------------------------------------------------------- transcribe a recording (the ready-made recipe)
// Setup's step 5 and the Library's button: files or a link in, a job set for this computer out, followed to the end.
const LANGS = [["", "English"], ["detect", "Detect the language"], ["es", "Spanish"], ["fr", "French"], ["de", "German"],
  ["pt", "Portuguese"], ["it", "Italian"], ["zh", "Chinese"], ["ja", "Japanese"], ["ko", "Korean"], ["ar", "Arabic"], ["hi", "Hindi"], ["ru", "Russian"]];
function transcribeForm(el, setup = null) {
  let chosen = [];
  const extraction = setup && setup.models.find((m) => m.name === setup.fit.extraction);
  el.innerHTML = `<p style="margin-top:0">Choose an audio or video file on this computer, or paste a link (YouTube and most video sites).
      On the Record finds who speaks when, writes down what they say, and lines the two up. It works in the background: you can close this.</p>
    <div class="row" style="display:flex;gap:6px"><button class="secondary" data-tx="pick">Choose a file…</button>
      <input data-tx="src" placeholder="…or paste a link, or a file's whole path" style="margin:0"></div>
    <div class="muted" data-tx="chosen" style="margin:4px 0"></div>
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:6px 0">
      <label style="margin:0">Language <select data-tx="lang" style="display:inline-block;width:auto;margin-left:4px">${LANGS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></label>
      <label style="margin:0"><input type="checkbox" data-tx="comments"> Also list the public comments (slower: a large language model${extraction && extraction.status !== true ? "; download it in step 3 first" : ""})</label></div>
    <button data-tx="go">Transcribe</button> <span class="msg" data-tx="msg"></span>`;
  const q = (k) => el.querySelector(`[data-tx="${k}"]`);
  const say = (html, kind = "") => { q("msg").innerHTML = html; q("msg").className = `msg ${kind}`; };
  el.onclick = async (e) => {
    const t = e.target.closest("[data-tx]")?.dataset.tx;
    if (t === "pick") pickMedia(LS.get("mediaDir"), (paths, dir) => {
      chosen = paths; LS.set("mediaDir", dir);
      q("chosen").textContent = paths.length === 1 ? paths[0] : `${paths.length} recordings in ${dir}`;
    });
    if (t === "go") {
      const typed = q("src").value.trim(), lang = q("lang").value;
      const sources = chosen.concat(typed ? [typed] : []);
      say("Queueing…");
      try {
        const r = await api("/api/transcribe", { json: { sources, with_comments: q("comments").checked, language: lang === "" ? null : lang === "detect" ? "" : lang } });
        say(`Queued as <code>${esc(r.run_id)}</code>.${r.estimate && r.estimate.text ? ` Expected: ${esc(r.estimate.text)}.` : ""}`, "ok");
        followJob(r.run_id, (html, kind) => { if (document.body.contains(el)) say(html, kind); });
      } catch (err) { say(esc(err.message), "err"); }
    }
    const open = e.target.closest("[data-open-run]");
    if (open) { closeModal(); openRun(open.dataset.openRun); }
  };
}
async function followJob(runId, say) {
  const j = (await api("/api/jobs").catch(() => [])).find((x) => x.run_id === runId);
  if (!j) return;
  const c = j.cells || {}, total = Object.values(c).reduce((a, b) => a + b, 0), fin = (c.ran || 0) + (c.cached || 0);
  if (j.status === "done") return say(`Done. <button class="small" data-open-run="${esc(runId)}">Open it in the Library</button>`, "ok");
  if (j.status === "failed" || j.status === "cancelled") return say(`It ${j.status === "failed" ? "stopped with a problem" : "was cancelled"}: the Jobs tab shows its log, and Results shows which step and why.`, "err");
  const left = j.left_s ? ` (${leftText(j.left_s, j.status)})` : "";
  say(j.status === "queued" ? `Queued as <code>${esc(runId)}</code>, waiting for the job ahead of it${left}.` : `Working: ${fin} of ${total || "?"} steps done${left}.`, "ok");
  setTimeout(() => followJob(runId, say), 3000);
}
async function openRun(runId) {
  const lib = await api("/api/library").catch(() => []);
  const rec = lib.find((r) => r.views.some((v) => v.run_id === runId));
  if (rec) openPlayer(runId, rec.id); else showTab("results");
}

// Audio and video files on this machine, for "Transcribe a recording": one file, or every recording in a folder.
async function pickMedia(start, done) {
  async function show(path) {
    let d;
    try { d = await api(`/api/fs?files=true${path ? `&path=${encodeURIComponent(path)}` : ""}`); } catch (err) { d = await api("/api/fs?files=true"); }
    const join = (n) => d.path.replace(/[\\/]$/, "") + (d.path.includes("\\") ? "\\" : "/") + n;
    openModal(`<h3>Choose a recording</h3><div class="muted" style="font-family:var(--mono)">${esc(d.path)}</div>
      <div class="row" style="display:flex;gap:6px;margin-top:6px">${d.parent ? `<button class="small secondary" data-go="${esc(d.parent)}">↑ Up</button>` : ""}
        <button class="small secondary" data-go="${esc(d.home)}">Home</button><button class="small secondary" data-go="${esc(d.archive)}">Archive</button></div>
      <div class="fs-list">${d.dirs.map((n) => `<div data-go="${esc(join(n))}">📁 ${esc(n)}</div>`).join("")}
        ${d.files.map((f) => `<div data-file="${esc(join(f.name))}">🎞 ${esc(f.name)} <span class="muted">${gb(f.bytes)}</span></div>`).join("")}
        ${d.dirs.length || d.files.length ? "" : `<p class="muted" style="padding:8px">No folders or recordings here.</p>`}</div>
      ${d.files.length > 1 ? `<button class="secondary" data-all="1">Use all ${d.files.length} recordings in this folder</button>` : ""}`);
    $("#modal-body").onclick = (e) => {
      const g = e.target.closest("[data-go]");
      if (g) return show(g.dataset.go);
      const f = e.target.closest("[data-file]"), all = e.target.closest("[data-all]");
      if (f || all) { closeModal(); $("#modal-body").onclick = null; done(f ? [f.dataset.file] : d.files.map((x) => join(x.name)), d.path); }
    };
  }
  show(start);
}
function showAddRecording() {
  playing = null;
  history.replaceState(null, "", location.pathname);
  $("#player").innerHTML = `<h3>Transcribe a recording</h3><div id="tx-lib"></div>`;
  transcribeForm($("#tx-lib"), SETUP);
}
$("#lib-add").addEventListener("click", showAddRecording);

// ---------------------------------------------------------------- review: checking by hand (reviews.py)
// One item per screen, as the gold coding and stress labelling pages did: blind (the machine's answer comes only
// after yours, unless you ask for it first, and that is recorded), keys for everything, every answer saved the
// moment it is given, Back to change one. The server never sends what a set keeps hidden.
let RV = null, rvItem = null, rvK = 2, rvShown = 0, rvPrev = null, rvWords = new Set();
const rvBy = () => LS.get("reviewer") || "me";
const RV_KINDS = { comments: ["Public comments", "is each sampled speech block a public comment? (needs comment extraction)"],
  speakers: ["Speakers", "who is each speaker label? (needs speakers)"], transcript: ["Transcript lines", "correct sampled lines (needs a transcript)"],
  check: ["Check and code", "every speaker turn, word by word, with keys for Whisper's other guesses, then codes (needs word choices)"] };

async function refreshReview(id = null) {
  id = id ?? RV?.id ?? null;
  if (!id) return reviewList();
  const fresh = !RV || RV.id !== id;
  RV = await api(`/api/reviews/${encodeURIComponent(id)}`).catch(() => null);
  if (!RV) return reviewList();
  if (RV.kind === "check") { location.href = `/check#${encodeURIComponent(RV.id)}`; return; }  // its own page
  if (fresh) { rvItem = null; rvPrev = null; }
  history.replaceState(null, "", `#review/${RV.id}`);
  reviewShow(rvItem ? rvItem.id : rvNextOpen(null));
}
function rvNextOpen(after) {
  const i = after == null ? -1 : RV.order.indexOf(after);
  return RV.order.slice(i + 1).concat(RV.order.slice(0, i + 1)).find((x) => !RV.answers[x]) ?? null;
}

async function reviewList() {
  RV = null; rvItem = null;
  if (location.hash.startsWith("#review")) history.replaceState(null, "", location.pathname);
  const [sets, runs] = await Promise.all([api("/api/reviews").catch(() => []), api("/api/runs").catch(() => [])]);
  const done = runs.filter((r) => r.status === "done").sort((a, b) => (b.created_utc || "").localeCompare(a.created_utc || ""));
  $("#review").innerHTML = `<p class="lede">Check the machine's work by hand. Each review is a set of items, one per screen, in a shuffled order
      (so stopping early still leaves a random sample). Where the machine has an answer, you see it only after giving yours.</p>
    ${sets.length ? table(sets, [["review", (p) => p.name], ["kind", (p) => p.kind], ["answered", (p) => `${p.answered} / ${p.items}`],
      ["left", (p) => (p.left ? (p.left_s ? `about ${fmtDur(Math.round(p.left_s / 60) * 60).replace(/ 0 s$/, "")}` : `${p.left} items`) : "done")],
      ["machine agreed", (p) => (p.agreement == null ? "" : `${Math.round(p.agreement * 100)}% (${p.machine_label})`)], ["", () => ""]])
      : `<p class="muted">No reviews yet.</p>`}
    <div class="imp-card" style="margin-top:12px"><h3>New review</h3>
      <div class="row" style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
        <label>What to check <select id="rv-kind">${Object.entries(RV_KINDS).map(([k, [t, d]]) => `<option value="${k}" title="${esc(d)}">${t}: ${esc(d)}</option>`).join("")}</select></label>
        <label>From the run <select id="rv-run">${done.map((r) => `<option value="${esc(r.run_id)}">${esc(r.name)} · ${esc(fmtTime(r.created_utc))} · ${r.items} item${r.items > 1 ? "s" : ""}</option>`).join("")}</select></label>
        <label style="width:110px">How many <input id="rv-sample" type="number" value="100" min="1"></label>
        <label><input type="checkbox" id="rv-blind"> transcript: write from scratch</label>
        <button id="rv-make">Make it</button></div>
      <div id="rv-msg" class="msg"></div>
      <div class="muted" style="font-size:12px">Speakers makes one item per speaker label per recording. From the command line: <code>ontherecord review new RUN --kind comments</code>;
        sets made by another project: <code>ontherecord review create FILE.json</code> (see scripts/).</div></div>`;
  const rows = document.querySelectorAll("#review table.data tr");
  sets.forEach((p, i) => { rows[i + 1].lastElementChild.innerHTML = `<button class="small" data-rv-open="${esc(p.id)}">${p.answered ? "Continue" : "Start"}</button>
    <a class="small" href="/api/reviews/${esc(p.id)}/export.csv">CSV</a>`; });
}

async function reviewShow(id) {
  const n = Object.keys(RV.answers).length, pace = RV.seconds_per_item;
  const left = RV.items - n;
  const head = `<div class="rv-bar"><a href="#" data-rv-list="1">← All reviews</a><b>${esc(RV.name)}</b>
      <button class="small secondary" data-rv-step="-1">← Back</button><button class="small secondary" data-rv-step="1">Next →</button>
      <button class="small secondary" data-rv-todo="1">Next unanswered</button>
      <span class="muted">${n} / ${RV.items} answered${left && pace ? ` · about ${fmtDur(Math.round(pace * left / 60) * 60).replace(/ 0 s$/, "")} left at your pace` : ""}</span>
      <span class="grow"></span><label class="muted" style="margin:0">answering as <input id="rv-by" value="${esc(rvBy())}" style="width:90px;display:inline-block;margin:0"></label>
      <button class="small secondary" data-rv-score="1">Score</button></div>
    <div class="rv-progress"><div style="width:${(100 * n / RV.items).toFixed(1)}%"></div></div>`;
  const prev = rvPrev ? `<div class="since">Item ${esc(rvPrev.label)}: you said <b>${esc(rvPrev.mine)}</b>${rvPrev.machine != null ? `; ${esc(RV.machine_label)} said <b>${esc(rvPrev.machine)}</b>${rvPrev.mine === rvPrev.machine ? " ✓" : ""}` : ""}.
      <a href="#" data-rv-goto="${esc(rvPrev.id)}">Back to change it</a></div>` : "";
  if (id == null) {
    $("#review").innerHTML = head + prev + `<p class="lede" style="margin-top:16px">Every item has an answer. Thank you. Score shows how the machine did; CSV exports the answers.</p>`;
    return;
  }
  const it = await api(`/api/reviews/${encodeURIComponent(RV.id)}/items/${encodeURIComponent(id)}?k=${rvK}`);
  rvItem = it; rvShown = Date.now();
  const a = RV.answers[id] || {}, ans = RV.answer, opts = ans.options || [];
  rvWords = new Set(Array.isArray(a.value) ? a.value : []);
  const keyOf = (o) => (o.key ? `<kbd>${esc(o.key.toUpperCase())}</kbd>` : "");
  const optBtns = opts.flatMap((o) => (o.reasons && o.reasons.length ? o.reasons.map((r) => ({ ...o, reason: r.value, label: `${o.label}: ${r.label}`, key: r.key })) : [o]))
    .map((o) => `<button class="rv-opt ${a.value === o.value && (!o.reason || a.reason === o.reason) ? "sel" : ""}" data-rv-ans="${esc(o.value)}" data-rv-reason="${esc(o.reason || "")}">${esc(o.label)}${keyOf(o)}</button>`).join("");
  const words = ans.kind === "words" ? `<div class="rv-words">${it.words.map((w, i) => `<button class="rv-word ${rvWords.has(i) ? "sel" : ""}" data-rv-word="${i}">${esc(w)}${i < 10 ? `<kbd>${(i + 1) % 10}</kbd>` : ""}</button>`).join("")}
      <button class="rv-opt" data-rv-save-words="1">Save these words<kbd>⏎</kbd></button></div>` : "";
  const text = ans.kind === "text" ? `<textarea id="rv-text" rows="3" placeholder="Type the answer">${esc(typeof a.value === "string" && !opts.some((o) => o.value === a.value) ? a.value : it.prefill || "")}</textarea>
      <button class="rv-opt" data-rv-save-text="1">Save<kbd>Ctrl ⏎</kbd></button>` : "";
  const flags = (ans.flags || []).map((f) => `<label class="muted" style="margin:0"><input type="checkbox" data-rv-flag="${esc(f.name)}" ${(a.flags || {})[f.name] ? "checked" : ""}> ${esc(f.label)}${keyOf(f)}</label>`).join(" ");
  const ctx = (it.context && it.context.length ? it.context : []).filter((c) => c.offset !== 0);
  const target = `<div class="rv-blk rv-target"><div class="who">TARGET${it.speaker ? ` · ${esc(it.speaker)}` : ""}${it.start != null ? ` · ${hm(it.start)}–${hm(it.end)}` : ""}</div>${esc(it.text || "") || '<span class="muted">(write what you hear)</span>'}</div>`;
  const blk = (c) => `<div class="rv-blk"><div class="who">${esc(c.speaker || "")}${c.start != null ? ` · ${hm(c.start)}–${hm(c.end)}` : ""}</div>${esc(c.text || "")}</div>`;
  $("#review").innerHTML = head + prev + `
    <details class="rv-q"><summary>The question, and the keys</summary><p>${esc(RV.question)}</p>
      <p class="muted">Keys: the letter on each button answers and moves on${ans.kind === "words" ? "; 1–9 and 0 pick words, Enter saves them" : ""}${ans.kind === "text" ? "; Ctrl+Enter saves the text" : ""};
        P plays or pauses; M shows more context; / jumps to the note (Esc leaves it); ←/→ move without answering.</p></details>
    <div class="meta" style="margin:8px 0">${esc(it.title || "")}${it.link ? ` · <a href="${esc(it.link)}" target="_blank" rel="noopener">source at ${hm(it.start)}</a>` : ""}
      ${a.value !== undefined ? ` · <b>answered: ${esc(rvShowValue(a.value, it))}</b>` : ""}</div>
    <div class="row" style="display:flex;gap:8px;align-items:center">${it.has_clip ? `<audio id="rv-aud" controls preload="auto" src="/api/reviews/${encodeURIComponent(RV.id)}/clip/${encodeURIComponent(id)}"></audio>` : `<span class="muted">no audio for this item</span>`}
      ${it.more_context ? `<button class="small secondary" data-rv-more="1">More context<kbd>M</kbd></button>` : ""}</div>
    ${ctx.filter((c) => c.offset < 0).map(blk).join("")}${target}${ctx.filter((c) => c.offset > 0).map(blk).join("")}
    ${words}${text}<div class="rv-opts">${ans.kind === "choice" ? optBtns : `<span class="muted">or:</span> ${optBtns}`}</div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;margin:6px 0">${flags}
      ${it.has_machine && RV.reveal === "after" ? `<label class="muted" style="margin:0"><input type="checkbox" id="rv-peek"> show ${esc(RV.machine_label)}'s answer first (recorded as seen)</label>` : ""}</div>
    <div id="rv-peeked" class="msg"></div>
    <textarea id="rv-note" rows="1" placeholder="Note: anything that made this hard (optional)">${esc(a.note || "")}</textarea>
    <div id="rv-saved" class="msg"></div>`;
  history.replaceState(null, "", `#review/${RV.id}/${encodeURIComponent(id)}`);
  if (ans.kind === "text") $("#rv-text").focus();
}
function rvShowValue(v, it) {
  if (Array.isArray(v)) return v.map((i) => (it && it.words ? it.words[i] : i)).join(" ");
  return typeof v === "number" && it && it.words ? it.words[v] : String(v);
}
async function rvAnswer(value, reason = "") {
  const flags = Object.fromEntries([...document.querySelectorAll("[data-rv-flag]")].map((x) => [x.dataset.rvFlag, x.checked]));
  const body = { item: rvItem.id, value, reason, flags, note: $("#rv-note").value, seconds: Math.round((Date.now() - rvShown) / 1000), by: rvBy() };
  try {
    const r = await api(`/api/reviews/${encodeURIComponent(RV.id)}/answer`, { json: body });
    rvPrev = { id: rvItem.id, label: RV.order.indexOf(rvItem.id) + 1, mine: rvShowValue(r.value, rvItem),
      machine: r.machine === undefined ? null : rvShowValue(r.machine, rvItem) };
    const at = rvItem.id;
    RV = await api(`/api/reviews/${encodeURIComponent(RV.id)}`);
    rvK = 2;
    reviewShow(rvNextOpen(at) ?? RV.order[RV.order.indexOf(at) + 1] ?? null);  // all answered: walk on, revising
  } catch (err) { $("#rv-saved").textContent = `Not saved: ${err.message}`; $("#rv-saved").className = "msg err"; }
}
function rvStep(d) { const i = RV.order.indexOf(rvItem?.id) + d; if (i >= 0 && i < RV.order.length) { rvK = 2; reviewShow(RV.order[i]); } }

$("#review").addEventListener("click", async (e) => {
  const t = e.target.closest("[data-rv-open],[data-rv-list],[data-rv-step],[data-rv-todo],[data-rv-ans],[data-rv-word],[data-rv-save-words],[data-rv-save-text],[data-rv-more],[data-rv-goto],[data-rv-score],#rv-make");
  if (!t) return;
  if (t.tagName === "A") e.preventDefault();
  const d = t.dataset;
  if (d.rvOpen) return refreshReview(d.rvOpen);
  if (d.rvList) return reviewList();
  if (d.rvStep) return rvStep(+d.rvStep);
  if (d.rvTodo) return reviewShow(rvNextOpen(rvItem?.id ?? null));
  if (d.rvGoto) { rvPrev = null; return reviewShow(d.rvGoto); }
  if (d.rvMore) { rvK = Math.min(rvK + 4, 12); return reviewShow(rvItem.id); }
  if (d.rvAns !== undefined) return rvAnswer(d.rvAns, d.rvReason || "");
  if (d.rvWord !== undefined) { const i = +d.rvWord; rvWords.has(i) ? rvWords.delete(i) : rvWords.add(i); t.classList.toggle("sel"); return; }
  if (d.rvSaveWords) return rvWords.size ? rvAnswer([...rvWords]) : ($("#rv-saved").textContent = "Pick a word first, or one of the other answers.");
  if (d.rvSaveText) return rvAnswer($("#rv-text").value);
  if (d.rvScore) {
    const s = await api(`/api/reviews/${encodeURIComponent(RV.id)}/score`);
    const main = [["agreement", "agreed"], ["kappa", "Cohen's kappa"], ["exact", "exact match"], ["wer", "word error rate"]].filter(([k]) => s[k] != null);
    return openModal(`<h3>How ${esc(s.machine_label || "the machine")} did on ${esc(RV.name)}</h3>
      <p>${s.scored} item${s.scored === 1 ? "" : "s"} scored of ${s.answered} answered${s.blind ? ", each on the first answer given before seeing the machine's" : ""}.
        ${main.map(([k, l]) => `${l}: <b>${k === "kappa" ? s[k] : Math.round(s[k] * 1000) / 10 + "%"}</b>`).join(" · ") || "Nothing to compare yet (this set has no machine answers, or none are answered)."}</p>
      ${s.confusion ? table(Object.entries(s.confusion).map(([m, row]) => ({ m, ...row })), [["machine said", (r) => r.m], ...[...new Set(Object.values(s.confusion).flatMap(Object.keys))].sort().map((h) => [`you: ${h}`, (r) => r[h] || 0])]) : ""}
      <p class="muted" style="font-size:12px">Set ${esc((s.set_sha256 || "").slice(0, 16))}, answers ${esc((s.answers_sha256 || "–").slice(0, 16))} (sha256): what a write-up cites with these numbers.
        Unsure and other answers marked as not scored are left out. <code>ontherecord review score ${esc(RV.id)}</code> prints the same.</p>`);
  }
  if (t.id === "rv-make") {
    const m = $("#rv-msg");
    m.textContent = "Making it…"; m.className = "msg";
    try {
      const p = await api("/api/reviews", { json: { run: $("#rv-run").value, kind: $("#rv-kind").value, sample: $("#rv-kind").value === "check" ? 0 : +$("#rv-sample").value || 100, blind: $("#rv-blind").checked } });
      refreshReview(p.id);
    } catch (err) { m.textContent = err.message; m.className = "msg err"; }
  }
});
$("#review").addEventListener("change", async (e) => {
  if (e.target.id === "rv-by") LS.set("reviewer", e.target.value.trim() || "me");
  if (e.target.id === "rv-peek" && e.target.checked) {
    const r = await api(`/api/reviews/${encodeURIComponent(RV.id)}/reveal/${encodeURIComponent(rvItem.id)}?by=${encodeURIComponent(rvBy())}`, { method: "POST" }).catch((err) => ({ error: err.message }));
    $("#rv-peeked").textContent = r.error || `${RV.machine_label} said: ${rvShowValue(r.machine, rvItem)}`;
    e.target.disabled = true;
  }
});
document.addEventListener("keydown", (e) => {
  if (!$("#tab-review").classList.contains("active") || !RV || !rvItem || !$("#modal").hidden) return;
  const typing = ["TEXTAREA", "INPUT", "SELECT"].includes(e.target.tagName);
  if (typing) {
    if (e.key === "Escape") e.target.blur();
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.id === "rv-text") { e.preventDefault(); rvAnswer(e.target.value); }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase(), ans = RV.answer;
  if (e.key === "ArrowLeft") return rvStep(-1);
  if (e.key === "ArrowRight") return rvStep(1);
  if (k === "p") { const a = $("#rv-aud"); if (a) a.paused ? a.play() : a.pause(); return; }
  if (k === "m") { const b = document.querySelector("[data-rv-more]"); if (b) b.click(); return; }
  if (e.key === "/") { e.preventDefault(); $("#rv-note").focus(); return; }
  if (ans.kind === "words" && /^[0-9]$/.test(e.key)) { const b = document.querySelector(`[data-rv-word="${(+e.key + 9) % 10}"]`); if (b) b.click(); return; }
  if (ans.kind === "words" && e.key === "Enter") { document.querySelector("[data-rv-save-words]").click(); return; }
  const flag = (ans.flags || []).find((f) => f.key === k);
  if (flag) { const x = document.querySelector(`[data-rv-flag="${flag.name}"]`); x.checked = !x.checked; return; }
  for (const o of ans.options || []) {
    if (o.reasons && o.reasons.length) { const r = o.reasons.find((r) => r.key === k); if (r) return rvAnswer(o.value, r.value); }
    else if (o.key === k) return rvAnswer(o.value);
  }
});

// ---------------------------------------------------------------- comparing two runs
async function showCompare(a, b) {
  const c = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`).catch((err) => ({ error: err.message }));
  if (c.error) { $("#grid").innerHTML = `<p class="msg err">${esc(c.error)}</p>`; return; }
  CMP = c;
  const num = (v) => (v == null ? "–" : typeof v === "number" ? v.toLocaleString() : v);
  const head = `<tr><th>item</th>${c.columns.map((col) => `<th>${colHead({ name: col.name, stage: col.stage })}${col.a_step && col.a_step !== col.name ? `<br><span class="step">a: ${esc(col.a_step)}</span>` : ""}</th>`).join("")}</tr>`;
  const rows = Object.entries(c.rows).map(([item, row]) => `<tr><td class="item">${esc(item)}</td>${c.columns.map((col) => {
    const x = row[col.name] || {};
    const cls = x.same === true ? "c-cached" : x.same === false ? "c-run" : "c-pending";
    const label = x.same === true ? "same" : x.same === false ? "differs" : (x.a?.status || "–") + " / " + (x.b?.status || "–");
    const vals = x.unit !== undefined && (x.a?.value != null || x.b?.value != null) ? `<br><span class="t">${esc(num(x.a?.value))} → ${esc(num(x.b?.value))}</span>` : "";
    const chk = [x.a?.checks, x.b?.checks].some((k) => k === "fail") ? ` <span class="chk-fail">● fail</span>` : "";
    return `<td><span class="cell ${cls}" data-cmp-item="${esc(item)}" data-cmp-col="${esc(col.name)}"><span class="s">${esc(label)}</span>${chk}${vals}</span></td>`;
  }).join("")}</tr>`).join("");
  $("#grid").innerHTML = `<p class="muted">Left of each arrow: <b>${esc(c.a.name)}</b> (${esc(c.a.run_id)}). Right: <b>${esc(c.b.name)}</b> (this run).
    “same” means byte-identical outputs.</p><table class="grid">${head}${rows}</table>`;
  $("#receipt").innerHTML = "";
}
$("#run-head").addEventListener("change", (e) => {
  if (e.target.id === "compare-pick") { compareWith = e.target.value || null; showRun(currentRun); }
});
$("#grid").addEventListener("click", (e) => {
  const c = e.target.closest("[data-cmp-item]");
  if (!c || !CMP) return;
  const x = CMP.rows[c.dataset.cmpItem][c.dataset.cmpCol];
  const unit = x.unit ? ` ${esc(x.unit)}` : "";
  const settings = Object.entries(x.settings_differ || {}).map(([k, [va, vb]]) => `<tr><td>${esc(k)}</td><td class="v">${esc(JSON.stringify(va))} → ${esc(JSON.stringify(vb))}</td></tr>`).join("");
  $("#receipt").innerHTML = `<div class="card"><h3>${esc(c.dataset.cmpCol)} · ${esc(c.dataset.cmpItem)}</h3>
    <p>${x.same ? "The two runs made byte-identical outputs here." : x.same === false ? "The outputs differ." : "Only one run has this stage for this item."}
      ${x.a?.value != null || x.b?.value != null ? `<b>${esc(String(x.a?.value ?? "–"))} → ${esc(String(x.b?.value ?? "–"))}</b>${unit}.` : ""}</p>
    <h4>Why</h4><ul>
      ${settings ? `<li>Settings that differ:<table class="kv">${settings}</table></li>` : "<li>Same settings.</li>"}
      ${(x.inputs_differ || []).length ? `<li>Inputs that differ (made differently upstream): ${esc(x.inputs_differ.join(", "))}</li>` : "<li>Same inputs.</li>"}
    </ul>
    <h4>Checks</h4><div>${esc(CMP.a.name)}: ${esc(x.a?.checks || x.a?.status || "–")} · ${esc(CMP.b.name)}: ${esc(x.b?.checks || x.b?.status || "–")}</div></div>`;
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
  const draft = LS.json("draft");
  if (draft && Array.isArray(draft.stages)) {
    form = { ...form, ...draft, stages: draft.stages.filter((st) => DEFS[st.name]) };
    $("#r-name").value = form.name || ""; $("#r-desc").value = form.description || "";
    $("#r-urls").value = form.urls || ""; $("#r-files").value = form.files || "";
  }
  renderStages();
  restoreChat();
  await refreshLibrary();
  // Pick up where the person left off: a link to a moment wins, then the last recording (paused where it was),
  // then the last tab. Until the Library has a recording, Setup's guide comes first.
  const m = location.hash.match(/^#play\/([^/]+)\/([^/]+)(?:\/([\d.]+))?/);
  const rv = location.hash.match(/^#review\/([a-z0-9-]+)(?:\/(.+))?/);
  const last = LS.json("play");
  if (m) openPlayer(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3] ? +m[3] : null);
  else if (rv) { RV = { id: rv[1] }; rvItem = rv[2] ? { id: decodeURIComponent(rv[2]) } : null; showTab("review"); refreshReview(rv[1]); }
  else {
    if (last && LIB.some((r) => r.id === last.item)) openPlayer(last.run, last.item, last.t || null, { switchTab: false, autoplay: false });
    else if (!LIB.length) showAddRecording();
    const tab = LS.get("tab");
    showTab(!LIB.length ? "setup" : tab && document.querySelector(`nav button[data-tab="${tab}"]`) ? tab : "library");
  }
  sinceLastTime();
  api("/api/setup").then((d) => { SETUP = d; markSetupTab(setupDone(d, LIB.length)); }).catch(() => {});
  setInterval(() => { if ($("#tab-jobs").classList.contains("active")) refreshJobs(); }, 2000);
})();

// What finished, failed or is still going since this browser last had the GUI open.
async function sinceLastTime() {
  const seen = LS.get("seen"), jobs = await api("/api/jobs").catch(() => []);
  const mark = () => LS.set("seen", new Date().toISOString());
  mark();
  setInterval(mark, 60000);
  if (!seen) return;
  const ended = jobs.filter((j) => j.finished_utc && new Date(j.finished_utc) > new Date(seen));
  const going = jobs.filter((j) => j.status === "running" || j.status === "queued");
  if (!ended.length && !going.length) return;
  const item = (j) => j.status === "done" ? `✓ <b>${esc(j.name)}</b> finished${j.backend === "local" ? ` <a href="#" data-since-open="${esc(j.run_id)}">open</a>` : ""}`
    : `✗ <b>${esc(j.name)}</b> ${j.status === "failed" ? "failed" : "was cancelled"} <a href="#" data-since-why="${esc(j.run_id)}">why</a>`;
  $("#since").innerHTML = `<span>Since you were last here:</span> ${ended.slice(0, 6).map(item).join(" · ")}${ended.length > 6 ? ` · and ${ended.length - 6} more` : ""}
    ${going.length ? `${ended.length ? " · " : ""}${going.length} job${going.length > 1 ? "s" : ""} still going (<a href="#" data-since-jobs="1">Jobs</a>)` : ""}
    <span class="grow"></span><button class="small secondary" data-since-close="1">×</button>`;
  $("#since").hidden = false;
}
$("#since").addEventListener("click", (e) => {
  const t = e.target.closest("a, button");
  if (!t) return;
  e.preventDefault();
  if (t.dataset.sinceOpen) openRun(t.dataset.sinceOpen);
  if (t.dataset.sinceWhy) { showTab("results"); showRun(t.dataset.sinceWhy); }
  if (t.dataset.sinceJobs) showTab("jobs");
  if (t.dataset.sinceClose) $("#since").hidden = true;
});
