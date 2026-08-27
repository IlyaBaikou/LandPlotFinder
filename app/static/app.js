const state = {
  view: 'dashboard', page: 1, settings: null, profiles: [], profileId: 'default',
  map: null, mapMarkers: null, detail: null, widgetAdmin: null,
};
let viewerMode = false;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[char]));
const number = (value, fallback = '—') => value === null || value === undefined || value === ''
  ? fallback : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value);
const money = (value) => value == null ? 'Цена не указана'
  : `$${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value)}`;
const dt = (value) => value ? new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium', timeStyle: 'short',
}).format(new Date(value)) : '—';
const decisions = { new: 'Не разобрано', liked: 'Приглянулось', studying: 'Изучаем', trip: 'К поездке', rejected: 'Отказ' };
const presentationMode = new URLSearchParams(location.search).get('presentation') === '1';
const sourceNames = presentationMode
  ? { realt: 'Площадка A', kufar: 'Площадка B', domovita: 'Площадка C', realt_auction: 'Торги A', rlt_auction: 'Торги B', e_auction: 'Торги C' }
  : { realt: 'Realt.by', kufar: 'Kufar', domovita: 'Domovita.by', realt_auction: 'Аукционы Realt', rlt_auction: 'RLT', e_auction: 'e-auction.by' };
const healthNames = { healthy: 'Работает', degraded: 'Нужна проверка', error: 'Ошибка', empty: 'Пустая выдача', never: 'Ещё не проверен' };
const mapProviderNames = { google: 'Google Maps', yandex: 'Яндекс Карты' };
const displayTitle = (listing) => presentationMode
  ? `Участок ${listing.area_sotok == null ? '' : `${number(listing.area_sotok)} сот. `}в выбранном районе`
  : listing.title;
const displayPlace = (listing) => presentationMode
  ? 'Выбранное направление, пригород'
  : [listing.locality, listing.district, listing.address].filter(Boolean).join(', ');
const displayPhone = (phone) => presentationMode ? '+375 •• •••-••-••' : phone;

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, { ...options, headers });
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON response */ }
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить действие');
  return data;
}
function profileParams(extra = {}) { return new URLSearchParams({ profile_id: state.profileId, ...extra }); }
function toast(text) {
  const element = $('#toast'); element.textContent = text; element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 2800);
}
function setView(view) {
  state.view = view;
  $$('.view').forEach((element) => element.classList.toggle('active', element.id === `view-${view}`));
  $$('.nav-item').forEach((element) => element.classList.toggle('active', element.dataset.view === view));
  const titles = {
    dashboard: ['Ваш поиск', 'Добрый день'], listings: ['Каталог', 'Найденные объекты'],
    map: ['География', 'Карта участков'], health: ['Надёжность', 'Источники данных'],
    widget: ['Клиентский подбор', 'Виджет для сайта'],
    settings: ['Конфигурация', 'Настройки поиска'],
  };
  $('#pageEyebrow').textContent = titles[view][0]; $('#pageTitle').textContent = titles[view][1];
  $('.sidebar').classList.remove('open');
  if (view === 'listings') loadListings();
  if (view === 'map') loadMap();
  if (view === 'widget') loadWidgetAdmin();
  if (view === 'health') loadHealth();
  if (view === 'settings') { fillSettings(); loadBackupInfo(); }
  location.hash = view;
}

async function loadProfiles() {
  const data = await api('/api/profiles'); state.profiles = data.items; state.profileId = data.active_profile_id;
  $('#profileSelect').innerHTML = state.profiles.map((profile, index) => `<option value="${esc(profile.id)}">${esc(presentationMode ? `Демо-профиль ${index + 1}` : profile.name)}</option>`).join('');
  $('#profileSelect').value = state.profileId;
}
async function switchProfile(profileId) {
  if (!viewerMode) await api(`/api/profiles/${encodeURIComponent(profileId)}/activate`, { method: 'POST' });
  state.profileId = profileId; state.page = 1;
  if (!viewerMode) await loadSettings(); else { $('#profileSelect').value = profileId; fillSettings(); }
  await Promise.all([loadSummary(), loadDashboard()]);
  if (state.view === 'listings') loadListings();
  if (state.view === 'map') loadMap();
  if (state.view === 'health') loadHealth();
}
async function loadSummary() {
  try {
    const data = await api(`/api/summary?${profileParams()}`);
    $('#statActive').textContent = number(data.active); $('#statStrong').textContent = number(data.high_score);
    $('#statSelected').textContent = number(data.selected); $('#statArchived').textContent = number(data.archived);
    renderJob(data.job, data.latest_run); if (!viewerMode && !data.configured) $('#setupModal').classList.remove('hidden');
  } catch { $('#serviceDot').style.background = '#a54f42'; $('#serviceText').textContent = 'Нет соединения'; }
}
function renderJob(job, latest) {
  const running = job.running; $('#jobPill').classList.toggle('running', running);
  $('#jobPill b').textContent = running ? (job.kind === 'scan' ? 'Идёт поиск' : 'Проверяем объявления') : 'В ожидании';
  const scheduled = (job.scheduled || []).find((item) => item.id === `listing-scan:${state.profileId}`) || (job.scheduled || [])[0];
  $('#jobDetails').innerHTML = `<strong>${running ? 'Задача выполняется' : 'Сервис готов'}</strong>${latest ? `Последний поиск: ${esc(dt(latest.completed_at || latest.started_at))}<br>Статус: ${esc(latest.status)}` : 'Поиск ещё не запускался.'}${scheduled?.next_run_at ? `<br>Следующий запуск: ${esc(dt(scheduled.next_run_at))}` : ''}${job.last_error ? `<br><span style="color:#a54f42">${esc(job.last_error)}</span>` : ''}`;
}
async function loadDashboard() {
  try {
    const data = await api(`/api/listings?${profileParams({ min_score: 75, page_size: 4 })}`);
    $('#dashboardListings').innerHTML = data.items.length ? data.items.map(compactCard).join('')
      : '<div class="empty">Подходящих объектов пока нет. Запустите первый поиск.</div>';
  } catch (error) { $('#dashboardListings').innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
}
function compactCard(listing) {
  return `<article class="compact-listing" data-open="${listing.id}"><div class="score-badge">${listing.score}</div><div><h4>${esc(displayTitle(listing))}</h4><div class="meta"><span>${esc(displayPlace(listing) || 'Место не указано')}</span><span>${number(listing.area_sotok)} сот.</span><span>${number(listing.distance_mkad_km)} км</span></div></div><div class="price">${money(listing.price_usd)}</div></article>`;
}
function card(listing) {
  return `<article class="listing-card"><div class="card-top"><span class="source-tag">${esc(sourceNames[listing.source] || listing.source)}</span><span class="decision-tag ${listing.decision}">${esc(decisions[listing.decision] || listing.decision)}</span></div><h3>${esc(displayTitle(listing))}</h3><div class="location">${esc(displayPlace(listing) || 'Место не указано')}</div><div class="numbers"><div><span>Цена</span><b>${money(listing.price_usd)}</b></div><div><span>Площадь</span><b>${number(listing.area_sotok)} сот.</b></div><div><span>До МКАД</span><b>${number(listing.distance_mkad_km)} км</b></div></div><div class="meta"><span>Оценка <b>${listing.score}/100</b></span>${listing.possible_duplicate ? '<span>Возможный дубль</span>' : ''}</div><div class="card-actions"><button data-open="${listing.id}">Подробнее</button><button class="${listing.decision === 'liked' ? 'liked' : ''}" data-decision="liked" data-id="${listing.id}">☆ Приглянулось</button></div></article>`;
}
let searchTimer;
async function loadListings() {
  clearTimeout(searchTimer); searchTimer = setTimeout(async () => {
    const params = profileParams({ q: $('#searchInput').value, decision: $('#decisionFilter').value,
      source: $('#sourceFilter').value, active: $('#activeFilter').value, min_score: $('#scoreInput').value,
      page: String(state.page), page_size: '24' });
    try {
      const data = await api(`/api/listings?${params}`); $('#resultsCount').textContent = `Найдено: ${number(data.total)}`;
      $('#listingsGrid').innerHTML = data.items.length ? data.items.map(card).join('') : '<div class="empty">По этим условиям ничего не найдено.</div>';
      renderPagination(data.page, data.pages);
    } catch (error) { $('#listingsGrid').innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
  }, 180);
}
function renderPagination(page, pages) {
  const values = []; for (let current = Math.max(1, page - 2); current <= Math.min(pages, page + 2); current += 1) {
    values.push(`<button class="${current === page ? 'active' : ''}" data-page="${current}">${current}</button>`);
  } $('#pagination').innerHTML = pages > 1 ? values.join('') : '';
}

async function openDetail(id) {
  try {
    const [listing, history] = await Promise.all([api(`/api/listings/${id}?${profileParams()}`), api(`/api/listings/${id}/history`)]);
    state.detail = listing;
    const facts = [
      ['Цена', money(listing.price_usd)], ['Площадь', `${number(listing.area_sotok)} сот.`],
      ['Расстояние', `${number(listing.distance_mkad_km)} км`], ['Направление', listing.direction || '—'],
      ['Электричество', listing.electricity_kw ? `${listing.electricity_kw} кВт` : listing.electricity || '—'],
      ['Газ', listing.gas || '—'], ['Водоснабжение', listing.water || '—'],
      ['Канализация', listing.sewerage || '—'], ['Назначение земли', listing.purpose || '—'],
      ['Право на землю', listing.ownership || '—'], ['Интернет', listing.internet || '—'], ['Дорога', listing.road || '—'],
    ];
    const factsHtml = facts.map(([label, value]) => `<div><small>${esc(label)}</small><b>${esc(value)}</b></div>`).join('');
    const decisionControls = viewerMode ? '' : `<h3>Ваше решение</h3><div class="decision-row">${Object.entries(decisions).map(([key, value]) => `<button class="${listing.decision === key ? 'active' : ''}" data-detail-decision="${key}">${value}</button>`).join('')}</div><textarea class="detail-note" id="detailNote" placeholder="Заметки об участке">${esc(presentationMode ? '' : listing.note)}</textarea><button class="button secondary full" id="saveDecision">Сохранить решение</button>`;
    const sourceLink = listing.url && !viewerMode ? `<a class="button primary full" href="${esc(listing.url)}" target="_blank" rel="noopener">Открыть исходную карточку ↗</a>` : '';
    $('#drawerContent').innerHTML = `<div class="detail-score">${listing.score}</div><p class="eyebrow">${esc(sourceNames[listing.source] || listing.source)} · ${listing.active ? 'актуально' : 'архив'}</p><h2 class="detail-title">${esc(displayTitle(listing))}</h2><p>${esc(displayPlace(listing))}</p><div class="detail-grid">${factsHtml}</div>${decisionControls}${sourceLink}${listing.description ? `<h3>Описание</h3><div class="detail-description">${esc(presentationMode ? 'Описание и контакты скрыты в демонстрационном режиме.' : listing.description)}</div>` : ''}${historyBlock(history)}`;
    const drawer = $('#detailDrawer');
    drawer.scrollTop = 0; drawer.classList.add('open'); $('#drawerShade').classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('drawer-open');
  } catch (error) { toast(error.message); }
}
function historyBlock(history) {
  const prices = history.snapshots.filter((item) => item.price_usd != null); const events = [...history.events].reverse().slice(0, 12);
  return `<section class="history-block"><p class="eyebrow">История объявления</p><h3>Цена и изменения</h3><p class="meta">Впервые найдено: ${esc(dt(history.first_seen_at))} · Последний раз замечено: ${esc(dt(history.last_seen_at))}</p>${priceChart(prices)}<div class="timeline">${events.length ? events.map(historyEvent).join('') : '<p class="meta">Изменений пока не было.</p>'}</div></section>`;
}
function priceChart(points) {
  if (!points.length) return '<div class="empty">История цены появится после обновлений.</div>';
  const values = points.map((point) => Number(point.price_usd)); const minimum = Math.min(...values); const maximum = Math.max(...values); const range = Math.max(1, maximum - minimum);
  const coordinates = values.map((value, index) => `${points.length === 1 ? 50 : 5 + (index / (points.length - 1)) * 90},${90 - ((value - minimum) / range) * 75}`);
  const circles = coordinates.map((pair) => { const [x, y] = pair.split(','); return `<circle cx="${x}" cy="${y}" r="2.4" fill="#b8874d"></circle>`; }).join('');
  return `<div class="history-chart"><svg viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points="${coordinates.join(' ')}" fill="none" stroke="#31473a" stroke-width="2" vector-effect="non-scaling-stroke"></polyline>${circles}</svg><div class="history-axis"><span>${money(values[0])}</span><span>${money(values[values.length - 1])}</span></div></div>`;
}
function historyEvent(event) {
  const labels = { created: 'Объявление впервые найдено', updated: 'Данные объявления изменились', archived: 'Объявление снято с публикации', reactivated: 'Объявление снова опубликовано', decision: 'Изменено ваше решение' };
  let detail = ''; if (event.type === 'updated') detail = Object.values(event.payload.changes || {}).map((change) => change.label).join(', ');
  else if (event.type === 'decision') detail = `${decisions[event.payload.from] || event.payload.from || '—'} → ${decisions[event.payload.to] || event.payload.to || '—'}`;
  else if (event.payload.reason) detail = event.payload.reason;
  return `<div class="timeline-item"><i></i><div><b>${esc(labels[event.type] || event.type)}</b>${esc(dt(event.occurred_at))}${detail ? ` · ${esc(detail)}` : ''}</div></div>`;
}
function closeDetail() { $('#detailDrawer').classList.remove('open'); $('#drawerShade').classList.remove('open'); $('#detailDrawer').setAttribute('aria-hidden', 'true'); document.body.classList.remove('drawer-open'); }
async function quickDecision(id, decision) {
  try { await api(`/api/listings/${id}/decision`, { method: 'PUT', body: JSON.stringify({ state: decision, note: '' }) }); toast('Решение сохранено'); loadListings(); loadDashboard(); loadSummary(); }
  catch (error) { toast(error.message); }
}
async function saveDetailDecision() {
  if (!state.detail) return; const active = $('[data-detail-decision].active'); const decision = active?.dataset.detailDecision || state.detail.decision;
  try { await api(`/api/listings/${state.detail.id}/decision`, { method: 'PUT', body: JSON.stringify({ state: decision, note: $('#detailNote').value }) }); toast('Решение и заметка сохранены'); closeDetail(); loadListings(); loadDashboard(); loadSummary(); }
  catch (error) { toast(error.message); }
}
async function runJob(kind) {
  try { const query = kind === 'scan' ? `?${profileParams()}` : ''; await api(`/api/jobs/${kind}${query}`, { method: 'POST' }); toast(kind === 'scan' ? 'Поиск запущен' : 'Проверка запущена'); pollJobs(); }
  catch (error) { toast(error.message); }
}
function pollJobs() {
  const timer = setInterval(async () => { try { const job = await api('/api/jobs'); renderJob(job, null); if (!job.running) { clearInterval(timer); loadSummary(); loadDashboard(); loadHealth(); if (state.view === 'listings') loadListings(); toast(job.last_error ? 'Задача завершилась с ошибкой' : 'Задача завершена'); } } catch { clearInterval(timer); } }, 2500);
}

async function loadSettings() {
  state.settings = await api('/api/settings'); state.profileId = state.settings.active_profile_id; $('#profileSelect').value = state.profileId;
  $('#mapProviderHint').textContent = `Использовать: ${mapProviderNames[state.settings.map_provider] || 'Google Maps'}`;
  if (!viewerMode && !state.settings.configured) $('#setupModal').classList.remove('hidden'); fillSettings();
}
function activeProfile() { return state.profiles.find((profile) => profile.id === state.profileId) || state.settings?.profiles?.find((profile) => profile.id === state.profileId) || state.settings; }
function fillSettings() {
  if (!state.settings) return; const form = $('#settingsForm'); const values = { ...state.settings, ...activeProfile() };
  Object.entries(values).forEach(([key, value]) => { const elements = [...form.querySelectorAll(`[name="${key}"]`)]; if (!elements.length) return;
    if (key === 'sources') elements.forEach((element) => { element.checked = value.includes(element.value); });
    else if (elements[0].type === 'checkbox') elements[0].checked = Boolean(value); else elements[0].value = value ?? ''; });
}
function formObject(form) {
  const formData = new FormData(form); const result = {};
  for (const [key, value] of formData) { if (key === 'sources') (result.sources ??= []).push(value); else result[key] = value; }
  ['enabled', 'schedule_enabled', 'activity_check_enabled', 'telegram_enabled'].forEach((key) => { result[key] = form.querySelector(`[name="${key}"]`)?.checked || false; });
  ['target_price_usd', 'max_price_usd', 'min_area_sotok', 'max_area_sotok', 'max_distance_km', 'primary_electricity_kw', 'secondary_electricity_kw', 'schedule_interval_hours'].forEach((key) => { if (key in result) result[key] = Number(result[key]); });
  result.sources ||= []; return result;
}
function profilePayload(data) {
  return { name: data.name, enabled: data.enabled, schedule_enabled: data.schedule_enabled,
    schedule_interval_hours: data.schedule_interval_hours, sources: data.sources,
    target_price_usd: data.target_price_usd, max_price_usd: data.max_price_usd,
    min_area_sotok: data.min_area_sotok, max_area_sotok: data.max_area_sotok,
    max_distance_km: data.max_distance_km, primary_electricity_kw: data.primary_electricity_kw,
    secondary_electricity_kw: data.secondary_electricity_kw };
}
async function saveSettings(form) {
  try {
    const data = formObject(form); const profile = profilePayload(data);
    await api(`/api/profiles/${encodeURIComponent(state.profileId)}`, { method: 'PUT', body: JSON.stringify(profile) });
    state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ ...profile,
      activity_check_enabled: data.activity_check_enabled, telegram_enabled: data.telegram_enabled,
      telegram_bot_token: data.telegram_bot_token, telegram_chat_id: data.telegram_chat_id,
      map_provider: data.map_provider }) });
    $('#mapProviderHint').textContent = `Использовать: ${mapProviderNames[state.settings.map_provider] || 'Google Maps'}`;
    await loadProfiles(); fillSettings(); $('#setupModal').classList.add('hidden'); toast('Настройки сохранены'); loadSummary();
  } catch (error) { toast(error.message); }
}
async function saveSetup(form) {
  const data = formObject(form); const current = await api('/api/settings');
  state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ ...current, ...data,
    sources: ['realt', 'kufar', 'domovita'], target_price_usd: Math.min(20000, data.max_price_usd),
    primary_electricity_kw: 20, secondary_electricity_kw: 6, activity_check_enabled: true,
    telegram_enabled: false, telegram_bot_token: '', telegram_chat_id: '', schedule_interval_hours: 6 }) });
  await loadProfiles(); $('#setupModal').classList.add('hidden'); fillSettings(); toast('Готово. Запускаем первый поиск'); runJob('scan');
}
async function createProfile() {
  const current = activeProfile(); const name = prompt('Название нового профиля', `Новый поиск ${state.profiles.length + 1}`); if (!name?.trim()) return;
  try { const created = await api('/api/profiles', { method: 'POST', body: JSON.stringify({ ...current, name: name.trim() }) }); await loadProfiles(); await switchProfile(created.id); toast('Профиль создан'); }
  catch (error) { toast(error.message); }
}
async function deleteProfile() {
  const profile = activeProfile(); if (!confirm(`Удалить профиль «${profile.name}»? Объявления останутся в общей базе.`)) return;
  try { const result = await api(`/api/profiles/${encodeURIComponent(state.profileId)}`, { method: 'DELETE' }); await loadProfiles(); await switchProfile(result.active_profile_id); toast('Профиль удалён'); }
  catch (error) { toast(error.message); }
}

async function loadHealth() {
  try { const data = await api(`/api/source-health?${profileParams()}`); $('#sourceHealthGrid').innerHTML = data.items.length ? data.items.map(healthCard).join('') : '<div class="empty">В этом профиле нет источников.</div>'; }
  catch (error) { $('#sourceHealthGrid').innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
}

async function loadWidgetAdmin() {
  try {
    state.widgetAdmin = await api('/api/widget/leads');
    $('#widgetLeadCount').textContent = number(state.widgetAdmin.total, '0');
    $('#widgetRequestCount').textContent = number(state.widgetAdmin.requests_total, '0');
    const since = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const recent = widgetRequests().filter((item) => new Date(item.created_at).getTime() >= since).length;
    $('#widgetRecentCount').textContent = number(recent, '0');
    renderWidgetRequests();
  } catch (error) {
    $('#widgetRequestList').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}
function widgetRequests() {
  return (state.widgetAdmin?.items || []).flatMap((lead) => (lead.interests || []).map((interest) => ({
    ...interest, phone: lead.phone, lead_id: lead.id, lead_source_page: lead.source_page,
  }))).sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
}
function renderWidgetRequests() {
  const query = ($('#widgetSearchInput')?.value || '').trim().toLowerCase();
  const requests = widgetRequests().filter((item) => !query || [item.phone, item.reference, item.title, JSON.stringify(item.search_params || {})].join(' ').toLowerCase().includes(query));
  $('#widgetRequestList').innerHTML = requests.length ? requests.map(widgetRequestCard).join('')
    : '<div class="empty">Запросов по конкретным объявлениям пока нет.</div>';
}
function widgetRequestCard(item) {
  const sourceLink = item.url && !viewerMode ? `<a class="button primary" href="${esc(item.url)}" target="_blank" rel="noopener">Исходная карточка ↗</a>` : '';
  return `<article class="widget-request"><div class="widget-request-head"><div><span class="widget-ref">${esc(item.reference)}</span><h4>${esc(displayTitle(item))}</h4></div><time>${esc(dt(item.created_at))}</time></div><div class="widget-client"><a href="${presentationMode ? '#' : `tel:${esc(item.phone)}`}">${esc(displayPhone(item.phone))}</a><span>подтверждённый номер</span></div><div class="widget-criteria">${widgetCriteria(item.search_params)}</div><div class="widget-request-actions"><button class="button secondary" data-open="${item.listing_id}">Карточка в базе</button>${sourceLink}</div></article>`;
}
function widgetCriteria(params = {}) {
  const values = [];
  if (params.q) values.push(`Место: ${presentationMode ? 'выбранное направление' : params.q}`);
  if (params.max_price_usd) values.push(`До $${number(params.max_price_usd)}`);
  if (params.min_area_sotok || params.max_area_sotok) values.push(`Площадь ${params.min_area_sotok || '—'}–${params.max_area_sotok || '—'} сот.`);
  if (params.max_distance_km) values.push(`До ${params.max_distance_km} км`);
  if (params.electricity === 'true') values.push('Нужно электричество');
  if (params.gas === 'true') values.push('Нужен газ');
  return values.length ? values.map((value) => `<span>${esc(value)}</span>`).join('') : '<span>Без дополнительных фильтров</span>';
}
function healthCard(item) {
  const diagnostics = item.diagnostics || {}; const http = diagnostics.http || {}; const warnings = diagnostics.warnings || [];
  const message = presentationMode && (item.last_error || warnings[0]) ? 'Последняя проверка завершилась ошибкой' : item.last_error || warnings[0] || (item.last_success_at ? `Последняя успешная проверка: ${dt(item.last_success_at)}` : 'Запустите первый поиск');
  return `<article class="health-card"><div class="health-head"><h3>${esc(sourceNames[item.source] || item.source)}</h3><span class="health-status ${esc(item.status)}">${esc(healthNames[item.status] || item.status)}</span></div><div class="health-numbers"><div><small>Объектов</small><b>${number(item.last_item_count, '0')}</b></div><div><small>Запросов</small><b>${number(http.requests, '0')}</b></div><div><small>Повторов</small><b>${number(http.retries, '0')}</b></div></div><div class="health-message">${esc(message)}</div>${item.next_retry_at ? `<div class="health-warning">Повтор: ${esc(dt(item.next_retry_at))}</div>` : ''}${presentationMode ? '' : warnings.slice(1).map((warning) => `<div class="health-warning">${esc(warning)}</div>`).join('')}</article>`;
}
async function loadMap() {
  if (!window.L) { toast('Не удалось загрузить карту'); return; }
  if (!state.map) { state.map = L.map('map').setView([53.95, 27.56], 9); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '© OpenStreetMap' }).addTo(state.map); state.mapMarkers = L.layerGroup().addTo(state.map); }
  setTimeout(() => state.map.invalidateSize(), 50);
  try { const data = await api(`/api/map?${profileParams()}`); state.mapMarkers.clearLayers(); const colors = { new: '#718277', liked: '#b8874d', studying: '#9a713f', trip: '#a54f42', rejected: '#999' };
    $('#mapProviderHint').textContent = `Использовать: ${data.map_provider_label}`;
    data.items.forEach((listing) => L.circleMarker([listing.latitude, listing.longitude], { radius: 7, color: '#fff', weight: 2, fillColor: colors[listing.decision] || colors.new, fillOpacity: 1 }).addTo(state.mapMarkers).bindPopup(`<b>${esc(displayTitle(listing))}</b><br>${money(listing.price_usd)} · ${number(listing.area_sotok)} сот.<div class="map-popup-actions"><button onclick="window.openListing(${listing.id})">Подробнее</button><a href="${esc(listing.map_url)}" target="_blank" rel="noopener">${esc(data.map_provider_label)} ↗</a></div>`)); }
  catch (error) { toast(error.message); }
}
async function buildTrip() {
  try { const data = await api(`/api/trips/plan?${profileParams()}`, { method: 'POST', body: JSON.stringify({ listing_ids: [], max_points_per_route: 4 }) });
    $('#routeResults').innerHTML = data.routes.length ? data.routes.map((route) => `<article class="route-card"><div><h4>Маршрут ${route.index} · ${route.distance_km} км</h4><p>${route.items.map((item) => esc(displayTitle(item))).join(' → ')}</p></div><a class="button primary" href="${esc(route.url)}" target="_blank" rel="noopener">Открыть: ${esc(data.map_provider_label)} ↗</a></article>`).join('') : '<div class="empty">Отметьте объекты статусом «К поездке», и сервис соберёт маршрут.</div>'; }
  catch (error) { toast(error.message); }
}
async function loadBackupInfo() {
  if (viewerMode) return;
  try { const data = await api('/api/backups/info'); $('#backupInfo').textContent = `${number(data.listings, '0')} объявлений, ${number(data.snapshots, '0')} снимков истории · ${number(data.size_bytes / 1024 / 1024, '0')} МБ`; }
  catch (error) { $('#backupInfo').textContent = error.message; }
}
async function restoreSelectedBackup(file) {
  if (!file || !confirm(`Восстановить данные из «${file.name}»? Текущая база будет заменена.`)) return;
  try { const response = await fetch('/api/backups/restore', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Не удалось восстановить базу'); toast('База восстановлена. Перезагружаем интерфейс'); setTimeout(() => location.reload(), 900); }
  catch (error) { toast(error.message); }
}

function applyPresentationMode() {
  if (!presentationMode) return;
  Object.entries(sourceNames).forEach(([source, label]) => {
    const option = document.querySelector(`#sourceFilter option[value="${source}"]`);
    if (option) option.textContent = label;
    const checkbox = document.querySelector(`input[name="sources"][value="${source}"]`);
    const title = checkbox?.closest('.check-card')?.querySelector('b');
    if (title) title.textContent = label;
  });
}

function applyViewerMode() {
  if (!viewerMode) return;
  document.body.classList.add('viewer-mode'); $('#viewerBadge').hidden = false;
  $('#serviceText').textContent = 'Демонстрационный доступ';
  $('#setupModal').classList.add('hidden');
  $$('#settingsForm input, #settingsForm select, #settingsForm button').forEach((element) => { element.disabled = true; });
}

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } finally { location.replace('/login'); }
}

window.openListing = openDetail;
document.addEventListener('click', (event) => {
  const nav = event.target.closest('[data-view]'); if (nav) setView(nav.dataset.view);
  const go = event.target.closest('[data-go]'); if (go) setView(go.dataset.go);
  const open = event.target.closest('[data-open]'); if (open) openDetail(open.dataset.open);
  const decision = event.target.closest('[data-decision]'); if (decision) quickDecision(decision.dataset.id, decision.dataset.decision);
  const page = event.target.closest('[data-page]'); if (page) { state.page = Number(page.dataset.page); loadListings(); scrollTo({ top: 0, behavior: 'smooth' }); }
  const detailDecision = event.target.closest('[data-detail-decision]'); if (detailDecision) { $$('[data-detail-decision]').forEach((element) => element.classList.remove('active')); detailDecision.classList.add('active'); }
  if (event.target.id === 'saveDecision') saveDetailDecision();
});
$$('.nav-item').forEach((element) => element.addEventListener('click', () => setView(element.dataset.view)));
$('#mobileMenu').onclick = () => $('.sidebar').classList.toggle('open'); $('#scanButton').onclick = () => runJob('scan');
$('#activityButton').onclick = () => runJob('activity'); $('#refreshHealthButton').onclick = loadHealth; $('#buildTripButton').onclick = buildTrip;
$('#refreshWidgetButton').onclick = loadWidgetAdmin; $('#widgetSearchInput').oninput = renderWidgetRequests;
$('#newProfileButton').onclick = createProfile; $('#deleteProfileButton').onclick = deleteProfile; $('#profileSelect').onchange = (event) => switchProfile(event.target.value);
$('#drawerClose').onclick = closeDetail; $('#drawerShade').onclick = closeDetail;
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('#detailDrawer').classList.contains('open')) closeDetail(); });
$('#settingsForm').onsubmit = (event) => { event.preventDefault(); saveSettings(event.currentTarget); };
$('#setupForm').onsubmit = (event) => { event.preventDefault(); saveSetup(event.currentTarget); };
$('#restoreButton').onclick = () => $('#restoreFile').click(); $('#restoreFile').onchange = (event) => restoreSelectedBackup(event.target.files[0]);
$('#logoutButton').onclick = logout;
['searchInput', 'decisionFilter', 'sourceFilter', 'activeFilter', 'scoreInput'].forEach((id) => { $(`#${id}`).addEventListener(id === 'searchInput' ? 'input' : 'change', () => { state.page = 1; if (id === 'scoreInput') $('#scoreValue').textContent = $('#scoreInput').value; loadListings(); }); });

(async () => {
  const auth = await api('/api/auth/me'); viewerMode = auth.role === 'viewer';
  if (viewerMode && !presentationMode) { const target = new URL(location.href); target.searchParams.set('presentation', '1'); location.replace(target.href); return; }
  applyPresentationMode();
  applyViewerMode();
  const hash = location.hash.slice(1); if (['dashboard', 'listings', 'map', 'widget', 'health', 'settings'].includes(hash)) setView(hash);
  await loadProfiles(); await loadSettings(); await Promise.all([loadSummary(), loadDashboard()]);
  const job = await api('/api/jobs'); if (job.running) pollJobs();
})();
