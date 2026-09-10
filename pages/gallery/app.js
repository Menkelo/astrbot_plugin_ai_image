// No direct API fetches or storage access: every request goes through AstrBot's bridge.
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const paths = {
  aperture: '<circle cx="12" cy="12" r="9"/><path d="m9 3 4 7 8 0M21 10l-6 5 1 6M16 21l-7-3-6 1M3 19l1-8 5-8M4 11l5 7 6-3-2-5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  layout: '<rect x="3" y="3" width="7" height="11" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="18" width="7" height="3" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  heart: '<path d="M20.5 4.9a5.4 5.4 0 0 0-7.6 0l-.9.9-.9-.9a5.4 5.4 0 0 0-7.6 7.6L12 21l8.5-8.5a5.4 5.4 0 0 0 0-7.6Z"/>',
  inbox: '<path d="m3 13 4-9h10l4 9v7H3Z"/><path d="M3 13h5l2 3h4l2-3h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  drive: '<rect x="3" y="12" width="18" height="8" rx="2"/><path d="m3 14 3-10h12l3 10M16 16h2M6 16h5"/>',
  edit: '<path d="m14 5 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14Z"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M5.5 6a8 8 0 0 1 13 0L20 8M4 16l1.5 2a8 8 0 0 0 13 0"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  sliders: '<path d="M4 6h7m4 0h5M4 18h4m4 0h8M4 12h2m4 0h10"/><circle cx="13" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
  sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
  'check-square': '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m7 12 3 3 7-7"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  'chevron-left': '<path d="m15 5-7 7 7 7"/>',
  'chevron-right': '<path d="m9 5 7 7-7 7"/>',
  'folder-plus': '<path d="M3 7V4h6l3 3h9v13H3ZM9 14h6m-3-3v6"/>',
  'folder-minus': '<path d="M3 7V4h6l3 3h9v13H3ZM9 14h6"/>',
  tag: '<path d="M3 3h8l10 10-8 8L3 11Z"/><circle cx="7.5" cy="7.5" r="1"/>',
  restore: '<path d="M3 4v6h6M3 10a9 9 0 1 1 1 8M12 7v5l3 2"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.1"/>',
  expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v12h5"/>',
  terminal: '<path d="m4 6 6 6-6 6m9 0h7"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
};

function icon(name) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  element.setAttribute('viewBox', '0 0 24 24');
  element.setAttribute('class', 'icon');
  element.setAttribute('aria-hidden', 'true');
  element.innerHTML = paths[name] || paths.image; // Only static, local path definitions.
  return element;
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function setIcon(element, name) { element.replaceChildren(icon(name)); }
for (const element of $$('[data-icon]')) setIcon(element, element.dataset.icon);

const state = {
  ready: false, overview: null, items: [], total: 0, page: 1, pageSize: 48,
  view: 'all', album: '', query: '', source: '', tag: '', model: '', orientation: '',
  from: '', to: '', sort: 'newest', compact: false, selecting: false,
  selected: new Set(), current: null, dirty: false, busy: false, uploading: false,
};
let bridge;
let loadSerial = 0;
let detailSerial = 0;
let searchTimer;
let layoutFrame;
let thumbTimer;
let thumbRequests = 0;
let lastFocus;
let actionResolve;
let unsubscribeContext;
const thumbCache = new Map();
const thumbQueue = new Set();
const thumbPending = new Set();
const grid = $('#gallery-grid');
const viewer = $('#viewer');
const actionDialog = $('#action-dialog');
const actionForm = $('#action-form');
const actionFields = $('#action-fields');
const empty = $('#empty-state');

const formatNumber = value => Number(value || 0).toLocaleString('zh-CN');
function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = value / 1024, index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
  return `${amount.toFixed(amount < 10 ? 1 : 0)} ${units[index]}`;
}
function shortDate(timestamp) {
  const date = new Date(timestamp * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return '今天';
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return '昨天';
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}
function errorMessage(error) { return error?.message || '操作未完成，请稍后重试'; }
function listen(selector, event, handler) {
  $(selector).addEventListener(event, e => {
    try { Promise.resolve(handler(e)).catch(error => toast(errorMessage(error), 'error')); }
    catch (error) { toast(errorMessage(error), 'error'); }
  });
}
function placeToasts() {
  (actionDialog.open ? actionDialog : viewer.open ? viewer : document.body).append($('#toasts'));
}
function toast(message, type = 'success', action) {
  placeToasts();
  const item = node('div', `toast ${type}`);
  item.setAttribute('role', type === 'error' ? 'alert' : 'status');
  item.append(icon(type === 'error' ? 'info' : 'check'), node('span', '', message));
  if (action) {
    const button = node('button', 'text-button', action.label);
    button.addEventListener('click', () => {
      item.remove();
      Promise.resolve().then(action.run).catch(error => toast(errorMessage(error), 'error'));
    });
    item.append(button);
  }
  $('#toasts').append(item);
  while ($('#toasts').children.length > 3) $('#toasts').firstElementChild.remove();
  setTimeout(() => item.remove(), action ? 10000 : type === 'error' ? 6500 : 4000);
}

// Native confirm/prompt are unavailable in the Pages sandbox. These dialogs also
// provide keyboard focus, validation, and readable descriptions for destructive actions.
function ask({ title, description = '', fields = [], submit = '确定', danger = false, symbol = 'folder-plus' }) {
  if (actionResolve) return Promise.resolve(null);
  $('#action-title').textContent = title;
  $('#action-description').textContent = description;
  $('#action-submit').textContent = submit;
  $('#action-submit').className = `button ${danger ? 'danger' : 'primary'}`;
  actionDialog.classList.toggle('is-danger', danger);
  setIcon($('#action-icon'), symbol);
  actionFields.replaceChildren();
  for (const field of fields) {
    const label = node('label', '', field.label || '');
    const input = node(field.type === 'select' ? 'select' : field.type === 'textarea' ? 'textarea' : 'input');
    input.name = field.name;
    if (input.tagName === 'INPUT') input.type = 'text';
    if (field.options) {
      for (const option of field.options) input.add(new Option(option.label, option.value));
    }
    if (field.maxLength) input.maxLength = field.maxLength;
    if (field.placeholder) input.placeholder = field.placeholder;
    if (field.readOnly) input.readOnly = true;
    input.required = Boolean(field.required);
    input.value = field.value || '';
    if (input.tagName === 'TEXTAREA') input.rows = field.rows || 5;
    label.append(input);
    actionFields.append(label);
  }
  actionDialog.showModal();
  placeToasts();
  const focusTarget = $('input, select, textarea', actionFields) || $('#action-cancel');
  focusTarget.focus();
  if (focusTarget.select) focusTarget.select();
  return new Promise(resolve => { actionResolve = resolve; });
}
function finishAction(value) {
  const resolve = actionResolve;
  actionResolve = null;
  actionDialog.close();
  placeToasts();
  resolve?.(value);
}
actionForm.addEventListener('submit', e => {
  e.preventDefault();
  if (actionForm.reportValidity()) finishAction(Object.fromEntries(new FormData(actionForm)));
});
actionDialog.addEventListener('cancel', e => { e.preventDefault(); finishAction(null); });
listen('#action-close', 'click', () => finishAction(null));
listen('#action-cancel', 'click', () => finishAction(null));

function syncBusy() {
  const disabled = state.busy || !state.ready;
  for (const element of $$('.batch-button, #save-detail, #detail-download, #detail-favorite, #detail-trash, #detail-restore')) {
    element.disabled = disabled || (element.closest('.detail-column') && !state.current);
  }
  $('#import-button').disabled = !state.ready || state.uploading;
  $('#create-album').disabled = disabled;
  $('#rename-album').disabled = disabled;
  $('#delete-album').disabled = disabled;
  for (const element of $$('.card-favorite')) element.disabled = disabled;
}
async function withBusy(handler) {
  if (!state.ready) throw new Error('请在 AstrBot 插件页面中打开图库');
  if (state.busy) return;
  state.busy = true;
  syncBusy();
  try { return await handler(); }
  finally { state.busy = false; syncBusy(); }
}

function params() {
  const result = { page: state.page, page_size: state.pageSize, view: state.view,
    album_id: state.album, query: state.query, source: state.source, tag: state.tag,
    model: state.model, orientation: state.orientation, sort: state.sort };
  if (state.from) result.after = new Date(`${state.from}T00:00:00`).getTime() / 1000;
  if (state.to) {
    const next = new Date(`${state.to}T00:00:00`);
    next.setDate(next.getDate() + 1); // End date is inclusive in the user's local timezone.
    result.before = next.getTime() / 1000;
  }
  return result;
}
function filterCount() { return [state.query, state.source, state.tag, state.model, state.orientation, state.from, state.to].filter(Boolean).length; }
function syncFilters() {
  $('#search').value = state.query;
  $('#model-filter').value = state.model;
  $('#tag-filter').value = state.tag;
  $('#orientation-filter').value = state.orientation;
  $('#date-from').value = state.from;
  $('#date-to').value = state.to;
  $('#sort').value = state.sort;
  for (const button of $$('.source-tab')) {
    const active = button.dataset.source === state.source;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  }
  const count = filterCount();
  $('#filter-count').hidden = !count;
  $('#filter-count').textContent = count;
  $('#clear-search').hidden = !count;
}
function clearFilters() {
  for (const key of ['query', 'source', 'tag', 'model', 'orientation', 'from', 'to']) state[key] = '';
  syncFilters();
}
function clearSelection() {
  state.selected.clear();
  state.selecting = false;
  renderSelection();
}
async function navigate(view = 'all', album = '') {
  clearTimeout(searchTimer);
  state.view = view; state.album = album; state.page = 1;
  clearFilters(); clearSelection();
  await refresh();
}
function options(select, values, current, placeholder) {
  select.replaceChildren(new Option(placeholder, ''));
  for (const value of values) select.add(new Option(value, value));
  if (current && !values.includes(current)) select.add(new Option(current, current));
  select.value = current;
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  for (const [id, key] of [['all', 'total'], ['favorites', 'favorites'], ['unfiled', 'unfiled'], ['trash', 'trash']]) {
    $(`#count-${id}`).textContent = formatNumber(data[key]);
  }
  const album = data.albums.find(item => item.id === state.album);
  const titles = { all: '全部图片', favorites: '我的收藏', unfiled: '未分组', trash: '回收站' };
  $('#page-title').textContent = album?.name || titles[state.view];
  const descriptions = {
    all: '让每一次灵感，都有迹可循。',
    favorites: '值得反复回看的，都是偏爱。',
    unfiled: '这些灵感，还在等待自己的归属。',
    trash: '暂时告别的作品，也有重新相遇的机会。',
  };
  $('#page-description').textContent = album ? `${formatNumber(album.count)} 张作品，组成一段灵感。` : descriptions[state.view];
  $('#album-actions').hidden = !album;
  $('#source-total').textContent = formatNumber(album?.count ?? data[state.view === 'all' ? 'total' : state.view]);
  for (const button of $$('[data-view]')) {
    const active = button.dataset.view === state.view && !state.album;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  const palette = ['#97aa83', '#bea68a', '#92acae', '#baa1af', '#a2a2bd', '#b4ba82'];
  const albums = $('#album-nav'); albums.replaceChildren();
  data.albums.forEach((item, index) => {
    const button = node('button', `nav-item${state.album === item.id ? ' active' : ''}`);
    const dot = node('span', 'album-dot'); dot.style.setProperty('--album-color', palette[index % palette.length]);
    button.title = item.name;
    button.append(dot, node('span', '', item.name), node('span', 'nav-count', formatNumber(item.count)));
    if (state.album === item.id) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => navigate('all', item.id));
    albums.append(button);
  });
  $('#album-empty').hidden = data.albums.length > 0;
  const tags = $('#sidebar-tags'); tags.replaceChildren();
  for (const tag of data.tags.slice(0, 10)) {
    const button = node('button', `tag-chip${state.tag === tag.name ? ' active' : ''}`, `# ${tag.name}`);
    button.title = `${tag.name} · ${tag.count} 张`;
    button.addEventListener('click', () => {
      state.tag = state.tag === tag.name ? '' : tag.name;
      state.page = 1; clearSelection(); syncFilters(); refresh();
    });
    tags.append(button);
  }
  $('#tags-empty').hidden = data.tags.length > 0;
  $('#storage-used').textContent = formatBytes(data.storage_bytes);
  $('#storage-caption').textContent = data.storage_limit_bytes
    ? `总容量 ${formatBytes(data.storage_limit_bytes)} · 回收站 ${formatBytes(data.trash_bytes)}`
    : `${formatNumber(data.total + data.trash)} 张图片 · 回收站 ${formatBytes(data.trash_bytes)}`;
  $('#storage-meter').hidden = !data.storage_limit_bytes;
  $('#storage-meter').value = data.storage_limit_bytes ? Math.min(100, data.storage_bytes / data.storage_limit_bytes * 100) : 0;
  const archive = $('#archive-status');
  archive.classList.toggle('paused', !data.auto_archive);
  $('span:last-child', archive).textContent = data.auto_archive ? '新作品自动收录' : '自动收录已暂停';
  options($('#model-filter'), data.models, state.model, '全部模型');
  options($('#tag-filter'), data.tags.map(tag => tag.name), state.tag, '全部标签');
  $('#trash-notice').hidden = state.view !== 'trash';
}

function renderSkeleton() {
  thumbObserver.disconnect();
  grid.replaceChildren();
  for (let i = 0; i < 8; i++) {
    const card = node('div', 'skeleton-card');
    card.setAttribute('aria-hidden', 'true');
    card.append(node('div', 'skeleton-art'), node('div', 'skeleton-line'), node('div', 'skeleton-line'));
    grid.append(card);
  }
  grid.hidden = false; empty.hidden = true;
}
function showEmpty({ title, description, actionLabel, action }) {
  grid.hidden = true; empty.hidden = false;
  $('#empty-title').textContent = title;
  $('#empty-description').textContent = description;
  const button = $('#empty-action');
  button.hidden = !action;
  button.replaceChildren(icon(actionLabel === '重新加载' ? 'refresh' : actionLabel === '清除筛选' ? 'sliders' : 'upload'), node('span', '', actionLabel || ''));
  button.onclick = () => Promise.resolve().then(action).catch(error => toast(errorMessage(error), 'error'));
}

async function refresh(showLoading = true) {
  if (!state.ready) return;
  const serial = ++loadSerial;
  grid.setAttribute('aria-busy', 'true');
  if (showLoading) renderSkeleton();
  $('#result-count').textContent = '正在整理你的图库…';
  try {
    const [overview, result] = await Promise.all([bridge.apiGet('gallery/overview'), bridge.apiGet('gallery/images', params())]);
    if (serial !== loadSerial) return;
    state.overview = overview; state.items = result.items; state.total = result.total; state.page = result.page;
    renderOverview(); syncFilters(); renderImages(); renderSelection(); syncBusy();
  } catch (error) {
    if (serial !== loadSerial) return;
    state.items = []; state.total = 0;
    $('#result-count').textContent = '图库暂时无法加载';
    showEmpty({ title: '连接图库时遇到了一点问题', description: errorMessage(error),
      actionLabel: '重新加载', action: () => refresh() });
    $('#previous-page').disabled = true; $('#next-page').disabled = true;
  } finally {
    if (serial === loadSerial) grid.setAttribute('aria-busy', 'false');
  }
}

function renderImages() {
  thumbObserver.disconnect(); thumbQueue.clear(); grid.replaceChildren();
  $('#result-count').textContent = filterCount() ? `找到 ${formatNumber(state.total)} 张图片` : `共 ${formatNumber(state.total)} 张图片`;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $('#page-number').textContent = `${state.page} / ${pages}`;
  $('#previous-page').disabled = state.page <= 1;
  $('#next-page').disabled = state.page >= pages;
  $('#footer-note').textContent = state.total ? `显示 ${formatNumber((state.page - 1) * state.pageSize + 1)}–${formatNumber(Math.min(state.page * state.pageSize, state.total))} 张 · 每页 ${state.pageSize} 张` : '属于你的每一帧灵感。';
  if (!state.items.length) {
    if (filterCount()) showEmpty({ title: '还没找到这份灵感', description: '试试其他关键词，或放宽筛选条件。', actionLabel: '清除筛选', action: async () => { clearFilters(); state.page = 1; await refresh(); } });
    else if (state.view === 'trash') showEmpty({ title: '回收站空空如也', description: '移入回收站的图片会出现在这里，可以随时恢复。' });
    else if (state.view === 'favorites') showEmpty({ title: '把偏爱留在这里', description: '点亮图片右上角的爱心，收藏让你心动的作品。' });
    else showEmpty({ title: state.album ? '给这个相册，添一点灵感' : '你的灵感，即将入藏', description: state.album ? '导入图片，或从图库批量选择作品加入这个相册。' : '生成一张图片，或把喜欢的作品拖到这里。\n启用后的生成结果会自动出现在图库中。', actionLabel: '导入图片', action: () => $('#file-input').click() });
    return;
  }
  grid.hidden = false; empty.hidden = true;
  for (const image of state.items) {
    const card = node('article', 'image-card'); card.dataset.id = image.id;
    const art = node('div', 'card-art');
    art.style.setProperty('--card-color', image.color);
    art.style.setProperty('--card-ratio', Math.max(.62, Math.min(1.8, image.width / image.height)));
    const open = node('button', 'open-image'); open.setAttribute('aria-label', `查看图片：${image.title}`);
    const img = node('img'); img.alt = image.title; img.decoding = 'async';
    img.onload = () => img.classList.add('loaded');
    img.onerror = () => thumbnailError(image.id);
    open.append(img);
    open.addEventListener('click', () => {
      if (state.selecting) toggleImage(image.id); else openViewer(image.id).catch(error => toast(errorMessage(error), 'error'));
    });
    const selectLabel = node('label', 'card-select');
    const check = node('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `选择图片：${image.title}`);
    check.addEventListener('change', () => toggleImage(image.id, check.checked));
    selectLabel.append(check);
    art.append(open, selectLabel);
    if (!image.deleted_at) {
      const favorite = node('button', 'icon-button card-favorite');
      favorite.setAttribute('aria-label', image.favorite ? '取消收藏' : '收藏图片');
      favorite.setAttribute('aria-pressed', String(image.favorite)); favorite.title = image.favorite ? '取消收藏' : '收藏';
      favorite.append(icon('heart'));
      favorite.addEventListener('click', () => withBusy(async () => {
        await bridge.apiPost('gallery/batch', { ids: [image.id], action: 'favorite', value: !image.favorite });
        await refresh(false);
      }).catch(error => toast(errorMessage(error), 'error')));
      art.append(favorite);
    }
    art.append(node('span', 'card-dimensions', `${image.width} × ${image.height} · ${formatBytes(image.size_bytes)}`));
    if (image.resolution || image.animated) art.append(node('span', 'card-badge', image.animated ? image.extension.toUpperCase() : image.resolution));
    const info = node('button', 'card-info'); info.setAttribute('aria-label', `查看详情：${image.title}`);
    const subtitle = node('span', 'card-subtitle');
    const model = node('span', 'card-model');
    model.append(icon(image.source === 'generated' ? 'sparkles' : 'image'), node('span', '', image.model || '本地导入'));
    const date = node('time', '', shortDate(image.created_at)); date.dateTime = new Date(image.created_at * 1000).toISOString();
    subtitle.append(model, date);
    info.append(node('span', 'card-title', image.title), subtitle);
    info.addEventListener('click', () => openViewer(image.id).catch(error => toast(errorMessage(error), 'error')));
    card.append(art, info); grid.append(card);
    if (thumbCache.has(image.id)) img.src = thumbCache.get(image.id);
    else thumbObserver.observe(card);
  }
  scheduleLayout();
}
function scheduleLayout() {
  cancelAnimationFrame(layoutFrame);
  layoutFrame = requestAnimationFrame(() => {
    const style = getComputedStyle(grid);
    const gap = parseFloat(style.rowGap);
    const row = parseFloat(style.gridAutoRows);
    for (const card of $$('.image-card', grid)) {
      const height = $('.card-art', card).getBoundingClientRect().height + $('.card-info', card).getBoundingClientRect().height + 2;
      card.style.gridRowEnd = `span ${Math.ceil((height + gap) / (row + gap))}`;
    }
  });
}
const resizeObserver = new ResizeObserver(scheduleLayout);
resizeObserver.observe(grid);

const thumbObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const id = entry.target.dataset.id;
    thumbObserver.unobserve(entry.target);
    if (!thumbPending.has(id) && !thumbCache.has(id)) thumbQueue.add(id);
  }
  clearTimeout(thumbTimer); thumbTimer = setTimeout(flushThumbnails, 20);
}, { rootMargin: '350px' });

function thumbnailError(id) {
  const art = $(`.image-card[data-id="${id}"] .card-art`);
  if (art && !$('.thumb-error', art)) art.append(node('span', 'thumb-error', '预览不可用 · 可尝试下载原图'));
}
async function flushThumbnails() {
  if (!state.ready || thumbRequests >= 2 || !thumbQueue.size) return;
  const ids = [...thumbQueue].slice(0, 12);
  ids.forEach(id => { thumbQueue.delete(id); thumbPending.add(id); });
  thumbRequests++;
  if (thumbQueue.size) flushThumbnails();
  try {
    const result = await bridge.apiPost('gallery/thumbnails', { ids });
    for (const item of result.items) {
      thumbCache.delete(item.id); thumbCache.set(item.id, item.data_url);
      while (thumbCache.size > 160) thumbCache.delete(thumbCache.keys().next().value);
      const img = $(`.image-card[data-id="${item.id}"] img`);
      if (img) img.src = item.data_url;
    }
    for (const id of result.missing) thumbnailError(id);
  } catch { ids.forEach(thumbnailError); }
  finally { ids.forEach(id => thumbPending.delete(id)); thumbRequests--; if (thumbQueue.size) flushThumbnails(); }
}

function toggleImage(id, checked = !state.selected.has(id)) {
  if (checked && state.selected.size >= (state.overview?.max_batch || 100)) { toast('单次最多选择 100 张图片，请分批操作', 'error'); renderSelection(); return; }
  if (checked) state.selected.add(id); else state.selected.delete(id);
  if (state.selected.size) state.selecting = true;
  renderSelection();
}
function renderSelection() {
  grid.classList.toggle('selecting', state.selecting);
  for (const card of $$('.image-card', grid)) {
    const selected = state.selected.has(card.dataset.id);
    card.classList.toggle('selected', selected);
    $('input[type=checkbox]', card).checked = selected;
  }
  $('#selection-mode').setAttribute('aria-pressed', String(state.selecting));
  $('#select-page-label').hidden = !state.selecting || !state.items.length;
  const visibleSelected = state.items.filter(image => state.selected.has(image.id)).length;
  $('#select-page').checked = visibleSelected > 0 && visibleSelected === state.items.length;
  $('#select-page').indeterminate = visibleSelected > 0 && visibleSelected < state.items.length;
  $('#batch-bar').hidden = state.selected.size === 0;
  $('#selected-count').textContent = state.selected.size;
  for (const button of $$('.active-only')) button.hidden = state.view === 'trash';
  for (const button of $$('.trash-only')) button.hidden = state.view !== 'trash';
  $('#remove-from-album').hidden = !state.album || state.view === 'trash';
}

async function discardChanges() {
  if (!state.dirty) return true;
  const answer = await ask({ title: '放弃尚未保存的修改？', description: '作品名称、标签或备注有未保存的修改。', submit: '放弃修改', symbol: 'edit' });
  if (!answer) return false;
  state.dirty = false;
  return true;
}
async function closeViewer() {
  if (!await discardChanges()) return;
  viewer.close();
}
viewer.addEventListener('close', () => {
  detailSerial++; state.current = null; state.dirty = false;
  $('#preview-image').removeAttribute('src');
  placeToasts();
  if (lastFocus?.isConnected) lastFocus.focus();
});
viewer.addEventListener('cancel', e => { e.preventDefault(); closeViewer(); });
viewer.addEventListener('click', e => {
  if (e.target !== viewer) return;
  const bounds = viewer.getBoundingClientRect();
  if (e.clientX < bounds.left || e.clientX > bounds.right || e.clientY < bounds.top || e.clientY > bounds.bottom) closeViewer();
});

async function openViewer(id) {
  if (!await discardChanges()) return;
  const serial = ++detailSerial;
  state.current = null; state.dirty = false;
  if (!viewer.open) { lastFocus = document.activeElement; viewer.showModal(); placeToasts(); }
  $('#detail-title').textContent = '正在读取图片…';
  $('#detail-content').style.opacity = '.5';
  $('#preview-image').hidden = true; $('#preview-image').removeAttribute('src');
  $('#preview-loading').hidden = false; $('#preview-loading').textContent = '正在载入预览…';
  $('#preview-stage').classList.remove('zoomed'); $('#preview-zoom').setAttribute('aria-pressed', 'false');
  syncBusy();
  try {
    const image = await bridge.apiGet(`gallery/images/${id}`);
    if (serial !== detailSerial || !viewer.open) return;
    state.current = image; renderDetail(image); syncBusy();
    $('#detail-content').style.opacity = '1';
    $('#detail-content').scrollTop = 0;
    try {
      const preview = await bridge.apiGet(`gallery/images/${id}/preview`);
      if (serial !== detailSerial || !viewer.open) return;
      const img = $('#preview-image');
      img.onload = () => { if (serial === detailSerial) { img.hidden = false; $('#preview-loading').hidden = true; } };
      img.onerror = () => { if (serial === detailSerial) { img.hidden = true; $('#preview-loading').hidden = false; $('#preview-loading').textContent = '预览无法显示，请尝试下载原图。'; } };
      img.src = preview.data_url; img.alt = image.title;
    } catch (error) {
      if (serial === detailSerial) $('#preview-loading').textContent = `${errorMessage(error)}，可尝试下载原图。`;
    }
  } catch (error) {
    if (serial === detailSerial) { viewer.close(); toast(errorMessage(error), 'error'); }
  }
}
function renderDetail(image) {
  $('#detail-title').textContent = image.title;
  $('#detail-date').textContent = new Date(image.created_at * 1000).toLocaleString('zh-CN', { hour12: false });
  const badges = $('#detail-badges'); badges.replaceChildren();
  for (const text of [image.source === 'generated' ? 'AI 生成' : '本地导入', image.reference_count ? `图生图 · ${image.reference_count} 张参考` : '', image.deleted_at ? '在回收站' : '', image.animated ? '动图 · 预览为首帧' : ''].filter(Boolean)) badges.append(node('span', 'detail-badge', text));
  const metadata = $('#detail-metadata'); metadata.replaceChildren();
  const rows = [['尺寸', `${image.width} × ${image.height}`], ['文件', `${image.extension.toUpperCase()} · ${formatBytes(image.size_bytes)}`], ['模型', image.model || '—'], ['来源', image.provider || '本地上传'], ['创作者', image.user_name || image.user_id || '—'], ['生成耗时', image.duration_ms ? `${(image.duration_ms / 1000).toFixed(1)} 秒` : '—']];
  if (image.user_id && image.user_name) rows.push(['用户 ID', image.user_id]);
  if (image.group_id) rows.push(['群组 ID', image.group_id]);
  for (const [label, value] of rows) { const group = node('div'); group.append(node('dt', '', label), node('dd', '', value)); metadata.append(group); }
  $('#detail-prompt').textContent = image.prompt || '这张图片没有记录生成提示词。';
  $('#copy-prompt').disabled = !image.prompt;
  $('#copy-command').hidden = !image.command || !image.prompt;
  $('#reference-hint').hidden = !image.reference_count;
  $('#edit-title').value = image.title; $('#edit-tags').value = image.tags.join(', '); $('#edit-notes').value = image.notes;
  $('#detail-form').hidden = Boolean(image.deleted_at);
  $('#detail-album-count').textContent = image.albums.length ? `· ${image.albums.length}` : '';
  const albums = $('#detail-albums'); albums.replaceChildren();
  for (const album of state.overview?.albums || []) {
    const label = node('label'); const input = node('input'); input.type = 'checkbox'; input.value = album.id;
    input.checked = image.albums.some(item => item.id === album.id);
    label.append(input, node('span', '', album.name)); albums.append(label);
  }
  if (!albums.children.length) albums.append(node('span', 'field-hint', '还没有相册，可以在图库左侧新建。'));
  $('#detail-favorite').hidden = Boolean(image.deleted_at); $('#detail-trash').hidden = Boolean(image.deleted_at);
  $('#detail-restore').hidden = !image.deleted_at;
  $('#detail-favorite').setAttribute('aria-pressed', String(image.favorite));
  $('#detail-favorite').setAttribute('aria-label', image.favorite ? '取消收藏' : '收藏图片');
  $('#detail-favorite').title = image.favorite ? '取消收藏（F）' : '收藏（F）';
  $('#preview-format').textContent = `${image.width} × ${image.height} · ${image.extension.toUpperCase()} · 预览最长边 1800 px`;
  const index = state.items.findIndex(item => item.id === image.id);
  $('#viewer-position').textContent = index >= 0 ? `${index + 1} / ${state.items.length}` : '预览';
  $('#viewer-previous').disabled = index <= 0; $('#viewer-next').disabled = index < 0 || index >= state.items.length - 1;
}
async function adjacentImage(direction) {
  if (!state.current) return;
  const index = state.items.findIndex(item => item.id === state.current.id);
  if (index >= 0 && state.items[index + direction]) await openViewer(state.items[index + direction].id);
}
function parseTags(text) { return [...new Set(text.split(/[,，\n]/).map(value => value.trim()).filter(Boolean))]; }

async function copyText(text) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板');
  } catch {
    // Sandbox permissions differ by browser. Always give a selectable fallback.
    await ask({ title: '复制内容', description: '当前浏览器限制了剪贴板访问。内容已选中，可按 Ctrl+C（Mac 为 ⌘C）复制。',
      fields: [{ name: 'text', type: 'textarea', value: text, readOnly: true, rows: 7 }], submit: '完成', symbol: 'copy' });
  }
}
async function createAlbum() {
  const values = await ask({ title: '新建相册', description: '给这组作品取个名字，让灵感有一个归属。', fields: [{ name: 'name', label: '相册名称', placeholder: '例如：山野来信', maxLength: 60, required: true }], submit: '创建相册' });
  if (!values) return null;
  const album = await bridge.apiPost('gallery/albums/save', { name: values.name });
  await refresh(false); toast('相册已创建'); return album;
}
async function addToAlbum(ids) {
  let albums = state.overview?.albums || [];
  if (!albums.length) {
    const created = await createAlbum();
    if (!created) return;
    albums = [created];
  }
  const answer = await ask({ title: '加入相册', description: `把选中的 ${ids.length} 张图片加入相册，一张图片可以属于多个相册。`,
    fields: [{ name: 'album', type: 'select', label: '选择相册', options: albums.map(album => ({ value: album.id, label: album.name })), value: albums[0].id }], submit: '加入相册' });
  if (!answer) return;
  await bridge.apiPost('gallery/batch', { ids, action: 'add_album', value: answer.album });
  clearSelection(); await refresh(false); toast(`已将 ${ids.length} 张图片加入相册`);
}
async function batchAction(action, ids = [...state.selected]) {
  if (!ids.length) return;
  return withBusy(async () => {
    if (action === 'download') {
      await bridge.download('gallery/export', { ids: ids.join(',') }, `ai-images-${new Date().toISOString().slice(0, 10)}.zip`);
      toast('已开始下载，压缩包包含原图和元数据'); return;
    }
    if (action === 'album') return addToAlbum(ids);
    if (action === 'tags') {
      const values = await ask({ title: '批量添加标签', description: `为 ${ids.length} 张图片添加标签，原有标签会保留。`, fields: [{ name: 'tags', label: '用逗号分隔', placeholder: '风景, 壁纸, 待整理', required: true, maxLength: 820 }], submit: '添加标签', symbol: 'tag' });
      if (!values) return;
      const tags = parseTags(values.tags);
      if (!tags.length || tags.length > 20) throw new Error('请输入 1–20 个标签');
      await bridge.apiPost('gallery/batch', { ids, action: 'add_tags', value: tags });
      clearSelection(); await refresh(false); toast('标签已添加'); return;
    }
    if (action === 'trash' || action === 'purge') {
      const permanent = action === 'purge';
      const answer = await ask({ title: permanent ? `永久删除 ${ids.length} 张图片？` : `将 ${ids.length} 张图片移入回收站？`,
        description: permanent ? '原图、预览和对应记录都会从图库中移除，此操作无法撤销。' : '图片会暂时收进回收站，你可以随时恢复。',
        submit: permanent ? '永久删除' : '移入回收站', danger: permanent, symbol: 'trash' });
      if (!answer) return;
    }
    if (action === 'remove-album') {
      await bridge.apiPost('gallery/batch', { ids, action: 'remove_album', value: state.album });
    } else {
      await bridge.apiPost('gallery/batch', { ids, action, ...(action === 'favorite' ? { value: true } : {}) });
    }
    if (state.current && ids.includes(state.current.id) && ['trash', 'purge', 'restore'].includes(action)) { state.dirty = false; viewer.close(); }
    ids.forEach(id => thumbCache.delete(id));
    clearSelection(); await refresh(false);
    const messages = { trash: '图片已移入回收站', purge: '图片已永久删除', restore: '图片已恢复', favorite: '已加入收藏', 'remove-album': '图片已移出相册，仍保留在图库中' };
    toast(messages[action] || '操作已完成', 'success', action === 'trash' ? { label: '撤销', run: () => batchAction('restore', ids) } : undefined);
  });
}
async function downloadCurrent() {
  if (!state.current) return;
  const image = state.current;
  await withBusy(async () => {
    await bridge.download(`gallery/images/${image.id}/download`, {});
    toast('已开始下载原图');
  });
}
async function favoriteCurrent() {
  if (!state.current || state.current.deleted_at) return;
  const image = state.current;
  await withBusy(async () => {
    const updated = await bridge.apiPost(`gallery/images/${image.id}/update`, { favorite: !image.favorite });
    if (state.current?.id === image.id) {
      state.current.favorite = updated.favorite;
      // Do not overwrite an unsaved title or tag edit when toggling a favorite.
      $('#detail-favorite').setAttribute('aria-pressed', String(updated.favorite));
      $('#detail-favorite').setAttribute('aria-label', updated.favorite ? '取消收藏' : '收藏图片');
    }
    await refresh(false);
  });
}

async function importFiles(fileList) {
  if (!state.ready || state.uploading) return;
  const files = [...fileList];
  if (!files.length) return;
  if (files.length > 100) throw new Error('一次最多导入 100 张图片，请分批导入');
  const targetAlbum = state.album;
  state.uploading = true; syncBusy(); $('#upload-status').hidden = false;
  let imported = 0, duplicates = 0, trashed = 0;
  const failures = [], albumImages = [];
  try {
    for (let index = 0; index < files.length; index++) {
      const file = files[index];
      $('#upload-label').textContent = `正在导入 ${index + 1} / ${files.length} · ${file.name}`;
      $('#upload-progress').value = index / files.length * 100;
      try {
        if (file.size > (state.overview?.max_upload_bytes || 25 * 1024 * 1024)) throw new Error('文件超过单张导入大小限制');
        const result = await bridge.upload('gallery/import', file);
        if (result.duplicate) { duplicates++; if (result.image.deleted_at) trashed++; }
        else imported++;
        if (targetAlbum && !result.image.deleted_at) albumImages.push(result.image.id);
      } catch (error) { failures.push(`${file.name}：${errorMessage(error)}`); }
    }
    if (targetAlbum && albumImages.length) {
      try { await bridge.apiPost('gallery/batch', { ids: [...new Set(albumImages)], action: 'add_album', value: targetAlbum }); }
      catch (error) { failures.push(`图片已导入，但加入相册失败：${errorMessage(error)}`); }
    }
    $('#upload-progress').value = 100;
    state.view = 'all'; state.album = targetAlbum; state.page = 1; clearFilters(); clearSelection();
    await refresh();
    const summary = [`${imported} 张已导入`, duplicates ? `${duplicates} 张已存在${trashed ? `（${trashed} 张在回收站）` : ''}` : '', failures.length ? `${failures.length} 项未完成` : ''].filter(Boolean).join(' · ');
    toast(summary, failures.length ? 'error' : 'success', failures.length ? { label: '查看原因', run: () => ask({ title: '导入结果', description: failures.join('\n'), submit: '知道了', symbol: 'info' }) } : undefined);
  } finally {
    state.uploading = false; syncBusy(); $('#upload-status').hidden = true; $('#file-input').value = '';
  }
}

listen('#brand-home', 'click', e => { e.preventDefault(); return navigate(); });
for (const button of $$('[data-view]')) button.addEventListener('click', () => navigate(button.dataset.view));
for (const button of $$('.source-tab')) button.addEventListener('click', () => {
  state.source = button.dataset.source; state.page = 1; clearSelection(); syncFilters(); refresh();
});
listen('#refresh', 'click', () => { clearTimeout(searchTimer); return refresh(); });
listen('#import-button', 'click', () => $('#file-input').click());
listen('#file-input', 'change', e => importFiles(e.target.files));
listen('#create-album', 'click', () => withBusy(createAlbum));
listen('#rename-album', 'click', () => withBusy(async () => {
  const album = state.overview.albums.find(item => item.id === state.album);
  if (!album) return;
  const answer = await ask({ title: '重命名相册', fields: [{ name: 'name', label: '相册名称', value: album.name, maxLength: 60, required: true }], submit: '保存名称', symbol: 'edit' });
  if (!answer) return;
  await bridge.apiPost('gallery/albums/save', { id: album.id, name: answer.name });
  await refresh(false); toast('相册已重命名');
}));
listen('#delete-album', 'click', () => withBusy(async () => {
  const album = state.overview.albums.find(item => item.id === state.album);
  if (!album) return;
  const answer = await ask({ title: '删除这个相册？', description: `将删除「${album.name}」，其中的图片仍会保留在图库中。`, submit: '删除相册', symbol: 'folder-minus' });
  if (!answer) return;
  await bridge.apiPost('gallery/albums/delete', { id: album.id });
  await navigate(); toast('相册已删除，图片已保留');
}));
listen('#search', 'input', e => {
  state.query = e.target.value; state.page = 1; clearSelection(); clearTimeout(searchTimer);
  searchTimer = setTimeout(() => refresh(), 280);
});
listen('#filter-toggle', 'click', () => {
  const panel = $('#filter-panel'); panel.hidden = !panel.hidden;
  $('#filter-toggle').setAttribute('aria-expanded', String(!panel.hidden));
});
for (const [selector, key] of [['#model-filter', 'model'], ['#tag-filter', 'tag'], ['#orientation-filter', 'orientation'], ['#date-from', 'from'], ['#date-to', 'to'], ['#sort', 'sort']]) {
  listen(selector, 'change', e => { state[key] = e.target.value; state.page = 1; clearSelection(); syncFilters(); return refresh(); });
}
for (const selector of ['#reset-filters', '#clear-search']) listen(selector, 'click', () => { clearFilters(); clearSelection(); state.page = 1; return refresh(); });
listen('#density', 'click', () => {
  state.compact = !state.compact; grid.classList.toggle('compact', state.compact);
  $('#density').setAttribute('aria-pressed', String(state.compact)); scheduleLayout();
});
listen('#selection-mode', 'click', () => { if (state.selecting) clearSelection(); else { state.selecting = true; renderSelection(); } });
listen('#clear-selection', 'click', clearSelection);
listen('#select-page', 'change', e => {
  const ids = state.items.map(image => image.id);
  if (e.target.checked && new Set([...state.selected, ...ids]).size > (state.overview?.max_batch || 100)) { renderSelection(); throw new Error('单次最多选择 100 张图片，请先取消部分选择'); }
  for (const id of ids) { if (e.target.checked) state.selected.add(id); else state.selected.delete(id); }
  renderSelection();
});
for (const button of $$('[data-batch]')) button.addEventListener('click', () => batchAction(button.dataset.batch).catch(error => toast(errorMessage(error), 'error')));
for (const [selector, direction] of [['#previous-page', -1], ['#next-page', 1]]) listen(selector, 'click', async () => { state.page += direction; await refresh(); $('#main-content').scrollIntoView({ block: 'start', behavior: 'instant' }); });
listen('#close-viewer', 'click', closeViewer); listen('#close-viewer-mobile', 'click', closeViewer);
listen('#viewer-previous', 'click', () => adjacentImage(-1)); listen('#viewer-next', 'click', () => adjacentImage(1));
listen('#preview-zoom', 'click', () => { const zoomed = $('#preview-stage').classList.toggle('zoomed'); $('#preview-zoom').setAttribute('aria-pressed', String(zoomed)); });
listen('#detail-download', 'click', downloadCurrent); listen('#detail-favorite', 'click', favoriteCurrent);
listen('#detail-trash', 'click', () => state.current && batchAction('trash', [state.current.id]));
listen('#detail-restore', 'click', () => state.current && batchAction('restore', [state.current.id]));
listen('#copy-prompt', 'click', () => state.current?.prompt && copyText(state.current.prompt));
listen('#copy-command', 'click', () => {
  const image = state.current;
  if (image?.command && image.prompt) return copyText([`/${image.command}`, image.prompt, image.aspect_ratio, image.resolution].filter(Boolean).join(' '));
});
listen('#detail-form', 'input', () => { state.dirty = true; });
listen('#detail-form', 'change', () => { state.dirty = true; });
// Keep native form validation while saving through the bridge.
$('#detail-form').addEventListener('submit', e => {
  e.preventDefault();
  if (!state.current) return;
  const id = state.current.id;
  withBusy(async () => {
    const image = await bridge.apiPost(`gallery/images/${id}/update`, { title: $('#edit-title').value,
      tags: parseTags($('#edit-tags').value), notes: $('#edit-notes').value,
      album_ids: $$('#detail-albums input:checked').map(input => input.value) });
    if (state.current?.id === id) { state.current = image; state.dirty = false; renderDetail(image); }
    await refresh(false); toast('修改已保存');
  }).catch(error => toast(errorMessage(error), 'error'));
});

document.addEventListener('keydown', e => {
  if (actionDialog.open || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  const typing = e.target.matches('input, textarea, select, [contenteditable]');
  if (typing) return;
  if (viewer.open) {
    if (e.key === 'ArrowLeft') { e.preventDefault(); adjacentImage(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); adjacentImage(1); }
    if (e.key.toLowerCase() === 'f') { e.preventDefault(); favoriteCurrent().catch(error => toast(errorMessage(error), 'error')); }
  } else {
    if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
    if (e.key === 'Escape') clearSelection();
    if (e.key === 'Delete' && state.selected.size) { e.preventDefault(); batchAction(state.view === 'trash' ? 'purge' : 'trash').catch(error => toast(errorMessage(error), 'error')); }
  }
});
let dragDepth = 0;
document.addEventListener('dragenter', e => {
  if (![...(e.dataTransfer?.types || [])].includes('Files') || viewer.open || actionDialog.open) return;
  e.preventDefault(); dragDepth++; if (state.ready && !state.uploading) $('#drop-overlay').hidden = false;
});
document.addEventListener('dragover', e => { if ([...(e.dataTransfer?.types || [])].includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
document.addEventListener('dragleave', e => { e.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; $('#drop-overlay').hidden = true; } });
document.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; $('#drop-overlay').hidden = true;
  if (!viewer.open && !actionDialog.open) importFiles(e.dataTransfer.files).catch(error => toast(errorMessage(error), 'error'));
});

function applyContext(context) {
  if (context && typeof context.isDark === 'boolean') document.documentElement.dataset.theme = context.isDark ? 'dark' : 'light';
}
async function initialize() {
  renderSkeleton(); syncBusy();
  bridge = window.AstrBotPluginPage;
  if (!bridge) {
    $('#result-count').textContent = '等待连接 AstrBot';
    grid.setAttribute('aria-busy', 'false');
    showEmpty({ title: '从 AstrBot 打开你的图库', description: '前往 AstrBot WebUI → 插件 → AI 绘图聚合 → 图片管理。\n此页面需要支持插件 Pages 与 astrbot.api.web 的 AstrBot 版本。' });
    return;
  }
  let timeout;
  try {
    const context = await Promise.race([bridge.ready(), new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('连接 AstrBot 超时，请从插件详情页重新打开图库')), 12000); })]);
    applyContext(context);
    unsubscribeContext = bridge.onContext?.(applyContext);
    state.ready = true; syncBusy();
    await refresh();
  } catch (error) {
    grid.setAttribute('aria-busy', 'false');
    showEmpty({ title: '暂时无法连接 AstrBot', description: errorMessage(error), actionLabel: '重新加载', action: () => location.reload() });
  } finally { clearTimeout(timeout); }
}
window.addEventListener('beforeunload', () => {
  thumbObserver.disconnect(); resizeObserver.disconnect(); unsubscribeContext?.();
  clearTimeout(searchTimer); clearTimeout(thumbTimer); cancelAnimationFrame(layoutFrame);
});
initialize();
