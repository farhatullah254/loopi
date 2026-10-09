const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
};
const api = async (path, method = "GET", body) => {
  const r = await fetch("/api" + path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const ts = s => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtDate = t => new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const fmtSize = b => b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " KB";
const toast = (m, ms = 2500) => { let t = $(".toast") || document.body.appendChild(el("div", { class: "toast" })); t.textContent = m; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms); };
const readFile = f => new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });

const STAGES = ["new", "fetched", "cleaned", "extracted", "ready"];
const STAGE_LABELS = { new: "Fetch", fetched: "Clean", cleaned: "Extract", extracted: "Frames", ready: "Ready" };
const TIERS = ["none", "anchor", "sequence", "walkthrough"];
let current = null, project = null, poll = null, transcript = null, vid = null;

// ================================================================ router
function route() {
  const h = location.hash || "#/";
  clearInterval(poll); poll = null; vid = null;
  document.querySelectorAll("#nav nav a").forEach(a => a.classList.remove("active"));
  let m;
  if (h === "#/" || h === "#") { $('[data-route="library"]').classList.add("active"); return libraryPage(); }
  if (h === "#/new") { $('[data-route="new"]').classList.add("active"); return newPage(); }
  if (h === "#/settings") { $('[data-route="settings"]').classList.add("active"); return settingsPage(); }
  if ((m = h.match(/^#\/p\/(\d+)/))) { $('[data-route="library"]').classList.add("active"); return openProject(+m[1]); }
  location.hash = "#/";
}
window.addEventListener("hashchange", route);

async function navState() {
  try {
    const s = await api("/settings");
    const eng = s.engine === "claude-code" ? (s.claude_token ? "Claude Code · token" : (s.claude_cli ? "Claude Code · CLI login" : "Claude Code · CLI missing")) : (s.api_key_present ? "API key" : "API key missing");
    $("#navstate").textContent = eng;
  } catch { $("#navstate").textContent = ""; }
}

// ================================================================ library
const ACTIVE = ["running", "queued", "waiting"];
const tileSig = p => JSON.stringify([p.status, p.stage, p.progress, p.error, p.pdf_count, p.pdf_path, p.pattern_title, p.title, p.thumb_file, p.skill_level]);
let libState = null;

async function libraryPage() {
  const m = $("#main"); m.innerHTML = "";
  const q = el("input", { type: "search", placeholder: "Search titles and channels" });
  const grid = el("div", { class: "grid" });
  libState = { q, grid, projects: [], tiles: new Map() };
  q.addEventListener("input", drawLibrary);
  m.append(el("h1", { class: "page" }, "Library"),
    el("div", { class: "toolbar" }, q, el("a", { href: "#/new" }, el("button", {}, "New pattern"))), grid);
  await updateLibrary();
}

async function updateLibrary() {
  if (!libState || (location.hash || "#/") !== "#/") return;
  const { projects } = await api("/projects");
  libState.projects = projects;
  drawLibrary();
  clearInterval(poll); poll = null;
  if (projects.some(p => ACTIVE.includes(p.status))) poll = setInterval(updateLibrary, 5000);
}

function drawLibrary() {
  const { q, grid, projects, tiles } = libState;
  const needle = q.value.trim().toLowerCase();
  const rows = projects.filter(p => !needle || `${p.pattern_title || ""} ${p.title || ""} ${p.channel || ""}`.toLowerCase().includes(needle));
  grid.querySelector(".empty")?.remove();
  const keep = new Set(rows.map(p => p.id));
  for (const [id, t] of tiles) if (!keep.has(id)) { t.node.remove(); tiles.delete(id); }
  let prev = null;
  for (const p of rows) {
    const sig = tileSig(p);
    let t = tiles.get(p.id);
    if (!t || t.sig !== sig) {
      const node = tile(p);
      if (t) t.node.replaceWith(node);
      t = { node, sig }; tiles.set(p.id, t);
    }
    const want = prev ? prev.nextSibling : grid.firstChild;
    if (want !== t.node) grid.insertBefore(t.node, want);
    prev = t.node;
  }
  if (!rows.length) grid.append(el("div", { class: "empty" }, projects.length ? "No matches." : "No patterns yet. Start one under New pattern."));
}

function tile(p) {
  const st = p.status === "error" ? "error" : (p.status === "running" || p.status === "queued" || p.status === "waiting") ? "running" : p.stage === "ready" ? "ready" : "";
  const thumb = p.thumb_file ? `/files/${p.id}/thumb/${p.thumb_file}` : (p.thumbnail || "");
  const go = () => location.hash = `#/p/${p.id}`;
  const t = el("div", { class: "tile" },
    el("div", { class: "thumb", onclick: go }, el("img", { src: thumb, loading: "lazy" }), p.duration ? el("span", { class: "dur" }, ts(p.duration)) : null),
    el("div", { class: "body" },
      el("div", { class: "t", onclick: go }, p.pattern_title || p.title || p.url),
      el("div", { class: "s" }, [p.channel, p.skill_level ? p.skill_level : null].filter(Boolean).join(" · ") || " "),
      el("div", { class: "s" }, el("a", { href: p.url, target: "_blank", rel: "noopener" }, "Original video ↗"), "  ", el("span", {}, fmtDate(p.created_at))),
      el("div", {},
        el("span", { class: "badge " + st }, st === "running" ? (p.progress || "working") : (p.status === "error" ? "error" : STAGE_LABELS[p.stage] || p.stage)),
        p.pdf_count ? el("span", { class: "badge pdf" }, `PDF ×${p.pdf_count}`) : null),
      el("div", { class: "acts" },
        el("button", { class: "small ghost", onclick: go }, "Open"),
        p.pdf_path ? el("a", { href: `/files/${p.id}/pdf/${p.pdf_path}`, target: "_blank" }, el("button", { class: "small ghost" }, "PDF")) : null,
        p.stage === "ready" ? el("button", { class: "small ghost", disabled: st === "running" ? "1" : null, onclick: async e => { e.target.disabled = true; toast("Rendering… (a few minutes if the quality review has not run yet)", 8000); try { const r = await api(`/projects/${p.id}/render`, "POST", {}); window.open(r.pdf, "_blank"); updateLibrary(); } catch (err) { toast(err.message, 5000); e.target.disabled = false; } } }, "Re-render") : null)));
  return t;
}

// ================================================================ new pattern
function newPage() {
  const m = $("#main"); m.innerHTML = "";
  const slots = { featured: null, final1: null, final2: null };
  const slot = (key, title, help) => {
    const prev = el("div", { class: "prev" }, "no image");
    const inp = el("input", { type: "file", accept: "image/*", onchange: async e => { const f = e.target.files[0]; if (!f) return; slots[key] = { name: f.name, b64: await readFile(f) }; prev.innerHTML = ""; prev.append(el("img", { src: slots[key].b64 })); } });
    return el("div", { class: "slot" }, el("b", {}, title), prev, el("div", { class: "muted", style: "font-size:11px;margin-bottom:6px" }, help), inp);
  };
  const url = el("input", { type: "url", placeholder: "https://www.youtube.com/watch?v=…", required: "1" });
  const density = el("select", {}, ...["concise", "standard", "beginner"].map(d => el("option", { value: d, selected: d === "standard" ? "1" : null }, d[0].toUpperCase() + d.slice(1) + " images")));
  const start = el("button", { type: "submit" }, "Start");
  const form = el("form", { onsubmit: async e => {
    e.preventDefault(); start.disabled = true;
    try {
      const images = { featured: slots.featured ? [slots.featured] : [], final: [slots.final1, slots.final2].filter(Boolean) };
      const r = await api("/projects", "POST", { url: url.value, density: density.value, images });
      if (r.existing) { toast("This video is already in the library. Opening it.", 4000); }
      location.hash = `#/p/${r.id}`;
    } catch (err) { toast(err.message, 5000); start.disabled = false; }
  } },
    el("div", { class: "card" },
      el("h2", {}, "Video"),
      el("p", { class: "help" }, "Paste the YouTube URL of the tutorial. Loopi downloads the video and its caption track, writes the pattern, and screenshots the steps."),
      el("div", { class: "form-row" }, url, density)),
    el("div", { class: "card" },
      el("h2", {}, "Your images (optional)"),
      el("p", { class: "help" }, "Featured image replaces the YouTube thumbnail on the cover. Final images replace the AI-chosen finished-object frames. Any slot left empty falls back to what Loopi finds in the video. You can also upload photos for individual steps later, inside the pattern."),
      el("div", { class: "slots" },
        slot("featured", "Featured image", "Cover of the PDF"),
        slot("final1", "Final image 1", "Finished object"),
        slot("final2", "Final image 2", "Finished object, second view"))),
    el("div", { class: "form-row" }, start, el("span", { class: "muted" }, "Runs in the background. You can watch progress in the Library or the pattern page.")));
  m.append(el("h1", { class: "page" }, "New pattern"), form);
}

// ================================================================ settings
async function settingsPage() {
  const m = $("#main"); m.innerHTML = "";
  const [s, b] = await Promise.all([api("/settings"), api("/brand")]);

  // brand
  const name = el("input", { value: b.name || "", placeholder: "e.g. Mahum Crochet" });
  const footer = el("input", { value: b.footer || "", placeholder: "Optional line at the end of every PDF" });
  const logoPrev = el("div", {}, b.logo ? el("img", { class: "logo-prev", src: `/brand/${b.logo}?t=${Date.now()}` }) : el("span", { class: "muted" }, "no logo"));
  const saveBrand = async (extra = {}) => { try { await api("/brand", "PATCH", { name: name.value, footer: footer.value, ...extra }); toast("Brand saved"); } catch (e) { toast(e.message, 5000); } };
  name.addEventListener("change", () => saveBrand()); footer.addEventListener("change", () => saveBrand());
  const brandCard = el("div", { class: "card" },
    el("h2", {}, "Brand"),
    el("p", { class: "help" }, "Printed on every PDF: the name on the cover and in the page footer, the logo on the cover. Nothing from the video's channel is printed."),
    el("label", { class: "field" }, "Pattern author / brand name", name),
    el("label", { class: "field" }, "Footer line (optional)", footer),
    el("div", { class: "field" }, "Logo (optional, PNG with transparency works best)",
      el("div", { class: "form-row" }, logoPrev,
        el("input", { type: "file", accept: "image/*", onchange: async e => { const f = e.target.files[0]; if (!f) return; await saveBrand({ logo_b64: await readFile(f), logo_name: f.name }); settingsPage(); } }),
        b.logo ? el("button", { class: "small danger", onclick: async () => { await saveBrand({ remove_logo: true }); settingsPage(); } }, "Remove logo") : null)));

  // engine
  const engine = el("select", {}, el("option", { value: "claude-code", selected: s.engine === "claude-code" ? "1" : null }, "Claude Code (your subscription)"), el("option", { value: "api", selected: s.engine === "api" ? "1" : null }, "Anthropic API key"));
  const models = {}, efforts = {};
  const dl = el("datalist", { id: "modellist" }, ...s.choices.map(c => el("option", { value: c })));
  const jobRow = (job, label, help) => {
    models[job] = el("input", { list: "modellist", value: s.models[job], spellcheck: "false" });
    efforts[job] = el("select", {}, ...s.effort_choices.map(e => el("option", { value: e, selected: e === s.efforts[job] ? "1" : null }, e)));
    return el("div", { class: "jobrow", title: help }, el("span", {}, label), models[job], efforts[job]);
  };
  const state = el("div", { class: "muted", style: "font-size:12px;margin-top:8px" }, s.engine === "claude-code" ? (s.claude_token ? "Using the token from LOOPI/.env" : (s.claude_cli ? "Using the claude CLI's own login" : "claude CLI not found on PATH")) : (s.api_key_present ? "ANTHROPIC_API_KEY present" : "ANTHROPIC_API_KEY missing"));
  const saveEngine = async () => {
    const body = { engine: engine.value };
    for (const j of ["clean", "extract", "pick"]) { body["model_" + j] = models[j].value; body["effort_" + j] = efforts[j].value; }
    try { await api("/settings", "PATCH", body); toast("Engine settings saved"); navState(); } catch (e) { toast(e.message, 5000); }
  };
  engine.addEventListener("change", async () => { await api("/settings", "PATCH", { engine: engine.value }); settingsPage(); navState(); });
  const engineCard = el("div", { class: "card" },
    el("h2", {}, "Claude engine"),
    el("p", { class: "help" }, "Model and effort per job. Defaults to Claude Opus 5.5, pinned by full ID; add [1m] for the 1M-token context."),
    el("label", { class: "field" }, "Engine", engine), dl,
    jobRow("clean", "Clean", "Merges captions into sentences"),
    jobRow("extract", "Extract", "Writes the pattern and decides images"),
    jobRow("pick", "Frames", "Judges the 8-frame bursts"),
    el("div", { class: "jobrow", title: "How many patterns process at the same time. Video downloads still happen one at a time to avoid YouTube rate limits." },
      el("span", {}, "Parallel"),
      el("select", { onchange: async e => { await api("/settings", "PATCH", { workers: +e.target.value }); toast(`Up to ${e.target.value} patterns at once`); } },
        ...[1, 2, 3, 4, 5, 6].map(n => el("option", { value: n, selected: n === s.workers ? "1" : null }, `${n} at a time`))),
      el("span", { class: "muted", style: "font-size:11px" }, "downloads queue")),
    el("label", { class: "field" }, "Claude token (from `claude setup-token` in Terminal)",
      el("div", { class: "form-row", style: "margin:3px 0 0" },
        (() => { const t = el("input", { type: "password", placeholder: "sk-ant-oat01-…  paste a new token here", autocomplete: "off", style: "flex:1" });
          const b = el("button", { class: "small", onclick: async () => { b.disabled = true; state.textContent = "Saving and testing the token… up to 75 s"; try { const r = await api("/settings/token", "POST", { token: t.value }); t.value = ""; state.textContent = (r.ok ? "Token saved, engine OK" : "Token saved but the test failed: ") + (r.ok ? "" : r.detail); navState(); } catch (err) { state.textContent = err.message; } b.disabled = false; } }, "Save token");
          return [t, b]; })())),
    el("div", { class: "form-row" },
      el("button", { class: "ghost small", onclick: async e => { e.target.disabled = true; state.textContent = "Testing… up to 75 s"; try { const r = await api("/settings/test", "POST", {}); state.textContent = (r.ok ? "Engine OK: " : "Engine FAILED: ") + r.detail; } catch (err) { state.textContent = "Test error: " + err.message; } e.target.disabled = false; } }, "Test engine"),
      el("span", { class: "muted", style: "font-size:12px" }, `Review log: ${s.review.tier_up} tier up, ${s.review.tier_down} down`)),
    state);
  for (const j of ["clean", "extract", "pick"]) { models[j].addEventListener("change", saveEngine); efforts[j].addEventListener("change", saveEngine); }

  // youtube sign-in
  const ya = await api("/ytauth");
  const yaState = el("div", { class: "muted", style: "font-size:12.5px;margin:6px 0" },
    ya.signed_in ? `Signed in, saved ${fmtDate(ya.saved_at)}. Used only when YouTube refuses a caption file as a guest.` : "Not signed in. Captions are fetched as a guest only.");
  const yaCall = async (path, msg) => { try { await api(`/ytauth/${path}`, "POST", {}); toast(msg, 5000); settingsPage(); } catch (e) { toast(e.message, 8000); } };
  const ytCard = el("div", { class: "card" },
    el("h2", {}, "YouTube sign-in (captions fallback)"),
    el("p", { class: "help" }, "For when YouTube blocks caption downloads. Opens a separate Chrome window with its own profile; your everyday Chrome is never touched. Sign in there yourself, quit that window with Cmd+Q, then press Save sign-in. macOS may ask once to allow access to “Chrome Safe Storage”; click Allow. The sign-in is used for one caption request per video, only after a guest request was refused. yt-dlp's documentation warns that using an account with it can get that account limited, so a spare Google account is the safest choice."),
    yaState,
    el("div", { class: "form-row" },
      el("button", { class: "ghost", disabled: ya.chrome ? null : "1", onclick: () => yaCall("open", "Sign-in window opened. Sign in, then quit it with Cmd+Q.") }, "Open sign-in window"),
      el("button", {}, "Save sign-in"),
      ya.signed_in ? el("button", { class: "danger small", onclick: () => { if (confirm("Remove the saved YouTube sign-in and its Chrome profile?")) yaCall("signout", "Signed out"); } }, "Sign out") : null));
  ytCard.querySelector(".form-row button:nth-child(2)").addEventListener("click", () => yaCall("save", "Sign-in saved"));

  // storage
  const su = await api("/storage");
  const gb = b => (b / 1e9).toFixed(1) + " GB";
  const cleanBtn = (what, label, n, size, warn) => el("button", { class: "ghost", disabled: n ? null : "1", onclick: async () => {
    if (!confirm(warn)) return;
    try { const r = await api("/storage/clean", "POST", { what }); toast(`Deleted ${r.deleted} files, freed ${gb(r.freed)}`, 5000); settingsPage(); } catch (e) { toast(e.message, 6000); }
  } }, `${label} (${n}, ${gb(size)})`);
  const storageCard = el("div", { class: "card" },
    el("h2", {}, `Storage · ${gb(su.disk_free)} free on this Mac`),
    el("p", { class: "help" }, `Loopi is using ${gb(su.videos + su.partials + su.frames + su.library)}: videos ${gb(su.videos)} (${su.videos_count}), unfinished downloads ${gb(su.partials)}, frames ${gb(su.frames)}, library PDFs ${gb(su.library)}. Downloads stop with a clear message when less than 5 GB is free.`),
    el("div", { class: "form-row" },
      cleanBtn("partials", "Delete unfinished downloads", su.partials_count, su.partials, "Delete the half-downloaded video files left by failed downloads? Nothing finished is affected."),
      cleanBtn("finished_videos", "Delete videos of finished patterns", su.finished_videos_count, su.finished_videos, "Delete the downloaded videos of patterns that are Ready and already have a PDF? Patterns, frames and PDFs are kept. If you later regrab a frame, Loopi downloads that video again automatically.")));

  // library folder
  const lib = await api("/library");
  const libCard = el("div", { class: "card" },
    el("h2", {}, `Library folder · ${lib.files.length} PDFs`),
    el("p", { class: "help" }, "Every render is copied to data/library/ so it outlives its project. This is the folder to back up."),
    lib.files.length ? el("table", { class: "versions" }, ...lib.files.slice(0, 30).map(f => el("tr", {}, el("td", {}, el("a", { href: `/library/${f.name}` }, f.name)), el("td", {}, fmtSize(f.size)), el("td", { class: "muted" }, fmtDate(f.at))))) : el("div", { class: "muted" }, "Nothing rendered yet."));

  m.append(el("h1", { class: "page" }, "Settings"), el("div", { class: "settings-grid" }, brandCard, engineCard), storageCard, ytCard, libCard);
}

// ================================================================ workspace
let headSig = null;
const projSig = p => JSON.stringify([p.status, p.stage, p.progress, p.error, p.pdf_path, p.density, p.title, p.thumb_file]);

async function openProject(id) {
  current = id; transcript = null; project = null; headSig = null;
  await refresh();
}

function schedulePoll() {
  clearInterval(poll); poll = null;
  if (project && ACTIVE.includes(project.status))
    poll = setInterval(() => refresh(true), project.status === "waiting" ? 30000 : 3000);
}

async function refresh(light = false) {
  let fresh;
  try { fresh = await api(`/projects/${current}`); } catch (e) { $("#main").innerHTML = `<div class="empty">${e.message}</div>`; return; }
  if (!location.hash.startsWith(`#/p/${current}`)) return;
  const prev = project;
  project = fresh;
  const intervalChanged = !prev || ACTIVE.includes(prev.status) !== ACTIVE.includes(fresh.status) || (prev.status === "waiting") !== (fresh.status === "waiting");
  if (light && prev && ACTIVE.includes(prev.status) && ACTIVE.includes(fresh.status) && prev.stage === fresh.stage) {
    const sig = projSig(fresh);
    if (sig !== headSig) { headSig = sig; $("#head")?.replaceWith(headEl()); }
  } else {
    headSig = projSig(fresh);
    const y = window.scrollY;
    renderWorkspace();
    if (prev) window.scrollTo(0, y);
  }
  if (intervalChanged) schedulePoll();
}

function headEl() {
  const p = project;
  const running = p.status === "running" || p.status === "queued";
  const st = p.status === "error" ? "error" : running ? "running" : p.stage === "ready" ? "ready" : "";
  const thumb = p.thumb_file ? `/files/${p.id}/thumb/${p.thumb_file}` : (p.thumbnail || "");
  const idx = STAGES.indexOf(p.stage);
  const stepper = el("div", { class: "stepper" }, ...STAGES.slice(0, 4).map((s, i) => el("span", { class: i < idx ? "done" : (i === idx && running ? "cur" : "") }, STAGE_LABELS[s])), el("span", { class: p.stage === "ready" ? "done" : "" }, "Ready"));
  return el("div", { id: "head" },
    el("div", { class: "head" },
      el("img", { src: thumb }),
      el("div", {},
        el("h1", {}, (p.pattern && p.pattern.title) || p.title || p.url),
        el("div", { class: "sub" }, [p.title && p.pattern ? p.title : null, p.channel, p.duration ? ts(p.duration) : null].filter(Boolean).join(" · "), " ", el("a", { href: p.url, target: "_blank", rel: "noopener" }, "original video ↗")),
        stepper,
        el("div", {},
          el("span", { class: "badge " + st }, p.status === "idle" ? STAGE_LABELS[p.stage] : p.status),
          el("span", { class: "badge" }, p.density + " images"),
          p.pattern && p.pattern.terminology_detected === "UK" ? el("span", { class: "badge" }, "UK terms converted to US") : null))),
    running ? el("div", { class: "progress" }, "⏳ " + (p.progress || "Working")) : null,
    p.status === "waiting" ? el("div", { class: "progress" }, "⏸ " + (p.progress || "Waiting to retry"), " ", el("button", { class: "small ghost", onclick: () => rerun("new") }, "Try now")) : null,
    p.error ? el("div", { class: "errbox" }, p.error) : null,
    el("div", { class: "actions" },
      el("button", { class: "ghost", disabled: running ? "1" : null, onclick: () => rerun("new") }, "Re-fetch video"),
      el("button", { class: "ghost", disabled: running || idx < 1 ? "1" : null, onclick: () => rerun("fetched") }, "Re-clean transcript"),
      el("button", { class: "ghost", disabled: running || idx < 2 ? "1" : null, onclick: () => rerun("cleaned") }, "Re-extract pattern"),
      el("button", { class: "ghost", disabled: running || idx < 3 ? "1" : null, onclick: () => rerun("extracted") }, "Re-grab all frames"),
      el("select", { onchange: async e => { await api(`/projects/${p.id}/density`, "PATCH", { density: e.target.value }); toast("Density saved. Re-extract to apply."); } },
        ...["concise", "standard", "beginner"].map(d => el("option", { value: d, selected: d === p.density ? "1" : null }, d))),
      el("span", { style: "flex:1" }),
      el("button", { disabled: !p.pattern || running ? "1" : null, onclick: renderPdf }, "Render PDF"),
      p.pdf_path ? el("a", { href: `/files/${p.id}/pdf/${p.pdf_path}`, target: "_blank" }, el("button", { class: "ghost" }, "Open latest PDF")) : null,
      el("button", { class: "danger", onclick: del }, "Delete")));
}

async function rerun(from) {
  try { await api(`/projects/${project.id}/run`, "POST", { from, target: "ready" }); toast("Queued"); await refresh(); } catch (e) { toast(e.message, 5000); }
}
async function renderPdf() {
  toast(project.pattern?.review ? "Rendering PDF…" : "Reviewing the pattern, then rendering… a few minutes", project.pattern?.review ? 8000 : 600000);
  try { const r = await api(`/projects/${project.id}/render`, "POST", {}); toast(`PDF ready: ${r.pages} pages, ${fmtSize(r.size)}`, 4000); window.open(r.pdf, "_blank"); await refresh(); } catch (e) { toast(e.message, 6000); }
}
async function del() {
  if (!confirm("Delete this project (video, frames, review data)? PDFs already rendered stay in the library folder.")) return;
  try { await api(`/projects/${project.id}`, "DELETE"); location.hash = "#/"; } catch (e) { toast(e.message, 5000); }
}

function renderWorkspace() {
  const m = $("#main"); m.innerHTML = "";
  m.append(headEl());
  const p = project;
  m.append(imagesCard());
  if (STAGES.indexOf(p.stage) >= 1) m.append(videoCard());
  if (p.pattern) {
    m.append(validationCard(), frontCard());
    for (const sec of p.pattern.sections) m.append(sectionCard(sec));
    m.append(finalCard());
  }
  if (STAGES.indexOf(p.stage) >= 1) m.append(transcriptCard());
  m.append(versionsCard());
}

// ---- your images (featured + finals)
function imagesCard() {
  const up = project.uploads || { featured: [], final: [] };
  const slot = (kind, idx, title, help) => {
    const name = (up[kind] || [])[idx];
    const prev = el("div", { class: "prev" }, name ? el("img", { src: `/files/${project.id}/uploads/${name}` }) : (kind === "featured" && project.thumb_file ? el("img", { src: `/files/${project.id}/thumb/${project.thumb_file}`, style: "opacity:.55" }) : "empty"));
    return el("div", { class: "slot" }, el("b", {}, title), prev, el("div", { class: "muted", style: "font-size:11px;margin-bottom:6px" }, name ? name.replace(/^\d+_/, "") : help),
      el("div", { class: "form-row", style: "justify-content:center;margin:0" },
        el("input", { type: "file", accept: "image/*", onchange: async e => { const f = e.target.files[0]; if (!f) return; try { await api(`/projects/${project.id}/upload`, "POST", { kind, name: f.name, b64: await readFile(f) }); toast("Uploaded"); await refresh(); } catch (err) { toast(err.message, 5000); } } }),
        name ? el("button", { class: "small danger", onclick: async () => { await api(`/projects/${project.id}/upload/${kind}/${name}`, "DELETE"); await refresh(); } }, "Remove") : null));
  };
  const keep = el("input", { type: "checkbox", checked: up.keep_ai_finals ? "1" : null, onchange: async e => { await api(`/projects/${project.id}/uploads`, "PATCH", { keep_ai_finals: e.target.checked }); toast("Saved"); } });
  return el("div", { class: "card" },
    el("h2", {}, "Your images"),
    el("p", { class: "help" }, "Featured image is the PDF cover; without one the YouTube thumbnail is used. Final images replace the AI-chosen finished-object frames in the Finishing section."),
    el("div", { class: "slots" },
      slot("featured", 0, "Featured image", "Falls back to the thumbnail shown faintly here"),
      slot("final", 0, "Final image 1", "Finished object"),
      slot("final", 1, "Final image 2", "Second view")),
    el("label", { class: "field", style: "margin-top:10px" }, keep, " Also keep the AI-chosen final frames in the PDF when I have uploaded my own"));
}

// ---- video scrubber
function videoCard() {
  const p = project;
  vid = el("video", { controls: "1", preload: "metadata", src: `/files/${p.id}/video/video.mp4` });
  const cur = el("code", {}, "0:00");
  vid.addEventListener("timeupdate", () => cur.textContent = ts(vid.currentTime) + ` (${vid.currentTime.toFixed(1)}s)`);
  return el("div", { class: "card" },
    el("h2", {}, "Video"),
    vid,
    el("div", { class: "vbar" }, "Playhead ", cur,
      el("span", { class: "muted" }, "scrub to a moment, then use “Playhead” on any image, or click a transcript line."),
      el("button", { class: "small ghost", onclick: () => { if (vid) vid.currentTime = Math.max(0, vid.currentTime - 1); } }, "−1s"),
      el("button", { class: "small ghost", onclick: () => { if (vid) vid.currentTime = vid.currentTime + 1; } }, "+1s")));
}
const seek = t => { if (vid) { vid.currentTime = t; vid.pause(); vid.scrollIntoView({ behavior: "smooth", block: "center" }); } };

// ---- validation
function validationCard() {
  const v = project.pattern.validation || { issues: [] };
  return el("div", { class: "card", id: "validation" },
    el("h2", {}, `Checks · ${v.errors || 0} errors, ${v.warnings || 0} warnings `,
      el("button", { class: "small ghost", title: "One Claude pass that removes video references and writes out skipped routine steps; keeps every frame", onclick: async e => { e.target.disabled = true; toast("Rewording… about a minute", 60000); try { const r = await api(`/projects/${project.id}/polish`, "POST", {}); toast(`Reworded: ${r.steps_changed} steps changed`, 4000); await refresh(); } catch (err) { toast(err.message, 6000); e.target.disabled = false; } } }, "Fix wording")),
    reviewBlock(project.pattern.review),
    v.issues.length ? el("ul", { class: "issues" }, ...v.issues.map(i => el("li", { class: i.level, onclick: () => { const s = i.step_id && document.getElementById("step-" + i.step_id); if (s) s.scrollIntoView({ behavior: "smooth", block: "center" }); } }, i.msg))) : el("div", { class: "muted" }, "Stitch math and abbreviations look consistent."));
}

function reviewBlock(r) {
  if (!r) return el("div", { class: "muted review-none" }, "Not reviewed yet. The quality review runs automatically before the PDF is rendered.");
  const when = new Date(r.at * 1000).toLocaleString();
  return el("details", { class: "review", open: r.unresolved?.length ? "1" : null },
    el("summary", {}, `Quality review · ${r.fixes.length} fixes in ${r.passes} pass${r.passes > 1 ? "es" : ""} · ${when}` + (r.unresolved?.length ? ` · ${r.unresolved.length} still flagged` : "")),
    el("ul", { class: "fixes" }, ...r.fixes.map(f => el("li", {}, el("b", {}, f.where + ": "), f.problem, el("span", { class: "muted" }, " → " + f.fix)))));
}

// ---- front matter
const lines = arr => (arr || []).join("\n");
const pairs = (arr, a, b) => (arr || []).map(x => `${x[a] || ""} — ${x[b] || ""}`).join("\n");
const unpairs = (txt, a, b) => txt.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const [k, ...r] = l.split(/\s+[—-]\s+/); return { [a]: k.trim(), [b]: r.join(" - ").trim() }; });

function frontCard() {
  const p = project.pattern;
  const save = async (meta) => { try { const r = await api(`/projects/${project.id}/pattern`, "PATCH", { meta }); Object.assign(p, meta); p.validation = r.validation; toast("Saved"); } catch (e) { toast(e.message, 5000); } };
  return el("div", { class: "card" },
    el("h2", {}, "Pattern front matter"),
    el("div", { class: "grid2" },
      el("label", {}, "Title (also the PDF filename)", el("input", { value: p.title || "", onchange: e => save({ title: e.target.value }) })),
      el("label", {}, "Skill level", el("input", { value: p.skill_level || "", onchange: e => save({ skill_level: e.target.value }) })),
      el("label", { style: "grid-column:1/3" }, "Summary", el("textarea", { onchange: e => save({ summary: e.target.value }) }, p.summary || "")),
      el("label", {}, "Materials (one per line: item — detail)", el("textarea", { rows: 6, onchange: e => save({ materials: unpairs(e.target.value, "item", "detail") }) }, pairs(p.materials, "item", "detail"))),
      el("label", {}, "Abbreviations (abbr — meaning)", el("textarea", { rows: 6, onchange: e => save({ abbreviations: unpairs(e.target.value, "abbr", "meaning") }) }, pairs(p.abbreviations, "abbr", "meaning"))),
      el("label", {}, "Notes (one per line)", el("textarea", { rows: 4, onchange: e => save({ notes: e.target.value.split("\n").filter(x => x.trim()) }) }, lines(p.notes))),
      el("label", {}, "Finishing (one per line)", el("textarea", { rows: 4, onchange: e => save({ finishing: e.target.value.split("\n").filter(x => x.trim()) }) }, lines(p.finishing))),
      el("label", {}, "Gauge", el("input", { value: p.gauge || "", onchange: e => save({ gauge: e.target.value || null }) })),
      el("label", {}, "Finished size", el("input", { value: p.finished_size || "", onchange: e => save({ finished_size: e.target.value }) })),
      el("label", { style: "grid-column:1/3" }, "Yarn substitution", el("textarea", { rows: 2, onchange: e => save({ yarn_substitution: e.target.value }) }, p.yarn_substitution || ""))));
}

function fileInput(cb) {
  return el("input", { type: "file", accept: "image/*", onchange: async e => {
    const f = e.target.files[0]; if (!f) return;
    try { await cb(f.name, await readFile(f)); toast("Uploaded"); } catch (err) { toast(err.message, 5000); }
  } });
}

// ---- sections and steps
function sectionCard(sec) {
  return el("div", { class: "card" }, el("h2", {}, sec.name), ...sec.steps.map(stepEl));
}

function stepEl(st) {
  const flagged = (project.pattern.validation?.issues || []).some(i => i.step_id === st.id && i.level === "error");
  const save = async (fields) => {
    try { const r = await api(`/projects/${project.id}/pattern`, "PATCH", { step: { id: st.id, ...fields } }); Object.assign(st, fields); project.pattern.validation = r.validation; $("#validation")?.replaceWith(validationCard()); toast("Saved"); } catch (e) { toast(e.message, 5000); }
  };
  const box = el("div", { class: "step" + (flagged ? " flag" : ""), id: "step-" + st.id });
  box.append(el("div", { class: "top" },
    el("input", { value: st.label || "", title: "Label", onchange: e => save({ label: e.target.value }) }),
    el("select", { onchange: e => save({ kind: e.target.value }) }, ...["round", "row", "step", "repeat", "note"].map(k => el("option", { value: k, selected: k === st.kind ? "1" : null }, k))),
    el("div", { class: "tm" }, `${ts(st.start)} – ${ts(st.end)} `, el("a", { href: "#", onclick: e => { e.preventDefault(); seek(st.start); } }, "▶ seek")),
    el("input", { type: "number", value: st.stitch_count ?? "", placeholder: "count", title: "Stitch count", onchange: e => save({ stitch_count: e.target.value === "" ? null : +e.target.value }) }),
    el("input", { type: "number", value: st.delta ?? "", placeholder: "Δ", title: "Change vs previous round", onchange: e => save({ delta: e.target.value === "" ? null : +e.target.value }) }),
    el("select", { title: st.tier_reason || "", onchange: e => save({ tier: e.target.value }) }, ...TIERS.map(t => el("option", { value: t, selected: t === st.tier ? "1" : null }, t)))));
  box.append(el("textarea", { rows: 2, onchange: e => save({ instruction: e.target.value }) }, st.instruction || ""));
  if (st.tier_reason) box.append(el("div", { class: "why" }, "Images: " + st.tier_reason));
  box.append(imagesRow(st.images, {
    onchange: imgs => save({ images: imgs }),
    grab: (idx, t, description, exact) => api(`/projects/${project.id}/grab`, "POST", { step_id: st.id, index: idx, t, description, exact }),
    upload: (idx, name, b64) => api(`/projects/${project.id}/upload`, "POST", { kind: "step", step_id: st.id, index: idx, name, b64 }),
    defaultT: st.end || st.start || 0,
  }));
  return box;
}

function imagesRow(images, h) {
  const row = el("div", { class: "imgs" });
  images.forEach((img, i) => row.append(imgEl(images, i, h)));
  const t = el("input", { type: "number", step: "0.5", placeholder: "seconds", value: (h.defaultT || 0).toFixed(1) });
  const d = el("input", { placeholder: "what should the frame show" });
  row.append(el("div", { class: "addimg" }, "Add image", t, d,
    el("div", { class: "ctl" },
      el("button", { class: "small", onclick: async () => { toast("Grabbing burst…", 6000); try { await h.grab(images.length, +t.value, d.value, false); await refresh(); } catch (e) { toast(e.message, 5000); } } }, "Grab burst"),
      el("button", { class: "small ghost", onclick: async () => { if (!vid) return; toast("Grabbing…"); try { await h.grab(images.length, vid.currentTime, d.value, true); await refresh(); } catch (e) { toast(e.message, 5000); } } }, "Grab at playhead")),
    fileInput(async (name, b64) => { await h.upload(images.length, name, b64); await refresh(); })));
  return row;
}

function imgEl(images, i, h) {
  const img = images[i];
  const src = img.chosen ? (img.manual ? `/files/${project.id}/uploads/${img.chosen}` : `/files/${project.id}/frames/${img.chosen}`) : "";
  const box = el("div", { class: "img" + (img.quality === "poor" ? " poor" : "") });
  const main = el("img", { class: "main", src, title: "Click to seek the video here", onclick: () => img.t != null && seek(img.t) });
  box.append(main);
  const desc = el("input", { class: "desc", value: img.description || "", placeholder: "caption / what to show", onchange: e => { img.description = e.target.value; img.caption = e.target.value; h.onchange(images); } });
  box.append(desc);
  if (img.candidates && img.candidates.length > 1) {
    const c = el("div", { class: "cands" });
    for (const cand of img.candidates) c.append(el("img", { src: `/files/${project.id}/frames/${cand.path}`, class: cand.path === img.chosen ? "sel" : "", title: ts(cand.t), onclick: () => { img.chosen = cand.path; img.manual = false; img.quality = "manual"; h.onchange(images); main.src = `/files/${project.id}/frames/${cand.path}`; [...c.children].forEach(x => x.classList.toggle("sel", x.src.endsWith(cand.path))); } }));
    box.append(c);
  }
  const t = el("input", { type: "number", step: "0.5", value: img.t != null ? (+img.t).toFixed(1) : "" });
  box.append(el("div", { class: "ctl" }, t,
    el("button", { class: "small ghost", title: "Grab 8 frames around this time and let Claude pick", onclick: async () => { toast("Regrabbing…", 6000); try { await h.grab(i, +t.value, desc.value, false); await refresh(); } catch (e) { toast(e.message, 5000); } } }, "Burst"),
    el("button", { class: "small ghost", title: "Grab exactly this second", onclick: async () => { toast("Grabbing…"); try { await h.grab(i, +t.value, desc.value, true); await refresh(); } catch (e) { toast(e.message, 5000); } } }, "Exact"),
    el("button", { class: "small ghost", title: "Use the video playhead", onclick: async () => { if (!vid) return; toast("Grabbing…"); try { await h.grab(i, vid.currentTime, desc.value, true); await refresh(); } catch (e) { toast(e.message, 5000); } } }, "Playhead"),
    el("button", { class: "small danger", onclick: () => { images.splice(i, 1); h.onchange(images); box.remove(); } }, "×")));
  const replace = el("label", { class: "small ghost replace", title: "Swap in your own picture; caption, time and position stay" }, "Replace…",
    el("input", { type: "file", accept: "image/*", style: "display:none", onchange: async e => { const f = e.target.files[0]; if (!f) return; toast("Uploading…"); try { await h.upload(i, f.name, await readFile(f)); await refresh(); } catch (err) { toast(err.message, 5000); } } }));
  const ctl2 = el("div", { class: "ctl" }, replace);
  if (img.manual && img.prev_chosen) ctl2.append(el("button", { class: "small ghost", title: "Go back to the video frame", onclick: () => { img.chosen = img.prev_chosen; img.manual = false; img.quality = "manual"; img.pick_reason = "reverted to the video frame"; delete img.prev_chosen; h.onchange(images); refresh(); } }, "Undo"));
  box.append(ctl2);
  if (img.pick_reason) box.append(el("div", { class: "q" }, `${img.quality || ""}: ${img.pick_reason}`));
  return box;
}

// ---- final frames from the video
function finalCard() {
  const p = project.pattern;
  const up = project.uploads || {};
  const demoted = (up.final || []).length && !up.keep_ai_finals;
  return el("div", { class: "card" },
    el("h2", {}, "Finished-object frames from the video"),
    el("p", { class: "help" }, demoted ? "You uploaded final images, so these frames are kept as alternates and not printed. Tick the box under Your images to print them too." : "Printed in the Finishing section, and the best one is the cover if there is no featured image or thumbnail."),
    imagesRow(p.final_images, {
      onchange: imgs => api(`/projects/${project.id}/pattern`, "PATCH", { final_images: imgs }).then(() => toast("Saved")).catch(e => toast(e.message, 5000)),
      grab: (idx, t, description, exact) => api(`/projects/${project.id}/grab`, "POST", { index: idx, t, description, exact }),
      upload: (idx, name, b64) => api(`/projects/${project.id}/upload`, "POST", { kind: "step", index: idx, name, b64 }),
      defaultT: project.duration ? project.duration - 20 : 0,
    }));
}

// ---- transcript
function transcriptCard() {
  const box = el("div", { class: "card" });
  const body = el("div", { class: "transcript muted" }, "Loading…");
  let mode = "cleaned";
  const draw = () => {
    body.innerHTML = "";
    const rows = transcript[mode] || [];
    if (!rows.length) { body.textContent = mode === "cleaned" ? "Not cleaned yet." : "No captions."; return; }
    for (const s of rows) body.append(el("div", { onclick: () => seek(s.start) }, el("span", { class: "ts" }, ts(s.start)), el("span", { class: "k" }, s.kind || ""), el("span", {}, s.text)));
  };
  box.append(el("h2", {}, "Transcript ",
    el("button", { class: "small ghost", onclick: e => { mode = mode === "cleaned" ? "captions" : "cleaned"; e.target.textContent = mode === "cleaned" ? "show raw captions" : "show cleaned"; draw(); } }, "show raw captions")), body);
  api(`/projects/${project.id}/transcript`).then(t => { transcript = t; draw(); }).catch(e => body.textContent = e.message);
  return box;
}

// ---- PDF versions
function versionsCard() {
  const box = el("div", { class: "card" }, el("h2", {}, "PDF versions"));
  api(`/projects/${project.id}/versions`).then(({ versions }) => {
    if (!versions.length) { box.append(el("div", { class: "muted" }, "No PDF rendered yet.")); return; }
    box.append(el("table", { class: "versions" }, ...versions.map(v => {
      let s = {}; try { s = JSON.parse(v.settings_json || "{}"); } catch {}
      const models = s.models ? ["clean", "extract", "pick"].map(j => `${s.models[j]}/${(s.efforts || {})[j] || ""}`).join(" · ") : "";
      return el("tr", {}, el("td", {}, fmtDate(v.created_at)), el("td", {}, `${v.pages} pages`), el("td", {}, fmtSize(v.size)), el("td", { class: "muted", title: models }, models), el("td", {}, v.library_file ? el("a", { href: `/library/${v.library_file}` }, "download") : ""));
    })));
  }).catch(e => box.append(el("div", { class: "muted" }, e.message)));
  return box;
}

navState();
route();
