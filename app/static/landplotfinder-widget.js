(function () {
  "use strict";

  const script = document.currentScript;
  if (!script) return;

  const config = {
    target: script.dataset.target || "landplotfinder-widget",
    apiBase: (script.dataset.apiBase || new URL(script.src).origin).replace(/\/$/, ""),
    profileId: script.dataset.profileId || "",
    company: script.dataset.companyName || "ЛИДЕР СТРОЙ",
    accent: script.dataset.accent || "#ffb434",
    privacyUrl: script.dataset.privacyUrl || "#",
    maxPrice: script.dataset.maxPrice || "70000",
    minArea: script.dataset.minArea || "6",
    maxArea: script.dataset.maxArea || "20",
    maxDistance: script.dataset.maxDistance || "50",
  };
  const storageKey = `lpf-widget-token:${config.apiBase}`;
  const mount = document.getElementById(config.target) || createMount(script);
  const root = mount.shadowRoot || mount.attachShadow({ mode: "open" });
  let token = localStorage.getItem(storageKey) || sessionStorage.getItem(storageKey) || "";
  let currentPhone = "";
  let lastSearch = {};
  let lastPreview = { total: 0, preview_cards: 0 };

  if (token) {
    localStorage.setItem(storageKey, token);
    sessionStorage.removeItem(storageKey);
  }

  root.innerHTML = `<style>${styles()}</style><section class="lpf-shell"><div id="lpf-app"></div></section>`;
  const app = root.getElementById("lpf-app");
  renderSearch();
  if (token) {
    runSearch();
  } else {
    setStatus("Задайте параметры и нажмите «Показать варианты».", "ready");
  }

  function createMount(anchor) {
    const node = document.createElement("div");
    node.id = config.target;
    anchor.parentNode.insertBefore(node, anchor);
    return node;
  }

  function renderSearch() {
    app.innerHTML = `
      <header class="lpf-header">
        <div><div class="lpf-kicker">Каталог участков</div><h2>Подберите участок под ваш дом</h2></div>
        <div class="lpf-brand">${html(config.company)}</div>
      </header>
      <form id="search-form" class="lpf-filters">
        <label class="lpf-wide">Место или направление<input name="q" placeholder="Например, Логойское направление"></label>
        <label>Бюджет до, $<input name="max_price_usd" type="number" min="1" value="${attr(config.maxPrice)}"></label>
        <label>Площадь от, сот.<input name="min_area_sotok" type="number" min="1" step="0.1" value="${attr(config.minArea)}"></label>
        <label>Площадь до, сот.<input name="max_area_sotok" type="number" min="1" step="0.1" value="${attr(config.maxArea)}"></label>
        <label>До МКАД, км<input name="max_distance_km" type="number" min="0" value="${attr(config.maxDistance)}"></label>
        <label class="lpf-check"><input name="electricity" type="checkbox"><span>Есть электричество</span></label>
        <label class="lpf-check"><input name="gas" type="checkbox"><span>Есть газ</span></label>
        <button type="submit">Показать варианты</button>
      </form>
      <div id="search-status" class="lpf-status" aria-live="polite"></div>
      <div id="search-results"></div>
      <div class="lpf-bottom"><span>База обновляется автоматически</span><button id="logout" class="lpf-link" type="button" ${token ? "" : "hidden"}>Сменить телефон</button></div>`;
    restoreSearchForm();
    app.querySelector("#search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch();
    });
    app.querySelector("#logout").addEventListener("click", () => {
      clearToken();
      lastSearch = {};
      renderSearch();
      setStatus("Номер удалён с этого устройства. Задайте параметры для нового поиска.", "ready");
    });
  }

  function searchParams() {
    const form = app.querySelector("#search-form");
    const formData = new FormData(form);
    const params = new URLSearchParams();
    ["q", "max_price_usd", "min_area_sotok", "max_area_sotok", "max_distance_km"].forEach((key) => {
      const value = formData.get(key);
      if (value !== null && value.toString().trim()) params.set(key, value.toString().trim());
    });
    if (form.elements.electricity.checked) params.set("electricity", "true");
    if (form.elements.gas.checked) params.set("gas", "true");
    if (config.profileId) params.set("profile_id", config.profileId);
    lastSearch = Object.fromEntries(params.entries());
    return params;
  }

  function restoreSearchForm() {
    if (!Object.keys(lastSearch).length) return;
    const form = app.querySelector("#search-form");
    Object.entries(lastSearch).forEach(([key, value]) => {
      if (!form.elements[key]) return;
      if (form.elements[key].type === "checkbox") {
        form.elements[key].checked = value === "true";
      } else {
        form.elements[key].value = value;
      }
    });
  }

  async function runSearch() {
    const params = searchParams();
    setStatus("Ищем подходящие варианты…", "loading");
    app.querySelector("#search-results").innerHTML = "";
    if (!token) {
      await runPreview(params);
      return;
    }
    params.set("limit", "12");
    try {
      const payload = await api(`/api/public/widget/listings?${params.toString()}`);
      renderResults(payload);
    } catch (error) {
      if (error.status === 401) {
        clearToken();
        await runPreview(searchParams(), "Сессия закончилась. Подтвердите номер ещё раз.");
        return;
      }
      setStatus(error.message, "error");
    }
  }

  async function runPreview(params, message = "") {
    try {
      lastPreview = await api(`/api/public/widget/preview?${params.toString()}`, { public: true });
      if (!lastPreview.total) {
        setStatus("Под эти параметры пока ничего нет. Попробуйте немного расширить поиск.", "empty");
        return;
      }
      setStatus(`Нашли ${lastPreview.total}. Подтвердите телефон, чтобы открыть варианты.`, "ready");
      renderPhoneGate(message);
    } catch (error) {
      setStatus(error.message, "error");
    }
  }

  function renderPhoneGate(message = "") {
    renderBlurredCards();
    const gate = app.querySelector("#auth-gate");
    gate.innerHTML = `
      <div class="lpf-kicker">Результаты готовы</div>
      <h3>Откройте найденные участки</h3>
      <p>Подтвердите телефон — это защищает каталог от автоматических запросов.</p>
      ${notice(message)}
      <form id="phone-form" class="lpf-gate-form">
        <label>Номер телефона<input name="phone" type="tel" autocomplete="tel" placeholder="+375 29 000-00-00" required></label>
        <label class="lpf-consent"><input name="consent" type="checkbox" required><span>Соглашаюсь на обработку контактных данных и обратную связь. <a href="${safeUrl(config.privacyUrl)}" target="_blank" rel="noopener">Условия</a></span></label>
        <button type="submit">Получить код</button>
      </form>
      <small>После подтверждения повторно вводить номер на этом устройстве не придётся 30 дней.</small>`;
    gate.querySelector("#phone-form").addEventListener("submit", requestCode);
  }

  function renderBlurredCards() {
    const results = app.querySelector("#search-results");
    const count = Math.max(1, lastPreview.preview_cards || 0);
    results.className = "lpf-preview";
    results.innerHTML = `
      <div class="lpf-grid lpf-blurred" aria-hidden="true">
        ${Array.from({ length: count }, () => blurredCard()).join("")}
      </div>
      <div class="lpf-gate" id="auth-gate"></div>`;
  }

  function blurredCard() {
    return `<article class="lpf-card lpf-skeleton">
      <div class="lpf-cardtop"><span class="lpf-source">KUFAR</span><span class="lpf-score">90 / 100</span></div>
      <h3>Подходящий участок в выбранном направлении</h3>
      <p class="lpf-location">Минская область, населённый пункт</p>
      <div class="lpf-facts"><div><span>Цена</span><strong>$00 000</strong></div><div><span>Площадь</span><strong>00 сот.</strong></div><div><span>До МКАД</span><strong>00 км</strong></div></div>
      <div class="lpf-utils"><span>✓ Коммуникации</span></div>
      <div class="lpf-actions"><a>Открыть объявление</a><button type="button">Мне подходит</button></div>
    </article>`;
  }

  async function requestCode(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    currentPhone = new FormData(form).get("phone").toString().trim();
    setBusy(button, true, "Отправляем…");
    try {
      const payload = await api("/api/public/widget/auth/request-code", {
        method: "POST",
        public: true,
        body: {
          phone: currentPhone,
          consent: form.elements.consent.checked,
          source_page: window.location.href,
          utm: utmParams(),
        },
      });
      renderCodeGate(payload);
    } catch (error) {
      renderPhoneGate(error.message);
    }
  }

  function renderCodeGate(payload, message = "") {
    renderBlurredCards();
    const gate = app.querySelector("#auth-gate");
    gate.innerHTML = `
      <button id="back-phone" class="lpf-back" type="button">← Изменить номер</button>
      <div class="lpf-kicker">Подтверждение</div>
      <h3>Введите код из SMS</h3>
      <p>Отправили шестизначный код на ${html(payload.phone || currentPhone)}.</p>
      ${payload.demo_code ? `<div class="lpf-demo">Демо-код: <strong>${html(payload.demo_code)}</strong></div>` : ""}
      ${notice(message)}
      <form id="code-form" class="lpf-gate-form">
        <label>Код<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" required></label>
        <button type="submit">Открыть результаты</button>
      </form>`;
    gate.querySelector("#back-phone").addEventListener("click", () => renderPhoneGate());
    gate.querySelector("#code-form").addEventListener("submit", (event) => verifyCode(event, payload));
    gate.querySelector("input[name=code]").focus();
  }

  async function verifyCode(event, requestPayload) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    setBusy(button, true, "Проверяем…");
    try {
      const payload = await api("/api/public/widget/auth/verify-code", {
        method: "POST",
        public: true,
        body: { phone: currentPhone, code: new FormData(form).get("code") },
      });
      token = payload.token;
      localStorage.setItem(storageKey, token);
      app.querySelector("#logout").hidden = false;
      await runSearch();
    } catch (error) {
      renderCodeGate(requestPayload, error.message);
    }
  }

  function renderResults(payload) {
    const results = app.querySelector("#search-results");
    const items = payload.items || [];
    results.className = "lpf-grid";
    results.innerHTML = "";
    if (!items.length) {
      setStatus("Под эти параметры пока ничего нет. Попробуйте немного расширить поиск.", "empty");
      return;
    }
    setStatus(`Найдено ${payload.total}. Показываем лучшие варианты:`, "ready");
    items.forEach((item) => results.appendChild(listingCard(item)));
  }

  function listingCard(item) {
    const card = document.createElement("article");
    card.className = "lpf-card";
    card.innerHTML = `
      <div class="lpf-cardtop"><span class="lpf-source">${html(sourceName(item.source))}</span><span class="lpf-score">${html(item.score)} / 100</span></div>
      <h3 title="${attr(item.title)}">${html(item.title)}</h3>
      <p class="lpf-location" title="${attr(item.location || "Расположение уточняется")}">${html(item.location || "Расположение уточняется")}</p>
      <div class="lpf-facts">
        <div><span>Цена</span><strong>${item.price_usd == null ? "—" : `$${number(item.price_usd)}`}</strong></div>
        <div><span>Площадь</span><strong>${item.area_sotok == null ? "—" : `${number(item.area_sotok)} сот.`}</strong></div>
        <div><span>До МКАД</span><strong>${item.distance_mkad_km == null ? "—" : `${number(item.distance_mkad_km)} км`}</strong></div>
      </div>
      <div class="lpf-utils">${utility(item.electricity_kw ? `${number(item.electricity_kw)} кВт` : item.electricity, "Электричество")}${utility(item.gas, "Газ")}</div>
      <div class="lpf-actions"><a href="${safeUrl(item.url)}" target="_blank" rel="noopener">Открыть объявление</a><button type="button" data-listing-id="${attr(item.id)}">Мне подходит</button></div>`;
    card.querySelector("button").addEventListener("click", () => showInterestDialog(item));
    return card;
  }

  function showInterestDialog(item) {
    closeDialog();
    const dialog = document.createElement("div");
    dialog.id = "lpf-dialog";
    dialog.className = "lpf-dialog-layer";
    dialog.innerHTML = `<div class="lpf-dialog" role="dialog" aria-modal="true" aria-labelledby="interest-title">
      <button class="lpf-dialog-close" type="button" aria-label="Закрыть">×</button>
      <div class="lpf-kicker">Понравился участок</div>
      <h3 id="interest-title">Передать вариант специалисту?</h3>
      <p>${html(item.title)}</p>
      <form id="interest-form" class="lpf-gate-form">
        <label>Как к вам обращаться? <span>необязательно</span><input name="name" maxlength="80" autocomplete="name" placeholder="Например, Илья"></label>
        <button type="submit">Да, связаться со мной</button>
      </form>
      <small>Специалист увидит выбранный участок и параметры вашего поиска.</small>
    </div>`;
    root.appendChild(dialog);
    dialog.querySelector(".lpf-dialog-close").addEventListener("click", closeDialog);
    dialog.querySelector("#interest-form").addEventListener("submit", (event) => saveInterest(event, item));
    dialog.querySelector("input[name=name]").focus();
  }

  async function saveInterest(event, item) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    setBusy(button, true, "Сохраняем…");
    try {
      await api("/api/public/widget/interests", {
        method: "POST",
        body: {
          listing_id: item.id,
          name: new FormData(form).get("name"),
          search_params: lastSearch,
          source_page: window.location.href,
        },
      });
      const cardButton = app.querySelector(`[data-listing-id="${item.id}"]`);
      cardButton.textContent = "✓ Вы выбрали этот участок";
      cardButton.classList.add("selected");
      cardButton.disabled = true;
      const dialog = root.getElementById("lpf-dialog");
      dialog.querySelector(".lpf-dialog").innerHTML = `
        <div class="lpf-success-icon">✓</div>
        <h3>Вариант передан специалисту</h3>
        <p>Мы сохранили участок и параметры поиска. С вами смогут связаться и помочь оценить его под строительство.</p>
        <button id="dialog-done" type="button">Вернуться к вариантам</button>`;
      dialog.querySelector("#dialog-done").addEventListener("click", closeDialog);
    } catch (error) {
      setBusy(button, false, "Да, связаться со мной");
      setStatus(error.message, "error");
    }
  }

  function closeDialog() {
    root.getElementById("lpf-dialog")?.remove();
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token && !options.public) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`${config.apiBase}${path}`, {
      method: options.method || "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) {
      const error = new Error(payload.detail || "Не удалось выполнить запрос. Попробуйте ещё раз.");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function clearToken() {
    token = "";
    localStorage.removeItem(storageKey);
    sessionStorage.removeItem(storageKey);
  }

  function setStatus(text, kind) {
    const node = app.querySelector("#search-status");
    if (!node) return;
    node.className = `lpf-status ${kind || ""}`;
    node.textContent = text;
  }

  function setBusy(button, busy, label) {
    button.disabled = busy;
    button.textContent = label;
  }

  function notice(message) {
    return message ? `<div class="lpf-notice">${html(message)}</div>` : "";
  }

  function utility(value, label) {
    return value ? `<span>✓ ${html(label)}: ${html(value)}</span>` : "";
  }

  function utmParams() {
    const result = {};
    new URLSearchParams(window.location.search).forEach((value, key) => {
      if (key.startsWith("utm_")) result[key] = value;
    });
    return result;
  }

  function sourceName(value) {
    return ({ kufar: "Kufar", realt: "Realt", rlt_auction: "RLT", e_auction: "e-auction" })[value] || value || "Источник";
  }

  function number(value) {
    return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value);
  }

  function html(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function attr(value) {
    return html(value);
  }

  function safeUrl(value) {
    if (value === "#") return "#";
    try {
      const parsed = new URL(value, window.location.href);
      return ["http:", "https:"].includes(parsed.protocol) ? attr(parsed.href) : "#";
    } catch (_) {
      return "#";
    }
  }

  function styles() {
    return `
      :host{--accent:${config.accent};--ink:#121212;--muted:#6c6c69;--paper:#f4f4f1;display:block;color:var(--ink);font-family:Inter,Arial,sans-serif}
      *{box-sizing:border-box}.lpf-shell{position:relative;background:var(--paper);border:1px solid #deded9;border-radius:28px;box-shadow:0 24px 70px rgba(0,0,0,.12);overflow:hidden}.lpf-kicker{text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:800;color:#a26300;margin-bottom:12px}
      h2{font-size:clamp(30px,5vw,48px);font-weight:650;line-height:1.04;letter-spacing:-.035em;text-transform:uppercase;margin:0 0 17px}h3{font-size:22px;font-weight:750;line-height:1.12;margin:15px 0 8px}
      input{width:100%;height:52px;border:1px solid #d4d4cf;border-radius:12px;background:#fff;color:var(--ink);font:inherit;font-size:15px;padding:0 15px;outline:none}input:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}
      button,.lpf-actions a{border:0;border-radius:999px;min-height:48px;padding:12px 20px;font:inherit;font-size:13px;font-weight:750;cursor:pointer;text-decoration:none;text-align:center;display:inline-flex;align-items:center;justify-content:center;transition:transform .18s ease,filter .18s ease}button{background:var(--accent);color:#171717}button:hover,.lpf-actions a:hover{filter:brightness(.96);transform:translateY(-1px)}button:disabled{cursor:default;opacity:.76;transform:none}
      .lpf-header{padding:38px 38px 28px;display:flex;align-items:start;justify-content:space-between;gap:24px;background:#fff;border-bottom:1px solid #deded9}.lpf-header h2{font-size:clamp(28px,4vw,42px);margin:0;max-width:760px}.lpf-brand{font-size:12px;font-weight:850;background:#171717;color:#fff;border-radius:8px;padding:12px 15px;white-space:nowrap;letter-spacing:.04em}.lpf-brand:before{content:"▰";color:var(--accent);margin-right:7px}
      .lpf-filters{display:grid;grid-template-columns:2fr repeat(4,1fr);gap:14px;padding:26px 38px;background:#1b1b1b;color:#fff;border-bottom:1px solid #303030}.lpf-filters label,.lpf-gate-form label{display:grid;gap:7px;font-size:12px;font-weight:650;color:inherit}.lpf-filters button{grid-column:5}.lpf-check{display:flex!important;flex-direction:row;align-items:center;gap:8px!important;font-weight:600!important;color:#d3d3cf!important}.lpf-check input,.lpf-consent input{width:18px;height:18px;accent-color:var(--accent);margin:1px 0}
      .lpf-status{margin:24px 38px 0;color:var(--muted);font-size:13px}.lpf-status.error{color:#a12f2a}.lpf-status.success{color:#493800;background:#fff1be;border-left:4px solid var(--accent);padding:13px 15px;border-radius:8px}
      .lpf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-rows:1fr;align-items:stretch;gap:18px;padding:20px 38px 38px}.lpf-card{display:flex;flex-direction:column;height:100%;min-height:398px;background:#fff;border:1px solid #dadad5;border-radius:24px;padding:21px;min-width:0}.lpf-cardtop{display:flex;justify-content:space-between;gap:12px}.lpf-source{font-size:10px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:#a26300}.lpf-score{font-size:11px;font-weight:800;background:#171717;color:#fff;padding:6px 9px;border-radius:999px;white-space:nowrap}.lpf-card h3{height:50px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;margin-bottom:8px}.lpf-location{height:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:13px;margin:0 0 17px}.lpf-facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;border-block:1px solid #e8e8e3;padding:15px 0}.lpf-facts span{display:block;color:#898985;font-size:10px;margin-bottom:5px}.lpf-facts strong{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lpf-utils{display:flex;align-content:flex-start;gap:7px;flex-wrap:wrap;height:52px;overflow:hidden;padding:13px 0 7px}.lpf-utils span{font-size:10px;background:#f0f0ed;border-radius:999px;padding:7px 9px;white-space:nowrap}.lpf-actions{display:grid;gap:9px;margin-top:auto}.lpf-actions a{background:#fff;color:#171717;border:1px solid #171717;font-size:12px}.lpf-actions button{font-size:12px}.lpf-actions button.selected{background:#171717;color:#fff}
      .lpf-preview{position:relative;min-height:500px}.lpf-blurred{filter:blur(8px);user-select:none;pointer-events:none;opacity:.58}.lpf-skeleton{color:#4e4e4b}.lpf-gate{position:absolute;z-index:2;left:50%;top:50%;transform:translate(-50%,-50%);width:min(520px,calc(100% - 40px));background:#191919;color:#fff;border:1px solid #343434;border-radius:24px;padding:30px;box-shadow:0 24px 80px rgba(0,0,0,.35)}.lpf-gate h3{font-size:27px;text-transform:uppercase;margin:0 0 10px}.lpf-gate>p,.lpf-dialog>p{color:#b8b8b3;font-size:14px;line-height:1.5;margin:0 0 20px}.lpf-gate>small,.lpf-dialog>small{display:block;color:#8e8e89;font-size:10px;line-height:1.45;margin-top:12px}.lpf-gate-form{display:grid;gap:13px}.lpf-consent{grid-template-columns:20px 1fr!important;align-items:start;font-weight:400!important;line-height:1.45;color:#c1c1bc!important}.lpf-consent a,.lpf-link{color:#b17700}.lpf-notice,.lpf-demo{padding:12px 14px;border-radius:10px;margin:0 0 14px;font-size:12px}.lpf-notice{background:#4a2421;color:#ffd3cf}.lpf-demo{background:#302c1e;color:#ffe395}.lpf-back,.lpf-link{padding:0;min-height:auto;background:none;border:0;font-weight:700}.lpf-back{color:#aaa9a5;margin-bottom:18px}
      .lpf-bottom{display:flex;justify-content:space-between;align-items:center;padding:0 38px 26px;color:#7d7d79;font-size:11px}.lpf-bottom .lpf-link{margin:0}.lpf-bottom button[hidden]{display:none}
      .lpf-dialog-layer{position:fixed;z-index:2147483000;inset:0;background:rgba(0,0,0,.66);display:grid;place-items:center;padding:20px}.lpf-dialog{position:relative;width:min(500px,100%);background:#191919;color:#fff;border-radius:24px;padding:32px;box-shadow:0 30px 100px rgba(0,0,0,.5)}.lpf-dialog h3{font-size:27px;text-transform:uppercase;margin:0 28px 10px 0}.lpf-dialog-close{position:absolute;right:18px;top:16px;background:transparent;color:#aaa;min-height:32px;width:32px;padding:0;font-size:25px}.lpf-gate-form label span{font-weight:400;color:#999}.lpf-success-icon{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:#171717;font-size:25px;font-weight:900;margin-bottom:20px}.lpf-dialog>button{width:100%;margin-top:8px}
      @media(max-width:980px){.lpf-filters{grid-template-columns:repeat(2,1fr)}.lpf-filters button{grid-column:auto}.lpf-grid{grid-template-columns:repeat(2,1fr)}}
      @media(max-width:640px){.lpf-shell{border-radius:18px}.lpf-header,.lpf-filters,.lpf-grid{padding-left:20px;padding-right:20px}.lpf-header{display:block}.lpf-brand{display:inline-block;margin-top:18px}.lpf-filters{grid-template-columns:1fr}.lpf-grid{grid-template-columns:1fr}.lpf-status{margin-left:20px;margin-right:20px}.lpf-bottom{padding-left:20px;padding-right:20px}.lpf-facts strong{font-size:13px}.lpf-gate{position:absolute;top:24px;transform:translateX(-50%);padding:24px}.lpf-preview{min-height:590px}.lpf-dialog{padding:26px}}
    `;
  }
})();
