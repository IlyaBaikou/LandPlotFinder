(function () {
  "use strict";

  const script = document.currentScript;
  if (!script) return;

  const config = {
    target: script.dataset.target || "landplotfinder-widget",
    apiBase: (script.dataset.apiBase || new URL(script.src).origin).replace(/\/$/, ""),
    profileId: script.dataset.profileId || "",
    company: script.dataset.companyName || "ВАША КОМПАНИЯ",
    accent: script.dataset.accent || "#ffb434",
    privacyUrl: script.dataset.privacyUrl || "#",
    maxPrice: script.dataset.maxPrice || "70000",
    minArea: script.dataset.minArea || "6",
    maxArea: script.dataset.maxArea || "20",
    maxDistance: script.dataset.maxDistance || "50",
    defaultMapProvider: script.dataset.mapProvider || "yandex",
    resultsHeight: boundedNumber(script.dataset.resultsHeight, 680, 420, 1000),
  };
  const storagePrefix = `lpf-widget:${config.apiBase}`;
  const tokenKey = `${storagePrefix}:token`;
  const savedKey = `${storagePrefix}:saved`;
  const mapKey = `${storagePrefix}:map-provider`;
  const mount = document.getElementById(config.target) || createMount(script);
  const root = mount.shadowRoot || mount.attachShadow({ mode: "open" });
  const tildaZeroBlock = findTildaZeroBlock(mount);

  if (tildaZeroBlock) mount.dataset.lpfTildaZeroBlock = "true";

  let token = localStorage.getItem(tokenKey) || sessionStorage.getItem(tokenKey) || "";
  let currentPhone = "";
  let lastSearch = {};
  let lastPreview = { total: 0, preview_cards: 0 };
  let items = [];
  let total = 0;
  let hasMore = false;
  let offset = 0;
  let sortMode = "match";
  let activeTab = "results";
  let mapProvider = localStorage.getItem(mapKey) || config.defaultMapProvider;
  let mapFocusRef = "";
  let compareRefs = new Set();
  let interestRefs = new Set();
  let telegramAvailable = false;
  let comparisonOpen = false;
  let savedStatuses = new Map();
  let savedStatusCheckedAt = 0;
  let savedStatusPromise = null;
  let savedStatusError = "";
  let pageScrollLock = null;
  let directions = [
    "Брестское", "Витебское", "Гродненское", "Логойское", "Могилёвское",
    "Молодечненское", "Московское", "Мядельское", "Пуховичское", "Раковское", "Слуцкое",
  ];

  if (!new Set(["yandex", "google"]).has(mapProvider)) mapProvider = "yandex";
  if (token) {
    localStorage.setItem(tokenKey, token);
    sessionStorage.removeItem(tokenKey);
  }

  root.innerHTML = `<style>${styles()}</style><section class="lpf-shell"><div id="lpf-app"></div></section>`;
  const app = root.getElementById("lpf-app");
  renderSearch();
  setupTildaZeroBlock();
  loadDirections();
  if (token) runSearch();
  else setStatus("Задайте параметры и нажмите «Показать варианты».", "ready");

  function createMount(anchor) {
    const node = document.createElement("div");
    node.id = config.target;
    anchor.parentNode.insertBefore(node, anchor);
    return node;
  }

  function findTildaZeroBlock(node) {
    if (!node.closest) return null;
    const artboard = node.closest(".t396__artboard");
    if (!artboard) return null;
    return {
      artboard,
      element: node.closest(".tn-elem"),
      atom: node.closest(".tn-atom"),
      carrier: artboard.querySelector(".t396__carrier"),
      filter: artboard.querySelector(".t396__filter"),
    };
  }

  function setupTildaZeroBlock() {
    if (!tildaZeroBlock) return;
    let frame = 0;
    const schedule = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(syncTildaZeroBlockHeight);
    };
    const shell = root.querySelector(".lpf-shell");
    if (window.ResizeObserver && shell) new ResizeObserver(schedule).observe(shell);
    window.addEventListener("resize", () => {
      schedule();
      window.setTimeout(schedule, 120);
    }, { passive: true });
    schedule();
    window.setTimeout(schedule, 250);
    window.setTimeout(schedule, 1000);
  }

  function syncTildaZeroBlockHeight() {
    if (!tildaZeroBlock) return;
    const shell = root.querySelector(".lpf-shell");
    if (!shell) return;
    const { artboard, element, atom, carrier, filter } = tildaZeroBlock;
    const shellRect = shell.getBoundingClientRect();
    const shellHeight = Math.ceil(shell.offsetHeight || shellRect.height);
    if (!shellHeight || !shellRect.height) return;

    mount.style.height = `${shellHeight}px`;
    if (atom) atom.style.height = `${shellHeight}px`;
    if (element) element.style.height = `${shellHeight}px`;

    const artboardRect = artboard.getBoundingClientRect();
    const mountRect = mount.getBoundingClientRect();
    let requiredBottom = Math.max(0, mountRect.top - artboardRect.top) + shellRect.height;
    artboard.querySelectorAll(":scope > .tn-elem").forEach((candidate) => {
      if (candidate === element) return;
      const rect = candidate.getBoundingClientRect();
      requiredBottom = Math.max(requiredBottom, rect.bottom - artboardRect.top);
    });
    const height = `${Math.ceil(requiredBottom + 24)}px`;
    artboard.style.setProperty("height", height, "important");
    if (carrier) carrier.style.setProperty("height", height, "important");
    if (filter) filter.style.setProperty("height", height, "important");
  }

  function renderSearch() {
    app.innerHTML = `
      <header class="lpf-header">
        <div><div class="lpf-kicker">Умный подбор участка</div><h2>Найдите идеальное место для будущего дома</h2></div>
        <div class="lpf-brand">${html(config.company)}</div>
      </header>
      <form id="search-form" class="lpf-filters">
        <label class="lpf-wide">Направление<select name="q" aria-label="Направление"><option value="">Все направления</option>${directionOptions()}</select></label>
        <label>Бюджет до, $<input name="max_price_usd" type="number" min="1" value="${attr(config.maxPrice)}"></label>
        <label>Площадь от, сот.<input name="min_area_sotok" type="number" min="1" step="0.1" value="${attr(config.minArea)}"></label>
        <label>Площадь до, сот.<input name="max_area_sotok" type="number" min="1" step="0.1" value="${attr(config.maxArea)}"></label>
        <label>До МКАД, км<input name="max_distance_km" type="number" min="0" value="${attr(config.maxDistance)}"></label>
        <div class="lpf-utility-options" aria-label="Обязательные коммуникации">
          <label class="lpf-check"><input name="electricity" type="checkbox"><span>Электричество</span></label>
          <label class="lpf-check"><input name="gas" type="checkbox"><span>Газ</span></label>
          <label class="lpf-check"><input name="water" type="checkbox"><span>Водоснабжение</span></label>
          <label class="lpf-check"><input name="sewerage" type="checkbox"><span>Канализация</span></label>
        </div>
        <button type="submit">Показать варианты</button>
      </form>
      <div id="search-status" class="lpf-status" aria-live="polite"></div>
      <div id="search-results"></div>
      <div class="lpf-bottom"><span>Данные обновляются автоматически</span><button id="logout" class="lpf-link" type="button" ${token ? "" : "hidden"}>Сменить телефон</button></div>`;
    restoreSearchForm();
    app.querySelector("#search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch();
    });
    app.querySelector("#logout").addEventListener("click", () => {
      clearToken();
      lastSearch = {};
      items = [];
      renderSearch();
      setStatus("Номер удалён с этого устройства. Сохранённые варианты остались в браузере.", "ready");
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
    if (form.elements.water.checked) params.set("water", "true");
    if (form.elements.sewerage.checked) params.set("sewerage", "true");
    if (config.profileId) params.set("profile_id", config.profileId);
    lastSearch = Object.fromEntries(params.entries());
    return params;
  }

  function paramsFromLastSearch() { return new URLSearchParams(lastSearch); }

  function directionOptions() {
    return directions.map((value) => `<option value="${attr(value)}">${html(value)}</option>`).join("");
  }

  async function loadDirections() {
    try {
      const params = new URLSearchParams();
      if (config.profileId) params.set("profile_id", config.profileId);
      const payload = await api(`/api/public/widget/options?${params.toString()}`, { public: true });
      telegramAvailable = Boolean(payload.telegram_subscriptions_available);
      directions = [...new Set([...(payload.directions || []), ...directions])].sort((left, right) => left.localeCompare(right, "ru"));
      const select = app.querySelector('select[name="q"]');
      if (!select) return;
      const selected = select.value;
      select.innerHTML = `<option value="">Все направления</option>${directionOptions()}`;
      if ([...select.options].some((option) => option.value === selected)) select.value = selected;
      restoreSearchForm();
      if (token && app.querySelector("#lpf-tab-content")) renderWorkspace();
    } catch (_) { /* встроенный список остаётся доступен */ }
  }

  function restoreSearchForm() {
    if (!Object.keys(lastSearch).length) return;
    const form = app.querySelector("#search-form");
    Object.entries(lastSearch).forEach(([key, value]) => {
      if (!form.elements[key]) return;
      if (form.elements[key].type === "checkbox") form.elements[key].checked = value === "true";
      else form.elements[key].value = value;
    });
  }

  async function runSearch() {
    const params = searchParams();
    setStatus("Сопоставляем участки с вашими параметрами…", "loading");
    app.querySelector("#search-results").innerHTML = "";
    offset = 0;
    items = [];
    activeTab = "results";
    if (!token) {
      await runPreview(params);
      return;
    }
    await fetchResults(params, false);
  }

  async function fetchResults(params, append) {
    params.set("limit", "9");
    params.set("offset", append ? String(offset) : "0");
    params.set("sort", sortMode);
    try {
      const payload = await api(`/api/public/widget/listings?${params.toString()}`);
      interestRefs = new Set([...interestRefs, ...(payload.interested_references || [])]);
      items = append ? uniqueItems([...items, ...(payload.items || [])]) : (payload.items || []);
      total = payload.total || 0;
      hasMore = Boolean(payload.has_more);
      offset = items.length;
      renderWorkspace();
      refreshSavedStatuses();
    } catch (error) {
      if (error.status === 401) {
        clearToken();
        await runPreview(searchParams(), "Сессия закончилась. Подтвердите номер ещё раз.");
        return;
      }
      setStatus(error.message, "error");
    }
  }

  async function loadMore() {
    const button = app.querySelector("#load-more");
    setBusy(button, true, "Загружаем…");
    await fetchResults(paramsFromLastSearch(), true);
  }

  async function runPreview(params, message = "") {
    try {
      lastPreview = await api(`/api/public/widget/preview?${params.toString()}`, { public: true });
      if (!lastPreview.total) {
        setStatus("Под эти параметры пока ничего нет. Попробуйте немного расширить поиск.", "empty");
        return;
      }
      setStatus(`Нашли ${lastPreview.total}. Подтвердите телефон, чтобы открыть подборку.`, "ready");
      renderPhoneGate(message);
    } catch (error) { setStatus(error.message, "error"); }
  }

  function renderPhoneGate(message = "") {
    renderBlurredCards();
    const gate = app.querySelector("#auth-gate");
    gate.innerHTML = `
      <div class="lpf-kicker">Подборка готова</div>
      <h3>Откройте найденные варианты</h3>
      <p>Введите номер, чтобы открыть подборку. После ввода кода повторно вводить номер на этом устройстве не придётся 30 дней.</p>
      ${notice(message)}
      <form id="phone-form" class="lpf-gate-form">
        <label>Номер телефона<input name="phone" type="tel" autocomplete="tel" placeholder="+375 29 000-00-00" required></label>
        <label class="lpf-consent"><input name="consent" type="checkbox" required><span>Соглашаюсь на обработку номера для доступа к каталогу. Менеджер увидит мой номер, условия поиска и участки, которые я явно отмечу как интересные. <a href="${safeUrl(config.privacyUrl)}" target="_blank" rel="noopener">Условия</a></span></label>
        <button type="submit">Получить код</button>
      </form>`;
    gate.querySelector("#phone-form").addEventListener("submit", requestCode);
  }

  function renderBlurredCards() {
    const results = app.querySelector("#search-results");
    const count = Math.max(1, lastPreview.preview_cards || 0);
    results.className = "lpf-preview";
    results.innerHTML = `<div class="lpf-grid lpf-blurred" aria-hidden="true">${Array.from({ length: count }, () => blurredCard()).join("")}</div><div class="lpf-gate" id="auth-gate"></div>`;
  }

  function blurredCard() {
    return `<article class="lpf-card lpf-skeleton"><div class="lpf-cardtop"><span class="lpf-reference">LP-••••••••••</span><span class="lpf-score">90% совпадение</span></div><h3>Участок 00 сот. в выбранном районе</h3><p class="lpf-location">Примерное расположение</p><div class="lpf-facts"><div><span>Цена</span><strong>$00 000</strong></div><div><span>Площадь</span><strong>00 сот.</strong></div><div><span>До МКАД</span><strong>00 км</strong></div></div><div class="lpf-location-score">Локация <strong>00/100</strong></div><div class="lpf-actions"><button type="button">Подробнее</button><button type="button">Сохранить</button></div></article>`;
  }

  async function requestCode(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    currentPhone = new FormData(form).get("phone").toString().trim();
    setBusy(button, true, "Отправляем…");
    try {
      const payload = await api("/api/public/widget/auth/request-code", {
        method: "POST", public: true,
        body: { phone: currentPhone, consent: form.elements.consent.checked, source_page: window.location.href, utm: utmParams() },
      });
      renderCodeGate(payload);
    } catch (error) { renderPhoneGate(error.message); }
  }

  function renderCodeGate(payload, message = "") {
    renderBlurredCards();
    const gate = app.querySelector("#auth-gate");
    gate.innerHTML = `<button id="back-phone" class="lpf-back" type="button">← Изменить номер</button><div class="lpf-kicker">Подтверждение</div><h3>${payload.demo_code ? "Ваш индивидуальный демо-код" : "Введите код из SMS"}</h3>${payload.demo_code ? `<div class="lpf-demo"><strong id="demo-code">${html(payload.demo_code)}</strong><button id="copy-demo-code" type="button" aria-label="Скопировать демо-код" title="Скопировать демо-код">⧉ Копировать</button></div>` : `<p>Отправили шестизначный код на ${html(payload.phone || currentPhone)}.</p>`}${notice(message)}<form id="code-form" class="lpf-gate-form"><label>${payload.demo_code ? "Введите ваш демо-код" : "Код из SMS"}<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" required></label><button type="submit">Открыть подборку</button></form>`;
    gate.querySelector("#back-phone").addEventListener("click", () => renderPhoneGate());
    gate.querySelector("#copy-demo-code")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(payload.demo_code);
        } else {
          const field = document.createElement("textarea");
          field.value = payload.demo_code;
          gate.appendChild(field);
          field.select();
          const copied = document.execCommand("copy");
          field.remove();
          if (!copied) throw new Error("Copy unavailable");
        }
        button.textContent = "✓ Скопировано";
      } catch (_) {
        button.textContent = "Выделите код выше";
      }
    });
    gate.querySelector("#code-form").addEventListener("submit", (event) => verifyCode(event, payload));
    gate.querySelector("input[name=code]").focus();
  }

  async function verifyCode(event, requestPayload) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    setBusy(button, true, "Проверяем…");
    try {
      const payload = await api("/api/public/widget/auth/verify-code", { method: "POST", public: true, body: { phone: currentPhone, code: new FormData(form).get("code") } });
      token = payload.token;
      localStorage.setItem(tokenKey, token);
      app.querySelector("#logout").hidden = false;
      await runSearch();
    } catch (error) { renderCodeGate(requestPayload, error.message); }
  }

  function renderWorkspace() {
    const results = app.querySelector("#search-results");
    const savedCount = readSaved().length;
    results.className = "lpf-workspace";
    if (!items.length && !savedCount) {
      setStatus("Под эти параметры пока ничего нет. Попробуйте немного расширить поиск.", "empty");
      results.innerHTML = "";
      return;
    }
    setStatus(`Найдено ${total}. Оценка совпадения рассчитана по вашим параметрам.`, "success");
    results.innerHTML = `<div class="lpf-toolbar"><div class="lpf-tabs" role="tablist">${tabButton("results", `Варианты ${total}`)}${tabButton("map", "Карта")}${tabButton("saved", `Сохранённые ${savedCount}`)}</div><div class="lpf-toolbar-actions">${savedCount >= 2 ? '<button id="compare-saved-shortcut" class="lpf-compare-shortcut" type="button">Сравнить сохранённые</button>' : ""}<label class="lpf-sort">Сортировка<select id="sort-mode"><option value="match">Лучшее совпадение</option><option value="price">Сначала дешевле</option><option value="distance">Сначала ближе</option><option value="newest">Сначала свежие</option></select></label></div></div><div id="lpf-tab-content"></div>`;
    results.querySelector("#sort-mode").value = sortMode;
    results.querySelector("#sort-mode").addEventListener("change", async (event) => {
      sortMode = event.target.value;
      offset = 0;
      items = [];
      await fetchResults(paramsFromLastSearch(), false);
    });
    results.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => { activeTab = button.dataset.tab; renderWorkspace(); }));
    results.querySelector("#compare-saved-shortcut")?.addEventListener("click", openSavedComparison);
    renderActiveTab();
  }

  function tabButton(name, label) { return `<button class="lpf-tab ${activeTab === name ? "active" : ""}" type="button" role="tab" data-tab="${name}">${html(label)}</button>`; }
  function renderActiveTab() {
    if (activeTab === "map") renderMap();
    else if (activeTab === "saved") {
      renderSaved();
      refreshSavedStatuses();
    } else renderResults();
  }

  function renderResults() {
    const content = app.querySelector("#lpf-tab-content");
    content.innerHTML = `<div class="lpf-grid">${items.map((item) => listingCard(item)).join("")}</div>${hasMore ? '<div class="lpf-more"><button id="load-more" type="button">Показать ещё</button></div>' : ""}${telegramPromo()}`;
    wireCards(content, items);
    content.querySelector("#load-more")?.addEventListener("click", loadMore);
    content.querySelector("#telegram-invite")?.addEventListener("click", openTelegramModal);
  }

  function telegramPromo() {
    if (!token || !telegramAvailable) return "";
    return `<section class="lpf-telegram-promo"><div><strong>Новые участки — под ваш поиск</strong><span>Получайте одну личную подборку в Telegram раз в день, только если появились новые варианты. Подписку можно остановить в любой момент.</span></div><button id="telegram-invite" type="button">Получать в Telegram ↗</button></section>`;
  }

  function openTelegramModal() {
    closeDialog();
    const dialog = document.createElement("div");
    dialog.id = "lpf-dialog";
    dialog.className = "lpf-dialog-layer";
    dialog.innerHTML = `<div class="lpf-dialog lpf-telegram-modal" role="dialog" aria-modal="true" aria-labelledby="telegram-modal-title" tabindex="-1"><button class="lpf-dialog-close" type="button" aria-label="Закрыть">×</button><div class="lpf-kicker">Личная подборка</div><h3 id="telegram-modal-title">Участки по вашим фильтрам — в Telegram</h3><p>Бот покажет актуальные варианты со ссылками на объявления. Новые совпадения можно получать одной подборкой в день. Подписка включится только после вашего подтверждения в Telegram.</p><button id="telegram-create-link" type="button">Получить приглашение</button><div id="telegram-invite-result" role="status"></div><small>Можно выбрать «Только текущие варианты» без подписки. Пауза — /pause, отписка — /stop.</small></div>`;
    updateDialogTopOffset(dialog);
    root.appendChild(dialog);
    lockPageScroll();
    dialog.querySelector(".lpf-dialog").focus({ preventScroll: true });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(); });
    dialog.querySelector(".lpf-dialog-close").addEventListener("click", closeDialog);
    dialog.querySelector("#telegram-create-link").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      setBusy(button, true, "Готовим ссылку…");
      const result = dialog.querySelector("#telegram-invite-result");
      try {
        const payload = await api("/api/public/widget/telegram/invite", { method: "POST" });
        result.innerHTML = `<a class="lpf-telegram-open" href="${attr(payload.url)}" target="_blank" rel="noopener noreferrer">Открыть бота в Telegram ↗</a>`;
        button.hidden = true;
      } catch (error) {
        result.textContent = error.message;
        button.disabled = false;
        button.textContent = "Повторить";
      }
    });
  }

  function listingCard(item, options = {}) {
    const saved = isSaved(item.reference);
    const compare = compareRefs.has(item.reference);
    const unavailable = options.savedView && isUnavailable(item.reference);
    const badge = unavailable
      ? '<span class="lpf-availability">Снято</span>'
      : `<span class="lpf-score">${html(item.match_score)}% совпадение</span>`;
    return `<article class="lpf-card ${unavailable ? "lpf-unavailable" : ""}" data-card-ref="${attr(item.reference)}"><div class="lpf-cardtop"><span class="lpf-reference">${html(item.reference)}</span>${badge}</div>${unavailable ? '<div class="lpf-unavailable-note">Объявление больше не публикуется. Сохранённая карточка оставлена для истории.</div>' : ""}<h3 title="${attr(item.title)}">${html(item.title)}</h3><p class="lpf-location" title="${attr(item.location)}">≈ ${html(item.location)}</p><div class="lpf-facts"><div><span>Цена</span><strong>${item.price_usd == null ? "—" : `$${number(item.price_usd)}`}</strong></div><div><span>Площадь</span><strong>${item.area_sotok == null ? "—" : `${number(item.area_sotok)} сот.`}</strong></div><div><span>До МКАД</span><strong>${item.distance_mkad_km == null ? "—" : `${number(item.distance_mkad_km)} км`}</strong></div></div><div class="lpf-card-insights"><span class="lpf-location-score">Локация <strong>${item.location_score == null ? "изучается" : `${html(item.location_score)}/100`}</strong></span><span class="lpf-fresh">${freshness(item.last_seen_at)}</span></div><div class="lpf-utils">${utility(item.electricity, "Электричество")}${utility(item.gas, "Газ")}${utility(item.water, "Вода")}${utility(item.sewerage, "Канализация")}</div>${options.savedView ? `<button class="lpf-compare-toggle ${compare ? "active" : ""}" type="button" data-compare="${attr(item.reference)}" aria-pressed="${compare}">${compare ? "✓ Выбрано для сравнения" : "+ Добавить к сравнению"}</button>` : ""}${listingLink(item, unavailable)}${interestButton(item, unavailable)}<div class="lpf-actions"><button class="lpf-secondary" type="button" data-details="${attr(item.reference)}">Подробнее</button><button type="button" data-save="${attr(item.reference)}">${saved ? "✓ Сохранено" : "♡ Сохранить"}</button></div></article>`;
  }

  function wireCards(scope, sourceItems) {
    scope.querySelectorAll("[data-details]").forEach((button) => button.addEventListener("click", () => { const item = sourceItems.find((candidate) => candidate.reference === button.dataset.details) || findItem(button.dataset.details); if (item) showDetails(item); }));
    scope.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", () => { const item = sourceItems.find((candidate) => candidate.reference === button.dataset.save) || findItem(button.dataset.save); if (item) toggleSaved(item); }));
    scope.querySelectorAll("[data-interest]").forEach((button) => button.addEventListener("click", () => { const item = sourceItems.find((candidate) => candidate.reference === button.dataset.interest) || findItem(button.dataset.interest); if (item) markInterest(item, button); }));
    scope.querySelectorAll("[data-compare]").forEach((button) => button.addEventListener("click", () => toggleCompare(button.dataset.compare, !compareRefs.has(button.dataset.compare))));
  }

  function showDetails(item) {
    closeDialog();
    const unavailable = isUnavailable(item.reference);
    const dialog = document.createElement("div");
    dialog.id = "lpf-dialog";
    dialog.className = "lpf-dialog-layer";
    const similar = similarItems(item).slice(0, 3);
    dialog.innerHTML = `<div class="lpf-dialog lpf-detail" role="dialog" aria-modal="true" aria-labelledby="detail-title"><button class="lpf-dialog-close" type="button" aria-label="Закрыть">×</button><div class="lpf-detail-head"><div><div class="lpf-reference">${html(item.reference)}</div><h3 id="detail-title">${html(item.title)}</h3><p>≈ ${html(item.location)}</p></div><div class="lpf-big-score"><strong>${html(item.match_score)}%</strong><span>совпадение</span></div></div>${unavailable ? '<div class="lpf-unavailable-banner"><strong>Объявление снято с публикации</strong><span>Мы оставили сохранённую копию, чтобы вариант не исчез бесследно.</span></div>' : ""}<div class="lpf-detail-grid"><section><div class="lpf-section-title">Основные параметры</div>${detailFacts(item)}</section><section><div class="lpf-section-title">Почему подходит</div>${bulletList(item.match_reasons, "good")}${bulletList(item.warnings, "warning")}</section><section><div class="lpf-section-title">Коммуникации и участок</div>${detailUtilities(item)}</section><section><div class="lpf-section-title">Оценка локации</div>${locationDetail(item)}</section></div>${approximateMapActions(item)}${similar.length ? `<section class="lpf-similar"><div class="lpf-section-title">Похожие варианты</div><div class="lpf-similar-list">${similar.map((candidate) => `<button type="button" data-similar="${attr(candidate.reference)}"><strong>${html(candidate.reference)}</strong><span>${candidate.price_usd == null ? "Цена уточняется" : `$${number(candidate.price_usd)}`} · ${candidate.area_sotok == null ? "—" : `${number(candidate.area_sotok)} сот.`}</span></button>`).join("")}</div></section>` : ""}<div class="lpf-detail-actions"><button class="lpf-secondary" id="detail-close" type="button">Вернуться</button><button class="lpf-secondary" id="detail-save" type="button">${isSaved(item.reference) ? "✓ Сохранено" : "♡ Сохранить"}</button>${listingLink(item, unavailable, true)}${interestButton(item, unavailable, true)}</div><small>Сохранённые варианты остаются в браузере. Отмеченный интерес и номер телефона увидит менеджер.</small></div>`;
    updateDialogTopOffset(dialog);
    root.appendChild(dialog);
    lockPageScroll();
    dialog.querySelector(".lpf-dialog").setAttribute("tabindex", "-1");
    dialog.querySelector(".lpf-dialog").focus({ preventScroll: true });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(); });
    dialog.querySelector(".lpf-dialog-close").addEventListener("click", closeDialog);
    dialog.querySelector("#detail-close").addEventListener("click", closeDialog);
    dialog.querySelector("#detail-save").addEventListener("click", () => toggleSaved(item, true));
    dialog.querySelector("#detail-interest")?.addEventListener("click", (event) => markInterest(item, event.currentTarget));
    dialog.querySelectorAll("[data-similar]").forEach((button) => button.addEventListener("click", () => { const candidate = findItem(button.dataset.similar); if (candidate) showDetails(candidate); }));
  }

  async function markInterest(item, button) {
    if (interestRefs.has(item.reference) || isUnavailable(item.reference)) return;
    setBusy(button, true, "Сохраняем интерес…");
    try {
      await api("/api/public/widget/interests", {
        method: "POST",
        body: { reference: item.reference, search_params: lastSearch, source_page: window.location.href },
      });
      interestRefs.add(item.reference);
      renderWorkspace();
      if (root.getElementById("lpf-dialog")) showDetails(item);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Повторить отметку";
      setStatus(error.message, "error");
    }
  }

  function detailFacts(item) {
    return `<div class="lpf-detail-facts">${detailFact("Цена", item.price_usd == null ? "Неизвестно" : `$${number(item.price_usd)}`)}${detailFact("Площадь", item.area_sotok == null ? "Неизвестно" : `${number(item.area_sotok)} сот.`)}${detailFact("До МКАД", item.distance_mkad_km == null ? "Неизвестно" : `${number(item.distance_mkad_km)} км`)}${detailFact("Направление", item.direction || "Уточнить")}${detailFact("Размеры", item.facade_m && item.depth_m ? `${number(item.facade_m)} × ${number(item.depth_m)} м` : "Неизвестно")}${detailFact("Назначение земли", item.purpose || "Уточнить")}${detailFact("Право на землю", item.ownership || "Уточнить")}</div>`;
  }
  function detailFact(label, value) { return `<div><span>${html(label)}</span><strong>${html(value)}</strong></div>`; }
  function detailUtilities(item) {
    const values = [["Электричество", item.electricity], ["Газ", item.gas], ["Вода", item.water], ["Канализация", item.sewerage], ["Интернет", item.internet], ["Дорога", item.road]];
    return `<div class="lpf-utility-table">${values.map(([label, value]) => `<div><span>${html(label)}</span><strong class="${value == null ? "unknown" : value === "Нет" ? "absent" : "known"}">${html(value || "Нет данных")}</strong></div>`).join("")}</div>`;
  }
  function locationDetail(item) {
    if (item.location_score == null) return '<p class="lpf-muted">Локация ещё изучается. Оценка появится после накопления данных.</p>';
    return `<div class="lpf-location-summary"><strong>${html(item.location_score)}/100</strong><span>${html(item.location_verdict || "Есть данные для первичной оценки")}</span></div><p class="lpf-confidence">Достоверность: ${html(confidenceLabel(item.location_confidence))}${item.nearby_premium_houses ? ` · дорогих домов рядом: ${html(item.nearby_premium_houses)}` : ""}</p>${bulletList(item.location_signals, "good")}${bulletList(item.location_risks, "warning")}`;
  }
  function bulletList(values, kind) {
    if (!values || !values.length) return "";
    const icon = kind === "warning" ? "!" : "✓";
    return `<ul class="lpf-bullets ${kind}">${values.map((value) => `<li><span>${icon}</span>${html(value)}</li>`).join("")}</ul>`;
  }
  function approximateMapActions(item) {
    if (item.latitude == null || item.longitude == null) return "";
    return `<section class="lpf-route-box"><div><div class="lpf-section-title">Примерное расположение</div><p>Точка округлена примерно до района. Точный адрес менеджер проверит по коду ${html(item.reference)}.</p></div><div><a href="${mapRouteUrl(item, "yandex")}" target="_blank" rel="noopener">Маршрут в Яндекс</a><a class="lpf-alt-link" href="${mapRouteUrl(item, "google")}" target="_blank" rel="noopener">Google Maps</a></div></section>`;
  }
  function similarItems(item) { return items.filter((candidate) => candidate.reference !== item.reference).sort((left, right) => similarity(item, left) - similarity(item, right)); }
  function similarity(base, candidate) { return Math.abs((base.price_usd || 0) - (candidate.price_usd || 0)) / 2000 + Math.abs((base.area_sotok || 0) - (candidate.area_sotok || 0)) * 2 + Math.abs((base.distance_mkad_km || 0) - (candidate.distance_mkad_km || 0)); }

  function renderMap() {
    const content = app.querySelector("#lpf-tab-content");
    const plotted = items.filter((item) => item.latitude != null && item.longitude != null);
    if (!plotted.length) { content.innerHTML = '<div class="lpf-empty">Для найденных вариантов пока нет координат.</div>'; return; }
    const focused = plotted.find((item) => item.reference === mapFocusRef) || plotted[0];
    mapFocusRef = focused.reference;
    content.innerHTML = `<div class="lpf-map-layout"><aside class="lpf-map-list"><div class="lpf-map-provider"><button data-provider="yandex" class="${mapProvider === "yandex" ? "active" : ""}" type="button">Яндекс</button><button data-provider="google" class="${mapProvider === "google" ? "active" : ""}" type="button">Google</button></div>${plotted.map((item) => `<button class="lpf-map-item ${item.reference === focused.reference ? "active" : ""}" data-map-focus="${attr(item.reference)}" type="button"><strong>${html(item.reference)}</strong><span>${html(item.location)}</span><small>${item.price_usd == null ? "Цена уточняется" : `$${number(item.price_usd)}`} · ${html(item.match_score)}%</small></button>`).join("")}</aside><section class="lpf-map-canvas"><iframe title="Примерная карта участков" loading="lazy" referrerpolicy="no-referrer-when-downgrade" src="${attr(mapEmbedUrl(plotted, focused))}"></iframe><div class="lpf-map-note">Показано примерное расположение · координаты округлены</div><a class="lpf-map-route" href="${mapRouteUrl(focused, mapProvider)}" target="_blank" rel="noopener">Построить маршрут в ${mapProvider === "yandex" ? "Яндекс Картах" : "Google Maps"}</a></section></div>`;
    content.querySelectorAll("[data-provider]").forEach((button) => button.addEventListener("click", () => { mapProvider = button.dataset.provider; localStorage.setItem(mapKey, mapProvider); renderMap(); }));
    content.querySelectorAll("[data-map-focus]").forEach((button) => button.addEventListener("click", () => { mapFocusRef = button.dataset.mapFocus; renderMap(); }));
  }
  function mapEmbedUrl(plotted, focused) {
    if (mapProvider === "google") return `https://www.google.com/maps?q=${focused.latitude},${focused.longitude}&z=11&output=embed`;
    const points = plotted.slice(0, 20).map((item) => `${item.longitude},${item.latitude},pm2rdm`).join("~");
    return `https://yandex.ru/map-widget/v1/?ll=${focused.longitude}%2C${focused.latitude}&z=10&pt=${encodeURIComponent(points)}`;
  }
  function mapRouteUrl(item, provider) { if (provider === "google") return `https://www.google.com/maps/dir/?api=1&destination=${item.latitude},${item.longitude}`; return `https://yandex.ru/maps/?rtext=~${item.latitude},${item.longitude}&rtt=auto`; }

  function renderSaved() {
    const content = app.querySelector("#lpf-tab-content");
    const saved = readSaved();
    if (!saved.length) { content.innerHTML = '<div class="lpf-empty"><strong>Пока ничего не сохранено</strong><span>Нажмите «Сохранить» на понравившемся варианте. Он останется на этом устройстве.</span></div>'; return; }
    compareRefs = new Set([...compareRefs].filter((reference) => saved.some((item) => item.reference === reference)));
    if (compareRefs.size < 2) comparisonOpen = false;
    const unavailableCount = saved.filter((item) => isUnavailable(item.reference)).length;
    const checkText = savedStatusError
      ? savedStatusError
      : savedStatusPromise
        ? "Проверяем актуальность объявлений…"
        : savedStatusCheckedAt
          ? `${unavailableCount ? `Снято с публикации: ${unavailableCount}. ` : ""}Проверено ${shortTime(savedStatusCheckedAt)}.`
          : "Актуальность ещё не проверена.";
    const compareLabel = compareRefs.size >= 2
      ? `${comparisonOpen ? "Скрыть сравнение" : `Сравнить выбранные (${compareRefs.size})`}`
      : `Выберите ещё ${2 - compareRefs.size}`;
    content.innerHTML = `<div class="lpf-saved-intro"><div><strong>Ваш список</strong><span>Карточки хранятся только в этом браузере. Сервер получает лишь анонимные коды для проверки актуальности.</span><span class="${savedStatusError ? "lpf-saved-error" : ""}">${html(checkText)}</span></div><div class="lpf-saved-tools"><button id="refresh-saved" class="lpf-secondary" type="button" ${savedStatusPromise ? "disabled" : ""}>${savedStatusPromise ? "Проверяем…" : "Проверить"}</button><button id="clear-saved" class="lpf-text-button" type="button">Очистить список</button></div></div><div class="lpf-compare-panel"><div><strong>Сравнение вариантов</strong><span>Выбрано ${compareRefs.size} из 4. Отметьте нужные карточки ниже.</span></div><button id="show-comparison" type="button" ${compareRefs.size < 2 ? "disabled" : ""}>${html(compareLabel)}</button></div>${comparisonOpen ? comparison(saved.filter((item) => compareRefs.has(item.reference))) : ""}<div class="lpf-grid">${saved.map((item) => listingCard(item, { savedView: true })).join("")}</div>${telegramPromo()}`;
    wireCards(content, saved);
    content.querySelector("#telegram-invite")?.addEventListener("click", openTelegramModal);
    content.querySelector("#refresh-saved").addEventListener("click", () => refreshSavedStatuses(true));
    content.querySelector("#show-comparison").addEventListener("click", () => {
      comparisonOpen = !comparisonOpen;
      renderSaved();
      if (comparisonOpen) requestAnimationFrame(() => root.getElementById("lpf-comparison")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    });
    content.querySelector("#clear-saved").addEventListener("click", () => { localStorage.removeItem(savedKey); compareRefs.clear(); comparisonOpen = false; savedStatuses = new Map(); savedStatusCheckedAt = 0; renderWorkspace(); });
  }
  function comparison(selected) {
    const rows = [["Актуальность", (item) => isUnavailable(item.reference) ? "Снято" : "Актуально"], ["Цена", (item) => item.price_usd == null ? "—" : `$${number(item.price_usd)}`], ["Площадь", (item) => item.area_sotok == null ? "—" : `${number(item.area_sotok)} сот.`], ["До МКАД", (item) => item.distance_mkad_km == null ? "—" : `${number(item.distance_mkad_km)} км`], ["Совпадение", (item) => `${item.match_score}%`], ["Локация", (item) => item.location_score == null ? "—" : `${item.location_score}/100`], ["Электричество", (item) => item.electricity || "—"], ["Газ", (item) => item.gas || "—"], ["Вода", (item) => item.water || "—"], ["Канализация", (item) => item.sewerage || "—"]];
    return `<div class="lpf-comparison" id="lpf-comparison" style="--compare-count:${selected.length}"><div class="lpf-comparison-grid lpf-comparison-head"><span>Параметр</span>${selected.map((item) => `<strong>${html(item.reference)}</strong>`).join("")}</div>${rows.map(([label, getter]) => `<div class="lpf-comparison-grid"><span>${html(label)}</span>${selected.map((item) => `<strong>${html(getter(item))}</strong>`).join("")}</div>`).join("")}</div>`;
  }
  function openSavedComparison() {
    const saved = readSaved();
    if (compareRefs.size < 2) compareRefs = new Set(saved.slice(0, 4).map((item) => item.reference));
    comparisonOpen = compareRefs.size >= 2;
    activeTab = "saved";
    renderWorkspace();
    requestAnimationFrame(() => root.getElementById("lpf-comparison")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }
  function toggleCompare(reference, checked) {
    if (checked && compareRefs.size >= 4) { setStatus("Для сравнения можно выбрать не больше четырёх вариантов.", "error"); renderSaved(); return; }
    if (checked) compareRefs.add(reference); else compareRefs.delete(reference);
    if (compareRefs.size < 2) comparisonOpen = false;
    renderSaved();
  }
  function toggleSaved(item, fromDialog = false) {
    const saved = readSaved();
    const index = saved.findIndex((candidate) => candidate.reference === item.reference);
    if (index >= 0) {
      saved.splice(index, 1);
      compareRefs.delete(item.reference);
      if (compareRefs.size < 2) comparisonOpen = false;
    } else {
      saved.unshift({ ...item, saved_at: new Date().toISOString() });
      savedStatuses.set(item.reference, { reference: item.reference, active: true, last_seen_at: item.last_seen_at });
    }
    localStorage.setItem(savedKey, JSON.stringify(saved.slice(0, 50)));
    if (fromDialog) showDetails(item); else renderWorkspace();
  }
  function readSaved() { try { const value = JSON.parse(localStorage.getItem(savedKey) || "[]"); return Array.isArray(value) ? value : []; } catch (_) { return []; } }
  function isSaved(reference) { return readSaved().some((item) => item.reference === reference); }
  function isUnavailable(reference) { return savedStatuses.get(reference)?.active === false; }
  function findItem(reference) { return items.find((item) => item.reference === reference) || readSaved().find((item) => item.reference === reference); }
  function uniqueItems(values) { return [...new Map(values.map((item) => [item.reference, item])).values()]; }
  function shortTime(value) { return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }

  async function refreshSavedStatuses(force = false) {
    const saved = readSaved();
    if (!token || !saved.length) return;
    if (savedStatusPromise) return savedStatusPromise;
    if (!force && savedStatusCheckedAt && Date.now() - savedStatusCheckedAt < 60_000) return;
    savedStatusError = "";
    savedStatusPromise = api("/api/public/widget/statuses", {
      method: "POST",
      body: { references: saved.map((item) => item.reference) },
    });
    if (activeTab === "saved") renderSaved();
    try {
      const payload = await savedStatusPromise;
      savedStatuses = new Map((payload.items || []).map((item) => [item.reference, item]));
      savedStatusCheckedAt = Date.now();
    } catch (error) {
      savedStatusError = "Не удалось проверить актуальность. Попробуйте ещё раз.";
      if (error.status === 401) clearToken();
    } finally {
      savedStatusPromise = null;
      if (activeTab === "saved" && app.querySelector("#lpf-tab-content")) renderSaved();
    }
  }
  function lockPageScroll() {
    if (pageScrollLock || !document.body) return;
    pageScrollLock = {
      y: window.scrollY,
      position: document.body.style.position,
      top: document.body.style.top,
      width: document.body.style.width,
      overflow: document.body.style.overflow,
    };
    document.body.style.position = "fixed";
    document.body.style.top = `-${pageScrollLock.y}px`;
    document.body.style.width = "100%";
    document.body.style.overflow = "hidden";
  }
  function unlockPageScroll() {
    if (!pageScrollLock || !document.body) return;
    const state = pageScrollLock;
    pageScrollLock = null;
    document.body.style.position = state.position;
    document.body.style.top = state.top;
    document.body.style.width = state.width;
    document.body.style.overflow = state.overflow;
    window.scrollTo(0, state.y);
  }
  function closeDialog() {
    const dialog = root.getElementById("lpf-dialog");
    if (!dialog) return;
    dialog.remove();
    unlockPageScroll();
  }

  function updateDialogTopOffset(dialog = root.getElementById("lpf-dialog")) {
    if (!dialog) return;
    let headerBottom = 0;
    const maximumHeaderHeight = Math.min(180, window.innerHeight * 0.35);
    document.querySelectorAll("body *").forEach((candidate) => {
      if (candidate === mount || candidate.contains(mount)) return;
      const style = window.getComputedStyle(candidate);
      if (!["fixed", "sticky"].includes(style.position) || style.display === "none" || style.visibility === "hidden") return;
      const rect = candidate.getBoundingClientRect();
      if (rect.top > 4 || rect.bottom <= 4 || rect.height < 24 || rect.height > maximumHeaderHeight) return;
      if (rect.width < Math.min(300, window.innerWidth * 0.5)) return;
      headerBottom = Math.max(headerBottom, rect.bottom);
    });
    const mountScale = mount.offsetWidth ? mount.getBoundingClientRect().width / mount.offsetWidth : 1;
    const offset = headerBottom / (Number.isFinite(mountScale) && mountScale > 0 ? mountScale : 1);
    dialog.style.setProperty("--lpf-modal-top-offset", `${Math.ceil(offset)}px`);
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && root.getElementById("lpf-dialog")) closeDialog();
  });
  window.addEventListener("resize", () => updateDialogTopOffset(), { passive: true });

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token && !options.public) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`${config.apiBase}${path}`, { method: options.method || "GET", headers, body: options.body ? JSON.stringify(options.body) : undefined });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) { const error = new Error(payload.detail || "Не удалось выполнить запрос. Попробуйте ещё раз."); error.status = response.status; throw error; }
    return payload;
  }
  function clearToken() { token = ""; interestRefs = new Set(); savedStatuses = new Map(); savedStatusCheckedAt = 0; localStorage.removeItem(tokenKey); sessionStorage.removeItem(tokenKey); }
  function setStatus(text, kind) { const node = app.querySelector("#search-status"); if (!node) return; node.className = `lpf-status ${kind || ""}`; node.textContent = text; }
  function setBusy(button, busy, label) { if (!button) return; button.disabled = busy; button.textContent = label; }
  function notice(message) { return message ? `<div class="lpf-notice">${html(message)}</div>` : ""; }
  function utility(value, label) {
    const state = value == null ? "unknown" : value === "Нет" ? "absent" : "present";
    const symbol = state === "present" ? "✓" : state === "absent" ? "✕" : "?";
    return `<span class="lpf-utility-${state}" title="${html(label)}: ${html(value || "Нет данных")}">${symbol} ${html(label)}: ${html(value || "Нет данных")}</span>`;
  }
  function listingLink(item, unavailable = false, inDetail = false) {
    const url = safeUrl(item.url);
    if (unavailable || url === "#") return "";
    return `<a class="${inDetail ? "lpf-source-link lpf-source-detail" : "lpf-source-link"}" href="${url}" target="_blank" rel="noopener noreferrer">Открыть объявление ↗</a>`;
  }
  function interestButton(item, unavailable = false, inDetail = false) {
    if (unavailable) return "";
    const marked = interestRefs.has(item.reference);
    return `<button class="lpf-interest ${marked ? "active" : ""}" ${inDetail ? 'id="detail-interest"' : `data-interest="${attr(item.reference)}"`} type="button" ${marked ? "disabled" : ""}>${marked ? "✓ Интерес отмечен" : "Интересен этот участок"}</button>`;
  }
  function confidenceLabel(value) { return ({ high: "высокая", medium: "средняя", low: "предварительная" })[value] || "предварительная"; }
  function freshness(value) { if (!value) return "Дата уточняется"; const days = Math.floor((Date.now() - new Date(value).getTime()) / 86400000); if (days <= 0) return "Проверено сегодня"; if (days === 1) return "Проверено вчера"; return `Проверено ${days} дн. назад`; }
  function utmParams() { const result = {}; new URLSearchParams(window.location.search).forEach((value, key) => { if (key.startsWith("utm_")) result[key] = value; }); return result; }
  function number(value) { return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value); }
  function html(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
  function attr(value) { return html(value); }
  function safeUrl(value) { if (!value || value === "#") return "#"; try { const parsed = new URL(value, window.location.href); return ["http:", "https:"].includes(parsed.protocol) ? attr(parsed.href) : "#"; } catch (_) { return "#"; } }
  function boundedNumber(value, fallback, minimum, maximum) { const parsed = Number(value); return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback; }

  function styles() {
    return `
      :host{--accent:${config.accent};--ink:#121212;--muted:#6c6c69;--paper:#f4f4f1;--lpf-results-height:${config.resultsHeight}px;display:block;color:var(--ink);font-family:Inter,Arial,sans-serif}*{box-sizing:border-box}.lpf-shell{position:relative;background:var(--paper);border:1px solid #deded9;border-radius:28px;box-shadow:0 24px 70px rgba(0,0,0,.12);overflow:hidden}:host([data-lpf-tilda-zero-block="true"]) #search-results{max-height:var(--lpf-results-height);overflow-y:auto;overscroll-behavior-y:auto;scrollbar-gutter:stable;-webkit-overflow-scrolling:touch}:host([data-lpf-tilda-zero-block="true"]) .lpf-toolbar{position:sticky;top:0;z-index:4;padding-top:17px;background:var(--paper);box-shadow:0 8px 16px rgba(18,18,18,.06)}.lpf-kicker{text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:800;color:#a26300;margin-bottom:12px}h2{font-size:clamp(30px,5vw,48px);font-weight:650;line-height:1.04;letter-spacing:-.035em;text-transform:uppercase;margin:0}h3{font-size:22px;font-weight:750;line-height:1.12;margin:15px 0 8px}p{line-height:1.5}input,select{width:100%;height:52px;border:1px solid #d4d4cf;border-radius:12px;background:#fff;color:var(--ink);font:inherit;font-size:15px;padding:0 15px;outline:none}input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}button,.lpf-route-box a,.lpf-map-route{border:0;border-radius:999px;min-height:46px;padding:11px 19px;font:inherit;font-size:13px;font-weight:750;cursor:pointer;text-decoration:none;text-align:center;display:inline-flex;align-items:center;justify-content:center;transition:transform .18s ease,filter .18s ease}button{background:var(--accent);color:#171717}button:hover,.lpf-route-box a:hover,.lpf-map-route:hover{filter:brightness(.96);transform:translateY(-1px)}button:disabled{cursor:default;opacity:.7;transform:none}
      .lpf-header{padding:38px 38px 28px;display:flex;align-items:start;justify-content:space-between;gap:24px;background:#fff;border-bottom:1px solid #deded9}.lpf-header h2{font-size:clamp(28px,4vw,42px);max-width:780px}.lpf-brand{font-size:12px;font-weight:850;background:#171717;color:#fff;border-radius:8px;padding:12px 15px;white-space:nowrap;letter-spacing:.04em}.lpf-brand:before{content:"▰";color:var(--accent);margin-right:7px}.lpf-filters{display:grid;grid-template-columns:2fr repeat(4,minmax(120px,1fr));gap:14px;padding:26px 38px;background:#1b1b1b;color:#fff;border-bottom:1px solid #303030}.lpf-filters label,.lpf-gate-form label{display:grid;gap:7px;font-size:12px;font-weight:650;color:inherit;min-width:0}.lpf-filters button{grid-column:5}.lpf-utility-options{grid-column:1/5;display:flex;align-items:center;gap:12px 22px;flex-wrap:wrap;min-width:0}.lpf-check{display:flex!important;flex-direction:row;align-items:center;gap:8px!important;font-weight:600!important;color:#d3d3cf!important}.lpf-check input,.lpf-consent input,.lpf-compare input{width:18px;height:18px;accent-color:var(--accent);margin:1px 0;flex:0 0 auto}.lpf-status{margin:24px 38px 0;color:var(--muted);font-size:13px}.lpf-status.error{color:#a12f2a}.lpf-status.success{color:#493800;background:#fff1be;border-left:4px solid var(--accent);padding:13px 15px;border-radius:8px}
      .lpf-workspace{padding-top:17px}.lpf-toolbar{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:0 38px 4px}.lpf-toolbar-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px}.lpf-tabs{display:flex;gap:6px;background:#deded8;border-radius:999px;padding:5px}.lpf-tab{background:transparent;min-height:44px;padding:8px 15px;color:#555;font-size:12px}.lpf-tab.active{background:#171717;color:#fff}.lpf-compare-shortcut{min-height:44px;padding:8px 15px;background:#171717;color:#fff;white-space:nowrap}.lpf-sort{display:flex;align-items:center;gap:9px;color:#777;font-size:11px}.lpf-sort select{height:44px;width:190px;font-size:12px;padding:0 12px;border-radius:999px}.lpf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-rows:1fr;align-items:stretch;gap:18px;padding:20px 38px 38px}.lpf-card{display:flex;flex-direction:column;height:100%;min-height:425px;background:#fff;border:1px solid #dadad5;border-radius:24px;padding:21px;min-width:0}.lpf-card.lpf-unavailable{border-color:#d9a7a2;background:#fffafa}.lpf-cardtop{display:flex;justify-content:space-between;gap:12px}.lpf-reference{font-size:10px;font-weight:900;letter-spacing:.1em;color:#8c5b0a}.lpf-score,.lpf-availability{font-size:11px;font-weight:800;color:#fff;padding:6px 9px;border-radius:999px;white-space:nowrap}.lpf-score{background:#171717}.lpf-availability{background:#9d342f}.lpf-unavailable-note{margin-top:12px;padding:9px 11px;border-radius:10px;background:#f5dedb;color:#7f2924;font-size:10px;line-height:1.35}.lpf-card h3{height:50px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;margin-bottom:8px}.lpf-location{height:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:13px;margin:0 0 17px}.lpf-facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;border-block:1px solid #e8e8e3;padding:15px 0}.lpf-facts span{display:block;color:#898985;font-size:10px;margin-bottom:5px}.lpf-facts strong{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lpf-card-insights{display:flex;justify-content:space-between;gap:8px;padding:12px 0 2px;font-size:10px;color:#777}.lpf-location-score strong{color:#171717}.lpf-utils{display:flex;align-content:flex-start;gap:7px;flex-wrap:wrap;min-height:55px;height:auto;overflow:visible;padding:10px 0 7px}.lpf-utils span{font-size:9px;background:#f0f0ed;border-radius:999px;padding:7px 8px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}.lpf-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:auto}.lpf-secondary{background:#fff!important;color:#171717!important;border:1px solid #171717!important}.lpf-compare-toggle{width:100%;min-height:40px;margin:3px 0 10px;padding:8px 12px;background:#f0f0ed;color:#171717;border:1px solid #d8d8d2;font-size:11px}.lpf-compare-toggle.active{background:#171717;color:#fff;border-color:#171717}.lpf-more{text-align:center;padding:0 38px 38px}.lpf-more button{min-width:220px}
      .lpf-preview{position:relative;min-height:540px}.lpf-blurred{filter:blur(8px);user-select:none;pointer-events:none;opacity:.58}.lpf-skeleton{color:#4e4e4b}.lpf-gate{position:absolute;z-index:2;left:50%;top:50%;transform:translate(-50%,-50%);width:min(520px,calc(100% - 40px));background:#191919;color:#fff;border:1px solid #343434;border-radius:24px;padding:30px;box-shadow:0 24px 80px rgba(0,0,0,.35)}.lpf-gate h3{font-size:27px;text-transform:uppercase;margin:0 0 10px}.lpf-gate>p{color:#b8b8b3;font-size:14px;margin:0 0 20px}.lpf-gate-form{display:grid;gap:13px}.lpf-consent{grid-template-columns:20px 1fr!important;align-items:start;font-weight:400!important;line-height:1.45;color:#c1c1bc!important}.lpf-consent a,.lpf-link{color:#b17700}.lpf-notice,.lpf-demo{padding:12px 14px;border-radius:10px;margin:0 0 14px;font-size:12px}.lpf-notice{background:#4a2421;color:#ffd3cf}.lpf-demo{background:#302c1e;color:#ffe395}.lpf-back,.lpf-link,.lpf-text-button{padding:0;min-height:auto;background:none;border:0;font-weight:700}.lpf-back{color:#aaa9a5;margin-bottom:18px}.lpf-bottom{display:flex;justify-content:space-between;align-items:center;padding:0 38px 26px;color:#7d7d79;font-size:11px}.lpf-bottom .lpf-link{min-height:44px;padding:8px 0}.lpf-bottom button[hidden]{display:none}
      .lpf-dialog-layer{position:fixed;z-index:2147483000;top:var(--lpf-modal-top-offset,0px);right:0;bottom:0;left:0;background:rgba(0,0,0,.7);display:block;padding:20px;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}.lpf-dialog{position:relative;width:min(880px,100%);margin:0 auto;overflow:visible;background:#f4f4f1;color:#171717;border-radius:26px;padding:30px;box-shadow:0 30px 100px rgba(0,0,0,.5)}.lpf-dialog-close{position:sticky;z-index:5;top:0;margin:-14px -12px -34px auto;display:flex;background:#171717;color:#fff;min-height:44px;width:44px;padding:0;font-size:22px;box-shadow:0 6px 18px rgba(0,0,0,.22)}.lpf-detail-head{display:flex;justify-content:space-between;gap:24px;padding:4px 50px 24px 0;border-bottom:1px solid #d7d7d1;min-width:0}.lpf-detail-head>div:first-child{min-width:0}.lpf-detail-head h3{font-size:30px;margin:9px 0 5px;max-width:620px;overflow-wrap:anywhere}.lpf-detail-head p{font-size:13px;color:#777;margin:0}.lpf-big-score{min-width:112px;height:90px;border-radius:18px;background:#171717;color:#fff;display:grid;place-content:center;text-align:center}.lpf-big-score strong{font-size:29px}.lpf-big-score span{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#bbb}.lpf-unavailable-banner{display:flex;justify-content:space-between;gap:16px;margin-top:16px;padding:13px 15px;border-radius:12px;background:#f5dedb;color:#7f2924}.lpf-unavailable-banner strong{font-size:12px}.lpf-unavailable-banner span{font-size:10px}.lpf-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:18px 0}.lpf-detail-grid>section,.lpf-similar{background:#fff;border:1px solid #dddcd6;border-radius:18px;padding:19px;min-width:0}.lpf-section-title{font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:.09em;margin-bottom:14px}.lpf-detail-facts{display:grid;grid-template-columns:1fr 1fr;gap:13px}.lpf-detail-facts span{display:block;font-size:9px;color:#888;margin-bottom:4px}.lpf-detail-facts strong{font-size:12px;overflow-wrap:anywhere}.lpf-bullets{list-style:none;padding:0;margin:8px 0;display:grid;gap:7px}.lpf-bullets li{display:grid;grid-template-columns:19px 1fr;gap:7px;font-size:11px;line-height:1.35}.lpf-bullets li span{display:grid;place-items:center;width:18px;height:18px;border-radius:50%;background:#dcead5;color:#315125;font-weight:900}.lpf-bullets.warning li span{background:#fff0c7;color:#7b5700}.lpf-utility-table{display:grid;grid-template-columns:1fr 1fr;gap:10px}.lpf-utility-table div{border-bottom:1px solid #eee;padding-bottom:8px;min-width:0}.lpf-utility-table span{display:block;font-size:9px;color:#888}.lpf-utility-table strong{font-size:11px;overflow-wrap:anywhere}.lpf-utility-table .unknown{color:#aaa}.lpf-location-summary{display:flex;align-items:center;gap:12px}.lpf-location-summary>strong{font-size:25px}.lpf-location-summary>span{font-size:11px}.lpf-confidence,.lpf-muted{font-size:10px;color:#777}.lpf-route-box{display:flex;justify-content:space-between;align-items:center;gap:20px;background:#171717;color:#fff;border-radius:18px;padding:18px 20px;margin-bottom:18px}.lpf-route-box .lpf-section-title{margin-bottom:5px}.lpf-route-box p{font-size:10px;color:#bbb;margin:0;max-width:470px}.lpf-route-box>div:last-child{display:flex;gap:8px}.lpf-route-box a{background:var(--accent);color:#171717;font-size:11px;min-height:38px}.lpf-route-box .lpf-alt-link{background:#fff}.lpf-similar{margin-bottom:18px}.lpf-similar-list{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.lpf-similar-list button{display:grid;justify-content:start;text-align:left;border-radius:12px;background:#f0f0ec;min-height:62px}.lpf-similar-list span{font-size:9px;color:#777;margin-top:3px}.lpf-detail-actions{display:grid;grid-template-columns:.7fr .9fr 1.6fr;gap:10px}.lpf-request-success,.lpf-request-error{margin-top:12px;padding:12px 14px;border-radius:11px;font-size:11px;line-height:1.4}.lpf-request-success{background:#dcead5;color:#315125}.lpf-request-error{background:#f3d7d4;color:#7f2924}.lpf-dialog>small{display:block;color:#777;font-size:9px;line-height:1.4;margin-top:12px}
      .lpf-map-layout{display:grid;grid-template-columns:280px 1fr;gap:16px;padding:20px 38px 38px}.lpf-map-list{max-height:540px;overflow:auto;display:grid;align-content:start;gap:8px}.lpf-map-provider{display:grid;grid-template-columns:1fr 1fr;gap:5px;background:#dddcd7;border-radius:999px;padding:4px;margin-bottom:4px}.lpf-map-provider button{min-height:34px;padding:6px;background:transparent}.lpf-map-provider button.active{background:#171717;color:#fff}.lpf-map-item{display:grid;justify-content:start;text-align:left;background:#fff;border:1px solid #dadad5;border-radius:14px;padding:13px;min-height:76px}.lpf-map-item.active{border:2px solid var(--accent)}.lpf-map-item span,.lpf-map-item small{font-size:10px;color:#777;margin-top:3px}.lpf-map-canvas{position:relative;min-height:540px;background:#ddd;border-radius:20px;overflow:hidden}.lpf-map-canvas iframe{width:100%;height:100%;min-height:540px;border:0}.lpf-map-note{position:absolute;left:14px;top:14px;background:rgba(23,23,23,.9);color:#fff;border-radius:999px;padding:9px 12px;font-size:9px}.lpf-map-route{position:absolute;right:14px;bottom:14px;background:var(--accent);color:#171717}.lpf-saved-intro{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:18px 38px 0;background:#fff;border-radius:16px;padding:15px 18px}.lpf-saved-intro strong,.lpf-saved-intro span{display:block}.lpf-saved-intro span{font-size:10px;color:#777;margin-top:3px}.lpf-saved-intro .lpf-saved-error{color:#9d342f}.lpf-saved-tools{display:flex;align-items:center;gap:12px}.lpf-saved-tools .lpf-secondary{min-height:36px;padding:7px 14px}.lpf-text-button{color:#985f00}.lpf-compare-panel{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:12px 38px 0;padding:15px 18px;background:#fff1be;border:1px solid #edd98c;border-radius:16px}.lpf-compare-panel strong,.lpf-compare-panel span{display:block}.lpf-compare-panel span{margin-top:4px;color:#70612d;font-size:10px}.lpf-compare-panel button{min-width:210px}.lpf-comparison{scroll-margin-top:18px;margin:14px 38px 0;background:#171717;color:#fff;border-radius:18px;padding:15px;overflow:auto}.lpf-comparison-grid{display:grid;grid-template-columns:130px repeat(var(--compare-count),minmax(110px,1fr));gap:8px;padding:8px;border-top:1px solid #333;font-size:10px;min-width:max-content}.lpf-comparison-grid:first-child{border:0}.lpf-comparison-grid span{color:#aaa}.lpf-empty{display:grid;gap:7px;margin:20px 38px 38px;background:#fff;border:1px solid #ddd;border-radius:18px;padding:30px;text-align:center}.lpf-empty span{font-size:11px;color:#777}
      @media(max-width:980px){.lpf-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.lpf-utility-options{grid-column:1/-1}.lpf-filters button{grid-column:auto}.lpf-grid{grid-template-columns:repeat(2,1fr)}.lpf-map-layout{grid-template-columns:220px 1fr}.lpf-toolbar{align-items:stretch}.lpf-toolbar-actions{display:grid}.lpf-compare-shortcut{grid-row:2}.lpf-sort{grid-row:1}}@media(max-width:700px){.lpf-shell{border-radius:18px}.lpf-header,.lpf-filters,.lpf-grid{padding-left:20px;padding-right:20px}.lpf-header{display:block}.lpf-brand{display:inline-block;margin-top:18px}.lpf-filters{grid-template-columns:1fr}.lpf-utility-options{grid-column:1;display:grid;grid-template-columns:1fr 1fr;gap:12px}.lpf-filters button{grid-column:1;width:100%}.lpf-grid{grid-template-columns:1fr}.lpf-status{margin-left:20px;margin-right:20px}.lpf-bottom{padding-left:20px;padding-right:20px;display:grid;gap:10px}.lpf-toolbar{padding:0 20px;align-items:stretch;display:grid}.lpf-toolbar-actions{display:grid;grid-template-columns:1fr}.lpf-tabs{overflow:auto;overscroll-behavior-inline:contain}.lpf-compare-shortcut{grid-row:auto;width:100%}.lpf-sort{grid-row:auto;justify-content:space-between}.lpf-sort select{width:min(210px,60vw)}.lpf-gate{position:absolute;top:24px;transform:translateX(-50%);padding:24px}.lpf-preview{min-height:620px}.lpf-dialog-layer{padding:0}.lpf-detail{width:100%;min-height:100dvh;margin:0;border-radius:0;padding:22px 18px 28px}.lpf-dialog-close{top:max(8px,env(safe-area-inset-top));margin:-10px -6px -34px auto}.lpf-detail-head{display:grid;padding-right:44px}.lpf-big-score{height:70px}.lpf-unavailable-banner{display:grid}.lpf-detail-grid{grid-template-columns:1fr}.lpf-detail-actions{grid-template-columns:1fr}.lpf-route-box{display:grid}.lpf-route-box>div:last-child{display:grid}.lpf-similar-list{grid-template-columns:1fr}.lpf-map-layout{grid-template-columns:1fr;padding:20px}.lpf-map-list{grid-template-columns:repeat(2,1fr);max-height:220px}.lpf-map-provider{grid-column:1/-1}.lpf-map-canvas,.lpf-map-canvas iframe{min-height:410px}.lpf-saved-intro{margin-left:20px;margin-right:20px;display:grid}.lpf-saved-tools{justify-content:space-between}.lpf-compare-panel{margin-left:20px;margin-right:20px;display:grid}.lpf-compare-panel button{width:100%;min-width:0}.lpf-comparison{margin-left:20px;margin-right:20px}.lpf-comparison-grid{grid-template-columns:100px repeat(var(--compare-count),minmax(90px,1fr))}}@media(max-width:400px){.lpf-utility-options{grid-template-columns:1fr}.lpf-tabs{border-radius:16px}.lpf-tab{padding-inline:12px}.lpf-detail-facts,.lpf-utility-table{grid-template-columns:1fr}.lpf-map-list{grid-template-columns:1fr}}
      .lpf-demo{display:flex;align-items:center;justify-content:space-between;gap:12px}.lpf-demo strong{font-size:20px;letter-spacing:.14em}.lpf-demo button{min-height:34px;padding:6px 12px;background:#ffe395;white-space:nowrap;font-size:11px}
      .lpf-utils .lpf-utility-present{background:#e6f1e3;color:#275b2c}.lpf-utils .lpf-utility-absent{background:#f8e6e4;color:#952e28}.lpf-utils .lpf-utility-unknown{background:#efefed;color:#676762}.lpf-utility-table .absent{color:#952e28}
      .lpf-source-link{display:inline-flex;align-items:center;justify-content:center;min-height:36px;margin:0 0 10px;color:#6f4800;font-size:11px;font-weight:750;text-decoration:underline;text-underline-offset:3px}.lpf-source-link:hover{text-decoration:none}.lpf-source-detail{min-height:46px;margin:0;padding:11px 16px;border:1px solid #171717;border-radius:999px;color:#171717;background:#fff;text-decoration:none;text-align:center}.lpf-detail-actions{grid-template-columns:1fr 1fr 1.4fr}
      .lpf-interest{background:#fff1be;border:1px solid #d8bb64;color:#4e3b12;min-height:42px;margin:0 0 10px}.lpf-interest.active{background:#e6f1e3;border-color:#b9d4ba;color:#275b2c;opacity:1}.lpf-detail-actions .lpf-interest{margin:0}.lpf-card>.lpf-interest{width:100%}.lpf-detail-actions{grid-template-columns:repeat(2,minmax(0,1fr))}.lpf-detail-actions .lpf-source-detail,.lpf-detail-actions .lpf-interest{width:100%}
      .lpf-telegram-promo{margin:20px 38px 36px;padding:22px 24px;background:#1d1d1d;color:#fff;border-radius:20px;display:flex;align-items:center;justify-content:space-between;gap:20px}.lpf-telegram-promo strong,.lpf-telegram-promo span{display:block}.lpf-telegram-promo strong{font-size:17px;margin-bottom:6px}.lpf-telegram-promo span{font-size:12px;line-height:1.5;color:#c9c9c3;max-width:580px}.lpf-telegram-promo button,.lpf-telegram-open{background:var(--accent);color:#171717;border-radius:999px;min-height:44px;padding:12px 20px;white-space:nowrap;font-size:12px;font-weight:800;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}.lpf-telegram-modal{max-width:520px}.lpf-telegram-modal h3{font-size:27px;line-height:1.15;margin:12px 40px 14px 0}.lpf-telegram-modal p{font-size:13px;line-height:1.55;color:#555}.lpf-telegram-modal>button:not(.lpf-dialog-close){background:var(--accent);margin:12px 0}.lpf-telegram-modal small{display:block;margin-top:18px}.lpf-telegram-modal #telegram-invite-result{font-size:12px;color:#9d322a;margin-top:10px}
      @media(max-width:700px){.lpf-detail-actions{grid-template-columns:1fr}.lpf-telegram-promo{margin:18px 20px 28px;padding:20px;display:grid}.lpf-telegram-promo button{width:100%;white-space:normal}.lpf-telegram-modal{width:100%;min-height:100dvh;border-radius:0;padding:24px 20px}.lpf-telegram-modal h3{font-size:24px}}
      @media(min-width:981px){.lpf-header{padding:27px 32px 21px}.lpf-header h2{font-size:clamp(25px,2.7vw,34px);max-width:750px}.lpf-kicker{margin-bottom:9px}.lpf-filters{padding:20px 32px;gap:11px}.lpf-filters input,.lpf-filters select{height:46px;font-size:14px}.lpf-status{margin:17px 32px 0}.lpf-workspace{padding-top:12px}.lpf-toolbar{padding-left:32px;padding-right:32px}.lpf-grid{padding:17px 32px 28px;gap:15px}.lpf-bottom{padding:0 32px 18px}}
    `;
  }
})();
