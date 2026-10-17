/* KiraKB // LINJI ARCHIVE — integrated WebUI application */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const THEMES = ["violet", "pink", "blue", "amber", "sg", "green", "red", "gray"];
  const PREFIXES = ["记录", "残页", "旁注", "回收", "未归档", "观测", "编号", "残响"];
  const MOTTOS = [
    "知识不会拯救任何人，它只是让痛苦有了名字。",
    "记录即收容。未被写下的东西，会反过来注视你。",
    "档案室没有出口，只有更深的分类。",
    "名字是第一条锁链。",
    "每一次检索，都是一次招魂。",
    "不要在索引里寻找自己。",
    "遗忘是免费的，记住要付利息。",
    "所有的答案都带着编号。",
    "被归档的，不等于被理解。",
    "我们保存一切，除了当时的我们。"
  ];
  const BG_SLOTS = [
    ["catalog", "主厅 / CATALOGUE"], ["versions", "版本 / VERSIONS"],
    ["search", "检索 / SEARCH"], ["access", "权限 / ACCESS"],
    ["recall", "召回 / RECALL"], ["settings", "设置 / SETTINGS"],
    ["tasks", "任务 / TASKS"], ["ticker", "字幕框 / SUBTITLE"]
  ];
  const VIEW_META = {
    catalog: ["01", "CATALOGUE"], versions: ["02", "VERSIONS"],
    search: ["03", "SEARCH"], access: ["04", "ACCESS"],
    recall: ["05", "RECALL"], settings: ["06", "SETTINGS"], tasks: ["07", "TASKS"]
  };
  const UI_DEFAULT = {
    theme: "violet", crt: "high", performance: false, splash: true,
    text_fx: "mix", fx_loop: true, fx_interval: 6,
    quote_enabled: true, quote_interval: 9, quote_kb_ids: [],
    quote_custom_lines: [], quote_custom_mottos: []
  };

  const state = {
    kbs: [], current: null, docs: [], versions: [], tasks: [], deleted: [],
    models: [], globalRecall: {}, settings: null,
    view: "catalog", selectedDoc: null, selected: new Set(),
    docFilter: "", docFilterKind: "all",
    ui: { ...UI_DEFAULT },
    quoteTimer: null, quoteBusy: false, tickerTimer: null,
    uiSaveTimer: null, backgrounds: loadBackgrounds()
  };

  /* ---------- API ---------- */
  const isSidebarMode = typeof window.PluginPageContext !== "undefined"
    && window.PluginPageContext.ready && window.parent !== window;
  const token = new URLSearchParams(location.search).get("token") || "";
  let bridgeCtx = null;
  function pluginIdFromPath() {
    const m = location.pathname.match(/^\/plugin-page\/([^/]+)\//) || location.pathname.match(/^\/page\/plugin\/([^/]+)\//);
    return m ? decodeURIComponent(m[1]) : null;
  }
  async function api(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    let data;
    if (isSidebarMode) {
      if (!bridgeCtx) {
        bridgeCtx = await Promise.race([
          window.PluginPageContext.ready(),
          new Promise((_, rej) => setTimeout(() => rej(new Error("页面桥接超时")), 8000))
        ]);
      }
      if (method === "GET") data = await window.PluginPageContext.api.get(path);
      else if (method === "POST") data = await window.PluginPageContext.api.post(path, opts.body ? JSON.parse(opts.body) : {});
      else if (method === "DELETE") data = await window.PluginPageContext.api.delete(path);
      else {
        const res = await fetch("/api/plugin/" + encodeURIComponent(bridgeCtx.pluginId) + path, {
          method, credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: opts.body || "{}"
        });
        data = await res.json();
      }
    } else {
      const pid = pluginIdFromPath();
      const base = pid ? "/api/plugin/" + encodeURIComponent(pid) : "/api";
      const headers = { "Content-Type": "application/json" };
      if (token) headers.Authorization = "Bearer " + token;
      const res = await fetch(base + path, { ...opts, headers });
      data = await res.json().catch(() => ({ error: "返回内容不是有效 JSON" }));
    }
    if (data && data.error && !opts.allowError) throw new Error(data.error);
    return data;
  }
  async function apiUpload(path, formData) {
    if (isSidebarMode) {
      if (!bridgeCtx) bridgeCtx = await window.PluginPageContext.ready();
      const data = await window.PluginPageContext.api.upload(path, formData.get("file"), "file");
      if (data && data.error) throw new Error(data.error);
      return data;
    }
    const pid = pluginIdFromPath();
    const base = pid ? "/api/plugin/" + encodeURIComponent(pid) : "/api";
    const headers = token ? { Authorization: "Bearer " + token } : {};
    const res = await fetch(base + path, { method: "POST", body: formData, headers });
    const data = await res.json().catch(() => ({ error: "返回内容不是有效 JSON" }));
    if (data && data.error) throw new Error(data.error);
    return data;
  }

  /* ---------- utilities ---------- */
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icon(name, size = 14) {
    return `<svg style="width:${size}px;height:${size}px"><use href="#i-${name}"></use></svg>`;
  }
  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "—";
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fmtBytes(n) {
    const v = Number(n) || 0;
    if (v < 1024) return v + " B";
    if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
    return (v / 1024 / 1024).toFixed(1) + " MB";
  }
  function toast(message, kind = "") {
    const el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast is-on " + kind;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { el.className = "toast"; }, 2800);
  }
  function currentKb() { return state.kbs.find(k => k.kb_id === state.current) || null; }
  function activeVersion() { return state.versions.find(v => v.is_active) || null; }
  function currentDoc() { return state.docs.find(d => d.doc_id === state.selectedDoc) || null; }

  /* ---------- modal ---------- */
  function openModal({ title, subtitle = "", body = "", footer = "", small = false, onMount }) {
    const root = $("#modalRoot");
    root.innerHTML = `<div class="modal-root is-open"><div class="modal-backdrop" data-close></div><div class="modal-sheet ${small ? "small" : ""}" role="dialog" aria-modal="true"><div class="modal-head"><div><h3>${esc(title)}</h3>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div><button class="modal-x" type="button" data-close>${icon("x", 14)}</button></div><div class="modal-body">${body}</div>${footer ? `<div class="modal-foot">${footer}</div>` : ""}</div></div>`;
    const close = () => { root.innerHTML = ""; };
    $$("[data-close]", root).forEach(b => b.addEventListener("click", close));
    root.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    const first = $("input,textarea,select,button", root);
    if (first) setTimeout(() => first.focus(), 20);
    if (onMount) onMount(root, close);
    return { root, close };
  }
  function confirmDialog(title, message, okText = "确认", danger = false) {
    return new Promise(resolve => {
      const m = openModal({
        title, small: true, body: `<p>${esc(message)}</p>`,
        footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn ${danger ? "danger" : "primary"}" type="button" data-ok>${esc(okText)}</button>`,
        onMount(root, close) {
          $("[data-cancel]", root).onclick = () => { close(); resolve(false); };
          $("[data-ok]", root).onclick = () => { close(); resolve(true); };
        }
      });
    });
  }

  /* ---------- theme / splash ---------- */
  function loadBackgrounds() {
    try { return JSON.parse(localStorage.getItem("kirakb-cathode-bg-v2") || "{}") || {}; }
    catch (_) { return {}; }
  }
  function persistBackgrounds() {
    try { localStorage.setItem("kirakb-cathode-bg-v2", JSON.stringify(state.backgrounds)); }
    catch (_) { toast("背景图过大，未能写入本地存储", "err"); }
  }
  function renderThemeStrip() {
    const strip = $("#themeStrip");
    if (!strip) return;
    strip.innerHTML = THEMES.map(t => `<button class="swatch ${t === state.ui.theme ? "is-active" : ""}" type="button" data-theme-set="${t}" title="${t}" aria-label="${t}"></button>`).join("")
      + `<button class="swatch random" type="button" data-theme-set="random" title="随机配色" aria-label="随机配色"></button>`;
  }
  function setTheme(theme, save = true) {
    if (theme === "random") {
      const pool = THEMES.filter(t => t !== state.ui.theme);
      theme = pool[Math.floor(Math.random() * pool.length)];
    }
    if (!THEMES.includes(theme)) theme = "violet";
    state.ui.theme = theme;
    document.documentElement.setAttribute("data-theme", "dark");
    document.body.dataset.theme = theme;
    $$("#themeStrip [data-theme-set]").forEach(b => b.classList.toggle("is-active", b.dataset.themeSet === theme));
    const themeSelect = $("#setUiTheme");
    if (themeSelect) themeSelect.value = theme;
    const screen = $("#screen");
    if (screen && save) { screen.classList.add("theme-flash"); setTimeout(() => screen.classList.remove("theme-flash"), 500); }
    if (save) queueUiSave();
  }
  function applyCrt(level) {
    state.ui.crt = ["off", "low", "medium", "high"].includes(level) ? level : "high";
    document.body.dataset.crt = state.ui.crt;
    const q = $("#quickCrt");
    if (q) q.setAttribute("aria-pressed", String(state.ui.crt !== "off"));
    const crtSelect = $("#setUiCrt");
    if (crtSelect) crtSelect.value = state.ui.crt;
  }
  function applyPerformance() {
    document.body.classList.toggle("perf", !!state.ui.performance);
    if (window.KBTextFX) window.KBTextFX.setConfig({ enabled: !state.ui.performance && !reduced, level: state.ui.performance ? 0 : 2 });
  }
  function applyUi() {
    setTheme(state.ui.theme, false);
    applyCrt(state.ui.crt);
    applyPerformance();
    if (window.KBTextFX) {
      window.KBTextFX.setConfig({
        mode: state.ui.text_fx, idle: true, enabled: !state.ui.performance && !reduced,
        level: state.ui.performance ? 0 : 2, loop: state.ui.fx_loop, loopInterval: state.ui.fx_interval
      });
      if (!state.ui.performance && !reduced && state.ui.fx_loop) window.KBTextFX.startLoop(state.ui.fx_interval);
      else window.KBTextFX.stopLoop();
    }
    syncUiControls();
    startBrandModes();
    if (state.ui.quote_enabled) startQuoteTimer(); else stopQuoteTimer();
  }
  function queueUiSave() {
    clearTimeout(state.uiSaveTimer);
    state.uiSaveTimer = setTimeout(() => saveUi(false), 500);
  }
  async function saveUi(explicit = false) {
    try {
      const res = await api("/settings", { method: "PUT", body: JSON.stringify({ ui: collectUi() }) });
      if (res && res.settings) state.settings = res.settings;
      if (explicit) toast("机台设置已保存。");
    } catch (e) { if (explicit) toast("保存失败：" + e.message, "err"); }
  }
  function collectUi() {
    return {
      theme: state.ui.theme, crt: state.ui.crt, performance: !!state.ui.performance,
      splash: !!state.ui.splash, text_fx: $("#setUiTextFx")?.value || state.ui.text_fx,
      fx_loop: !!$("#setUiFxLoop")?.checked, fx_interval: Number($("#setUiFxInterval")?.value) || state.ui.fx_interval,
      quote_enabled: !!$("#setUiQuoteEnabled")?.checked, quote_interval: Number($("#setUiQuoteInterval")?.value) || state.ui.quote_interval,
      quote_kb_ids: selectedQuoteKbs(), quote_custom_lines: lines("#setUiQuoteLines"),
      quote_custom_mottos: lines("#setUiQuoteMottos")
    };
  }
  function lines(sel) { return ($(sel)?.value || "").split("\n").map(s => s.trim()).filter(Boolean); }
  function selectedQuoteKbs() {
    const checked = $$("#setUiQuoteKbs input:checked").map(x => x.value);
    return checked.length === state.kbs.length ? [] : checked;
  }
  function syncUiControls() {
    const set = (id, v) => { const el = $("#" + id); if (el) el.value = v; };
    const chk = (id, v) => { const el = $("#" + id); if (el) el.checked = !!v; };
    set("setUiTheme", state.ui.theme); set("setUiCrt", state.ui.crt);
    chk("setUiPerf", state.ui.performance); chk("setUiSplash", state.ui.splash);
    set("setUiTextFx", state.ui.text_fx); chk("setUiFxLoop", state.ui.fx_loop);
    set("setUiFxInterval", state.ui.fx_interval); chk("setUiQuoteEnabled", state.ui.quote_enabled);
    set("setUiQuoteInterval", state.ui.quote_interval);
    const ql = $("#setUiQuoteLines"); if (ql) ql.value = (state.ui.quote_custom_lines || []).join("\n");
    const qm = $("#setUiQuoteMottos"); if (qm) qm.value = (state.ui.quote_custom_mottos || []).join("\n");
    renderQuoteKbs(); renderBgGrid();
  }
  function renderQuoteKbs() {
    const box = $("#setUiQuoteKbs");
    if (!box) return;
    const selected = new Set(state.ui.quote_kb_ids || []);
    const all = selected.size === 0;
    box.innerHTML = state.kbs.length ? state.kbs.map(kb => {
      const on = all || selected.has(kb.kb_id);
      return `<label class="${on ? "is-on" : ""}"><input type="checkbox" value="${esc(kb.kb_id)}" ${on ? "checked" : ""}>${esc(kb.display_name || kb.kb_id)}</label>`;
    }).join("") : `<span class="faint" style="font-size:10px">还没有知识库</span>`;
  }
  function renderBgGrid() {
    const grid = $("#cathodeBgGrid");
    if (!grid) return;
    grid.innerHTML = BG_SLOTS.map(([key, label]) => {
      const img = state.backgrounds[key];
      return `<div class="bg-slot" data-bg="${key}" style="background-image:${img ? "none" : "linear-gradient(135deg,rgba(181,140,255,.13),rgba(0,0,0,.35))"}">${img ? `<img src="${img}" alt="">` : ""}<span>${label}</span><div><button type="button" data-bg-upload="${key}">上传</button><button type="button" data-bg-reset="${key}">清除</button></div><input type="file" accept="image/*" data-bg-file="${key}" hidden></div>`;
    }).join("");
  }
  function applyBackgrounds() {
    $$("[data-view]").forEach(view => {
      const key = view.dataset.view;
      const art = view.querySelector(".view-art");
      if (art) art.style.backgroundImage = state.backgrounds[key] ? `url("${state.backgrounds[key]}")` : "none";
    });
    const ticker = $("#ticker");
    if (ticker) ticker.style.setProperty("--ticker-art", state.backgrounds.ticker ? `url("${state.backgrounds.ticker}")` : "none");
  }

  /* ---------- textfx / boot ---------- */
  function initTextFx() {
    if (!window.KBTextFX) return;
    window.KBTextFX.init();
    applyUi();
    window.KBTextFX.replayView(state.view);
    replayBrand();
  }
  function replayText() {
    if (window.KBTextFX) window.KBTextFX.replayView(state.view || "catalog");
  }
  function replayBrand() {
    if (!window.KBTextFX) return;
    window.KBTextFX.play($("#railKb"), "mix");
    window.KBTextFX.play($("#railTitle"), "mix");
    window.KBTextFX.play($("#railAlias"), "mix");
  }
  const BRAND_MODES = ["brand-breathe", "brand-sheen", "brand-orbit", "brand-flicker", "brand-sweep"];
  let brandTimer = null;
  function cycleBrandMode() {
    const row = $("#railBrandRow");
    if (!row) return;
    row.classList.remove(...BRAND_MODES);
    if (state.ui.performance || reduced) return;
    row.classList.add(BRAND_MODES[Math.floor(Math.random() * BRAND_MODES.length)]);
  }
  function scheduleBrandMode() {
    clearTimeout(brandTimer);
    const row = $("#railBrandRow");
    if (!row || state.ui.performance || reduced) {
      row?.classList.remove(...BRAND_MODES);
      return;
    }
    if (!BRAND_MODES.some(c => row.classList.contains(c))) cycleBrandMode();
    brandTimer = setTimeout(() => {
      cycleBrandMode();
      scheduleBrandMode();
    }, 4200 + Math.random() * 4200);
  }
  function startBrandModes() {
    cycleBrandMode();
    scheduleBrandMode();
  }
  function runBoot() {
    const boot = $("#boot");
    if (!boot) return;
    const skip = $("#bootSkip");
    if (!state.ui.splash || reduced || new URLSearchParams(location.search).has("skipboot")) {
      boot.classList.add("is-off"); document.body.classList.remove("boot-hold"); return;
    }
    document.body.classList.add("boot-hold");
    const pct = $("#bootPct"), status = $("#bootStatus");
    const stages = ["ACQUIRING SIGNAL", "VERIFYING HASH", "MOUNTING ARCHIVE", "SIGNAL LOCKED"];
    let v = 0;
    boot.timer = setInterval(() => {
      v = Math.min(100, v + Math.max(1, Math.round((100 - v) / 10)));
      pct.textContent = String(v).padStart(2, "0");
      status.textContent = stages[Math.min(3, Math.floor(v / 28))];
      if (v >= 100) { clearInterval(boot.timer); setTimeout(finish, 350); }
    }, 95);
    skip.onclick = finish;
    function finish() {
      clearInterval(boot.timer);
      boot.classList.add("is-off"); document.body.classList.remove("boot-hold");
      setTimeout(() => { boot.style.display = "none"; }, 600);
      replayText(); replayBrand(); startQuoteTimer();
    }
  }

  /* ---------- data ---------- */
  async function loadKbs() {
    const kbs = await api("/kbs");
    state.kbs = Array.isArray(kbs) ? kbs : [];
    if (!state.current || !state.kbs.some(k => k.kb_id === state.current)) state.current = state.kbs[0]?.kb_id || null;
  }
  async function loadDocs() {
    if (!state.current) { state.docs = []; return; }
    const docs = await api(`/kbs/${encodeURIComponent(state.current)}/documents`);
    state.docs = Array.isArray(docs) ? docs : [];
    state.selected = new Set([...state.selected].filter(id => state.docs.some(d => d.doc_id === id)));
    if (state.selectedDoc && !state.docs.some(d => d.doc_id === state.selectedDoc)) state.selectedDoc = null;
  }
  async function loadVersions() {
    if (!state.current) { state.versions = []; return; }
    const versions = await api(`/kbs/${encodeURIComponent(state.current)}/versions`);
    state.versions = Array.isArray(versions) ? versions : [];
  }
  async function loadTasks() {
    try { const tasks = await api("/tasks"); state.tasks = Array.isArray(tasks) ? tasks : []; }
    catch (_) { state.tasks = []; }
  }
  async function loadGlobalRecall() {
    try { state.globalRecall = await api("/recall/global") || {}; }
    catch (_) { state.globalRecall = {}; }
  }
  async function loadSettings() {
    try { state.settings = await api("/settings"); }
    catch (_) { state.settings = null; }
  }
  async function refreshAll(render = true) {
    await loadKbs();
    await Promise.all([loadDocs(), loadVersions(), loadTasks(), loadGlobalRecall()]);
    if (render) renderAll();
  }

  /* ---------- render ---------- */
  function renderAll() {
    renderKbSelect();
    renderCatalog();
    renderVersions();
    renderAccess();
    renderRecall();
    renderSettings();
    renderTasks();
    renderThemeStrip();
    syncUiControls();
    applyBackgrounds();
    replayText();
  }
  function renderKbSelect() {
    const sel = $("#kbSelect");
    if (!sel) return;
    sel.innerHTML = state.kbs.length
      ? state.kbs.map(k => `<option value="${esc(k.kb_id)}">${esc(k.display_name || k.kb_id)} · ${k.entry_count || 0}</option>`).join("")
      : `<option value="">还没有知识库</option>`;
    if (state.current) sel.value = state.current;
    const count = $("#railKbCount"); if (count) count.textContent = state.kbs.length;
  }
  function renderCatalog() {
    const kb = currentKb();
    const ver = activeVersion();
    $("#statEntries").textContent = String(state.docs.length).padStart(3, "0");
    $("#statVersions").textContent = String(state.versions.length).padStart(2, "0");
    $("#statDimension").textContent = ver ? ver.dimension : "—";
    const recall = state.globalRecall?.enabled && kb?.recall?.enabled;
    $("#statRecall").textContent = recall ? "ON" : "OFF";
    $("#activeBadgeText").textContent = ver ? `${ver.model_name} · ${ver.dimension}D` : "NO ACTIVE VERSION";
    $("#docCount").textContent = `${state.docs.length} RECORDS`;
    $("#docFoot").textContent = `${filteredDocs().length} RECORDS`;
    renderDocList();
    renderInspector();
  }
  function filteredDocs() {
    const q = state.docFilter.trim().toLowerCase();
    return state.docs.filter(d => {
      if (q && !(d.name || "").toLowerCase().includes(q)) return false;
      if (state.docFilterKind === "ai" && d.permissions?.edit !== "admin") return false;
      if (state.docFilterKind === "frozen" && d.permissions?.edit !== "off" && d.permissions?.delete !== "off") return false;
      if (state.docFilterKind === "recall" && d.recall?.enabled !== true) return false;
      return !d.deleted;
    });
  }
  function renderDocList() {
    const list = filteredDocs();
    $("#docList").innerHTML = list.length ? list.map((d, i) => `
      <div class="entry-row ${d.doc_id === state.selectedDoc ? "is-selected" : ""}" data-doc="${esc(d.doc_id)}" tabindex="0">
        <span class="entry-no"><input class="doc-check" type="checkbox" data-check="${esc(d.doc_id)}" ${state.selected.has(d.doc_id) ? "checked" : ""}>${String(i + 1).padStart(2, "0")}</span>
        <span class="entry-main"><b>${esc(d.name)}</b><small>${fmtBytes(d.size)} · ${fmtTime(d.updated_at)}</small></span>
        <span class="entry-kb">${esc(currentKb()?.display_name || state.current || "—")}</span>
        <span class="entry-status ${d.recall?.enabled === true ? "is-recall" : ""}"><i></i>${d.recall?.enabled === true ? "召回" : "已索引"}</span>
        <span class="entry-time">${esc((d.updated_at ? fmtTime(d.updated_at) : "—").slice(5, 16))}</span>
      </div>`).join("") : `<div class="inspect-empty">没有匹配的记录。上传文件，或先创建一个版本。</div>`;
    $("#selectAllDocs").checked = list.length > 0 && list.every(d => state.selected.has(d.doc_id));
  }
  async function selectDoc(id) {
    state.selectedDoc = id;
    renderDocList();
    const d = state.docs.find(x => x.doc_id === id);
    if (!d) { renderInspector(); return; }
    $("#inspectEmpty").hidden = true;
    $("#inspectBody").hidden = false;
    $("#inspectBadge").textContent = "ARCHIVED";
    $("#inspectBadge").className = "badge ok";
    $("#inspectKicker").textContent = `RECORD ${String(id).replace(/\D/g, "").padStart(3, "0")} // ${currentKb()?.display_name || "ARCHIVE"}`;
    if (window.KBTextFX) window.KBTextFX.playText($("#inspectTitle"), d.name || id, "mix");
    else $("#inspectTitle").textContent = d.name || id;
    $("#inspectTags").innerHTML = (d.recall?.enabled ? ["召回"] : []).concat(d.permissions?.edit ? ["权限覆盖"] : []).map(t => `<span>${esc(t)}</span>`).join("");
    $("#inspectContent").textContent = "正在读取正文……";
    $("#inspectKb").textContent = currentKb()?.display_name || state.current || "—";
    $("#inspectUpdated").textContent = fmtTime(d.updated_at);
    $("#inspectHash").textContent = (d.content_hash || "").slice(0, 18) || "—";
    $("#inspectStatus").textContent = d.recall?.enabled === true ? "召回开启" : "已索引";
    try {
      const full = await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(id)}`);
      if (state.selectedDoc === id) $("#inspectContent").textContent = full.content || "（空正文）";
    } catch (e) { $("#inspectContent").textContent = "读取失败：" + e.message; }
  }
  function renderInspector() {
    if (!state.selectedDoc) {
      $("#inspectEmpty").hidden = false; $("#inspectBody").hidden = true;
      $("#inspectBadge").textContent = "NO SELECTION"; $("#inspectBadge").className = "badge";
      return;
    }
    $("#inspectEmpty").hidden = true; $("#inspectBody").hidden = false;
  }
  function renderVersions() {
    $("#versionGrid").innerHTML = state.versions.length ? state.versions.map((v, i) => `
      <article class="version-card ${v.is_active ? "is-active" : ""}">
        <div class="version-top"><div><div class="version-model">${esc(v.model_name)}</div><div class="version-dim">${v.dimension} DIMENSIONS · ${v.status === "ready" ? "完整" : "不完整"}</div></div><span class="badge ${v.is_active ? "ok" : ""}">${v.is_active ? "使用中" : "待用"}</span></div>
        <div class="version-date">${fmtTime(v.created_at)}</div>
        <div class="version-counts"><div><span>OUTDATED</span><b>${v.outdated || 0}</b></div><div><span>STATUS</span><b>${v.status === "ready" ? "OK" : "—"}</b></div></div>
        <div class="version-actions">
          ${v.status === "ready" ? `<button class="btn small ghost" type="button" data-activate="${esc(v.version_id)}">${icon("check", 12)}激活</button>` : ""}
          ${v.status === "ready" ? `<button class="btn small ghost" type="button" data-sync="${esc(v.version_id)}">${icon("refresh", 12)}同步</button>` : ""}
          ${!v.is_active || v.status !== "ready" ? `<button class="btn small danger" type="button" data-delver="${esc(v.version_id)}">${icon("trash", 12)}</button>` : ""}
        </div>
      </article>`).join("") : `<div class="inspect-empty" style="grid-column:1/-1">还没有索引。创建一个版本后才能检索。</div>`;
  }
  function renderAccess() {
    const kb = currentKb();
    const perms = kb?.permissions || {};
    $("#policyAdd").value = perms.add || "open";
    $("#policyEdit").value = perms.edit || "open";
    $("#policyDelete").value = perms.delete || "whitelist";
    $("#kbAllowUsers").value = (perms.allow_users || []).join("\n");
    const list = state.docs;
    $("#overrideCount").textContent = `${list.length} ENTRIES`;
    $("#entryPolicyList").innerHTML = list.length ? list.map(d => `<div class="entry-policy"><b title="${esc(d.name)}">${esc(d.name)}</b><select class="policy-select" data-entry-edit="${esc(d.doc_id)}">${["inherit", "open", "whitelist", "admin", "off"].map(v => `<option value="${v}" ${((d.permissions?.edit) || "inherit") === v ? "selected" : ""}>${v === "inherit" ? "继承" : v}</option>`).join("")}</select><select class="policy-select" data-entry-delete="${esc(d.doc_id)}">${["inherit", "open", "whitelist", "admin", "off"].map(v => `<option value="${v}" ${((d.permissions?.delete) || "inherit") === v ? "selected" : ""}>${v === "inherit" ? "继承" : v}</option>`).join("")}</select></div>`).join("") : `<div class="inspect-empty">没有条目。</div>`;
  }
  function renderRecall() {
    const kb = currentKb();
    const recall = kb?.recall || {};
    $("#globalRecallBadge").classList.toggle("is-on", !!state.globalRecall?.enabled);
    $("#globalRecallBadge").querySelector("small").textContent = state.globalRecall?.enabled ? "ON" : "OFF";
    $("#globalRecallInfo").innerHTML = `<p><b>MODE</b> ${esc(state.globalRecall?.mode || "—")}</p><p><b>TOP K</b> ${state.globalRecall?.top_k ?? "—"}</p><p><b>MIN SCORE</b> ${state.globalRecall?.min_score ?? "—"}</p><p><b>MAX CHARS</b> ${state.globalRecall?.max_chars ?? "—"}</p>`;
    $("#kbRecallEnabled").checked = !!recall.enabled;
    $("#kbRecallTopK").value = recall.top_k ?? "";
    $("#kbRecallMinScore").value = recall.min_score ?? "";
    $("#kbRecallMaxChars").value = recall.max_chars ?? "";
    $("#entryRecallList").innerHTML = state.docs.length ? state.docs.map(d => {
      const r = d.recall || {};
      return `<div class="entry-recall"><b title="${esc(d.name)}">${esc(d.name)}</b><select data-entry-recall="${esc(d.doc_id)}"><option value="inherit" ${r.enabled == null ? "selected" : ""}>继承</option><option value="on" ${r.enabled === true ? "selected" : ""}>强制</option><option value="off" ${r.enabled === false ? "selected" : ""}>排除</option></select><input data-entry-keywords="${esc(d.doc_id)}" value="${esc((r.keywords || []).join(", "))}" placeholder="关键词"><button class="btn small ghost" type="button" data-save-recall="${esc(d.doc_id)}">保存</button></div>`;
    }).join("") : `<div class="inspect-empty">没有条目。</div>`;
  }
  function renderTasks() {
    $("#taskList").innerHTML = state.tasks.length ? state.tasks.slice().reverse().map(t => `<div class="task"><div class="task-head"><span class="name">${esc(t.description)}</span><span class="st ${esc(t.status)}">${({ running: "进行中", pending: "等待中", completed: "已完成", failed: "失败" }[t.status] || t.status)}</span></div><div class="progress"><i style="width:${Number(t.progress) || 0}%"></i></div><div class="msg">${esc(t.message || "")}${t.progress ? " · " + t.progress + "%" : ""}</div></div>`).join("") : `<div class="empty">还没有后台任务。</div>`;
  }
  function renderSettings() {
    const cfg = state.settings;
    if (!cfg) return;
    const val = (id, v) => { const el = $("#" + id); if (el && v != null) el.value = v; };
    const chk = (id, v) => { const el = $("#" + id); if (el) el.checked = !!v; };
    const s = cfg.search || {}; const e = cfg.entries || {}; const p = cfg.permission || {}; const r = cfg.recall || {}; const st = cfg.storage || {}; const w = cfg.webui || {};
    val("setTopK", s.default_top_k); val("setChunkSize", s.chunk_size); val("setChunkOverlap", s.chunk_overlap);
    chk("setHybrid", s.enable_hybrid_search); chk("setStopwords", s.enable_stopwords); chk("setRerank", s.enable_rerank); chk("setLexical", s.enable_lexical_search); chk("setIgnoreTime", s.ignore_time); chk("setAutoInstallDeps", s.auto_install_dependencies); val("setLexicalMin", s.lexical_min_score);
    val("setTitleMax", e.entry_title_max_chars); val("setHistoryKeep", e.entry_history_keep); chk("setExtraTools", e.extra_tools_enabled); chk("setAutoSync", e.auto_sync_versions);
    const fill = (id, v) => { const el = $("#" + id); if (!el) return; el.innerHTML = ["open", "whitelist", "admin", "off"].map(x => `<option value="${x}" ${x === v ? "selected" : ""}>${{ open: "开放", whitelist: "名单", admin: "仅人工", off: "冻结" }[x]}</option>`).join(""); };
    fill("setAddPolicy", p.default_add_policy || "open"); fill("setEditPolicy", p.default_edit_policy || "open"); fill("setDeletePolicy", p.default_delete_policy || "whitelist");
    $("#setOwnerWhitelist").value = (p.owner_whitelist || []).join("\n");
    chk("setRecallEnabled", r.enabled); val("setRecallMode", r.mode || "hybrid"); val("setRecallTopK", r.top_k); val("setRecallMin", r.min_score); val("setRecallStrong", r.strong_score); val("setRecallChars", r.max_chars); val("setRecallTimeout", r.timeout_ms); val("setRecallDedupe", r.dedupe_turns); val("setRecallMaxKbs", r.max_kbs); val("setRecallMinQuery", r.min_query_chars);
    val("setKbDir", st.knowledge_base_dir); chk("setEnableWebui", w.enable_webui); val("setWebHost", w.webui_host); val("setWebPort", w.webui_port); val("setWebToken", w.webui_token);
  }
  function collectFunctionalSettings() {
    const num = id => { const el = $("#" + id); return el && el.value !== "" ? Number(el.value) : null; };
    return {
      search: {
        enable_hybrid_search: $("#setHybrid").checked, default_top_k: num("setTopK"), chunk_size: num("setChunkSize"), chunk_overlap: num("setChunkOverlap"),
        enable_stopwords: $("#setStopwords").checked, enable_rerank: $("#setRerank").checked, enable_lexical_search: $("#setLexical").checked,
        ignore_time: $("#setIgnoreTime").checked, auto_install_dependencies: $("#setAutoInstallDeps").checked, lexical_min_score: num("setLexicalMin")
      },
      entries: { entry_title_max_chars: num("setTitleMax"), entry_history_keep: num("setHistoryKeep"), extra_tools_enabled: $("#setExtraTools").checked, auto_sync_versions: $("#setAutoSync").checked },
      permission: { owner_whitelist: lines("#setOwnerWhitelist"), default_add_policy: $("#setAddPolicy").value, default_edit_policy: $("#setEditPolicy").value, default_delete_policy: $("#setDeletePolicy").value },
      recall: { recall_enabled: $("#setRecallEnabled").checked, recall_mode: $("#setRecallMode").value, recall_top_k: num("setRecallTopK"), recall_min_score: num("setRecallMin"), recall_strong_score: num("setRecallStrong"), recall_max_chars: num("setRecallChars"), recall_timeout_ms: num("setRecallTimeout"), recall_dedupe_turns: num("setRecallDedupe"), recall_max_kbs: num("setRecallMaxKbs"), recall_min_query_chars: num("setRecallMinQuery") },
      storage: { knowledge_base_dir: $("#setKbDir").value.trim() },
      webui: { enable_webui: $("#setEnableWebui").checked, webui_host: $("#setWebHost").value.trim(), webui_port: num("setWebPort"), webui_token: $("#setWebToken").value }
    };
  }

  /* ---------- actions: KB / docs ---------- */
  async function selectKb(id) {
    if (!id || id === state.current) return;
    state.current = id; state.selectedDoc = null; state.selected.clear();
    try { await Promise.all([loadDocs(), loadVersions()]); renderAll(); }
    catch (e) { toast("加载知识库失败：" + e.message, "err"); }
  }
  function openKbCreate() {
    openModal({
      title: "新建知识库", subtitle: "ID 只能包含字母、数字、下划线和连字符。", small: true,
      body: `<div class="field"><label for="newKbId">知识库 ID</label><input id="newKbId" placeholder="archive_01"></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>创建</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        $("[data-ok]", root).onclick = async () => {
          const id = $("#newKbId").value.trim(); if (!id) return toast("请填写 ID", "err");
          try { await api("/kbs", { method: "POST", body: JSON.stringify({ kb_id: id }) }); close(); state.current = id; await refreshAll(); toast("知识库已创建。"); }
          catch (e) { toast("创建失败：" + e.message, "err"); }
        };
      }
    });
  }
  function openKbEdit() {
    const kb = currentKb(); if (!kb) return toast("请先创建知识库", "err");
    openModal({
      title: "编辑知识库", subtitle: kb.kb_id, small: true,
      body: `<div class="field"><label for="kbNameEdit">显示名</label><input id="kbNameEdit" value="${esc(kb.display_name || "")}"></div><div class="field"><label for="kbDescEdit">描述</label><textarea id="kbDescEdit" rows="3">${esc(kb.description || "")}</textarea></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>保存</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        $("[data-ok]", root).onclick = async () => {
          try { await api(`/kbs/${encodeURIComponent(kb.kb_id)}/info`, { method: "PUT", body: JSON.stringify({ display_name: $("#kbNameEdit").value.trim() || kb.kb_id, description: $("#kbDescEdit").value.trim() }) }); close(); await refreshAll(); toast("已保存。"); }
          catch (e) { toast("保存失败：" + e.message, "err"); }
        };
      }
    });
  }
  async function deleteKb() {
    const kb = currentKb(); if (!kb) return;
    const ok = await confirmDialog("删除知识库", `删除「${kb.display_name || kb.kb_id}」以及全部条目和版本？此操作不可恢复。`, "删除", true);
    if (!ok) return;
    try { await api(`/kbs/${encodeURIComponent(kb.kb_id)}`, { method: "DELETE" }); state.current = null; state.selectedDoc = null; await refreshAll(); toast("知识库已删除。"); }
    catch (e) { toast("删除失败：" + e.message, "err"); }
  }
  function triggerUpload() { $("#fileInput").click(); }
  async function uploadFiles(files) {
    if (!state.current) return toast("请先创建知识库", "err");
    if (!files?.length) return;
    let ok = 0;
    for (const file of files) {
      const fd = new FormData(); fd.append("file", file);
      try { await apiUpload(`/kbs/${encodeURIComponent(state.current)}/documents`, fd); ok++; }
      catch (e) { toast(`上传 ${file.name} 失败：${e.message}`, "err"); }
    }
    $("#fileInput").value = "";
    if (ok) toast(`已上传 ${ok} 个文件。`);
    await refreshAll();
  }
  function openDocEditor(doc) {
    if (!doc) return;
    openModal({
      title: doc.name || "编辑条目", subtitle: `${fmtBytes(doc.size)} · ${fmtTime(doc.updated_at)}`, body: `<div class="field"><label for="docNameEdit">标题</label><input id="docNameEdit" value="${esc(doc.name || "")}"></div><div class="field"><label for="docContentEdit">正文</label><textarea id="docContentEdit" class="doc-text" rows="14">正在读取……</textarea></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>保存并重建</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}`).then(full => { $("#docContentEdit").value = full.content || ""; }).catch(e => toast("读取失败：" + e.message, "err"));
        $("[data-ok]", root).onclick = async () => {
          const name = $("#docNameEdit").value.trim(); if (!name) return toast("标题不能为空", "err");
          try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}`, { method: "PUT", body: JSON.stringify({ content: $("#docContentEdit").value, name }) }); close(); await refreshAll(); toast("条目已保存。"); }
          catch (e) { toast("保存失败：" + e.message, "err"); }
        };
      }
    });
  }
  async function deleteDoc(id) {
    const doc = state.docs.find(d => d.doc_id === id); if (!doc) return;
    const ok = await confirmDialog("删除条目", `软删除「${doc.name}」？之后可以恢复。`, "删除", true);
    if (!ok) return;
    try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(id)}`, { method: "DELETE" }); state.selectedDoc = null; await refreshAll(); toast("条目已删除。"); }
    catch (e) { toast("删除失败：" + e.message, "err"); }
  }
  function openEntrySettings(doc) {
    if (!doc) return;
    const perms = doc.permissions || {}, recall = doc.recall || {};
    openModal({
      title: doc.name || "条目设置", subtitle: "条目级权限与召回覆盖", body: `<div class="field"><label for="entryEditPolicy">编辑策略</label><select id="entryEditPolicy">${["inherit", "open", "whitelist", "admin", "off"].map(v => `<option value="${v}" ${((perms.edit) || "inherit") === v ? "selected" : ""}>${v === "inherit" ? "继承" : v}</option>`).join("")}</select></div><div class="field"><label for="entryDeletePolicy">删除策略</label><select id="entryDeletePolicy">${["inherit", "open", "whitelist", "admin", "off"].map(v => `<option value="${v}" ${((perms.delete) || "inherit") === v ? "selected" : ""}>${v === "inherit" ? "继承" : v}</option>`).join("")}</select></div><div class="field"><label for="entryAllowUsers">允许名单</label><textarea id="entryAllowUsers" rows="2">${esc((perms.allow_users || []).join("\n"))}</textarea></div><div class="field"><label for="entryRecallMode">召回</label><select id="entryRecallMode"><option value="inherit" ${recall.enabled == null ? "selected" : ""}>继承</option><option value="on" ${recall.enabled === true ? "selected" : ""}>强制开启</option><option value="off" ${recall.enabled === false ? "selected" : ""}>排除</option></select></div><div class="field"><label for="entryKeywords">召回关键词</label><input id="entryKeywords" value="${esc((recall.keywords || []).join(", "))}"></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>保存</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        $("[data-ok]", root).onclick = async () => {
          try {
            await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}/permissions`, { method: "PUT", body: JSON.stringify({ edit: $("#entryEditPolicy").value, delete: $("#entryDeletePolicy").value, allow_users: lines("#entryAllowUsers") }) });
            await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}/recall`, { method: "PUT", body: JSON.stringify({ enabled: $("#entryRecallMode").value === "on" ? true : $("#entryRecallMode").value === "off" ? false : null, keywords: $("#entryKeywords").value.split(/[,，]/).map(s => s.trim()).filter(Boolean) }) });
            close(); await refreshAll(); toast("条目设置已保存。");
          } catch (e) { toast("保存失败：" + e.message, "err"); }
        };
      }
    });
  }
  async function openHistory(doc) {
    if (!doc) return;
    let data;
    try { data = await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}/history`); }
    catch (e) { return toast("加载历史失败：" + e.message, "err"); }
    const revs = data.revisions || [];
    openModal({
      title: doc.name || "修订历史", subtitle: `${revs.length} 个版本 · 当前 ${data.keep || 5} 份过程版本`, body: revs.length ? revs.map(r => `<div class="history-item"><b>${esc(r.rev_id)}</b><span>${esc(r.actor || "system")} · ${fmtTime(r.created_at)} · ${fmtBytes(r.size)}</span>${r.is_current ? `<span class="badge ok">当前</span>` : `<button class="btn small ghost" type="button" data-rev="${esc(r.rev_id)}">回退</button>`}</div>`).join("") : `<div class="inspect-empty">还没有修订记录。</div>`,
      footer: `<button class="btn ghost" type="button" data-close>关闭</button>`,
      onMount(root, close) {
        $$("[data-close]", root).forEach(b => b.onclick = close);
        $$("[data-rev]", root).forEach(b => b.onclick = async () => {
          try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(doc.doc_id)}/history/${encodeURIComponent(b.dataset.rev)}/restore`, { method: "POST" }); close(); await refreshAll(); toast("已回退到该修订。"); }
          catch (e) { toast("回退失败：" + e.message, "err"); }
        });
      }
    });
  }
  async function openDeleted() {
    try { state.deleted = await api(`/kbs/${encodeURIComponent(state.current)}/documents/deleted`); }
    catch (e) { return toast("加载失败：" + e.message, "err"); }
    openModal({
      title: "已删除条目", subtitle: "恢复会回写所有可用版本。", body: state.deleted.length ? state.deleted.map(d => `<div class="deleted-row"><div></div><div><b>${esc(d.name)}</b><small>${fmtTime(d.updated_at)}</small></div><button class="btn small ghost" type="button" data-restore="${esc(d.doc_id)}">恢复</button></div>`).join("") : `<div class="inspect-empty">没有已删除的条目。</div>`,
      footer: `<button class="btn ghost" type="button" data-close>关闭</button>`,
      onMount(root, close) {
        $$("[data-close]", root).forEach(b => b.onclick = close);
        $$("[data-restore]", root).forEach(b => b.onclick = async () => {
          try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(b.dataset.restore)}/restore`, { method: "POST" }); close(); await refreshAll(); toast("条目已恢复。"); }
          catch (e) { toast("恢复失败：" + e.message, "err"); }
        });
      }
    });
  }
  function openBulk() {
    if (!state.selected.size) return toast("请先勾选条目", "err");
    openModal({
      title: "批量权限", subtitle: `应用到 ${state.selected.size} 个条目`, small: true,
      body: `<div class="field"><label for="bulkPolicy">编辑策略</label><select id="bulkPolicy"><option value="inherit">继承</option><option value="open">开放</option><option value="whitelist">名单</option><option value="admin">仅人工</option><option value="off">冻结</option></select></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>应用</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        $("[data-ok]", root).onclick = async () => {
          try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/permissions`, { method: "PUT", body: JSON.stringify({ doc_ids: [...state.selected], permissions: { edit: $("#bulkPolicy").value } }) }); close(); state.selected.clear(); await refreshAll(); toast("批量策略已应用。"); }
          catch (e) { toast("应用失败：" + e.message, "err"); }
        };
      }
    });
  }

  /* ---------- versions / search ---------- */
  async function openVersionModal() {
    let data;
    try { data = await api("/embedding-models"); }
    catch (e) { return toast("加载模型失败：" + e.message, "err"); }
    const models = data.models || [];
    openModal({
      title: "新建版本", subtitle: "选择嵌入模型，探测维度后创建。", body: `<div class="field"><label for="versionModel">嵌入模型</label><select id="versionModel"><option value="">KiraAI 默认嵌入模型</option>${models.map(m => `<option value="${esc(m.uuid)}">${esc(m.label)}</option>`).join("")}</select><span class="hint">留空使用默认模型。</span></div><div class="field"><label for="versionName">版本标签</label><input id="versionName" placeholder="例如 bge-m3"></div><div class="field"><label for="versionDim">维度</label><input id="versionDim" type="number" min="1"><div class="display-actions"><button class="btn small ghost" type="button" id="probeBtn">探测</button><span class="hint" id="probeHint"></span></div></div><div class="field"><label>文档（留空 = 全部）</label><div class="check-list" id="versionDocs">${state.docs.map(d => `<label><input type="checkbox" value="${esc(d.doc_id)}"> ${esc(d.name)}</label>`).join("")}</div></div>`,
      footer: `<button class="btn ghost" type="button" data-cancel>取消</button><button class="btn primary" type="button" data-ok>创建版本</button>`,
      onMount(root, close) {
        $("[data-cancel]", root).onclick = close;
        $("#probeBtn", root).onclick = async () => {
          const uuid = $("#versionModel").value;
          $("#probeHint").textContent = "探测中……";
          try { const res = await api("/embedding-models/probe", { method: "POST", body: JSON.stringify({ model_uuid: uuid || null }), allowError: true }); if (res.error) throw new Error(res.error); $("#versionDim").value = res.dimension; $("#probeHint").textContent = `已探测 ${res.dimension} 维`; }
          catch (e) { $("#probeHint").textContent = "探测失败：" + e.message; }
        };
        $("[data-ok]", root).onclick = async () => {
          const name = $("#versionName").value.trim(); const dim = Number($("#versionDim").value);
          if (!name) return toast("请填写版本标签", "err");
          if (!dim) return toast("请先探测维度", "err");
          const payload = { model_name: name, dimension: dim };
          const uuid = $("#versionModel").value; if (uuid) payload.model_uuid = uuid;
          const ids = $$("#versionDocs input:checked", root).map(x => x.value); if (ids.length) payload.doc_ids = ids;
          try { const res = await api(`/kbs/${encodeURIComponent(state.current)}/versions`, { method: "POST", body: JSON.stringify(payload) }); close(); if (res.task_id) { toast("版本构建已排队。"); await loadTasks(); renderTasks(); switchView("tasks"); } else { await refreshAll(); toast("版本已创建。"); } }
          catch (e) { toast("创建失败：" + e.message, "err"); }
        };
      }
    });
  }
  async function runSearch() {
    const query = $("#searchInput").value.trim(); if (!query) return;
    $("#searchResults").innerHTML = `<div class="inspect-empty">检索中……</div>`;
    try {
      const res = await api(`/kbs/${encodeURIComponent(state.current)}/search`, { method: "POST", body: JSON.stringify({ query, top_k: 8 }), allowError: true });
      if (res.error) { $("#searchResults").innerHTML = `<div class="inspect-empty">${esc(res.error)}</div>`; return; }
      $("#searchResults").innerHTML = Array.isArray(res) && res.length ? res.map(r => `<div class="result"><div class="result-head"><span class="name">${esc(r.doc_name || r.doc_id)}</span><span class="score">${Number(r.score || 0).toFixed(2)}</span></div><div class="score-bar"><i style="width:${Math.round(Math.max(0, Math.min(1, Number(r.score) || 0)) * 100)}%"></i></div><p>${esc(r.content || "")}</p></div>`).join("") : `<div class="inspect-empty">没有找到相关内容。</div>`;
    } catch (e) { $("#searchResults").innerHTML = `<div class="inspect-empty">${esc(e.message)}</div>`; }
  }

  /* ---------- quote ---------- */
  const docCache = Object.create(null), contentCache = Object.create(null);
  function quoteKbIds() {
    const known = new Set(state.kbs.map(k => k.kb_id));
    const ids = (state.ui.quote_kb_ids || []).filter(id => known.has(id));
    return ids.length ? ids : [...known];
  }
  async function docsFor(kbId) {
    if (kbId === state.current && state.docs.length) return state.docs;
    if (docCache[kbId]) return docCache[kbId];
    const docs = await api(`/kbs/${encodeURIComponent(kbId)}/documents`);
    docCache[kbId] = Array.isArray(docs) ? docs.filter(d => !d.deleted) : [];
    return docCache[kbId];
  }
  async function contentFor(kbId, docId) {
    const key = kbId + "/" + docId;
    if (contentCache[key] != null) return contentCache[key];
    const doc = await api(`/kbs/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}`);
    contentCache[key] = doc?.content || "";
    return contentCache[key];
  }
  function splitSentences(text) { return String(text || "").split(/(?<=[。！？!?；;])\s*/).map(s => s.trim()).filter(Boolean); }
  function motto() { const custom = (state.ui.quote_custom_mottos || []).filter(Boolean); const pool = custom.length ? custom : MOTTOS; return pool[Math.floor(Math.random() * pool.length)]; }
  async function pickQuote() {
    const custom = (state.ui.quote_custom_lines || []).filter(Boolean);
    if (custom.length && Math.random() < .3) return { prefix: "自定", text: custom[Math.floor(Math.random() * custom.length)], source: "CUSTOM SCRIPT / 自定义台词" };
    const ids = quoteKbIds().sort(() => Math.random() - .5).slice(0, 4);
    for (const id of ids) {
      try {
        const docs = await docsFor(id); if (!docs.length) continue;
        const doc = docs[Math.floor(Math.random() * docs.length)];
        const content = await contentFor(id, doc.doc_id);
        const ss = splitSentences(content).filter(s => s.length >= 8 && s.length <= 150);
        if (!ss.length) continue;
        let text = ss[Math.floor(Math.random() * ss.length)];
        if (text.length > 110) text = text.slice(0, 108) + "……";
        const kb = state.kbs.find(k => k.kb_id === id);
        return { prefix: PREFIXES[Math.floor(Math.random() * PREFIXES.length)], text, source: `${kb?.display_name || id} // ${doc.name || doc.doc_id}` };
      } catch (_) { /* next */ }
    }
    return { prefix: "格言", text: motto(), source: "MOTTO / 未归档格言" };
  }
  function typeTicker(text) {
    const el = $("#tickerText"); if (!el) return;
    clearInterval(state.tickerTimer);
    if (reduced || state.ui.performance) { el.textContent = text; return; }
    el.textContent = ""; let i = 0;
    state.tickerTimer = setInterval(() => { el.textContent = text.slice(0, ++i); if (i >= text.length) clearInterval(state.tickerTimer); }, 18);
  }
  async function broadcastQuote(force = false) {
    if (state.quoteBusy) return; state.quoteBusy = true;
    try {
      const quote = await pickQuote();
      $("#tickerPrefix").textContent = quote.prefix; $("#tickerSource").textContent = quote.source;
      $("#tickerMode").textContent = state.ui.quote_enabled ? "AUTO" : "MANUAL";
      typeTicker(quote.text);
      if (window.KBTextFX) window.KBTextFX.burst($("#tickerText"), state.ui.text_fx);
      const card = $("#quoteCard");
      if (card) {
        card.classList.add("is-speaking"); setTimeout(() => card.classList.remove("is-speaking"), 1700);
        $("#heroQuoteIndex").textContent = `RECORD ${String((quote.source || "").length + (quote.text || "").length).slice(0, 3)} // ${quote.source.split(" // ")[0] || "ARCHIVE"}`;
        $("#heroSource").textContent = quote.source;
        if (window.KBTextFX) window.KBTextFX.playText($("#heroQuote"), quote.text, state.ui.text_fx);
        else $("#heroQuote").textContent = quote.text;
      }
    } finally { state.quoteBusy = false; }
  }
  function startQuoteTimer() {
    stopQuoteTimer(); if (!state.ui.quote_enabled) return;
    state.quoteTimer = setInterval(() => { if (!document.hidden) broadcastQuote(); }, Math.max(4, state.ui.quote_interval) * 1000);
  }
  function stopQuoteTimer() { if (state.quoteTimer) clearInterval(state.quoteTimer); state.quoteTimer = null; }

  /* ---------- UI events ---------- */
  function switchView(view) {
    if (!VIEW_META[view] || view === state.view) return;
    const screen = $("#screen"); screen.classList.add("is-cutting");
    setTimeout(() => {
      state.view = view;
      $$(".channel").forEach(b => b.classList.toggle("is-active", b.dataset.view === view));
      $$(".view").forEach(v => v.classList.toggle("is-active", v.dataset.view === view));
      $("#hudKicker").textContent = `CH ${VIEW_META[view][0]} // ${VIEW_META[view][1]}`;
      $("#consoleNumber").textContent = VIEW_META[view][0];
      $("#viewStack").scrollTop = 0;
      if (view === "settings") { loadSettings().then(renderSettings); syncUiControls(); }
      replayText();
      replayBrand();
      cycleBrandMode();
    }, 170);
    setTimeout(() => screen.classList.remove("is-cutting"), 380);
  }
  function bind() {
    document.addEventListener("click", async (e) => {
      const nav = e.target.closest(".channel[data-view]"); if (nav) return switchView(nav.dataset.view);
      const theme = e.target.closest("[data-theme-set]"); if (theme) return setTheme(theme.dataset.themeSet);
      const doc = e.target.closest("[data-doc]"); if (doc && !e.target.closest("input")) return selectDoc(doc.dataset.doc);
      if (e.target.closest("#selectAllDocs")) return;
      const check = e.target.closest("[data-check]");
      if (check) { if (check.checked) state.selected.add(check.dataset.check); else state.selected.delete(check.dataset.check); return; }
      const act = e.target.closest("[data-activate]"); if (act) { try { await api(`/kbs/${encodeURIComponent(state.current)}/versions/${encodeURIComponent(act.dataset.activate)}/activate`, { method: "POST" }); await refreshAll(); toast("版本已激活。"); } catch (err) { toast("失败：" + err.message, "err"); } return; }
      const sync = e.target.closest("[data-sync]"); if (sync) { try { const res = await api(`/kbs/${encodeURIComponent(state.current)}/versions/${encodeURIComponent(sync.dataset.sync)}/sync`, { method: "POST", body: "{}" }); if (res.task_id) { toast("同步已排队。"); await loadTasks(); renderTasks(); switchView("tasks"); } else toast("同步完成。"); } catch (err) { toast("同步失败：" + err.message, "err"); } return; }
      const delver = e.target.closest("[data-delver]"); if (delver) { if (await confirmDialog("删除版本", "删除这个索引？原始条目会保留，只移除向量。", "删除", true)) { try { await api(`/kbs/${encodeURIComponent(state.current)}/versions/${encodeURIComponent(delver.dataset.delver)}`, { method: "DELETE" }); await refreshAll(); toast("版本已删除。"); } catch (err) { toast("失败：" + err.message, "err"); } } return; }
      const saveRecall = e.target.closest("[data-save-recall]"); if (saveRecall) { const id = saveRecall.dataset.saveRecall; const sel = $(`[data-entry-recall="${CSS.escape(id)}"]`); const kw = $(`[data-entry-keywords="${CSS.escape(id)}"]`); try { await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(id)}/recall`, { method: "PUT", body: JSON.stringify({ enabled: sel.value === "on" ? true : sel.value === "off" ? false : null, keywords: kw.value.split(/[,，]/).map(s => s.trim()).filter(Boolean) }) }); toast("条目召回已保存。"); } catch (err) { toast("保存失败：" + err.message, "err"); } return; }
      const bgUpload = e.target.closest("[data-bg-upload]"); if (bgUpload) { bgUpload.closest(".bg-slot")?.querySelector("input[type=file]")?.click(); return; }
      const bgReset = e.target.closest("[data-bg-reset]"); if (bgReset) { delete state.backgrounds[bgReset.dataset.bgReset]; persistBackgrounds(); applyBackgrounds(); renderBgGrid(); return; }
      if (e.target.closest("#settingsBtn")) return switchView("settings");
      if (e.target.closest("#newKbBtn")) return openKbCreate();
      if (e.target.closest("#editKbBtn")) return openKbEdit();
      if (e.target.closest("#deleteKbBtn")) return deleteKb();
      if (e.target.closest("#uploadBtn") || e.target.closest("#heroUploadBtn") || e.target.closest("#docUploadBtn")) return triggerUpload();
      if (e.target.closest("#deletedBtn")) return openDeleted();
      if (e.target.closest("#bulkEditBtn")) return openBulk();
      if (e.target.closest("#editDocBtn")) return openDocEditor(currentDoc());
      if (e.target.closest("#historyBtn")) return openHistory(currentDoc());
      if (e.target.closest("#entrySettingsBtn")) return openEntrySettings(currentDoc());
      if (e.target.closest("#deleteDocBtn")) return deleteDoc(state.selectedDoc);
      if (e.target.closest("#newVersionBtn")) return openVersionModal();
      if (e.target.closest("#savePermBtn")) return saveKbPerms();
      if (e.target.closest("#saveKbRecallBtn")) return saveKbRecall();
      if (e.target.closest("#saveSettingsBtn")) return saveAllSettings();
      if (e.target.closest("#refreshTasksBtn")) { await loadTasks(); renderTasks(); return; }
      if (e.target.closest("#drawQuoteBtn") || e.target.closest("#tickerNext")) return broadcastQuote(true);
      if (e.target.closest("#quoteToggleBtn")) { state.ui.quote_enabled = !state.ui.quote_enabled; if (state.ui.quote_enabled) startQuoteTimer(); else stopQuoteTimer(); syncUiControls(); queueUiSave(); return; }
      if (e.target.closest("#quickCrt")) { applyCrt(state.ui.crt === "off" ? "high" : "off"); queueUiSave(); return; }
      if (e.target.closest("#searchBtn")) return runSearch();
    });
    $("#kbSelect")?.addEventListener("change", e => selectKb(e.target.value));
    $("#docFilter")?.addEventListener("input", e => { state.docFilter = e.target.value; renderCatalog(); });
    $("#docFilterKind")?.addEventListener("change", e => { state.docFilterKind = e.target.value; renderCatalog(); });
    $("#fileInput")?.addEventListener("change", e => uploadFiles(e.target.files));
    $("#searchInput")?.addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
    $("#selectAllDocs")?.addEventListener("change", e => { const list = filteredDocs(); if (e.target.checked) list.forEach(d => state.selected.add(d.doc_id)); else state.selected.clear(); renderDocList(); });
    document.addEventListener("change", async (e) => {
      const permId = e.target.dataset.entryEdit || e.target.dataset.entryDelete;
      if (permId) {
        const doc = state.docs.find(d => d.doc_id === permId);
        if (!doc) return;
        const edit = $(`[data-entry-edit="${CSS.escape(permId)}"]`)?.value || "inherit";
        const del = $(`[data-entry-delete="${CSS.escape(permId)}"]`)?.value || "inherit";
        try {
          await api(`/kbs/${encodeURIComponent(state.current)}/documents/${encodeURIComponent(permId)}/permissions`, {
            method: "PUT", body: JSON.stringify({ edit, delete: del, allow_users: doc.permissions?.allow_users || [] })
          });
          toast("条目权限已更新。");
        } catch (err) { toast("保存失败：" + err.message, "err"); }
        return;
      }
      if (e.target.matches("#setUiTheme, #setUiCrt, #setUiTextFx, #setUiPerf, #setUiSplash, #setUiFxLoop, #setUiFxInterval, #setUiQuoteEnabled, #setUiQuoteInterval")) {
        state.ui.theme = $("#setUiTheme").value; state.ui.crt = $("#setUiCrt").value;
        state.ui.performance = $("#setUiPerf").checked; state.ui.splash = $("#setUiSplash").checked;
        state.ui.text_fx = $("#setUiTextFx").value; state.ui.fx_loop = $("#setUiFxLoop").checked;
        state.ui.fx_interval = Number($("#setUiFxInterval").value) || 6;
        state.ui.quote_enabled = $("#setUiQuoteEnabled").checked; state.ui.quote_interval = Number($("#setUiQuoteInterval").value) || 9;
        applyUi(); if (state.ui.fx_loop && !state.ui.performance && !reduced) window.KBTextFX?.startLoop(state.ui.fx_interval); else window.KBTextFX?.stopLoop();
        queueUiSave();
      }
      if (e.target.matches("#setUiQuoteKbs input")) {
        const checked = $$("#setUiQuoteKbs input:checked").length;
        if (!checked) { e.target.checked = true; toast("至少保留一个字幕知识库", "err"); return; }
        state.ui.quote_kb_ids = selectedQuoteKbs(); renderQuoteKbs(); queueUiSave();
      }
      const bg = e.target.closest("input[data-bg-file]");
      if (bg && bg.files?.[0]) {
        try { const data = await resizeImage(bg.files[0]); state.backgrounds[bg.dataset.bgFile] = data; persistBackgrounds(); applyBackgrounds(); renderBgGrid(); toast("背景立绘已更新。"); }
        catch (err) { toast("上传失败：" + err.message, "err"); }
      }
    });
    document.addEventListener("input", e => {
      if (e.target.matches("#setUiQuoteLines, #setUiQuoteMottos")) {
        state.ui.quote_custom_lines = lines("#setUiQuoteLines"); state.ui.quote_custom_mottos = lines("#setUiQuoteMottos"); queueUiSave();
      }
      if (e.target.matches("#setUiFxInterval, #setUiQuoteInterval")) {
        state.ui.fx_interval = Number($("#setUiFxInterval").value) || 6; state.ui.quote_interval = Number($("#setUiQuoteInterval").value) || 9;
        startQuoteTimer(); queueUiSave();
      }
    });
    document.addEventListener("visibilitychange", () => { if (document.hidden) stopQuoteTimer(); else if (state.ui.quote_enabled) startQuoteTimer(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape") { const m = $(".modal-root"); if (m) m.remove(); hideBoot(); } });
  }
  function hideBoot() { const b = $("#boot"); if (!b) return; b.classList.add("is-off"); document.body.classList.remove("boot-hold"); setTimeout(() => b.style.display = "none", 600); }
  async function resizeImage(file, max = 1600) {
    if (!file.type.startsWith("image/")) throw new Error("不是图片");
    if (file.size > 8 * 1024 * 1024) throw new Error("图片超过 8MB");
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((resolve, reject) => { const i = new Image(); i.onload = () => resolve(i); i.onerror = () => reject(new Error("无法读取图片")); i.src = url; });
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas"); c.width = Math.max(1, Math.round(img.width * scale)); c.height = Math.max(1, Math.round(img.height * scale));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL("image/jpeg", .82);
    } finally { URL.revokeObjectURL(url); }
  }

  /* ---------- settings save ---------- */
  async function saveKbPerms() {
    try { await api(`/kbs/${encodeURIComponent(state.current)}/permissions`, { method: "PUT", body: JSON.stringify({ add: $("#policyAdd").value, edit: $("#policyEdit").value, delete: $("#policyDelete").value, allow_users: lines("#kbAllowUsers") }) }); await refreshAll(); toast("权限策略已保存。"); }
    catch (e) { toast("保存失败：" + e.message, "err"); }
  }
  async function saveKbRecall() {
    const num = id => { const el = $("#" + id); return el && el.value !== "" ? Number(el.value) : null; };
    try { await api(`/kbs/${encodeURIComponent(state.current)}/recall`, { method: "PUT", body: JSON.stringify({ enabled: $("#kbRecallEnabled").checked, top_k: num("kbRecallTopK"), min_score: num("kbRecallMinScore"), max_chars: num("kbRecallMaxChars") }) }); await refreshAll(); toast("召回设置已保存。"); }
    catch (e) { toast("保存失败：" + e.message, "err"); }
  }
  async function saveAllSettings() {
    try {
      const payload = collectFunctionalSettings(); payload.ui = collectUi();
      const res = await api("/settings", { method: "PUT", body: JSON.stringify(payload) });
      if (res.settings) state.settings = res.settings;
      state.ui = { ...state.ui, ...(res.settings?.ui || {}) };
      applyUi(); await refreshAll(); renderSettings();
      if (res.warnings?.length) toast(res.warnings.join(" "), "err"); else toast("设置已应用。");
    } catch (e) { toast("保存失败：" + e.message, "err"); }
  }

  /* ---------- init ---------- */
  async function init() {
    bind();
    renderThemeStrip();
    document.documentElement.setAttribute("data-theme", "dark");
    document.body.dataset.theme = "violet";
    document.body.dataset.crt = "high";
    applyBackgrounds();
    await loadSettings();
    if (state.settings?.ui) state.ui = { ...state.ui, ...state.settings.ui };
    state.ui.theme = THEMES.includes(state.ui.theme) ? state.ui.theme : "violet";
    state.ui.text_fx = ["mix", "radio", "pulse", "type", "decode", "scan", "glitch"].includes(state.ui.text_fx) ? state.ui.text_fx : "mix";
    applyUi();
    renderThemeStrip();
    renderSettings();
    syncUiControls();
    runBoot();
    try {
      await refreshAll();
      renderAll();
    } catch (e) { toast("加载失败：" + e.message, "err"); }
    initTextFx();
    startBrandModes();
    if (state.ui.quote_enabled) { broadcastQuote(true); startQuoteTimer(); }
    setInterval(() => { if (!document.hidden) { loadTasks().then(renderTasks).catch(() => {}); } }, 5000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
