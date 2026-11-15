// On the Record: the Check page. A checking set's speaker turns, one per screen: fix the transcript word by word
// (Whisper's other guesses for its doubtful words sit on keys), then code stretches of it. Five ways of working
// ("schemes") share one engine: the audio, the edits, undo, drafts, saving and coding. Plain JavaScript, no build.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LS = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} }, del: (k) => { try { localStorage.removeItem(k); } catch {} },
  json: (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } } };
const norm = (t) => (String(t || "").toLowerCase().replace(/’/g, "'").match(/[a-z0-9']+/g) || []).join(" ");
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function api(path, opts = {}) {
  if (opts.json !== undefined) opts = { ...opts, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.json) };
  const r = await fetch(path, opts);
  const body = (r.headers.get("content-type") || "").includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof body === "string" ? body : body.detail || JSON.stringify(body));
  return body;
}

// ---------------------------------------------------------------- theme (same switch as the main page)
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $("#theme").textContent = `Theme: ${t}`;
}
applyTheme(LS.get("theme") || "auto");
$("#theme").addEventListener("click", () => {
  const t = THEMES[(THEMES.indexOf(LS.get("theme") || "auto") + 1) % THEMES.length];
  LS.set("theme", t); applyTheme(t);
});

// ---------------------------------------------------------------- keys: physical positions (e.code), so the
// left-hand layout holds on any keyboard layout; shown with their US-keyboard names
const CAP = { Backquote: "`", Tab: "Tab", CapsLock: "Caps", ShiftLeft: "Shift", Space: "Space", Escape: "Esc", Enter: "Enter" };
for (const d of "12345") CAP[`Digit${d}`] = d;
for (const k of "QWERTASDFGZXCVB") CAP[`Key${k}`] = k;
const ROWS = [["Escape"], ["Backquote", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5"], ["Tab", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT"],
  ["CapsLock", "KeyA", "KeyS", "KeyD", "KeyF", "KeyG"], ["ShiftLeft", "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB"], ["Space"]];
const CODE_CODES = ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT"];
const CODE_KEY = Object.fromEntries(CODE_CODES.map((c) => [c, CAP[c]]));
const PATTERNS = ["solid", "dashed", "dotted", "double"];

// ---------------------------------------------------------------- state
const A = $("#ck-audio");
const S = { set: null, idx: 0, item: null, scheme: null, mode: "check", practice: false, coach: null, codes: [], next: null };
let T = null;            // the turn being worked on (see openTurn)
let stopAt = null;       // clip time at which play() stops by itself
let lastNow = -1;

const setId = () => decodeURIComponent((location.hash.slice(1) || "").split("/")[0]);
const clipUrl = (id) => `/api/reviews/${encodeURIComponent(S.set.id)}/clip/${encodeURIComponent(id)}`;
const who = () => LS.get("reviewer") || "me";   // the annotator: shared with the Review tab, asked once per browser
const draftKey = () => `ck:${S.set.id}:${who()}:${S.item.id}`;

// ---------------------------------------------------------------- audio
const clipT = (sec) => Math.max(0, sec - (S.item.clip_start || 0));       // recording seconds -> clip seconds
const recT = () => A.currentTime + (S.item.clip_start || 0);
const playing = () => !A.paused && !A.ended;
function play(from, to = null) {
  if (!S.item.has_clip) return status("This turn has no audio.");
  A.currentTime = clipT(from);
  stopAt = to == null ? null : clipT(to);
  A.play().catch((e) => { if (e.name !== "AbortError") status("Press a key or click once so the browser lets the page play sound."); });  // AbortError: a pause came first
  T.stats.plays++;
  emit("play");
}
function pause() { A.pause(); stopAt = null; emit("pause"); }
function resume() { const t = recT(); play(A.currentTime > 0 && !A.ended && t < T.end - 0.3 ? t : T.start); }  // on from where it stopped
function playFrom(i) { play(Math.max(T.start, T.words[i].s - 0.1)); }
function leadIn(i) { return Math.max(T.start, Math.max(T.words[Math.max(0, i - 2)].s, T.words[i].s - 3) - 0.1); }  // two words before i
function playWord(i, before = 0.12, after = 0.12) {
  const w = T.words[i]; if (!w) return;
  const j = lastOfSpan(i);
  play(Math.max(T.start, w.s - before), Math.min(T.end + 0.5, T.words[j].e + after));
}
function playSpan(i, j) { play(Math.max(T.start, T.words[Math.max(0, i)].s - 0.12), T.words[Math.min(T.words.length - 1, j)].e + 0.15); }
function wordAt(t) {  // the last word starting at or before recording time t
  let lo = 0, hi = T.words.length - 1, ans = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (T.words[m].s <= t + 0.02) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}
function tick() {
  if (T && S.item) {
    if (stopAt != null && A.currentTime >= stopAt) { A.pause(); stopAt = null; emit("stopped"); }
    if (playing()) {
      const now = wordAt(recT());
      if (now !== lastNow) {
        lastNow = now;
        if (S.mode === "check") S.scheme.onWord?.(now);
        markNow();
      }
      T.lastActive = performance.now();
    } else if (lastNow !== -1 && !A.seeking) {
      lastNow = -1;
      markNow();
    }
    drawProgress();
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
function markNow() {
  document.querySelectorAll("#ck-turn .now").forEach((e) => e.classList.remove("now"));
  if (S.mode === "check" && lastNow >= 0) document.querySelectorAll(`#ck-turn [data-i="${lastNow}"]`).forEach((e) => e.classList.add("now"));
  if (playing()) keepInView($("#ck-turn .now"));
}
// the bar above the turn: where the audio is, moving through pauses in the speech too; a click plays from there
function drawProgress() {
  const bar = $("#ck-progress"), len = Math.max(0.1, T.end - T.start);
  const at = A.currentTime > 0 ? Math.min(T.end, Math.max(T.start, recT())) : T.start;
  const on = playing(), w = lastNow >= 0 ? T.words[lastNow] : null, gap = !!(on && w && recT() > w.e + 0.7);
  bar.firstElementChild.style.width = `${(100 * (at - T.start)) / len}%`;
  bar.classList.toggle("on", on); bar.classList.toggle("gap", gap);
  const label = `${on ? "▶" : "❚❚"} ${mmss(at - T.start)} / ${mmss(len)}${gap ? " · pause in the speech" : ""}`;
  if (bar.lastElementChild.textContent !== label) bar.lastElementChild.textContent = label;
}
$("#ck-progress").addEventListener("click", (e) => {
  if (!T) return;
  const r = e.currentTarget.getBoundingClientRect();
  play(T.start + ((T.end - T.start) * (e.clientX - r.left)) / r.width);
});
// the turns scroll past like one transcript: the playing word stays in view unless the person scrolled just now
const motion = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
let reelTouched = 0;
["wheel", "touchmove"].forEach((ev) => $("#ck-reel").addEventListener(ev, () => { reelTouched = performance.now(); }, { passive: true }));
function keepInView(el) {
  const reel = $("#ck-reel");
  if (!el || performance.now() - reelTouched < 4000) return;
  const r = el.getBoundingClientRect(), R = reel.getBoundingClientRect();
  if (r.top < R.top + R.height * 0.12 || r.bottom > R.bottom - R.height * 0.25) reel.scrollTo({ top: reel.scrollTop + r.top - R.top - R.height * 0.35, behavior: motion() });
}
function placeReel(from) {  // the turn just left sits where it was, then everything scrolls to the new one
  const reel = $("#ck-reel"), R = reel.getBoundingClientRect();
  const target = reel.scrollTop + $("#ck-who").getBoundingClientRect().top - R.top - Math.min(R.height * 0.22, $("#ck-before").offsetHeight);
  const old = from && [...reel.querySelectorAll(".ctx-turn")].find((p) => p.dataset.id === from.id);
  if (!old) { reel.scrollTop = target; return; }
  reel.scrollTop += old.getBoundingClientRect().top - from.top;
  requestAnimationFrame(() => reel.scrollTo({ top: target, behavior: motion() }));
}
// active time: seconds with a key, a click or the audio playing in the last 30 s (an idle page does not count)
setInterval(() => { if (T && (playing() || performance.now() - T.lastActive < 30000)) T.active += 1; }, 1000);

// ---------------------------------------------------------------- the turn: original words, and what replaces them
// out[i] is what word i now says ("" when deleted, or absorbed into the word before by a two-word choice); how[i] is
// null (as heard), "choice", "typed", "deleted" or "absorbed". Codes live on output tokens keyed "i.k".
function openTurn(item) {
  const words = item.words.map((w) => ({ ...w, alts: w.alts || [] }));
  T = { words, out: words.map((w) => w.w), how: words.map(() => null), unclear: new Set(), codes: new Map(),
        cursor: 0, sel: null, anchor: null, undo: [], start: item.start, end: item.end, flags: {}, schemes: new Set(),
        stats: { keys: 0, plays: 0, picks: 0, typed: 0 }, active: 0, lastActive: performance.now(), opened: Date.now() };
  const d = S.practice ? null : LS.json(draftKey()) || fromAnswer(S.set.answers?.[item.id]);
  if (d && d.out?.length === words.length) {
    T.out = d.out; T.how = d.how; T.unclear = new Set(d.unclear || []); T.codes = new Map(d.codes || []); T.flags = d.flags || {};
    T.restored = true;
  }
}
function fromAnswer(v) {
  if (!v || typeof v !== "object" || !v.words) return null;
  const n = S.item.words.length, out = S.item.words.map((w) => w.w), how = out.map(() => null);
  for (const e of v.edits || []) {
    if (e.i < 0 || e.i >= n) continue;
    out[e.i] = e.to; how[e.i] = e.how;
    for (let k = 1; k < e.n && e.i + k < n; k++) { out[e.i + k] = ""; how[e.i + k] = "absorbed"; }
  }
  const keyOf = [], seen = {};
  for (const w of v.words) { seen[w.i] = (seen[w.i] ?? -1) + 1; keyOf.push(`${w.i}.${seen[w.i]}`); }
  const codes = new Map();
  for (const c of v.codes || []) for (let k = c.from; k <= c.to; k++) {
    const key = keyOf[k]; if (!key) continue;
    if (!codes.has(key)) codes.set(key, []);
    if (!codes.get(key).includes(c.code)) codes.get(key).push(c.code);
  }
  return { out, how, unclear: v.words.filter((w) => w.unclear).map((w) => w.i), codes: [...codes], flags: {} };
}
function snapshot() {
  T.undo.push({ out: [...T.out], how: [...T.how], unclear: [...T.unclear], codes: [...T.codes].map(([k, v]) => [k, [...v]]) });
  if (T.undo.length > 200) T.undo.shift();
}
function changed() {
  if (!S.practice) LS.set(draftKey(), JSON.stringify({ out: T.out, how: T.how, unclear: [...T.unclear], codes: [...T.codes], flags: T.flags }));
  render();
}
function undo() {
  const u = T.undo.pop();
  if (!u) return status("Nothing to undo.");
  T.out = u.out; T.how = u.how; T.unclear = new Set(u.unclear); T.codes = new Map(u.codes);
  emit("undo"); status("Undone."); changed();
}
function lastOfSpan(i) { let j = i; while (j + 1 < T.words.length && T.how[j + 1] === "absorbed") j++; return j; }
function restoreSpan(i) { const j = lastOfSpan(i); for (let k = i; k <= j; k++) { T.out[k] = T.words[k].w; T.how[k] = null; } }
function choicesFor(i) { return i >= 0 && i < T.words.length && T.how[i] !== "absorbed" ? T.words[i].alts : []; }
function isPicked(i, alt) { return T.how[i] === "choice" && T.out[i] === alt.t; }
function pick(i, alt) {
  if (!alt) return status("No choice there.");
  snapshot();
  if (isPicked(i, alt)) { restoreSpan(i); status(`Back to “${T.words[i].w}”.`); }
  else {
    restoreSpan(i);
    T.out[i] = alt.t; T.how[i] = alt.t ? "choice" : "deleted";
    for (let k = 1; k < alt.n && i + k < T.words.length; k++) { T.out[i + k] = ""; T.how[i + k] = "absorbed"; }
    status(`“${T.words.slice(i, i + Math.max(1, alt.n)).map((w) => w.w).join(" ")}” → ${alt.t ? `“${alt.t}”` : "deleted"}`);
    T.stats.picks++;
  }
  emit("pick", { i }); changed();
}
function cycle(i, dir = 1) {  // as heard -> choice 1 -> choice 2 -> ... -> as heard
  const opts = [null, ...choicesFor(i)];
  if (opts.length < 2) return status("That word has no other choices.");
  const cur = Math.max(0, opts.findIndex((o) => (o ? isPicked(i, o) : T.how[i] == null)));
  const nxt = opts[(cur + dir + opts.length) % opts.length];
  if (nxt) pick(i, nxt); else { snapshot(); restoreSpan(i); emit("pick", { i }); changed(); status(`Back to “${T.words[i].w}”.`); }
}
function toggleDelete(i) {
  snapshot();
  if (T.how[i] === "deleted") { restoreSpan(i); status("Restored."); }
  else { restoreSpan(i); T.out[i] = ""; T.how[i] = "deleted"; status(`Deleted “${T.words[i].w}”.`); }
  emit("delete"); changed();
}
function toggleUnclear(i) {
  snapshot();
  if (T.unclear.has(i)) T.unclear.delete(i); else T.unclear.add(i);
  emit("unclear"); changed(); status(T.unclear.has(i) ? "Marked as hard to make out." : "Unmarked.");
}
// typed text over words i0..i1-1: lined up with them word by word (an edit-distance alignment), so the words that
// did not change keep their place, times and codes
function applyTyped(i0, i1, text, chosen = {}) {
  const typed = String(text).split(/\s+/).filter(Boolean);
  const cur = []; for (let k = i0; k < i1; k++) cur.push(T.how[k] === "absorbed" || T.how[k] === "deleted" ? null : T.out[k]);
  const ref = cur.map((x, k) => ({ k: i0 + k, t: x })).filter((x) => x.t !== null);
  const m = typed.length, n = ref.length;
  const D = Array.from({ length: m + 1 }, (_, a) => Array.from({ length: n + 1 }, (_, b) => (a === 0 ? b : b === 0 ? a : 0)));
  for (let a = 1; a <= m; a++) for (let b = 1; b <= n; b++)
    D[a][b] = Math.min(D[a - 1][b] + 1, D[a][b - 1] + 1, D[a - 1][b - 1] + (norm(typed[a - 1]) === norm(ref[b - 1].t) ? 0 : 1));
  const got = new Map(ref.map((r) => [r.k, []]));
  let a = m, b = n; const extra = [];
  while (a > 0 || b > 0) {
    if (a > 0 && b > 0 && D[a][b] === D[a - 1][b - 1] + (norm(typed[a - 1]) === norm(ref[b - 1].t) ? 0 : 1)) { got.get(ref[b - 1].k).unshift(typed[a - 1]); a--; b--; }
    else if (b > 0 && D[a][b] === D[a][b - 1] + 1) b--;
    else { (b > 0 ? got.get(ref[b - 1].k) : extra).unshift(typed[a - 1]); a--; }
  }
  if (extra.length) { const first = ref[0]?.k ?? i0; got.set(first, [...extra, ...(got.get(first) || [])]); }
  snapshot();
  for (const r of ref) {
    const t = (got.get(r.k) || []).join(" ");
    if (t === r.t) continue;
    T.out[r.k] = t;
    T.how[r.k] = !t ? "deleted" : t === T.words[r.k].w ? null : chosen[r.k] === t ? "choice" : "typed";
  }
  T.stats.typed++;
  status(typed.length ? `Typed “${typed.join(" ")}”.` : "Deleted.");
  emit("type"); changed();
}
function outWords() {  // the turn as it now reads: [{t, i, key, s, e, unclear}]
  const out = [];
  for (let i = 0; i < T.words.length; i++) {
    if (!T.out[i]) continue;
    const toks = T.out[i].split(/\s+/).filter(Boolean), j = lastOfSpan(i);
    const s = T.words[i].s, e = T.words[j].e, step = (e - s) / toks.length;
    toks.forEach((t, k) => out.push({ t, i, key: `${i}.${k}`, s: s + k * step, e: s + (k + 1) * step, unclear: T.unclear.has(i) }));
  }
  return out;
}
function edits() {
  const out = [];
  for (let i = 0; i < T.words.length; i++) {
    if (!T.how[i] || T.how[i] === "absorbed") continue;
    const j = lastOfSpan(i);
    out.push({ i, n: j - i + 1, from: T.words.slice(i, j + 1).map((w) => w.w).join(" "), to: T.out[i], how: T.how[i] });
  }
  return out;
}

// ---------------------------------------------------------------- coding: codes on stretches of the turn as it now reads
const OW = () => (T.ow ||= outWords());
function selRange() {
  const n = OW().length;
  if (!T.sel) return [0, n - 1];
  return [Math.max(0, Math.min(T.sel[0], T.sel[1])), Math.min(n - 1, Math.max(T.sel[0], T.sel[1]))];
}
function toggleCode(key) {
  const code = S.codes.find((c) => c.key === key); if (!code) return;
  const [a, b] = selRange(), ow = OW();
  const keys = ow.slice(a, b + 1).map((w) => w.key);
  const all = keys.every((k) => (T.codes.get(k) || []).includes(key));
  snapshot();
  for (const k of keys) {
    const cur = (T.codes.get(k) || []).filter((c) => c !== key);
    if (!all) cur.push(key);
    if (cur.length) T.codes.set(k, cur); else T.codes.delete(k);
  }
  emit("code", { key, on: !all });
  status(`${all ? "Removed" : "Applied"} ${code.name} ${T.sel ? `on ${b - a + 1} word${b > a ? "s" : ""}` : "on the whole turn"}.`);
  changed();
}
function clearCodes() {
  const [a, b] = selRange(); snapshot();
  for (const w of OW().slice(a, b + 1)) T.codes.delete(w.key);
  emit("clearcodes"); status("Codes removed."); changed();
}
function codeSpans() {
  const ow = OW(), spans = [];
  for (const c of S.codes) {
    let start = null;
    ow.forEach((w, k) => {
      const on = (T.codes.get(w.key) || []).includes(c.key);
      if (on && start === null) start = k;
      if ((!on || k === ow.length - 1) && start !== null) { spans.push({ code: c.key, from: start, to: on ? k : k - 1 }); start = null; }
    });
  }
  return spans;
}

// ---------------------------------------------------------------- saving
function answerValue() {
  const ow = outWords(); T.ow = ow;
  return { text: ow.map((w) => w.t).join(" "), words: ow.map((w) => ({ t: w.t, i: w.i, ...(w.unclear ? { unclear: true } : {}) })),
           edits: edits(), codes: codeSpans(), scheme: [...T.schemes, S.scheme.id].filter((v, i, a) => a.indexOf(v) === i).join("+"),
           keys: T.stats.keys, replays: T.stats.plays };
}
async function save(step = 1, option = null) {
  if (S.scheme.beforeSave) S.scheme.beforeSave();
  const value = option || answerValue();
  emit("save");
  if (S.practice) { status("Practice: not saved."); return; }
  try {
    await api(`/api/reviews/${encodeURIComponent(S.set.id)}/answer`, { json: { item: S.item.id, value, flags: T.flags, seconds: T.active, by: who() } });
    S.set.answers[S.item.id] = value;
    LS.del(draftKey());
    go(S.idx + step);
  } catch (e) { status(`Not saved: ${e.message}`, true); }
}
function go(idx) {
  if (idx < 0) return status("This is the first turn.");
  if (idx >= S.set.order.length) { status("That was the last turn. Every turn you saved is in the Review tab's CSV."); return; }
  S.idx = idx; load();
}

// ---------------------------------------------------------------- drawing
function wordClass(i) {
  const c = ["w"], h = T.how[i];
  if (h === "choice" || h === "typed") c.push("edited");
  if (h === "deleted" || h === "absorbed") c.push("gone");
  if (!h && T.words[i].alts.length) c.push("doubt");
  if (T.unclear.has(i)) c.push("unclear");
  return c;
}
function wordHtml(i, extra = {}) {
  const h = T.how[i], w = T.words[i];
  const cls = [...wordClass(i), ...(extra.cls || [])].join(" ");
  const text = h === "deleted" || h === "absorbed" ? `<s>${esc(w.w)}</s>` : esc(T.out[i]);
  const title = h ? ` title="heard as “${esc(T.words.slice(i, lastOfSpan(i) + 1).map((x) => x.w).join(" "))}”"` : w.alts.length ? ` title="Whisper's confidence: ${Math.round(w.p * 100)}%"` : "";
  return `<span class="${cls}" data-i="${i}"${extra.key ? ` data-key="${esc(extra.key)}"` : ""}${title}>${text}</span>`;
}
function codedHtml() {
  const ow = OW(), [a, b] = T.sel ? selRange() : [-1, -2];
  return ow.map((w, k) => {
    const on = T.codes.get(w.key) || [], nxt = T.codes.get(ow[k + 1]?.key) || [];
    const bars = S.codes.filter((c) => on.includes(c.key)).map((c) =>
      `<i class="bar${nxt.includes(c.key) ? " join" : ""}" style="--cc:var(--c${c.n});--cs:${PATTERNS[(c.n - 1) % 4]}"></i>`).join("");
    const tags = S.codes.filter((c) => on.includes(c.key) && !(T.codes.get(ow[k - 1]?.key) || []).includes(c.key)).map((c) => c.key).join("");
    return `<span class="w${k >= a && k <= b ? " sel" : ""}${k === T.cursor ? " cursor" : ""}${w.unclear ? " unclear" : ""}" data-o="${k}" style="--nb:${on.length}">${tags ? `<sup class="tags">${esc(tags)}</sup>` : ""}${esc(w.t)}${bars ? `<span class="bars">${bars}</span>` : ""}</span>`;
  }).join(" ");
}
function render() {
  T.ow = null;
  $("#ck-panel").onclick = null;
  if (S.mode === "code") {
    $("#ck-turn").className = "ck-turn coding";
    $("#ck-turn").innerHTML = codedHtml();
    $("#ck-panel").innerHTML = codePanel();
  } else {
    $("#ck-turn").className = `ck-turn scheme-${S.scheme.id}`;
    S.scheme.render();
    markNow();
  }
  drawHead();
  drawKeys();
}
function plainTurn(decorate = () => ({})) {
  $("#ck-turn").innerHTML = T.words.map((_, i) => wordHtml(i, decorate(i))).join(" ");
}
function drawHead() {
  const it = S.item, n = S.set.order.length, done = Object.keys(S.set.answers).length;
  const part = it.part ? ` · part ${it.part[0]} of ${it.part[1]}` : "";
  const flags = (S.set.answer.flags || []).map((f) => `<button class="flag-chip${T.flags[f.name] ? " on" : ""}" data-flag="${esc(f.name)}" aria-pressed="${!!T.flags[f.name]}">${esc(f.label)}</button>`).join("");
  const opts = (S.set.answer.options || []).map((o) => `<button class="flag-chip" data-option="${esc(o.value)}">${esc(o.label)}</button>`).join("");
  $("#ck-who").innerHTML = `<b class="spk">${esc(it.speaker || "speaker unknown")}</b>${part} <span class="muted"> · ${mmss(it.start)}–${mmss(it.end)}</span>`;
  $("#ck-head").innerHTML = `<div class="title muted">${esc(it.title || it.recording || "")}</div>
    <div class="where"><button class="small secondary" data-go="-1" title="Previous turn">◀</button>
      <span>Turn ${S.idx + 1} of ${n}${S.set.answers[it.id] ? " · saved before" : ""} · ${done} done</span>
      <button class="small secondary" data-go="1" title="Next turn (not saved)">▶</button>
      <span class="mode ${S.mode}">${S.mode === "code" ? "Coding" : `Checking · ${esc(S.scheme.name)}`}</span></div>
    <div class="flags">${flags}${opts}${T.restored ? `<span class="muted"> · your unsaved changes were brought back</span>` : ""}</div>`;
}
function ctxId(c) {  // the set's id for a neighboring turn (turns are numbered within their recording), if the set has it
  const m = /^(.*):(\d+)$/.exec(S.item.id); if (!m) return null;
  const id = `${m[1]}:${String(+m[2] + c.offset).padStart(m[2].length, "0")}`;
  return S.set.order.includes(id) ? id : null;
}
function drawContext() {  // the turns before and after, whole, as checked where they have been
  const ctx = (S.item.context || []).slice().sort((a, b) => a.offset - b.offset);
  const block = (c) => {
    const id = ctxId(c), v = id ? S.set.answers[id] : null, text = v && typeof v === "object" && v.text != null ? v.text : c.text;
    return `<p class="ctx-turn${id ? " go" : ""}"${id ? ` data-id="${esc(id)}" title="Go to this turn"` : ""}><b>${esc(c.speaker || "")}</b>
      <span class="muted">${mmss(c.start)}${v ? " · checked" : ""}</span><br>${esc(text)}</p>`;
  };
  $("#ck-before").innerHTML = ctx.filter((c) => c.offset < 0).map(block).join("");
  $("#ck-after").innerHTML = ctx.filter((c) => c.offset > 0).map(block).join("");
}
function status(msg, bad = false) { const s = $("#ck-status"); s.textContent = msg; s.classList.toggle("bad", bad); }
function codePanel() {
  const counts = {};
  for (const [, v] of T.codes) for (const c of v) counts[c] = (counts[c] || 0) + 1;
  const [a, b] = selRange();
  return `<div class="codes-legend">${S.codes.map((c) => `<button class="code" data-code="${esc(c.key)}" style="--cc:var(--c${c.n});--cs:${PATTERNS[(c.n - 1) % 4]}">
      <span class="cap">${esc(c.key)}</span><span class="swatch"></span>${esc(c.name)}${counts[c.key] ? ` <span class="muted">(${counts[c.key]})</span>` : ""}</button>`).join("")}</div>
    <p class="muted">${T.sel ? `Selected: words ${a + 1}–${b + 1}.` : "Nothing selected: a code key codes the whole turn."} Drag across words with the mouse, or move with A and F (hold Shift to stretch).
      Press a code's key to add it; press it again to take it off. Codes stack.</p>`;
}

// ---------------------------------------------------------------- the keyboard map: what each key does right now
function drawKeys() {
  const map = S.mode === "code" ? CODING.keys() : S.scheme.keys();
  const extra = Object.entries(map).filter(([k]) => !ROWS.flat().includes(k));
  const rows = ROWS.filter((row) => row[0] !== "Escape" || map.Escape);
  $("#ck-keys").innerHTML = `<div class="kb">${rows.map((row) => `<div class="kr">${row.map((k) => {
    const v = map[k]; return `<div class="k${v ? " used" : ""}${k === "Space" ? " space" : ""}${k === "Tab" || k === "CapsLock" || k === "ShiftLeft" ? " wide" : ""}" data-code="${k}">
      <span class="cap">${esc(CAP[k] || k)}</span>${v ? `<span class="what">${esc(v)}</span>` : ""}</div>`; }).join("")}</div>`).join("")}</div>
    ${extra.length ? `<div class="kx">${extra.map(([k, v]) => `<div><span class="cap">${esc(k)}</span> ${esc(v)}</div>`).join("")}</div>` : ""}`;
}
function flash(code) {
  const k = document.querySelector(`#ck-keys [data-code="${code}"]`);
  if (k) { k.classList.add("hit"); setTimeout(() => k.classList.remove("hit"), 160); }
}

// ---------------------------------------------------------------- key handling
document.addEventListener("keydown", (e) => {
  if (!T || $("#ck-work").hidden) return;
  const inField = e.target.closest("input, textarea");
  if (inField && !inField.dataset.scheme) return;    // an inline type box handles its own keys
  if (e.code === "Enter" && coachReady() && !inField) { e.preventDefault(); return coachNext(); }
  const handler = S.mode === "code" ? CODING : S.scheme;
  if (!inField && (e.ctrlKey || e.metaKey || e.altKey) && !handler.mods) return;  // leave the browser's shortcuts alone
  const done = handler.key(e.code, e);
  if (done) { e.preventDefault(); T.stats.keys++; T.lastActive = performance.now(); flash(e.code); }
});
document.addEventListener("mousedown", () => { if (T) T.lastActive = performance.now(); });

// clicks: words (play from there, double-click to type over; select when coding), choices, turns around this one,
// head buttons
$("#ck-main").addEventListener("click", (e) => {
  const b = e.target.closest("[data-go],[data-flag],[data-option],[data-code],[data-alt],[data-start],[data-tut],[data-coach],.ctx-turn.go");
  if (!b) return;
  const d = b.dataset;
  if (d.go) return go(S.idx + +d.go);
  if (d.id) return go(S.set.order.indexOf(d.id));
  if (d.flag) { T.flags[d.flag] = !T.flags[d.flag]; changed(); return; }
  if (d.option) return save(1, d.option);
  if (d.code && b.classList.contains("code")) { toggleCode(d.code); return; }
  if (d.alt) { const [i, k] = d.alt.split(":").map(Number); pick(i, T.words[i].alts[k]); return; }
  if (d.start) { useScheme(d.start); startWork(); return; }
  if (d.tut) { useScheme(d.tut); startTutorial(); return; }
  if (d.coach === "next") return coachNext();
  if (d.coach === "back") return coachBack();
  if (d.coach === "skip") return endTutorial(false);
});
// a second click on the same word within 450 ms is a double-click (counted here, because the first click redraws the
// turn and the browser would not see two clicks on one element)
let lastClick = { i: -1, t: 0 };
$("#ck-turn").addEventListener("click", (e) => {
  const w = e.target.closest("[data-i]");
  if (!w || S.mode !== "check") return;
  const i = +w.dataset.i, now = performance.now(), twice = lastClick.i === i && now - lastClick.t < 450;
  lastClick = twice ? { i: -1, t: 0 } : { i, t: now };
  if (twice && S.scheme.id !== "typist") return typeBox(i, lastOfSpan(i) + 1);
  if (!twice) S.scheme.click ? S.scheme.click(i) : playFrom(i);
});
// coding: drag across words to select them
let dragging = false;
$("#ck-turn").addEventListener("mousedown", (e) => {
  const w = e.target.closest("[data-o]"); if (!w || S.mode !== "code") return;
  e.preventDefault();
  const k = +w.dataset.o;
  if (e.shiftKey && T.sel) T.sel = [T.sel[0], k]; else T.sel = [k, k];
  T.cursor = k; dragging = true; render();
});
$("#ck-turn").addEventListener("mouseover", (e) => {
  const w = e.target.closest("[data-o]"); if (!dragging || !w) return;
  T.sel = [T.sel[0], +w.dataset.o]; T.cursor = +w.dataset.o; render();
});
document.addEventListener("mouseup", () => { if (dragging) { dragging = false; emit("select"); } });

// an inline box to type over words i0..i1-1 (Enter keeps it, Esc leaves it as it was)
function typeBox(i0, i1, done = () => {}) {
  const span = document.querySelector(`#ck-turn [data-i="${i0}"]`);
  if (!span) return;
  const was = playing(); if (was) pause();
  const cur = []; for (let k = i0; k < i1; k++) if (T.out[k]) cur.push(T.out[k]);
  const box = document.createElement("input");
  box.className = "typebox"; box.value = cur.join(" "); box.setAttribute("aria-label", "Type what was said");
  box.style.width = `${Math.max(8, box.value.length + 4)}ch`;
  span.replaceWith(box); box.focus(); box.select();
  status("Type what was said, then Enter. Esc leaves it as it was. Leave it empty to delete.");
  box.addEventListener("input", () => { box.style.width = `${Math.max(8, box.value.length + 4)}ch`; });
  let closed = false;  // re-rendering the turn removes the box (and fires its blur), so each way out runs once
  box.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter" && !closed) { e.preventDefault(); closed = true; applyTyped(i0, i1, box.value); done(true); }
    if (e.key === "Escape" && !closed) { e.preventDefault(); closed = true; render(); done(false); }
  });
  box.addEventListener("blur", () => { if (!closed) { closed = true; render(); } });
}

// ---------------------------------------------------------------- coding mode (the same in every scheme)
const CODING = {
  keys() {
    const k = { Space: "play / pause the selection", KeyZ: "replay selection", KeyA: "← word (Shift: stretch)", KeyF: "word → (Shift: stretch)",
      KeyX: "remove codes", KeyG: "select all", KeyC: "undo", KeyS: "save, next turn", KeyD: "back to checking", Escape: "select nothing" };
    for (const c of S.codes) k[CODE_CODES[c.n - 1]] = c.name;
    return k;
  },
  key(code, e) {
    const n = OW().length;
    if (CODE_KEY[code] && S.codes.some((c) => c.key === CODE_KEY[code])) { toggleCode(CODE_KEY[code]); return true; }
    switch (code) {
      case "KeyA": case "KeyF": {
        const d = code === "KeyA" ? -1 : 1;
        T.cursor = Math.max(0, Math.min(n - 1, T.cursor + d));
        T.sel = e.shiftKey && T.sel ? [T.sel[0], T.cursor] : e.shiftKey ? [T.cursor - d, T.cursor] : [T.cursor, T.cursor];
        emit("select"); render(); return true;
      }
      case "Escape": T.sel = null; emit("clearsel"); render(); return true;
      case "KeyG": T.sel = [0, n - 1]; render(); return true;
      case "Space": if (playing()) pause(); else { const [a, b] = selRange(), ow = OW(); play(ow[a].s - 0.1, ow[b].e + 0.15); } return true;
      case "KeyZ": { const [a, b] = selRange(), ow = OW(); play(ow[a].s - 0.1, ow[b].e + 0.15); return true; }
      case "KeyX": clearCodes(); return true;
      case "KeyC": undo(); return true;
      case "KeyS": save(e.shiftKey ? -1 : 1); return true;
      case "KeyD": setMode("check"); return true;
    }
    return false;
  },
};
function setMode(m) {
  if (m === "code" && S.scheme.beforeSave) S.scheme.beforeSave();
  S.mode = m; T.sel = null; T.cursor = 0; pause();
  emit(m === "code" ? "codeMode" : "checkMode");
  status(m === "code" ? "Coding. Select words (or nothing, for the whole turn) and press a code's key." : "Checking.");
  render();
}

// ---------------------------------------------------------------- the five schemes
// Each scheme: id, name, pitch, good (who it suits), keys() -> {code: what it does}, key(code, e) -> handled,
// render(), and a tutorial (steps the coach walks through on a practice turn).
const doubts = () => T.words.map((w, i) => (w.alts.length ? i : -1)).filter((i) => i >= 0);
const choiceLabel = (a) => (a.t ? a.t : "(delete)") + (a.n > 1 ? `  (for ${a.n} words)` : "");
const choiceRow = (keys, i, title) => {
  const alts = choicesFor(i);
  return `<div class="crow"><span class="ctitle">${title}${i >= 0 && i < T.words.length ? ` <b>“${esc(T.words[i].w)}”</b>` : ""}</span>${keys.map((k, n) => {
    const a = alts[n];
    return `<button class="choice${a ? "" : " empty"}${a && isPicked(i, a) ? " picked" : ""}" ${a ? `data-alt="${i}:${n}"` : "disabled"}><span class="cap">${k}</span>${a ? esc(choiceLabel(a)) : ""}${a ? `<span class="p">${Math.round(a.p * 100)}%</span>` : ""}</button>`;
  }).join("")}</div>`;
};

const FOLLOW = {
  id: "follow", name: "Follow along", pitch: "The audio plays and a highlight follows the voice. Fix words as they go by, without stopping.",
  good: "you like listening straight through, and want your hands to stay put.",
  stopAtDoubts: false,
  behind() { for (let i = T.cursor - 1; i >= Math.max(0, T.cursor - 4); i--) if (choicesFor(i).length) return i; return -1; },
  here() { for (let i = T.cursor; i <= Math.min(T.words.length - 1, T.cursor + 1); i++) if (choicesFor(i).length) return i; return T.cursor; },
  keys() {
    return { Space: "play / pause", KeyZ: "start over", KeyA: "← word", KeyF: "word →", KeyG: "replay last words",
      Digit1: "fix the word just passed", Digit2: "", Digit3: "", Digit4: "", Digit5: `stop at doubts: ${this.stopAtDoubts ? "on" : "off"}`,
      KeyQ: "fix this word", KeyW: "", KeyE: "", KeyR: "", KeyT: "type over it", KeyX: "delete it", KeyC: "undo", KeyV: "can't make out",
      KeyB: `speed ${A.playbackRate}×`, KeyS: "save, next turn", KeyD: "go to coding" };
  },
  key(code, e) {
    const n = T.words.length, row1 = ["Digit1", "Digit2", "Digit3", "Digit4"], row2 = ["KeyQ", "KeyW", "KeyE", "KeyR"];
    if (row1.includes(code)) { const i = this.behind(); if (i < 0) status("No doubtful word just behind."); else pick(i, choicesFor(i)[row1.indexOf(code)]); return true; }
    if (row2.includes(code)) { const i = this.here(); pick(i, choicesFor(i)[row2.indexOf(code)]); return true; }
    switch (code) {
      case "Space": if (playing()) pause(); else play(T.words[T.cursor].s - 0.05); return true;
      case "KeyZ": T.cursor = 0; T.stoppedAt = new Set(); play(T.start); render(); return true;
      case "KeyA": case "KeyF": T.cursor = Math.max(0, Math.min(n - 1, T.cursor + (code === "KeyA" ? -1 : 1))); playWord(T.cursor); emit("move", { d: code === "KeyA" ? -1 : 1 }); render(); return true;
      case "KeyG": playSpan(Math.max(0, T.cursor - 3), T.cursor); T.stats.plays++; return true;
      case "Digit5": this.stopAtDoubts = !this.stopAtDoubts; emit("stopToggle"); status(this.stopAtDoubts ? "The audio now stops after each doubtful word. Space goes on." : "The audio plays straight through."); render(); return true;
      case "KeyT": typeBox(T.cursor, lastOfSpan(T.cursor) + 1); return true;
      case "KeyX": toggleDelete(T.cursor); return true;
      case "KeyC": undo(); return true;
      case "KeyV": toggleUnclear(T.cursor); return true;
      case "KeyB": { const r = [1, 1.25, 1.5, 0.75]; A.playbackRate = r[(r.indexOf(A.playbackRate) + 1) % r.length]; status(`Speed ${A.playbackRate}×`); render(); return true; }
      case "KeyS": save(e.shiftKey ? -1 : 1); return true;
      case "KeyD": setMode("code"); return true;
    }
    return false;
  },
  onWord(i) {
    if (T.cursor !== i) { T.cursor = i; this.render(); drawKeys(); }
    if (this.stopAtDoubts && choicesFor(i).length && !T.stoppedAt?.has(i)) {
      (T.stoppedAt ||= new Set()).add(i);
      stopAt = clipT(T.words[lastOfSpan(i)].e + 0.1);
    }
  },
  enter() { const t = T; if (S.item.has_clip && !T.restored) setTimeout(() => { if (T === t && S.coach == null && !playing()) play(T.start); }, 150); },
  click(i) { T.cursor = i; playFrom(i); render(); },
  render() {
    plainTurn((i) => ({ cls: i === T.cursor ? ["cursor"] : [] }));
    const b = this.behind(), h = this.here();
    $("#ck-panel").innerHTML = choiceRow(["1", "2", "3", "4"], b, "Just passed:") + choiceRow(["Q", "W", "E", "R"], h, "Here:")
      + `<p class="muted">T types over the highlighted word · X deletes it · V marks it hard to make out · click a word to play from it, double-click to type over it.</p>`;
  },
  tutorial: [
    { say: "This is one speaker's turn. Words with a <span class='demo doubt'>dotted underline</span> are the ones the computer was unsure of. Press <kbd>Space</kbd> to start the audio.", on: "play" },
    { say: "The highlight follows the voice. Press <kbd>Space</kbd> again to pause.", on: "pause" },
    { say: "Press <kbd>F</kbd> to step forward one word. Each step plays just that word.", on: "move", when: (d) => d.d > 0 },
    { say: "Press <kbd>A</kbd> to step back one word.", on: "move", when: (d) => d.d < 0 },
    { say: "Step (A, F) onto a word with a dotted underline. The bar below shows its other possible words on <kbd>Q</kbd> <kbd>W</kbd> <kbd>E</kbd> <kbd>R</kbd>. Press <kbd>Q</kbd> to swap in the first one.", on: "pick" },
    { say: "Changed your mind? <kbd>C</kbd> undoes. Try it.", on: "undo" },
    { say: "If no suggestion is right, press <kbd>T</kbd>, type the word, and press <kbd>Enter</kbd>.", on: "type" },
    { say: "Press <kbd>Space</kbd> to play on. While it plays, the top row (<kbd>1</kbd> to <kbd>4</kbd>) always holds the doubtful word you just passed, so you can fix it without stopping.", on: "play" },
    { say: "Press <kbd>5</kbd> to make the audio stop by itself after each doubtful word, then <kbd>Space</kbd> to go on. Press <kbd>5</kbd> again to turn that off.", on: "stopToggle" },
    { say: "Also: <kbd>X</kbd> deletes a word that was never said, <kbd>V</kbd> marks one you can't make out, <kbd>G</kbd> replays the last few words, <kbd>Z</kbd> starts the turn over, <kbd>B</kbd> changes the speed. Click a word to play from it, or the bar above the turn to play from that point; the bar keeps moving through pauses in the speech. The keyboard map at the bottom always shows what each key does.", next: true },
    { say: "When the words are right, press <kbd>D</kbd> to code the turn. (<kbd>S</kbd> saves without coding.)", on: "codeMode" },
  ],
};

const SLOTS = {
  id: "slots", name: "Key per word", pitch: "Every doubtful word gets its own key, in reading order, like the keyboard: 1 2 3 4 5, then Q W E R T, then A S D F G. Press a word's key to flip through what it might be.",
  good: "you read faster than you listen, and want one press per fix with no moving around.",
  SLOT: ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyA", "KeyS", "KeyD", "KeyF", "KeyG"],
  page: 0, typing: false, last: null,
  slots() { return doubts().slice(this.page * 15, this.page * 15 + 15); },
  keys() {
    const k = { Space: "play / pause (goes on from there)", KeyZ: "play from the start", KeyX: "undo", KeyC: "go to coding", KeyV: "save, next turn", KeyB: "previous turn",
      Backquote: "then a word's key: type over it", Tab: "next doubtful word (Shift: back)" };
    this.slots().forEach((i, n) => { k[this.SLOT[n]] = T.out[i] || "(deleted)"; });
    return k;
  },
  focus(i) {  // ring doubtful word i; the keys move on to the next 15 when it is past these
    this.last = i;
    const p = Math.floor(doubts().indexOf(i) / 15);
    if (p >= 0 && p !== this.page) { this.page = p; status(`The keys are now on doubtful words ${p * 15 + 1}–${Math.min(doubts().length, p * 15 + 15)} of ${doubts().length}.`); }
  },
  hop(dir) {  // to the next (or previous) doubtful word, playing from a second before it to just after it
    const d = doubts(); if (!d.length) return status("The computer was sure of every word in this turn.");
    const here = this.last ?? (A.currentTime > 0 ? wordAt(recT()) - (dir > 0 ? 1 : 0) : dir > 0 ? -1 : T.words.length);
    const past = A.currentTime > 0 && dir > 0 ? Math.max(here, wordAt(recT()) - 1) : here;
    const i = dir > 0 ? d.find((x) => x > past) : [...d].reverse().find((x) => x < here);
    if (i == null) return status(dir > 0 ? "That was the last doubtful word. V saves and goes to the next turn; C codes it first." : "This is the first doubtful word.");
    this.focus(i); emit("hop");
    play(leadIn(i), T.words[lastOfSpan(i)].e + 0.25);
    render();
  },
  key(code, e) {
    const n = this.SLOT.indexOf(code);
    if (n >= 0) {
      const i = this.slots()[n]; if (i == null) return true;
      if (this.typing) { this.typing = false; typeBox(i, lastOfSpan(i) + 1); return true; }
      cycle(i, e.shiftKey ? -1 : 1); playSpan(Math.max(0, i - 1), lastOfSpan(i) + 1); this.focus(i); this.render(); return true;
    }
    switch (code) {
      case "Space": if (playing()) pause(); else resume(); return true;
      case "KeyZ": play(T.start); return true;
      case "KeyX": undo(); return true;
      case "KeyC": setMode("code"); return true;
      case "KeyV": save(1); return true;
      case "KeyB": go(S.idx - 1); return true;
      case "Backquote": this.typing = true; status("Now press the key of the word to type over."); return true;
      case "Tab": this.hop(e.shiftKey ? -1 : 1); return true;
    }
    return false;
  },
  enter() { this.page = 0; this.last = null; },
  click(i) { if (T.words[i].alts.length) this.focus(i); playFrom(i); render(); },
  render() {
    const slot = new Map(this.slots().map((i, n) => [i, CAP[this.SLOT[n]]]));
    plainTurn((i) => ({ ...(slot.has(i) ? { key: slot.get(i) } : {}), cls: [...(slot.has(i) ? ["slot"] : []), ...(i === this.last ? ["focus"] : [])] }));
    const i = this.last;
    const opts = i != null ? [{ t: T.words[i].w, heard: true }, ...choicesFor(i)] : [];
    $("#ck-panel").innerHTML = (i != null ? `<div class="crow"><span class="ctitle">Key <span class="cap">${esc(slot.get(i) || "")}</span> flips through:</span>${opts.map((o) =>
      `<span class="choice ${(o.heard ? T.how[i] == null : isPicked(i, o)) ? "picked" : ""}">${esc(o.heard ? `${o.t} (as heard)` : choiceLabel(o))}</span>`).join("")}</div>` : `<p class="muted">Tab plays the next doubtful word, from two words before it. Press a word's key (the small letter above it) to flip it to the next thing it might be; Shift flips back.</p>`)
      + `<p class="muted">${doubts().length} doubtful word${doubts().length === 1 ? "" : "s"} in this turn. Click a word to play from it; double-click to type over it.</p>`;
  },
  tutorial: [
    { say: "Every word the computer was unsure of has a small key letter above it, in reading order: the first doubtful word is <kbd>1</kbd>, the second <kbd>2</kbd>, and so on across the keyboard. Press <kbd>Space</kbd> to listen to the turn.", on: "play" },
    { say: "<kbd>Space</kbd> pauses, and <kbd>Space</kbd> again carries on from where it stopped. Now press <kbd>Tab</kbd>: it plays the next doubtful word, starting two words before it, and rings it. <kbd>Shift</kbd>+<kbd>Tab</kbd> goes back one.", on: "hop" },
    { say: "If the ringed word is wrong, press its key (the small letter above it). It flips to the computer's next guess and plays that bit of audio.", on: "pick" },
    { say: "Press the same key again to flip to the next guess. After the last guess it comes back to what was heard. <kbd>Shift</kbd> with the key flips backwards.", on: "pick" },
    { say: "<kbd>X</kbd> undoes the last change. Try it.", on: "undo" },
    { say: "To type a word yourself, press <kbd>`</kbd> (top left, under Esc) and then the word's key, or double-click any word. Type, then <kbd>Enter</kbd>.", on: "type" },
    { say: "Click any word to play from there. <kbd>Z</kbd> plays from the start, <kbd>B</kbd> goes back a turn. In a long turn the keys move on to the next 15 doubtful words by themselves as <kbd>Tab</kbd> reaches them.", next: true },
    { say: "When it reads right, press <kbd>C</kbd> to code the turn (<kbd>V</kbd> saves without coding).", on: "codeMode" },
  ],
};

const LATTICE = {
  id: "lattice", name: "Grid", pitch: "One line at a time. Under each doubtful word, its choices stand in a column, and each choice's key sits where it is on your keyboard: a key press is an exact pick.",
  good: "you like seeing every option at once and want no flipping or counting.",
  COLS: [["Digit1", "Digit2", "Digit3", "Digit4", "Digit5"], ["KeyQ", "KeyW", "KeyE", "KeyR", "KeyT"], ["KeyA", "KeyS", "KeyD", "KeyF", "KeyG"]],
  line: 0,
  lines() {
    if (T.lines) return T.lines;
    const out = []; let cur = [], d = 0;
    T.words.forEach((w, i) => {
      cur.push(i); if (w.alts.length) d++;
      if (d === 5 || cur.length >= 18) { out.push(cur); cur = []; d = 0; }
    });
    if (cur.length) out.push(cur);
    return (T.lines = out);
  },
  cols() { return (this.lines()[this.line] || []).filter((i) => T.words[i].alts.length).slice(0, 5); },
  keys() {
    const k = { Space: "play / pause line", KeyZ: "replay line", KeyX: "undo", KeyC: "go to coding", KeyV: "save, next turn", KeyB: "previous turn",
      Tab: "next line (Shift: back)", Backquote: "type over the line" };
    this.cols().forEach((i, c) => this.COLS.forEach((row, r) => { const a = T.words[i].alts[r]; if (a) k[row[c]] = a.t || "(delete)"; }));
    return k;
  },
  playLine() { const L = this.lines()[this.line]; if (L) playSpan(L[0], L[L.length - 1]); },
  hop(dir) {  // to the next (or previous) line with doubtful words, playing it from two words before the first of them
    const L = this.lines();
    let nx = this.line + dir;
    while (nx >= 0 && nx < L.length && !L[nx].some((i) => T.words[i].alts.length)) nx += dir;
    if (nx >= L.length) return status("That was the last line with doubtful words. V saves and goes to the next turn; C codes it first.");
    if (nx < 0) return status("This is the first line with doubtful words.");
    this.line = nx; emit("line"); render();
    const first = L[nx].find((i) => T.words[i].alts.length);
    play(leadIn(first), T.words[lastOfSpan(L[nx][L[nx].length - 1])].e + 0.15);
  },
  key(code, e) {
    for (let r = 0; r < 3; r++) {
      const c = this.COLS[r].indexOf(code);
      if (c >= 0) { const i = this.cols()[c]; if (i == null) return true; pick(i, T.words[i].alts[r]); playSpan(Math.max(0, i - 1), lastOfSpan(i) + 1); return true; }
    }
    const L = this.lines();
    switch (code) {
      case "Space": if (playing()) pause(); else this.playLine(); return true;
      case "KeyZ": this.playLine(); return true;
      case "Tab": this.hop(e.shiftKey ? -1 : 1); return true;
      case "KeyX": undo(); return true;
      case "KeyC": setMode("code"); return true;
      case "KeyV": save(1); return true;
      case "KeyB": go(S.idx - 1); return true;
      case "Backquote": { const l = L[this.line]; typeBox(l[0], l[l.length - 1] + 1); return true; }
    }
    return false;
  },
  enter() { this.line = 0; },
  click(i) { const l = this.lines().findIndex((L) => L.includes(i)); if (l !== this.line) { this.line = l; render(); } const L = this.lines()[l]; playSpan(i, L[L.length - 1]); },
  render() {
    const cur = new Set(this.lines()[this.line] || []), cols = this.cols(), colOf = new Map(cols.map((i, c) => [i, c]));
    plainTurn((i) => (colOf.has(i) ? { cls: ["inline", "slot", "colkey"], key: String(colOf.get(i) + 1) } : { cls: [cur.has(i) ? "inline" : "offline"] }));
    const heads = `<div class="lrow heads">${cols.map((i, c) => `<span class="lhead"><span class="cap">${c + 1}</span>${esc(T.out[i] || "(deleted)")}</span>`).join("")}</div>`;
    const grid = cols.length ? `<div class="lattice">${heads}${this.COLS.map((row, r) => `<div class="lrow r${r}">${cols.map((i, c) => {
      const a = T.words[i].alts[r];
      return `<button class="choice${a ? "" : " empty"}${a && isPicked(i, a) ? " picked" : ""}" ${a ? `data-alt="${i}:${r}"` : "disabled"}><span class="cap">${CAP[row[c]]}</span>${a ? esc(choiceLabel(a)) : ""}</button>`;
    }).join("")}</div>`).join("")}</div>`
      : `<p class="muted">No doubtful words on this line. Tab goes to the next.</p>`;
    $("#ck-panel").innerHTML = `<div class="lmeta">Line ${this.line + 1} of ${this.lines().length}</div>${grid}
      <p class="muted">Columns are this line's doubtful words, left to right; rows are their choices. Press a key again to put the word back as heard. Click a word to play from it; double-click to type over it.</p>`;
  },
  tutorial: [
    { say: "The turn is cut into lines; the current line is the bright one. Press <kbd>Space</kbd> to hear it.", on: "play" },
    { say: "Under the line is a grid. Each column is one doubtful word (its current text sits at the bottom). Each row is a choice, and each choice shows the key that picks it: the grid is laid out like the left side of your keyboard. Pick one.", on: "pick" },
    { say: "Pressing the same key again puts that word back as heard. <kbd>X</kbd> undoes.", on: ["pick", "undo"] },
    { say: "<kbd>Tab</kbd> goes to the next line with doubtful words and plays it from two words before the first of them; <kbd>Shift</kbd>+<kbd>Tab</kbd> goes back. <kbd>Space</kbd> plays the whole line.", on: "line" },
    { say: "To type, press <kbd>`</kbd> (top left) to type over the whole line, or double-click a word. <kbd>Enter</kbd> keeps it, <kbd>Esc</kbd> leaves it. (A single click plays from that word.)", on: "type" },
    { say: "When the turn reads right, press <kbd>C</kbd> to code it (<kbd>V</kbd> saves without coding).", on: "codeMode" },
  ],
};

const SPELL = {
  id: "spell", name: "One at a time", pitch: "Like a spelling checker: it jumps from one doubtful word to the next, plays it, and asks. Pick a number; it moves on by itself.",
  good: "you are new to this, or want the least to remember: everything you can press is written on the screen.",
  q: 0,
  queue() { return doubts(); },
  at() { return this.queue()[this.q]; },
  keys() {
    const i = this.at(), alts = i == null ? [] : choicesFor(i);
    const k = { Space: "hear it again", KeyA: "previous word", KeyF: "skip (keep)", KeyG: "play the whole turn", KeyC: "undo", KeyS: "save, next turn", KeyD: "go to coding",
      Digit1: i == null ? "" : `keep “${T.words[i].w}”`, KeyT: "type it", KeyX: "delete it", KeyV: "can't make out" };
    alts.slice(0, 4).forEach((a, n) => { k[`Digit${n + 2}`] = a.t || "(delete)"; });
    return k;
  },
  show(playIt = true) {
    const i = this.at();
    render();
    if (i != null && playIt) playSpan(Math.max(0, i - 1), Math.min(T.words.length - 1, lastOfSpan(i) + 1));
  },
  advance() { this.q++; if (this.at() == null) { status("That was the last doubtful word. S saves and opens the next turn; D codes it first; G plays it all."); render(); } else this.show(); },
  key(code) {
    const i = this.at();
    const m = /^Digit([1-5])$/.exec(code);
    if (m && i != null) {
      const n = +m[1];
      if (n === 1) { if (T.how[i] != null) { snapshot(); restoreSpan(i); changed(); } emit("pick", { keep: true }); this.advance(); return true; }
      const a = choicesFor(i)[n - 2]; if (!a) return true;
      if (!isPicked(i, a)) pick(i, a); this.advance(); return true;
    }
    switch (code) {
      case "Space": if (i != null) this.show(); else play(T.start); return true;
      case "KeyA": this.q = Math.max(0, this.q - 1); emit("move", { d: -1 }); this.show(); return true;
      case "KeyF": if (i != null) { emit("move", { d: 1 }); this.advance(); } return true;
      case "KeyG": play(T.start); return true;
      case "KeyT": if (i != null) typeBox(i, lastOfSpan(i) + 1, (ok) => { if (ok) this.advance(); }); return true;
      case "KeyX": if (i != null) { toggleDelete(i); this.advance(); } return true;
      case "KeyV": if (i != null) { toggleUnclear(i); this.advance(); } return true;
      case "KeyC": undo(); return true;
      case "KeyS": save(1); return true;
      case "KeyD": setMode("code"); return true;
    }
    return false;
  },
  enter() { this.q = 0; const t = T; if (this.at() != null && !T.restored) setTimeout(() => { if (T === t && S.coach == null && !playing()) this.show(); }, 150); },
  click(i) { const k = this.queue().indexOf(i); if (k >= 0) { this.q = k; this.show(); } else playFrom(i); },
  render() {
    const i = this.at();
    plainTurn((k) => ({ cls: k === i ? ["focus"] : [] }));
    if (i == null) {
      $("#ck-panel").innerHTML = `<div class="menu done"><p><b>${this.queue().length ? "All the doubtful words are done." : "The computer was sure of every word in this turn."}</b>
        Listen once more with <kbd>G</kbd> if you like, then <kbd>S</kbd> saves and opens the next turn, or <kbd>D</kbd> codes this one first. Double-click a word to fix anything else.</p></div>`;
      return;
    }
    const alts = choicesFor(i);
    $("#ck-panel").innerHTML = `<div class="menu"><div class="mq">Doubtful word ${this.q + 1} of ${this.queue().length}: what did they say?</div>
      <button class="choice${T.how[i] == null ? " picked" : ""}" data-spellkey="Digit1"><span class="cap">1</span>keep “${esc(T.words[i].w)}”</button>
      ${alts.slice(0, 4).map((a, n) => `<button class="choice${isPicked(i, a) ? " picked" : ""}" data-spellkey="Digit${n + 2}"><span class="cap">${n + 2}</span>${esc(choiceLabel(a))}<span class="p">${Math.round(a.p * 100)}%</span></button>`).join("")}
      <button class="choice" data-spellkey="KeyT"><span class="cap">T</span>type it</button>
      <button class="choice" data-spellkey="KeyX"><span class="cap">X</span>delete it</button>
      <button class="choice${T.unclear.has(i) ? " picked" : ""}" data-spellkey="KeyV"><span class="cap">V</span>can't make out</button></div>
      <p class="muted">Space hears it again · A goes back · F skips · the percentages are how likely the computer thought each one was.</p>`;
    $("#ck-panel").onclick = (e) => { const b = e.target.closest("[data-spellkey]"); if (b) this.key(b.dataset.spellkey); };
  },
  tutorial: [
    { say: "This way works one word at a time. The ringed word is one the computer was unsure of. The menu under the turn says what each key does. Press <kbd>Space</kbd> to hear the word in its place.", on: "play" },
    { say: "If the word is right, press <kbd>1</kbd> (keep). If one of the other words is what they said, press its number. Either way it moves on to the next word and plays it.", on: "pick" },
    { say: "Do another one.", on: "pick" },
    { say: "<kbd>A</kbd> goes back to the word before, <kbd>F</kbd> skips one. <kbd>C</kbd> undoes. Try <kbd>A</kbd>.", on: "move" },
    { say: "If none of them is right, press <kbd>T</kbd>, type the word, and press <kbd>Enter</kbd>. (If you can't make it out at all, <kbd>V</kbd> marks it and moves on.)", on: "type" },
    { say: "Words without a ring can be fixed too: double-click any word to type over it. A single click plays from that word, and <kbd>G</kbd> plays the whole turn.", next: true },
    { say: "When you are done, press <kbd>D</kbd> to code the turn (<kbd>S</kbd> saves without coding).", on: "codeMode" },
  ],
};

const TYPIST = {
  id: "typist", name: "Type it", pitch: "A plain text box with the transcript in it. Type freely; the audio pauses while you type, and Esc plays from where you are, as transcription software does.",
  good: "you type fast and want to fix anything, not only the doubtful words; it works like oTranscribe or Express Scribe.",
  mods: true, chosen: {},
  box() { return $("#ck-typist"); },
  keys() {
    return { Escape: "play / pause (goes back 1 s)", Tab: "word's next choice (Shift: back)", "Alt+A": "play from the cursor",
      "Alt+Z": "back 3 s", "Alt+C": "go to coding", "Ctrl+Enter": "save, next turn", "Ctrl+Z": "undo typing" };
  },
  caretIndex() {  // which typed word the text cursor is in (or just after)
    const before = this.box().value.slice(0, this.box().selectionStart);
    return Math.max(0, (before.match(/\S+/g) || []).length - (/\S$/.test(before) ? 1 : 0));
  },
  caretWord() {  // the original word under the text cursor, by lining up what is typed with the turn
    const map = this.mapping();
    return map[Math.max(0, Math.min(map.length - 1, this.caretIndex()))] ?? 0;
  },
  mapping() {  // typed word k -> original word index
    const typed = (this.box().value.match(/\S+/g) || []);
    const ow = outWords();
    const m = typed.length, n = ow.length;
    const D = Array.from({ length: m + 1 }, (_, a) => Array.from({ length: n + 1 }, (_, b) => (a === 0 ? b : b === 0 ? a : 0)));
    for (let a = 1; a <= m; a++) for (let b = 1; b <= n; b++) D[a][b] = Math.min(D[a - 1][b] + 1, D[a][b - 1] + 1, D[a - 1][b - 1] + (norm(typed[a - 1]) === norm(ow[b - 1].t) ? 0 : 1));
    const out = new Array(m).fill(null); let a = m, b = n;
    while (a > 0 && b > 0) {
      if (D[a][b] === D[a - 1][b - 1] + (norm(typed[a - 1]) === norm(ow[b - 1].t) ? 0 : 1)) { out[a - 1] = ow[b - 1].i; a--; b--; }
      else if (D[a][b] === D[a][b - 1] + 1) b--; else { out[a - 1] = ow[Math.max(0, b - 1)]?.i ?? 0; a--; }
    }
    for (let k = 0; k < m; k++) if (out[k] == null) out[k] = k > 0 ? out[k - 1] : 0;
    return out;
  },
  sync() {  // what is typed becomes the turn's words
    if (this.draft == null) return;
    const text = this.draft; this.draft = null;
    if (text !== outWords().map((w) => w.t).join(" ")) applyTyped(0, T.words.length, text, this.chosen);
  },
  beforeSave() { this.sync(); },
  key(code, e) {
    const b = this.box();
    if (code === "Escape") {
      if (playing()) { pause(); this.pausedAt = recT(); }
      else {
        const i = this.caretWord(), moved = this.caretAt !== b.selectionStart;
        play(moved || this.pausedAt == null ? T.words[i].s - 0.1 : Math.max(T.start, this.pausedAt - 1));
      }
      this.caretAt = b.selectionStart; return true;
    }
    if (code === "Tab") { this.cycleAtCaret(e.shiftKey ? -1 : 1); return true; }
    if (e.altKey && code === "KeyA") { play(T.words[this.caretWord()].s - 0.1); return true; }
    if (e.altKey && code === "KeyZ") { play(Math.max(T.start, recT() - 3)); return true; }
    if (e.altKey && code === "KeyC") { this.sync(); setMode("code"); return true; }
    if ((e.ctrlKey || e.metaKey) && code === "Enter") { this.sync(); save(1); return true; }
    return false;
  },
  cycleAtCaret(dir) {
    const b = this.box(), i = this.caretWord(), alts = T.words[i].alts.filter((a) => a.t);  // deleting is just typing here
    if (!alts.length) { status(`“${T.words[i].w}” has no other choices.`); return; }
    const words = [...b.value.matchAll(/\S+/g)];
    const k = Math.min(words.length - 1, this.caretIndex()); if (k < 0) return;
    const opts = [T.words[i].w, ...alts.map((a) => a.t)];
    const cur = Math.max(0, opts.findIndex((o) => norm(o) === norm(words[k][0])));
    const nxt = opts[(cur + dir + opts.length) % opts.length];
    const at = words[k].index, len = words[k][0].length;
    b.value = b.value.slice(0, at) + nxt + b.value.slice(at + len);
    this.draft = b.value;
    b.setSelectionRange(at, at + nxt.length);
    if (cur + dir !== 0) this.chosen[i] = nxt;
    T.stats.picks++; emit("pick", { i });
    playSpan(Math.max(0, i - 1), i + 1);
    this.hint();
  },
  hint() {
    const i = this.caretWord(), alts = (T.words[i]?.alts || []).filter((a) => a.t);
    $("#ck-suggest").innerHTML = alts.length ? `Tab: ${[T.words[i].w, ...alts.map((a) => a.t)].map(esc).join(" · ")}` : "&nbsp;";
    document.querySelectorAll(".ck-ruler .caret").forEach((e) => e.classList.remove("caret"));
    document.querySelector(`.ck-ruler [data-i="${i}"]`)?.classList.add("caret");
  },
  enter() { this.chosen = {}; this.draft = null; this.pausedAt = null; },
  render() {
    $("#ck-turn").innerHTML = `<div class="ck-ruler" title="As heard. Click a word to play from it.">${T.words.map((w, i) => `<span class="${wordClass(i).join(" ")}" data-i="${i}">${esc(w.w)}</span>`).join(" ")}</div>`;
    const old = this.box(), caret = old ? [old.selectionStart, old.selectionEnd] : null;
    const text = this.draft ?? outWords().map((w) => w.t).join(" ");
    $("#ck-panel").innerHTML = `<div id="ck-suggest" class="suggest">&nbsp;</div>
      <textarea id="ck-typist" data-scheme="typist" spellcheck="true" rows="7" aria-label="The turn's text">${esc(text)}</textarea>
      <p class="muted">The grey line above is what was heard; your text is below. Esc plays and pauses · Tab swaps the word at the cursor for the computer's other guesses · typing pauses the audio.</p>`;
    const b = this.box();
    if (caret) b.setSelectionRange(...caret);
    b.addEventListener("keydown", (e) => { if (["Escape", "Tab"].includes(e.code) || e.altKey || ((e.ctrlKey || e.metaKey) && e.code === "Enter")) { if (this.key(e.code, e)) { e.preventDefault(); e.stopPropagation(); T.stats.keys++; T.lastActive = performance.now(); } } else if (playing() && e.key.length === 1) { pause(); this.pausedAt = recT(); } });
    b.addEventListener("keyup", () => this.hint());
    b.addEventListener("click", () => this.hint());
    b.addEventListener("input", () => { this.draft = b.value; T.lastActive = performance.now(); emit("typing"); });
    if (S.mode === "check" && (old ? document.activeElement === old || caret : true)) setTimeout(() => b.focus(), 0);
  },
  click(i) { play(T.words[i].s - 0.1); },
  tutorial: [
    { say: "This way is a text box. The grey line on top is what the computer heard; the box below is yours to edit. Click in the box, then press <kbd>Esc</kbd> to play.", on: "play" },
    { say: "Press <kbd>Esc</kbd> again to pause. Playing again goes back one second, so you don't miss anything.", on: "pause" },
    { say: "Start typing anywhere in the box: the audio pauses by itself. Fix a word.", on: "typing" },
    { say: "Put the text cursor on a doubtful word (dotted underline in the grey line) and press <kbd>Tab</kbd>: it swaps in the computer's next guess. <kbd>Shift</kbd>+<kbd>Tab</kbd> goes back.", on: "pick" },
    { say: "<kbd>Alt</kbd>+<kbd>A</kbd> plays from the word at your cursor: try it. (<kbd>Alt</kbd>+<kbd>Z</kbd> jumps back 3 seconds, and <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes typing as usual.)", on: "play" },
    { say: "When it reads right, press <kbd>Alt</kbd>+<kbd>C</kbd> to code the turn (<kbd>Ctrl</kbd>+<kbd>Enter</kbd> saves without coding).", on: "codeMode" },
  ],
};

const CODING_TUTORIAL = [
  { say: "Coding: drag the mouse across some words to select them (or use <kbd>A</kbd> and <kbd>F</kbd>, holding <kbd>Shift</kbd> to stretch). Then press <kbd>1</kbd>.", on: "code" },
  { say: "Codes stack: press <kbd>2</kbd> to add a second code to the same words. Each code has its own color and line style, and its key appears where it starts. Press a key again to take that code off.", on: "code" },
  { say: "With nothing selected, a code key codes the whole turn. Press <kbd>Esc</kbd> to select nothing.", on: "clearsel" },
  { say: "<kbd>D</kbd> goes back to checking; <kbd>S</kbd> saves the turn and opens the next. In this practice nothing is saved: press <kbd>S</kbd> to finish.", on: "save" },
];
const SCHEMES = [FOLLOW, SLOTS, LATTICE, SPELL, TYPIST];

function useScheme(id) {
  const sch = SCHEMES.find((x) => x.id === id) || FOLLOW;
  if (T && S.scheme && S.scheme !== sch) { S.scheme.beforeSave?.(); T.schemes.add(S.scheme.id); }
  if (T) pause();
  S.scheme = sch; LS.set("ck:scheme", sch.id);
  document.querySelectorAll("#ck-schemes button").forEach((b) => b.classList.toggle("active", b.dataset.scheme === sch.id));
  if (T) { S.mode = "check"; sch.enter?.(); render(); }
}
$("#ck-schemes").innerHTML = SCHEMES.map((x, n) => `<button data-scheme="${x.id}" title="${esc(x.pitch)}">${n + 1}. ${esc(x.name)}</button>`).join("");
$("#ck-schemes").addEventListener("click", (e) => { const b = e.target.closest("[data-scheme]"); if (b) { useScheme(b.dataset.scheme); if (!$("#ck-start").hidden) startWork(); } });

// ---------------------------------------------------------------- the start screen and the tutorials
function startScreen() {
  $("#ck-work").hidden = true; $("#ck-start").hidden = false; $("#ck-keys").innerHTML = "";
  const n = S.set ? Object.keys(S.set.answers).length : 0;
  $("#ck-start").innerHTML = `<h2>Checking a transcript, by ear</h2>
    <p class="lede">You will hear one speaker at a time and see what the computer thinks they said. Your job is to fix what it got wrong, then (if you like) mark
      stretches of it with codes. Words the computer was unsure of have a <span class="demo doubt">dotted underline</span>, and for those it offers its other guesses,
      so most fixes are one key press. Everything works from the left side of the keyboard, so your right hand can stay on the mouse or rest.</p>
    <p>There are five ways of working. They do the same job; try each one and keep the one that suits you. Each has a short tutorial on a practice turn (nothing in a tutorial is saved).
      ${S.set ? `This set has ${S.set.order.length} turns; ${n} done.` : ""}</p>
    <div class="cards">${SCHEMES.map((x, k) => `<div class="card"><h3>${k + 1}. ${esc(x.name)}</h3><p>${esc(x.pitch)}</p><p class="muted">Good if ${esc(x.good)}</p>
      <div class="actions"><button data-tut="${x.id}">Tutorial (2 min)</button><button class="secondary" data-start="${x.id}">Start checking</button></div></div>`).join("")}</div>
    <p class="muted">Every way saves the same thing: the corrected turn, which fixes came from the computer's guesses and which you typed, your codes, and how long it took,
      so the ways can be compared. The bar at the bottom always shows what each key does; keys light up as you press them. The time on each turn counts only while you are
      working (a page left idle does not count).</p>`;
}
// Who is coding: asked once per browser, shown in the header, sent with every answer. Two coders on one set never
// overwrite each other; each sees and continues only their own answers.
function askWho() {
  if (T) pause();
  $("#ck-work").hidden = true; $("#ck-start").hidden = false; $("#ck-keys").innerHTML = "";
  $("#ck-start").innerHTML = `<h2>Who is checking?</h2>
    <p class="lede">Your name (or initials) is saved with each turn you check, so two people can check the same set without replacing each other's work.
      This browser remembers it; change it any time with the Coder button at the top. Answers given before names were asked for are under the name "me": type me to carry on with those.</p>
    <form id="ck-who-form" class="row" style="display:flex;gap:8px;align-items:end"><label>Name <input id="ck-who-name" value="${esc(LS.get("reviewer") || "")}" autocomplete="off" required></label>
      <button>Continue</button></form>`;
  $("#ck-who-name").focus();
  $("#ck-who-form").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#ck-who-name").value.trim(); if (!v) return; LS.set("reviewer", v); S.set = null; T = null; boot(); });
}
$("#ck-who").addEventListener("click", askWho);
function startWork() { $("#ck-start").hidden = true; $("#ck-work").hidden = false; if (!T) return load(); S.scheme.enter?.(); render(); }
$("#ck-help").addEventListener("click", () => { pause(); startScreen(); });
$("#ck-tutorial").addEventListener("click", () => startTutorial());

async function startTutorial() {
  // practice on the first turn with at least three doubtful words (or the current one)
  let pickId = null;
  for (const id of S.set.order.slice(0, 25)) {
    const it = await api(`/api/reviews/${encodeURIComponent(S.set.id)}/items/${encodeURIComponent(id)}?k=2`).catch(() => null);
    if (it && it.words && it.words.filter((w) => w.alts?.length).length >= 3 && it.has_clip) { pickId = id; break; }
  }
  S.practice = true;
  S.coach = { steps: [...S.scheme.tutorial, ...CODING_TUTORIAL], n: 0, done: new Set(), back: S.idx };
  $("#ck-banner").hidden = false;
  $("#ck-banner").innerHTML = `Practice: tutorial for <b>${esc(S.scheme.name)}</b>. Nothing you do here is saved. <button class="small secondary" data-coach="skip">End the tutorial</button>`;
  $("#ck-start").hidden = true; $("#ck-work").hidden = false;
  if (pickId) S.idx = S.set.order.indexOf(pickId);
  await load();
  drawCoach();
}
// a step stays on screen when its key is pressed (marked done) until Next, so it can be read to the end; Back rereads
function drawCoach() {
  const c = S.coach; if (!c) return;
  const st = c.steps[c.n];
  if (!st) return endTutorial(true);
  const ready = coachReady(), done = c.done.has(c.n);
  $("#ck-coach").hidden = false;
  $("#ck-coach").innerHTML = `<div class="step">Step ${c.n + 1} of ${c.steps.length}${done ? ` · <span class="done">✓ done</span>` : ""}</div><div class="say">${st.say}</div>
    <div class="actions"><button class="small secondary" data-coach="back"${c.n ? "" : " disabled"}>◀ Back</button>
      <button class="small${ready ? "" : " secondary"}" data-coach="next">${c.n === c.steps.length - 1 ? "Finish" : "Next"} ▶${ready ? " (Enter)" : ""}</button>
      <button class="small secondary" data-coach="skip">End the tutorial</button></div>`;
}
function coachReady() { const c = S.coach; return !!c && !!(c.steps[c.n]?.next || c.done.has(c.n)); }
function coachNext() { if (!S.coach) return; S.coach.n++; drawCoach(); }
function coachBack() { if (!S.coach?.n) return; S.coach.n--; drawCoach(); }
function emit(name, data = {}) {
  const c = S.coach; if (!c) return;
  const st = c.steps[c.n]; if (!st || st.next || c.done.has(c.n)) return;
  const on = [].concat(st.on);
  if (on.includes(name) && (!st.when || st.when(data))) { c.done.add(c.n); drawCoach(); }
}
async function endTutorial(finished) {
  const back = S.coach?.back ?? S.idx;
  S.coach = null; S.practice = false; pause();
  $("#ck-coach").hidden = true; $("#ck-banner").hidden = true;
  S.idx = back; await load();
  status(finished ? `Tutorial done. This is for real now: your work on each turn is saved when you save it.` : "Tutorial ended.");
}

// ---------------------------------------------------------------- loading a set and a turn
async function load() {
  const id = S.set.order[S.idx];
  const from = T && S.item && !$("#ck-work").hidden ? { id: S.item.id, top: $("#ck-who").getBoundingClientRect().top } : null;
  pause(); S.mode = "check";
  S.item = S.next?.id === id ? S.next : await api(`/api/reviews/${encodeURIComponent(S.set.id)}/items/${encodeURIComponent(id)}?k=3`);
  S.next = null;
  openTurn(S.item);
  A.src = S.item.has_clip ? clipUrl(S.item.id) : "";
  if (!S.practice) history.replaceState(null, "", `#${encodeURIComponent(S.set.id)}/${encodeURIComponent(id)}`);
  LS.set(`ck:at:${S.set.id}`, id);
  drawContext();
  S.scheme.enter?.();
  render();
  placeReel(from);
  status(T.restored ? "Your earlier work on this turn is back." : "");
  const nid = S.set.order[S.idx + 1];  // fetch the next turn and cut its audio ahead of time
  if (nid) api(`/api/reviews/${encodeURIComponent(S.set.id)}/items/${encodeURIComponent(nid)}?k=3`).then((it) => { S.next = it; if (it.has_clip) fetch(clipUrl(nid)).catch(() => {}); }).catch(() => {});
}
async function boot() {
  const id = setId();
  if (!id) { $("#ck-start").hidden = false; $("#ck-start").innerHTML = `<p>Open a checking set from the <a href="/#review">Review tab</a>, or make one: <code>ontherecord review new RUN --kind check</code> (the run needs the word_choices stage).</p>`; return; }
  if (LS.get("reviewer") == null) return askWho();
  $("#ck-who").textContent = `Coder: ${who()}`;
  try { S.set = await api(`/api/reviews/${encodeURIComponent(id)}?by=${encodeURIComponent(who())}`); }
  catch (e) { $("#ck-start").hidden = false; $("#ck-start").innerHTML = `<p>No set ${esc(id)}: ${esc(e.message)}</p>`; return; }
  if (S.set.kind !== "check") { location.href = `/#review/${encodeURIComponent(id)}`; return; }
  S.set.answers ||= {};
  S.codes = (S.set.answer.codes || []).map((c) => ({ ...c, n: CODE_CODES.indexOf({ 1: "Digit1", 2: "Digit2", 3: "Digit3", 4: "Digit4", 5: "Digit5" }[c.key] || `Key${c.key}`) + 1 }));
  const want = decodeURIComponent((location.hash.slice(1).split("/")[1]) || "") || LS.get(`ck:at:${id}`);
  const at = S.set.order.indexOf(want);
  S.idx = at >= 0 ? at : Math.max(0, S.set.order.findIndex((x) => !S.set.answers[x]));
  useScheme(LS.get("ck:scheme") || "follow");
  if (!LS.get("ck:seen")) { LS.set("ck:seen", "1"); startScreen(); return; }
  $("#ck-work").hidden = false;
  await load();
}
boot();
