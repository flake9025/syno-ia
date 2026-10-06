/* syno-ia — interface web (sans dépendance, sans build). */
'use strict';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = {
  user: null,
  sources: [],
  history: [],
  controller: null,
  adminTimer: null,
};

/* ------------------------------------------------------------------ API */
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401) {
    state.user = null;
    showLogin();
    throw new Error(t('error.session'));
  }
  const payload = response.headers.get('content-type')?.includes('application/json')
    ? await response.json()
    : {};
  if (!response.ok) {
    const error = new Error(payload.detail || `HTTP ${response.status}`);
    error.status = response.status;
    error.dsmCode = response.headers.get('X-DSM-Error');
    throw error;
  }
  return payload;
}

/* ------------------------------------------------------------- Utilitaires */
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

/** Rendu Markdown minimal et sûr : le HTML est échappé avant toute mise en forme. */
function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  html = html.replace(/^### (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^## (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1">$1</span>');
  return html;
}

function formatBytes(bytes) {
  if (!bytes) return '0 o';
  const units = ['o', 'Ko', 'Mo', 'Go', 'To'];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
}

function formatDate(seconds) {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toLocaleString(LANG === 'fr' ? 'fr-FR' : 'en-GB', {
    dateStyle: 'short', timeStyle: 'short',
  });
}

let toastTimer = null;
function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add('hidden'), 2800);
}

/* ------------------------------------------------------------ Navigation */
function showLogin() {
  $('#login-view').classList.remove('hidden');
  $('#app-view').classList.add('hidden');
  $('#password').value = '';
  // Un code à usage unique ne se rejoue pas : on repart du formulaire simple,
  // le refus DSM révélera à nouveau le champ si le second facteur est requis.
  $('#otp').value = '';
  $('#otp-field').classList.add('hidden');
  $('#trust-field').classList.add('hidden');
}

function showApp() {
  $('#login-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  $('#question').focus();
}

/* --------------------------------------------------------------- Connexion */
async function handleLogin(event) {
  event.preventDefault();
  const button = $('#login-submit');
  const error = $('#login-error');
  error.classList.add('hidden');
  button.disabled = true;
  button.firstElementChild.textContent = t('login.pending');

  try {
    const otpVisible = !$('#otp-field').classList.contains('hidden');
    const user = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        account: $('#account').value.trim(),
        password: $('#password').value,
        otp_code: $('#otp').value.trim() || null,
        language: LANG,
        // Hors de l'étape 2FA, « null » signifie « ne touche pas à l'appareil mémorisé ».
        trust_device: otpVisible ? $('#trust-device').checked : null,
      }),
    });
    state.user = user;
    await enterApp();
  } catch (exc) {
    if (exc.status === 428 || ['403', '404', '406'].includes(exc.dsmCode)) {
      $('#otp-field').classList.remove('hidden');
      $('#trust-field').classList.remove('hidden');
      $('#otp').focus();
      error.textContent = exc.message || t('login.otpRequired');
    } else {
      error.textContent = exc.message || t('login.failed');
    }
    error.classList.remove('hidden');
  } finally {
    button.disabled = false;
    button.firstElementChild.textContent = t('login.submit');
  }
}

async function enterApp() {
  showApp();
  renderUser();
  await Promise.all([loadShares(), loadDocuments(), loadEngineBadge()]);
}

function renderUser() {
  const user = state.user;
  if (!user) return;
  $('#user-name').textContent = user.account;
  $('#user-role').textContent = user.is_admin ? 'Administrateur DSM' : 'Utilisateur';
  $('#user-initials').textContent = user.account.slice(0, 2).toUpperCase();
  $('#open-admin').classList.toggle('hidden', !user.is_admin);
}

/* ------------------------------------------------------------- Barre latérale */
async function loadShares() {
  const list = $('#share-list');
  try {
    const { shares } = await api('/api/auth/shares');
    list.innerHTML = shares.length
      ? shares.map((share) => `
          <li title="${escapeHtml(share.path)}">
            <span class="badge-dot ${share.indexed ? '' : 'off'}"></span>
            <span class="doc-name">${escapeHtml(share.name || share.path)}</span>
          </li>`).join('')
      : `<li class="empty">${t('app.noShares')}</li>`;
  } catch {
    list.innerHTML = `<li class="empty">${t('app.noShares')}</li>`;
  }
}

const badgeState = { engine: null, totals: null };

function renderEngineBadge() {
  const { engine, totals } = badgeState;
  if (!engine) {
    $('#engine-badge').textContent = '';
    return;
  }
  const llm = engine.llm === 'extractive' ? 'extractif' : engine.llm;
  const parts = [];
  if (totals) parts.push(`${totals.documents} doc · ${totals.chunks} extraits`);
  parts.push(llm, engine.profile);
  $('#engine-badge').textContent = parts.join(' · ');
}

async function loadDocuments(query = '') {
  const list = $('#doc-list');
  try {
    const { documents, total } = await api(`/api/documents?limit=60&q=${encodeURIComponent(query)}`);
    list.innerHTML = documents.length
      ? documents.map((doc) => `
          <li title="${escapeHtml(doc.dsm_path)}">
            <span class="doc-name">${escapeHtml(doc.name)}</span>
            <span class="doc-meta">${formatBytes(doc.size)}</span>
          </li>`).join('')
      : `<li class="empty">${t('app.noDocs')}</li>`;
    // Le bandeau annonce le corpus complet de l'utilisateur, pas le résultat d'un filtre.
    if (!query && total) {
      badgeState.totals = total;
      renderEngineBadge();
    }
  } catch {
    list.innerHTML = `<li class="empty">${t('app.noDocs')}</li>`;
  }
}

async function loadEngineBadge() {
  try {
    badgeState.engine = await (await fetch('/api/health')).json();
  } catch {
    badgeState.engine = null;
  }
  renderEngineBadge();
}

/* --------------------------------------------------------------------- Chat */
function appendMessage(role, html) {
  $('#welcome')?.classList.add('hidden');
  const wrapper = document.createElement('div');
  wrapper.className = `msg ${role}`;
  wrapper.innerHTML = `
    <div class="role">${role === 'user' ? 'Moi' : 'IA'}</div>
    <div class="bubble"><div class="text">${html}</div></div>`;
  $('#messages').appendChild(wrapper);
  scrollToBottom();
  return wrapper;
}

function scrollToBottom() {
  const container = $('#messages');
  container.scrollTop = container.scrollHeight;
}

function renderSources(node, sources, meta) {
  if (!sources.length) return;
  const block = document.createElement('div');
  block.className = 'sources';
  const notes = [];
  if (meta.filtered_out) notes.push(t('app.filtered', { n: meta.filtered_out }));
  if (meta.lexical_only) notes.push(t(meta.embeddings_pending ? 'app.lexicalWarmup' : 'app.lexicalOnly'));
  block.innerHTML = `
    <h3>${t('app.sources')}</h3>
    ${sources.map((source) => `
      <div class="source" id="source-${source.index}">
        <span class="num">${source.index}</span>
        <div class="info">
          <div class="name">${escapeHtml(source.name)}${source.location ? ` — ${escapeHtml(source.location)}` : ''}</div>
          <div class="path">${escapeHtml(source.path)}</div>
          <div class="excerpt">${escapeHtml(source.excerpt)}</div>
        </div>
        <a class="btn ghost small" href="/api/document?path=${encodeURIComponent(source.path)}">${t('app.open')}</a>
      </div>`).join('')}
    ${notes.length ? `<div class="notice">${notes.map(escapeHtml).join(' ')}</div>` : ''}`;
  node.querySelector('.bubble').appendChild(block);
  scrollToBottom();
}

async function ask(question) {
  if (!question.trim() || state.controller) return;

  appendMessage('user', escapeHtml(question));
  const assistant = appendMessage('assistant',
    '<span class="typing"><i></i><i></i><i></i></span> ' + t('app.thinking'));
  const textNode = assistant.querySelector('.text');

  state.controller = new AbortController();
  $('#send').classList.add('hidden');
  $('#stop').classList.remove('hidden');

  const status = document.createElement('div');
  status.className = 'msg-status';
  assistant.querySelector('.bubble').appendChild(status);

  const askedAt = Date.now();
  let engine = null;
  const refreshStatus = () => {
    const name = engine ? (engine.model || engine.backend) : '…';
    const s = ((Date.now() - askedAt) / 1000).toFixed(0);
    status.textContent = t('app.working', { model: name, s });
  };
  refreshStatus();
  const ticker = setInterval(refreshStatus, 1000);

  let answer = '';
  let started = false;
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history: state.history.slice(-4) }),
      signal: state.controller.signal,
    });
    if (response.status === 401) { showLogin(); return; }
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let separator;
      while ((separator = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);

        let event = 'message';
        let data = '';
        for (const line of raw.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        const payload = JSON.parse(data);

        if (event === 'sources') {
          state.sources = payload.sources || [];
          engine = payload.engine || null;
          refreshStatus();
          renderSources(assistant, state.sources, payload);
        } else if (event === 'token') {
          if (!started) { textNode.innerHTML = ''; started = true; }
          answer += payload.text;
          textNode.innerHTML = renderMarkdown(answer);
          scrollToBottom();
        } else if (event === 'notice') {
          toast(payload.message);
        } else if (event === 'error') {
          textNode.innerHTML = `<span class="error">${escapeHtml(payload.message)}</span>`;
          started = true;
        } else if (event === 'done') {
          answer = payload.answer || answer;
          textNode.innerHTML = renderMarkdown(answer);
          renderStats(status, payload.engine || engine, payload.timing);
          addMessageTools(assistant, answer);
        }
      }
    }
    state.history.push({ role: 'user', content: question }, { role: 'assistant', content: answer });
  } catch (exc) {
    if (exc.name === 'AbortError') {
      textNode.innerHTML = renderMarkdown(answer) + `<div class="notice">${t('app.stopped')}</div>`;
    } else {
      textNode.innerHTML = `<span class="error">${escapeHtml(exc.message || t('error.network'))}</span>`;
    }
  } finally {
    clearInterval(ticker);
    if (!status.classList.contains('final')) status.remove();
    state.controller = null;
    $('#send').classList.remove('hidden');
    $('#stop').classList.add('hidden');
    scrollToBottom();
  }
}

function renderStats(node, engine, timing) {
  if (!timing) { node.remove(); return; }
  const parts = [];
  if (engine && engine.model) parts.push(engine.model);
  else if (engine && engine.backend === 'extractive') parts.push(t('app.engineExtractive'));
  else if (engine && engine.backend) parts.push(engine.backend);
  parts.push(t('app.statTotal', { s: (timing.total_ms / 1000).toFixed(1) }));
  if (timing.first_token_ms) {
    parts.push(t('app.statFirst', { s: (timing.first_token_ms / 1000).toFixed(1) }));
  }
  if (timing.tokens_per_second) {
    parts.push(t('app.statSpeed', { n: timing.tokens_per_second }));
  }
  if (timing.truncated) parts.push(t('app.statTruncated'));
  node.textContent = parts.join(' · ');
  node.classList.add('final');
}

function addMessageTools(node, answer) {
  const tools = document.createElement('div');
  tools.className = 'msg-tools';
  const copy = document.createElement('button');
  copy.className = 'btn ghost small';
  copy.textContent = t('app.copy');
  copy.addEventListener('click', async () => {
    await navigator.clipboard.writeText(answer);
    toast(t('app.copied'));
  });
  tools.appendChild(copy);
  node.querySelector('.bubble').appendChild(tools);
}

/* -------------------------------------------------------------- Administration */
async function openAdmin() {
  $('#admin-dialog').showModal();
  await refreshAdmin();
  state.adminTimer = setInterval(refreshAdmin, 3000);
}

function closeAdmin() {
  clearInterval(state.adminTimer);
  state.adminTimer = null;
  $('#admin-dialog').close();
}

async function refreshAdmin() {
  try {
    const status = await api('/api/admin/status');
    renderOverview(status);
    renderProgress(status.indexing);
    if ($('.tab-panel[data-panel="models"]').classList.contains('active')) {
      renderModels(await api('/api/admin/models'));
    }
  } catch (exc) {
    console.warn('admin', exc);
  }
}

function embeddingState(status) {
  const state = status.embedder.state;
  const vector = status.vectorization || {};
  if (state === 'pending' || state === 'loading') return 'chargement du modèle…';
  if (state === 'failed') return `indisponible — ${status.embedder.reason || 'BM25 seul'}`;
  if (state === 'disabled') return 'désactivé (BM25 seul)';
  if (vector.running) return `vectorisation ${vector.done}/${vector.total || '?'}`;
  if (vector.status === 'error') return `rattrapage en erreur — ${vector.last_error}`;
  return status.embedder.available ? 'recherche hybride active' : 'BM25 seul';
}

function renderOverview(status) {
  const hardware = status.hardware;
  const index = status.index;
  const warnings = (hardware.warnings || []).concat(status.startup_errors || []);
  $('#panel-overview').innerHTML = `
    <div class="cards">
      <div class="card">
        <h3>${t('admin.hardware')}</h3>
        ${kv('CPU', `${hardware.cpu_model || hardware.architecture} · ${hardware.cpu_count} cœur(s)`)}
        ${kv('RAM', `${hardware.available_ram_mb} / ${hardware.total_ram_mb} Mo`)}
        ${kv('SIMD', hardware.has_avx2 ? 'AVX2' : hardware.has_avx ? 'AVX' : '—')}
        ${kv('Profil', `${hardware.profile} (mém. ${hardware.memory_tier} / calc. ${hardware.compute_tier})`)}
        ${kv('Débit estimé', `~${hardware.estimated_tokens_per_second} jetons/s`)}
      </div>
      <div class="card">
        <h3>${t('admin.engine')}</h3>
        ${kv('Embeddings', `${status.embedder.backend} · ${status.embedder.model || '—'}`)}
        ${kv('Dimension', status.embedder.dimension || '—')}
        ${kv('État', embeddingState(status))}
        ${kv('LLM', `${status.llm.backend} · ${status.llm.model || '—'}`)}
        ${kv('Modèle suggéré', hardware.suggested_llm || '—')}
      </div>
      <div class="card">
        <h3>${t('admin.indexState')}</h3>
        ${kv('Documents', index.documents)}
        ${kv('Extraits', index.chunks)}
        ${kv('Vecteurs', index.vectors)}
        ${kv('En erreur', index.documents_failed)}
        ${kv('Base', formatBytes(index.db_size))}
      </div>
      <div class="card">
        <h3>${t('admin.dsm')}</h3>
        ${kv('URL', status.dsm.url)}
        ${kv('Compte de service', status.dsm.service_account ? 'connecté' : 'absent')}
        ${kv('Partages cartographiés', status.dsm.shares_mapped)}
        ${kv('Mode de lecture', status.dsm.mode)}
        ${kv('ACL stricte', status.dsm.acl_strict ? 'oui' : 'non')}
        ${kv('Sessions actives', status.sessions)}
      </div>
    </div>
    ${warnings.length ? `<ul class="warn-list">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : ''}`;
}

function kv(label, value) {
  return `<div class="kv"><span>${escapeHtml(label)}</span><span>${escapeHtml(String(value))}</span></div>`;
}

function renderProgress(progress) {
  const total = progress.total || 0;
  const done = progress.scanned || 0;
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  $('#index-progress').innerHTML = `
    ${kv('État', progress.status)}
    ${kv('Progression', total ? `${done} / ${total}` : '—')}
    <div class="bar"><i style="width:${percent}%"></i></div>
    ${kv('Indexés', progress.indexed)}
    ${kv('Inchangés', progress.skipped)}
    ${kv('Échecs', progress.failed)}
    ${kv('Supprimés', progress.removed)}
    ${kv('Durée', `${Math.round(progress.elapsed || 0)} s`)}
    ${kv('Dernier passage', formatDate(progress.last_run_at))}
    ${progress.current ? kv('En cours', progress.current) : ''}`;
  $('#index-errors').textContent = progress.last_error || '';
}

function renderModels(data) {
  const download = data.download || {};
  $('#panel-models').innerHTML = `
    ${download.status === 'running' || download.status === 'done'
      ? `<div class="progress-block">${kv('Téléchargement', download.model)}${kv('État', download.message)}</div><br>`
      : ''}
    ${data.models.map((model) => `
      <div class="model-row">
        <div class="info">
          <strong>${escapeHtml(model.label)}</strong>
          <small>${escapeHtml(model.repo_id)} · ~${model.approx_ram_mb} Mo de RAM · profil ${model.profile}</small>
        </div>
        ${model.recommended ? `<span class="pill">${t('admin.recommended')}</span>` : ''}
        ${model.installed
          ? `<span class="pill ok">${t('admin.installed')} (${formatBytes(model.size)})</span>
             <button class="btn ghost small danger" data-delete="${model.profile}"
                     title="${t('admin.deleteHint')}">${t('admin.delete')}</button>`
          : `<button class="btn small" data-download="${model.profile}">${t('admin.download')}</button>`}
      </div>`).join('')}`;
}

/* ------------------------------------------------------------------ Thème */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('syno-ia.theme', theme);
}

/* ------------------------------------------------------------ Initialisation */
function bindEvents() {
  $('#login-form').addEventListener('submit', handleLogin);

  $('#composer').addEventListener('submit', (event) => {
    event.preventDefault();
    const field = $('#question');
    const question = field.value.trim();
    field.value = '';
    field.style.height = 'auto';
    ask(question);
  });

  $('#question').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#composer').requestSubmit();
    }
  });

  $('#question').addEventListener('input', (event) => {
    event.target.style.height = 'auto';
    event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`;
  });

  $('#stop').addEventListener('click', () => state.controller?.abort());

  $('#new-chat').addEventListener('click', () => {
    state.history = [];
    $('#messages').innerHTML = '';
    $('#messages').appendChild($('#welcome') || document.createElement('div'));
    location.reload();
  });

  $('#logout').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    state.user = null;
    state.history = [];
    showLogin();
  });

  $('#doc-search').addEventListener('input', debounce((event) => loadDocuments(event.target.value), 350));

  $('#toggle-theme').addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });

  $('#toggle-lang').addEventListener('click', () => {
    setLang(LANG === 'fr' ? 'en' : 'fr');
    $('#toggle-lang').textContent = LANG.toUpperCase();
    renderUser();
    loadShares();
    loadDocuments();
  });

  $('#open-admin').addEventListener('click', openAdmin);
  $('#close-admin').addEventListener('click', closeAdmin);
  $('#admin-dialog').addEventListener('close', () => clearInterval(state.adminTimer));

  $('#open-sidebar').addEventListener('click', () => $('#sidebar').classList.add('open'));
  $('#close-sidebar').addEventListener('click', () => $('#sidebar').classList.remove('open'));

  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      $$('.tab').forEach((other) => other.classList.toggle('active', other === tab));
      $$('.tab-panel').forEach((panel) => {
        panel.classList.toggle('active', panel.dataset.panel === tab.dataset.tab);
      });
      refreshAdmin();
    });
  });

  $('#index-start').addEventListener('click', () => adminAction('/api/admin/index/start', { full: false }));
  $('#index-full').addEventListener('click', () => adminAction('/api/admin/index/start', { full: true }));
  $('#index-cancel').addEventListener('click', () => adminAction('/api/admin/index/cancel'));
  $('#index-optimize').addEventListener('click', () => adminAction('/api/admin/index/optimize'));

  $('#panel-models').addEventListener('click', async (event) => {
    const profile = event.target.dataset?.download;
    if (profile) { adminAction('/api/admin/models/download', { profile }); return; }

    const removable = event.target.dataset?.delete;
    if (!removable) return;
    if (!confirm(t('admin.deleteConfirm'))) return;
    try {
      const result = await api(`/api/admin/models?profile=${encodeURIComponent(removable)}`,
        { method: 'DELETE' });
      toast(t('admin.deleted', { size: formatBytes(result.freed_bytes) }));
      await refreshAdmin();
    } catch (exc) {
      toast(exc.message);
    }
  });

  $('#messages').addEventListener('click', (event) => {
    const index = event.target.dataset?.cite;
    if (index) document.getElementById(`source-${index}`)?.scrollIntoView({ block: 'center' });
  });

  $('#suggestions').addEventListener('click', (event) => {
    if (event.target.tagName === 'BUTTON') ask(event.target.textContent);
  });
}

async function adminAction(path, body) {
  try {
    await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
    await refreshAdmin();
  } catch (exc) {
    toast(exc.message);
  }
}

function debounce(callback, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

function renderSuggestions() {
  $('#suggestions').innerHTML = ['suggestion.1', 'suggestion.2', 'suggestion.3']
    .map((key) => `<button type="button">${escapeHtml(t(key))}</button>`)
    .join('');
}

async function init() {
  applyTheme(localStorage.getItem('syno-ia.theme') || 'dark');
  setLang(LANG);
  $('#toggle-lang').textContent = LANG.toUpperCase();
  renderSuggestions();
  bindEvents();

  try {
    const info = await (await fetch('/api/info')).json();
    $('#login-version').textContent = `${info.app} ${info.version || ''} · ${info.profile || ''}`;
  } catch { /* le serveur démarre peut-être encore */ }

  try {
    const me = await (await fetch('/api/auth/me', { credentials: 'same-origin' })).json();
    if (me.authenticated) {
      state.user = me;
      if (me.language) setLang(me.language);
      await enterApp();
      return;
    }
  } catch { /* ignoré : on affiche la connexion */ }
  showLogin();
}

document.addEventListener('DOMContentLoaded', init);
