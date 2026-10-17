/* KiraKB // CATHODE ARCHIVE — integration layer.
   Keeps the original WebUI actions/API calls, adds the machine skin,
   real KB-driven subtitles, text effects and local backdrop overrides. */
(() => {
  "use strict";
  const q = (sel, root = document) => root.querySelector(sel);
  const qa = (sel, root = document) => [...root.querySelectorAll(sel)];
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const THEMES = ["violet", "pink", "blue", "amber", "green", "red", "gray"];
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
    ["overview", "主厅 / OVERVIEW"], ["docs", "条目 / ENTRIES"],
    ["versions", "版本 / VERSIONS"], ["search", "检索 / SEARCH"],
    ["access", "权限 / ACCESS"], ["recall", "召回 / RECALL"],
    ["settings", "设置 / SETTINGS"], ["tasks", "任务 / TASKS"],
    ["ticker", "字幕框 / SUBTITLE"]
  ];
  const VIEW_LABELS = {
    overview: "CH 01 // OVERVIEW", docs: "CH 02 // ENTRIES",
    versions: "CH 03 // VERSIONS", search: "CH 04 // SEARCH",
    access: "CH 05 // ACCESS", recall: "CH 06 // RECALL",
    settings: "CH 07 // SETTINGS", tasks: "CH 08 // TASKS"
  };
  const DEFAULTS = {
    theme: "violet", crt: "high", performance: false, splash: true,
    text_fx: "mix", fx_loop: true, fx_interval: 6,
    quote_enabled: true, quote_interval: 9,
    quote_kb_ids: [], quote_custom_lines: [], quote_custom_mottos: []
  };

  let ui = { ...DEFAULTS };
  let bootReady = false;
  let textfxReady = false;
  let quoteTimer = null;
  let quoteBusy = false;
  let tickerTimer = null;
  let saveTimer = null;
  let currentQuote = null;
  const docCache = Object.create(null);
  const contentCache = Object.create(null);
  let backgrounds = loadBackgrounds();

  function loadBackgrounds() {
    try {
      const raw = localStorage.getItem("kirakb-cathode-bg-v1");
      return raw ? (JSON.parse(raw) || {}) : {};
    } catch (_) { return {}; }
  }
  function persistBackgrounds() {
    try { localStorage.setItem("kirakb-cathode-bg-v1", JSON.stringify(backgrounds)); }
    catch (_) { toast("背景图过大，未能写入本地存储", "err"); }
  }

  function ensureSplash() {
    if (q("#cathodeSplash")) return;
    document.body.insertAdjacentHTML("afterbegin", `
      <div class="cathode-splash" id="cathodeSplash" aria-hidden="true">
        <div class="cathode-splash-inner">
          <div class="cathode-splash-kicker">PROJECT // CATHODE ARCHIVE</div>
          <div class="cathode-splash-logo">KIRA<span>KB</span></div>
          <div class="cathode-splash-sub">KNOWLEDGE RETRIEVAL TERMINAL · MODEL 07</div>
          <div class="cathode-splash-bar"><i></i></div>
          <div class="cathode-splash-status"><span>SIGNAL LOCKED</span><b>100</b></div>
        </div>
        <button class="cathode-splash-skip" id="cathodeSplashSkip" type="button">跳过 / SKIP</button>
      </div>`);
    q("#cathodeSplashSkip")?.addEventListener("click", hideSplash, { once: true });
    if (reduced) hideSplash();
  }
  function showSplash() {
    ensureSplash();
    const el = q("#cathodeSplash");
    if (!el) return;
    el.classList.remove("is-off");
    clearTimeout(showSplash.t);
    showSplash.t = setTimeout(hideSplash, 2100);
  }
  function hideSplash() {
    const el = q("#cathodeSplash");
    if (!el) return;
    el.classList.add("is-off");
    clearTimeout(showSplash.t);
  }

  function ensureShell() {
    // Channel strip in the topbar.
    if (!q("#cathodeThemeStrip")) {
      const strip = document.createElement("div");
      strip.className = "cathode-theme-strip";
      strip.id = "cathodeThemeStrip";
      strip.setAttribute("role", "group");
      strip.setAttribute("aria-label", "配色主题");
      strip.innerHTML = THEMES.map(t =>
        `<button type="button" data-theme="${t}" class="${t === ui.theme ? "is-active" : ""}" aria-label="${t}"></button>`
      ).join("");
      const top = q(".topbar");
      const actions = q(".top-actions", top);
      if (top && actions) top.insertBefore(strip, actions);
      strip.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-theme]");
        if (btn) setTheme(btn.dataset.theme);
      });
    }
    if (!q("#cathodeHud")) {
      const hud = document.createElement("span");
      hud.className = "cathode-hud-label";
      hud.id = "cathodeHud";
      hud.textContent = VIEW_LABELS[state.view] || VIEW_LABELS.overview;
      const left = q(".topbar .left");
      if (left) left.appendChild(hud);
    }

    // Extraction console quote card.
    if (!q(".cathode-quote-card")) {
      const hero = q("#view-overview .hero");
      if (hero) {
        hero.insertAdjacentHTML("afterend", `
          <div class="cathode-quote-card" id="cathodeQuoteCard">
            <div class="cathode-quote-copy">
              <div class="cathode-quote-kicker" id="cathodeQuoteIndex">RECORD 000 // ARCHIVE</div>
              <div class="cathode-quote-text" id="cathodeQuoteText">正在从档案中抽取……</div>
              <div class="cathode-quote-source" id="cathodeQuoteSource">SOURCE // PENDING</div>
            </div>
            <div class="cathode-quote-side">
              <div class="cathode-quote-bars" aria-hidden="true">
                <i></i><i></i><i></i><i></i><i></i><i></i>
                <i></i><i></i><i></i><i></i><i></i><i></i>
              </div>
              <button type="button" class="btn ghost" id="cathodeQuoteNext">抽取一句</button>
            </div>
          </div>`);
        q("#cathodeQuoteNext")?.addEventListener("click", () => { broadcastQuote(true); });
      }
    }

    // Global subtitle rail.
    if (!q("#cathodeTicker")) {
      const main = q(".main");
      if (main) {
        main.insertAdjacentHTML("beforeend", `
          <footer class="cathode-ticker" id="cathodeTicker">
            <div class="cathode-ticker-side">
              <span class="rec"></span><b>NOW TRANSMITTING</b>
              <small id="cathodeTickerSource">ARCHIVE // PENDING</small>
            </div>
            <div class="cathode-ticker-line">
              <span class="cathode-ticker-prefix" id="cathodeTickerPrefix">记录</span>
              <span class="cathode-ticker-text" id="cathodeTickerText">正在接收信号……</span>
              <span class="cathode-caret" aria-hidden="true"></span>
            </div>
            <div class="cathode-ticker-tools">
              <button type="button" id="cathodeTickerNext" aria-label="下一条">›</button>
              <span id="cathodeTickerMode">AUTO</span>
            </div>
          </footer>`);
        q("#cathodeTickerNext")?.addEventListener("click", () => { broadcastQuote(true); });
      }
    }
  }

  function setTheme(theme) {
    if (!THEMES.includes(theme)) theme = "violet";
    ui.theme = theme;
    document.documentElement.setAttribute("data-theme", "dark");
    document.body.dataset.theme = theme;
    qa("#cathodeThemeStrip [data-theme]").forEach(b => b.classList.toggle("is-active", b.dataset.theme === theme));
    const select = q("#setUiTheme");
    if (select) select.value = theme;
    document.body.classList.add("cathode-theme-cut");
    setTimeout(() => document.body.classList.remove("cathode-theme-cut"), 460);
    queueSave();
  }

  function applyVisuals() {
    document.documentElement.setAttribute("data-theme", "dark");
    document.body.dataset.theme = THEMES.includes(ui.theme) ? ui.theme : "violet";
    document.body.dataset.crt = ["off", "low", "medium", "high"].includes(ui.crt) ? ui.crt : "high";
    document.body.classList.toggle("perf", !!ui.performance);
    qa("#cathodeThemeStrip [data-theme]").forEach(b => b.classList.toggle("is-active", b.dataset.theme === ui.theme));
    if (window.KBTextFX) {
      window.KBTextFX.setConfig({
        mode: ui.text_fx, idle: true, enabled: !ui.performance && !reduced,
        level: ui.performance ? 0 : 2, loop: ui.fx_loop, loopInterval: ui.fx_interval
      });
      if (!ui.performance && !reduced && ui.fx_loop) window.KBTextFX.startLoop(ui.fx_interval);
      else window.KBTextFX.stopLoop();
    }
    applyBackgrounds();
    syncControls();
    if (ui.quote_enabled) startQuoteTimer(); else stopQuoteTimer();
  }

  function queueSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveUi(false), 500);
  }
  async function saveUi(explicit = false) {
    try {
      const res = await api("/settings", {
        method: "PUT", body: JSON.stringify({ ui: { ...ui } })
      });
      if (res && res.settings) state.settings = res.settings;
      if (explicit) toast("机台设置已保存。");
    } catch (e) {
      if (explicit) toast("机台保存失败：" + e.message, "err");
    }
  }

  function syncControls() {
    const set = (id, v) => { const el = q("#" + id); if (el) el.value = v; };
    const chk = (id, v) => { const el = q("#" + id); if (el) el.checked = !!v; };
    set("setUiTheme", ui.theme); set("setUiCrt", ui.crt);
    chk("setUiPerf", ui.performance); chk("setUiSplash", ui.splash);
    set("setUiTextFx", ui.text_fx); chk("setUiFxLoop", ui.fx_loop);
    set("setUiFxInterval", ui.fx_interval);
    chk("setUiQuoteEnabled", ui.quote_enabled); set("setUiQuoteInterval", ui.quote_interval);
    const lines = q("#setUiQuoteLines"); if (lines) lines.value = (ui.quote_custom_lines || []).join("\n");
    const mottos = q("#setUiQuoteMottos"); if (mottos) mottos.value = (ui.quote_custom_mottos || []).join("\n");
    renderQuoteKbs();
  }

  function renderQuoteKbs() {
    const box = q("#setUiQuoteKbs");
    if (!box) return;
    if (!state.kbs || !state.kbs.length) {
      box.innerHTML = `<span class="faint" style="font-size:10px">等待知识库列表…</span>`;
      return;
    }
    const selected = new Set(ui.quote_kb_ids || []);
    const all = selected.size === 0;
    box.innerHTML = state.kbs.map(kb => {
      const on = all || selected.has(kb.kb_id);
      return `<label class="${on ? "is-on" : ""}"><input type="checkbox" value="${esc(kb.kb_id)}" ${on ? "checked" : ""}>${esc(kb.display_name || kb.kb_id)}</label>`;
    }).join("");
  }

  function applyBackgrounds() {
    const map = {
      overview: "#view-overview", docs: "#view-docs", versions: "#view-versions",
      search: "#view-search", access: "#view-access", recall: "#view-recall",
      settings: "#view-settings", tasks: "#view-tasks"
    };
    Object.entries(map).forEach(([key, sel]) => {
      const el = q(sel);
      if (!el) return;
      const img = backgrounds[key];
      el.style.setProperty("--cathode-bg", img ? `url("${img}")` : "none");
    });
    const ticker = q("#cathodeTicker");
    if (ticker) ticker.style.setProperty("--cathode-ticker-art", backgrounds.ticker ? `url("${backgrounds.ticker}")` : "none");
  }

  function renderBgGrid() {
    const grid = q("#cathodeBgGrid");
    if (!grid) return;
    grid.innerHTML = BG_SLOTS.map(([key, label]) => {
      const img = backgrounds[key];
      return `<div class="cathode-bg-slot" data-bg="${key}" style="background-image:${img ? "none" : "linear-gradient(135deg,rgba(181,140,255,.12),rgba(0,0,0,.35))"}">
        ${img ? `<img src="${img}" alt="">` : ""}
        <span>${label}</span>
        <div><button type="button" data-bg-upload="${key}">上传</button><button type="button" data-bg-reset="${key}">清除</button></div>
        <input type="file" accept="image/*" data-bg-file="${key}" hidden>
      </div>`;
    }).join("");
  }

  async function resizeImage(file, max = 1600) {
    if (!file.type.startsWith("image/")) throw new Error("不是图片");
    if (file.size > 8 * 1024 * 1024) throw new Error("图片超过 8MB");
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("无法读取图片"));
        image.src = url;
      });
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(img.width * scale));
      canvas.height = Math.max(1, Math.round(img.height * scale));
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      return canvas.toDataURL("image/jpeg", .82);
    } finally { URL.revokeObjectURL(url); }
  }

  async function loadUi() {
    try {
      const settings = await api("/settings");
      state.settings = settings;
      if (settings && settings.ui && typeof settings.ui === "object") {
        ui = { ...ui, ...settings.ui };
        ui.theme = THEMES.includes(ui.theme) ? ui.theme : "violet";
        ui.crt = ["off", "low", "medium", "high"].includes(ui.crt) ? ui.crt : "high";
        ui.text_fx = ["mix", "radio", "pulse", "type", "decode", "scan", "glitch"].includes(ui.text_fx) ? ui.text_fx : "mix";
        ui.quote_kb_ids = Array.isArray(ui.quote_kb_ids) ? ui.quote_kb_ids : [];
        ui.quote_custom_lines = Array.isArray(ui.quote_custom_lines) ? ui.quote_custom_lines : [];
        ui.quote_custom_mottos = Array.isArray(ui.quote_custom_mottos) ? ui.quote_custom_mottos : [];
      }
    } catch (_) { /* keep defaults; the page still works */ }
  }

  /* ---------- quote engine ---------- */
  function selectedKbIds() {
    const known = new Set((state.kbs || []).map(k => k.kb_id));
    const explicit = (ui.quote_kb_ids || []).filter(id => known.has(id));
    if (explicit.length) return explicit;
    return [...known];
  }
  async function docsFor(kbId) {
    if (kbId === state.current && Array.isArray(state.docs) && state.docs.length) return state.docs;
    if (docCache[kbId]) return docCache[kbId];
    const docs = await api(`/kbs/${encodeURIComponent(kbId)}/documents`);
    docCache[kbId] = Array.isArray(docs) ? docs.filter(d => !d.deleted) : [];
    return docCache[kbId];
  }
  async function contentFor(kbId, docId) {
    const key = kbId + "/" + docId;
    if (contentCache[key] != null) return contentCache[key];
    const doc = await api(`/kbs/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}`);
    contentCache[key] = doc && typeof doc.content === "string" ? doc.content : "";
    return contentCache[key];
  }
  function splitSentences(text) {
    return String(text || "").split(/(?<=[。！？!?；;])\s*/).map(s => s.trim()).filter(Boolean);
  }
  function motto() {
    const custom = (ui.quote_custom_mottos || []).filter(Boolean);
    const pool = custom.length ? custom : MOTTOS;
    return pool[Math.floor(Math.random() * pool.length)];
  }
  async function pickQuote() {
    const custom = (ui.quote_custom_lines || []).filter(Boolean);
    if (custom.length && Math.random() < .35) {
      return { prefix: "自定", text: custom[Math.floor(Math.random() * custom.length)], source: "CUSTOM SCRIPT / 自定义台词" };
    }
    const kbs = (state.kbs || []).filter(k => selectedKbIds().includes(k.kb_id));
    for (const kb of shuffle(kbs).slice(0, 4)) {
      try {
        const docs = await docsFor(kb.kb_id);
        if (!docs.length) continue;
        const doc = docs[Math.floor(Math.random() * docs.length)];
        const content = await contentFor(kb.kb_id, doc.doc_id);
        const sentences = splitSentences(content).filter(s => s.length >= 8 && s.length <= 150);
        if (!sentences.length) continue;
        let text = sentences[Math.floor(Math.random() * sentences.length)];
        if (text.length > 110) text = text.slice(0, 108) + "……";
        return {
          prefix: PREFIXES[Math.floor(Math.random() * PREFIXES.length)],
          text,
          source: `${kb.display_name || kb.kb_id} // ${doc.name || doc.doc_id}`
        };
      } catch (_) { /* try next KB */ }
    }
    return { prefix: "格言", text: motto(), source: "MOTTO / 未归档格言" };
  }
  function shuffle(list) {
    const a = list.slice();
    for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
    return a;
  }
  function typeTicker(text) {
    const el = q("#cathodeTickerText");
    if (!el) return;
    clearInterval(tickerTimer);
    if (reduced || ui.performance) { el.textContent = text; return; }
    el.textContent = "";
    let i = 0;
    tickerTimer = setInterval(() => {
      el.textContent = text.slice(0, ++i);
      if (i >= text.length) clearInterval(tickerTimer);
    }, 18);
  }
  async function broadcastQuote(force = false) {
    if (quoteBusy && !force) return;
    if (quoteBusy) return;
    quoteBusy = true;
    try {
      const quote = await pickQuote();
      currentQuote = quote;
      q("#cathodeTickerPrefix").textContent = quote.prefix || "记录";
      q("#cathodeTickerSource").textContent = quote.source || "ARCHIVE";
      q("#cathodeTickerMode").textContent = ui.quote_enabled ? "AUTO" : "MANUAL";
      typeTicker(quote.text);
      if (window.KBTextFX) window.KBTextFX.burst(q("#cathodeTickerText"), ui.text_fx);
      const card = q("#cathodeQuoteCard");
      if (card) {
        card.classList.add("is-speaking");
        setTimeout(() => card.classList.remove("is-speaking"), 1700);
        q("#cathodeQuoteIndex").textContent = `RECORD ${String((quote.source || "").length + (quote.text || "").length).slice(0,3)} // ${quote.source.split(" // ")[0] || "ARCHIVE"}`;
        q("#cathodeQuoteSource").textContent = quote.source || "ARCHIVE";
        if (window.KBTextFX) window.KBTextFX.playText(q("#cathodeQuoteText"), quote.text, ui.text_fx);
        else q("#cathodeQuoteText").textContent = quote.text;
      }
    } finally { quoteBusy = false; }
  }
  function startQuoteTimer() {
    stopQuoteTimer();
    if (!ui.quote_enabled) return;
    quoteTimer = setInterval(() => { if (!document.hidden) broadcastQuote(); }, Math.max(4, ui.quote_interval) * 1000);
  }
  function stopQuoteTimer() {
    if (quoteTimer) clearInterval(quoteTimer);
    quoteTimer = null;
  }

  /* ---------- UI controls ---------- */
  function renderSkinControls() {
    syncControls();
    renderBgGrid();
  }
  function readUiControls() {
    const num = (id, fallback) => {
      const el = q("#" + id); const v = el ? Number(el.value) : NaN;
      return Number.isFinite(v) ? v : fallback;
    };
    const lines = (q("#setUiQuoteLines")?.value || "").split("\n").map(s => s.trim()).filter(Boolean);
    const mottos = (q("#setUiQuoteMottos")?.value || "").split("\n").map(s => s.trim()).filter(Boolean);
    const ids = [...qa("#setUiQuoteKbs input:checked")].map(x => x.value);
    const all = (state.kbs || []).length > 0 && ids.length === (state.kbs || []).length;
    ui = {
      ...ui,
      theme: q("#setUiTheme")?.value || ui.theme,
      crt: q("#setUiCrt")?.value || ui.crt,
      performance: !!q("#setUiPerf")?.checked,
      splash: !!q("#setUiSplash")?.checked,
      text_fx: q("#setUiTextFx")?.value || ui.text_fx,
      fx_loop: !!q("#setUiFxLoop")?.checked,
      fx_interval: Math.max(4, Math.min(15, num("setUiFxInterval", ui.fx_interval))),
      quote_enabled: !!q("#setUiQuoteEnabled")?.checked,
      quote_interval: Math.max(4, Math.min(30, num("setUiQuoteInterval", ui.quote_interval))),
      quote_kb_ids: all ? [] : ids,
      quote_custom_lines: lines,
      quote_custom_mottos: mottos
    };
  }
  function bindControls() {
    document.addEventListener("change", (event) => {
      const target = event.target;
      if (target.matches("#setUiTheme, #setUiCrt, #setUiPerf, #setUiSplash, #setUiTextFx, #setUiFxLoop, #setUiFxInterval, #setUiQuoteEnabled, #setUiQuoteInterval")) {
        if (target.id === "setUiTheme") { ui.theme = target.value; applyVisuals(); }
        else if (target.id === "setUiCrt") { ui.crt = target.value; applyVisuals(); }
        else { readUiControls(); applyVisuals(); }
        queueSave();
      }
      if (target.matches("#setUiQuoteKbs input")) {
        const checked = qa("#setUiQuoteKbs input:checked").length;
        if (checked === 0) {
          target.checked = true;
          toast("至少保留一个字幕知识库", "err");
          return;
        }
        readUiControls(); renderQuoteKbs(); queueSave();
      }
    });
    document.addEventListener("input", (event) => {
      if (event.target.matches("#setUiQuoteLines, #setUiQuoteMottos, #setUiFxInterval, #setUiQuoteInterval")) {
        readUiControls(); queueSave();
      }
    });
    document.addEventListener("click", (event) => {
      const save = event.target.closest("#cathodeSaveUi");
      if (save) { readUiControls(); applyVisuals(); saveUi(true); return; }
      const nav = event.target.closest('[data-action="nav"]');
      if (nav) {
        setTimeout(() => {
          const hud = q("#cathodeHud");
          if (hud) hud.textContent = VIEW_LABELS[nav.dataset.view] || hud.textContent;
          if (nav.dataset.view === "settings") renderSkinControls();
          if (window.KBTextFX) window.KBTextFX.replayView(nav.dataset.view);
        }, 210);
      }
      const bgUpload = event.target.closest("[data-bg-upload]");
      if (bgUpload) {
        const slot = bgUpload.closest("[data-bg]");
        slot?.querySelector("input[type=file]")?.click();
        return;
      }
      const bgReset = event.target.closest("[data-bg-reset]");
      if (bgReset) {
        delete backgrounds[bgReset.dataset.bgReset];
        persistBackgrounds(); applyBackgrounds(); renderBgGrid();
        toast("已恢复默认背景");
        return;
      }
    });
    document.addEventListener("change", async (event) => {
      const file = event.target.closest("input[data-bg-file]");
      if (!file || !file.files?.[0]) return;
      try {
        const data = await resizeImage(file.files[0]);
        backgrounds[file.dataset.bgFile] = data;
        persistBackgrounds(); applyBackgrounds(); renderBgGrid();
        toast("背景立绘已更新");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    });
  }

  /* ---------- boot / lifecycle ---------- */
  async function waitForState() {
    for (let i = 0; i < 30; i++) {
      if (state.kbs && state.kbs.length) return;
      await sleep(200);
    }
  }
  async function init() {
    ensureShell();
    ensureSplash();
    bindControls();
    await loadUi();
    // Old theme bootstrap runs before this script; force the cathode base.
    document.documentElement.setAttribute("data-theme", "dark");
    applyVisuals();
    applyTitles();
    if (window.KBTextFX) {
      window.KBTextFX.setConfig({
        mode: ui.text_fx, idle: true, enabled: !ui.performance && !reduced,
        level: ui.performance ? 0 : 2, loop: ui.fx_loop, loopInterval: ui.fx_interval
      });
      window.KBTextFX.init();
    }
    await waitForState();
    renderSkinControls();
    if (ui.splash && !new URLSearchParams(location.search).has("skipboot")) showSplash();
    else hideSplash();
    if (window.KBTextFX) window.KBTextFX.replayView(state.view || "overview");
    broadcastQuote(true);
    if (ui.quote_enabled) startQuoteTimer();
    bootReady = true;
  }

  const VIEW_TITLES = {
    overview: "记忆不是档案。<br><em>但档案会记得。</em>",
    docs: "被写下的，<em>才会留下。</em>",
    versions: "同一段记忆，<em>不同的折射率。</em>",
    search: "不要等它发问，<em>先检索。</em>",
    access: "不是所有人，<em>都能修改过去。</em>",
    recall: "记忆会先开口。",
    settings: "调到你喜欢的<em>那一道噪点。</em>",
    tasks: "后台正在整理，<em>请不要断电。</em>"
  };
  const VIEW_FX = {
    overview: "decode", docs: "scan", versions: "glitch", search: "decode",
    access: "radio", recall: "type", settings: "pulse", tasks: "scan"
  };
  function applyTitles() {
    const search = q("#view-search");
    if (search && !search.querySelector(".hero h1")) {
      search.insertAdjacentHTML("afterbegin", `
        <div class="hero" style="padding-bottom:4px">
          <div class="eyebrow">ARCHIVE / RETRIEVAL</div>
          <h1>不要等它发问，<em>先检索。</em></h1>
          <p>从词法到语义，找到那条被遗忘的记录。</p>
        </div>`);
    }
    Object.entries(VIEW_TITLES).forEach(([view, html]) => {
      const h = q(`#view-${view} .hero h1`);
      if (!h) return;
      h._fxOriginal = null;
      h.innerHTML = html;
      h.dataset.cathodeTitle = view;
      h.dataset.fx = VIEW_FX[view] || "mix";
    });
    const desc = q("#view-overview #heroDesc");
    if (desc) {
      desc.textContent = "从已建立的知识库中抽取、编排、复述。这里的每一行字幕都来自真实条目，而不是装饰性的空话。";
      desc.dataset.cathodeDesc = "1";
    }
  }
  // Keep the existing render pipeline, then reapply the skin to newly rendered nodes.
  function afterRender() {
    renderQuoteKbs();
    applyTitles();
    if (window.KBTextFX) {
      qa("#view-overview .hero h1, #view-docs .hero h1, #view-versions .hero h1, #view-search .hero h1, #view-access .hero h1, #view-recall .hero h1, #view-settings .hero h1, #view-tasks .hero h1").forEach(h => {
        if (!h.dataset.fx) h.dataset.fx = VIEW_FX[(h.closest(".view")?.id || "").replace("view-", "")] || "mix";
      });
      window.KBTextFX.replayView(state.view || "overview");
    }
  }
  const originalRenderAll = window.renderAll;
  if (typeof originalRenderAll === "function") {
    window.renderAll = function () {
      const result = originalRenderAll.apply(this, arguments);
      setTimeout(afterRender, 40);
      return result;
    };
  }
  const originalRefresh = window.refresh;
  if (typeof originalRefresh === "function") {
    window.refresh = async function () {
      const result = await originalRefresh.apply(this, arguments);
      setTimeout(afterRender, 60);
      return result;
    };
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopQuoteTimer();
    else if (ui.quote_enabled) startQuoteTimer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSplash();
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
