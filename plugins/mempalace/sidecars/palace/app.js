/**
 * MemPalace Antigravity Aux Pane UI Extension — Frontend Application
 * Optimized for 400k+ drawer massive palaces (instant FTS5 search, indexed pagination,
 * progressive disclosure, keyboard card navigation, and strict a11y/contrast compliance).
 */
(function () {
  'use strict';

  const state = {
    activeTab: 'recall',
    activeSubtab: 'events',
    overview: null,
    taxonomy: {},
    wingTotals: [], // sorted [{ wing, count, roomsCount }]
    workspaceWing: '',
    palaceOffset: 0,
    palaceLimit: 30,
    palaceTotal: 0,
    roomsExpanded: false,
    tunnelsExpanded: false,
    livePollTimer: null,
    warmupPollTimer: null,
    inspectorPayload: null,
    lastFocusedElement: null,
    searchDebounceTimer: null,
    searchSeq: 0,
    kbCardIndex: -1,
  };

  // ── Utilities & Host Integration ───────────────────────────────────

  function getSidecar() {
    return window.sidecar || null;
  }

  async function apiPost(path, body = {}) {
    const sc = getSidecar();
    const fetchFn = sc && typeof sc.fetch === 'function' ? sc.fetch.bind(sc) : window.fetch.bind(window);
    const t0 = performance.now();
    try {
      let res = await fetchFn(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.status === 405) {
        const qs = new URLSearchParams();
        Object.entries(body || {}).forEach(([k, v]) => {
          if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
        });
        const url = qs.toString() ? `${path}?${qs.toString()}` : path;
        res = await fetchFn(url, { method: 'GET' });
      }
      const json = await res.json();
      json._clientMs = Math.round(performance.now() - t0);
      return json;
    } catch (err) {
      return { ok: false, error: err && err.message ? err.message : String(err) };
    }
  }

  function showToast(msg, durationMs = 2400) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      el.classList.add('hidden');
    }, durationMs);
  }

  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatNumber(n) {
    if (n == null || Number.isNaN(Number(n))) return '—';
    return Number(n).toLocaleString();
  }

  function formatCompact(n) {
    const num = Number(n);
    if (!Number.isFinite(num)) return '—';
    if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(num);
  }

  function basename(filePath) {
    if (!filePath) return '';
    const norm = String(filePath).replace(/\\/g, '/');
    const parts = norm.split('/').filter(Boolean);
    return parts.length ? parts[parts.length - 1] : norm;
  }

  function isResolvableFilePath(filePath) {
    if (!filePath) return false;
    const raw = String(filePath).trim();
    return /^(?:[a-zA-Z]:[\\/]|\/|\\\\|file:\/\/)/.test(raw);
  }

  function highlightQueryHtml(rawText, query) {
    const safe = escapeHtml(rawText || '');
    if (!query || !query.trim()) return safe;
    const terms = Array.from(
      new Set(
        query
          .trim()
          .split(/\s+/)
          .map((t) => t.replace(/[^a-zA-Z0-9_-]/g, ''))
          .filter((t) => t.length >= 2)
      )
    )
      .sort((a, b) => b.length - a.length)
      .slice(0, 6);
    if (!terms.length) return safe;
    const pattern = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
    return safe.replace(pattern, '<mark class="search-hl">$1</mark>');
  }

  // Detect whether host theme is light or dark from CSS variables injected by preload.js
  function syncThemeMode() {
    const style = getComputedStyle(document.documentElement);
    const bg = (style.getPropertyValue('--background') || '').trim();
    if (!bg) return;
    let r = 19,
      g = 21,
      b = 28;
    const hexMatch = bg.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (hexMatch) {
      let h = hexMatch[1];
      if (h.length === 3) {
        h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      }
      r = parseInt(h.slice(0, 2), 16);
      g = parseInt(h.slice(2, 4), 16);
      b = parseInt(h.slice(4, 6), 16);
    } else {
      const rgbMatch = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
      if (rgbMatch) {
        r = Number(rgbMatch[1]);
        g = Number(rgbMatch[2]);
        b = Number(rgbMatch[3]);
      }
    }
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    document.documentElement.setAttribute('data-theme', luminance > 0.55 ? 'light' : 'dark');
  }

  async function detectWorkspacePath() {
    const sc = getSidecar();
    if (!sc || typeof sc.getWorkspaceUris !== 'function') return '';
    try {
      const uris = await sc.getWorkspaceUris();
      if (Array.isArray(uris) && uris.length > 0) {
        const raw = String(uris[0] || '');
        const stripped = raw.replace(/^file:\/\/\/?/i, '');
        return decodeURIComponent(stripped);
      }
    } catch {
      // ignore
    }
    return '';
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      showToast('Copied verbatim to clipboard');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      showToast('Copied verbatim to clipboard');
    }
  }

  async function citeInChat(title, verbatimBody, locusTag = '') {
    const header = locusTag ? `[MemPalace ${locusTag}] ${title}` : `[MemPalace] ${title}`;
    const fullMessage = `${header}\n\`\`\`\n${verbatimBody}\n\`\`\``;
    const sc = getSidecar();
    if (sc && sc.agent && typeof sc.agent.sendMessage === 'function') {
      try {
        const convId = sc.conversationId || undefined;
        await sc.agent.sendMessage(fullMessage, convId);
        showToast('Sent verbatim citation to Antigravity chat');
        return;
      } catch {
        // Fallback to clipboard if agent channel is busy
      }
    }
    await copyToClipboard(fullMessage);
    showToast('Copied citation block for chat');
  }

  async function openSourceFile(sourceFile) {
    if (!isResolvableFilePath(sourceFile)) {
      showToast('Source is a relative label, not an absolute path on disk');
      return;
    }
    const sc = getSidecar();
    const raw = String(sourceFile).trim();
    const uri = raw.startsWith('file://')
      ? raw
      : 'file:///' + raw.replace(/\\/g, '/').replace(/^\/+/, '');
    if (sc && sc.ui && typeof sc.ui.toggleAuxPane === 'function') {
      try {
        await sc.ui.toggleAuxPane({ tabType: 'fileView', uri });
        return;
      } catch {
        // Fallback to copy path
      }
    }
    await copyToClipboard(raw);
    showToast('Copied source file path');
  }

  // ── Inspector Modal (with Focus Trap & Restoration) ────────────────

  function openInspector({ title, badgesHtml, body, citeTitle, citeLocus }) {
    state.lastFocusedElement = document.activeElement;
    state.inspectorPayload = {
      title: title || 'Drawer Inspector',
      body: body || '',
      citeTitle: citeTitle || title || 'Memory Drawer',
      citeLocus: citeLocus || '',
    };
    document.getElementById('inspector-title').textContent = state.inspectorPayload.title;
    document.getElementById('inspector-meta').innerHTML = badgesHtml || '';
    document.getElementById('inspector-body').textContent = state.inspectorPayload.body;
    const modal = document.getElementById('inspector-modal');
    modal.classList.remove('hidden');
    const closeBtn = document.getElementById('inspector-close-btn');
    if (closeBtn) closeBtn.focus();
  }

  function closeInspector() {
    const modal = document.getElementById('inspector-modal');
    if (modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    state.inspectorPayload = null;
    if (state.lastFocusedElement && typeof state.lastFocusedElement.focus === 'function') {
      state.lastFocusedElement.focus();
    }
    state.lastFocusedElement = null;
  }

  // ── Tab & Subtab Navigation ────────────────────────────────────────

  function switchTab(tabId) {
    state.activeTab = tabId;
    state.kbCardIndex = -1;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      const isMatch = btn.dataset.tab === tabId;
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
      btn.setAttribute('tabindex', isMatch ? '0' : '-1');
    });
    document.querySelectorAll('.tab-panel').forEach((panel) => {
      const isMatch = panel.id === `panel-${tabId}`;
      panel.classList.toggle('active', isMatch);
      if (isMatch) {
        panel.removeAttribute('hidden');
      } else {
        panel.setAttribute('hidden', '');
      }
    });

    if (tabId === 'palace') {
      loadPalaceDrawers();
    } else if (tabId === 'kg') {
      loadKnowledgeGraph();
    } else if (tabId === 'logstream') {
      if (state.activeSubtab === 'events') {
        loadLogstreamEvents();
      } else {
        loadAgentDiary();
      }
    }
  }

  function switchSubtab(subtabId) {
    state.activeSubtab = subtabId;
    document.querySelectorAll('.subtab-btn').forEach((btn) => {
      const isMatch = btn.dataset.subtab === subtabId;
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
    });
    document.getElementById('subview-events').classList.toggle('hidden', subtabId !== 'events');
    document.getElementById('subview-diary').classList.toggle('hidden', subtabId !== 'diary');
    if (subtabId === 'events') {
      loadLogstreamEvents();
    } else {
      loadAgentDiary();
    }
  }

  // ── Setup / Diagnostic Fallback Banner ─────────────────────────────

  function showSetupBanner(errorMsg, pythonBin) {
    const banner = document.getElementById('setup-banner');
    const msgEl = document.getElementById('setup-banner-msg');
    const detail = pythonBin ? `${errorMsg} (python: ${pythonBin})` : errorMsg;
    msgEl.textContent = detail;
    banner.classList.remove('hidden');
  }

  function hideSetupBanner() {
    document.getElementById('setup-banner').classList.add('hidden');
  }

  function updateEngineBadge(backend, totalDrawers, vectorWarm) {
    const badge = document.getElementById('engine-badge');
    if (!badge) return;
    const shortBackend = String(backend || 'sqlite').replace('sqlite_exact', 'sqlite_exact');
    if (vectorWarm) {
      badge.textContent = `${shortBackend} · ${formatCompact(totalDrawers)} warm`;
      badge.classList.add('warm');
      badge.title = `Backend: ${shortBackend} (${formatNumber(totalDrawers)} drawers). Vector matrix & FTS5 lexical engine are warm in RAM.`;
    } else {
      badge.textContent = `${shortBackend} · FTS5 ready`;
      badge.classList.remove('warm');
      badge.title = `Backend: ${shortBackend}. Instant FTS5 search is ready; pre-warming ${formatNumber(totalDrawers)} vectors in background…`;
    }
  }

  // ── Overview & Taxonomy Hydration ──────────────────────────────────

  async function loadOverview(restartBridge = false) {
    const workspacePath = await detectWorkspacePath();
    const res = await apiPost('/api/overview', {
      workspacePath,
      restartBridge,
      forceRefresh: Boolean(restartBridge),
    });
    if (!res || !res.ok) {
      showSetupBanner(
        (res && res.error) || 'Unable to reach MemPalace Python bridge.',
        res && res.pythonBin
      );
      return;
    }
    hideSetupBanner();

    const data = res.data || {};
    state.overview = data;
    state.workspaceWing = data.workspace_wing || '';

    const version = data.version || (data.status && data.status.version) || '3.12.0';
    document.getElementById('version-badge').textContent = `v${version}`;

    const status = data.status || {};
    const taxObj = (data.taxonomy && data.taxonomy.taxonomy) || status.wings || {};
    state.taxonomy = taxObj;

    // Sort wings by total drawers descending
    const wingEntries = [];
    for (const [wName, roomsMap] of Object.entries(taxObj)) {
      const rMap = roomsMap || {};
      const count = Object.values(rMap).reduce((acc, v) => acc + Number(v || 0), 0);
      wingEntries.push({
        wing: wName,
        count,
        roomsCount: Object.keys(rMap).length,
      });
    }
    wingEntries.sort((a, b) => b.count - a.count);
    state.wingTotals = wingEntries;

    const totalDrawers = status.total_drawers != null ? status.total_drawers : 0;
    const graphStats = data.graph_stats || {};
    const totalRooms =
      graphStats.total_rooms != null
        ? graphStats.total_rooms
        : Object.values(taxObj).reduce((acc, r) => acc + Object.keys(r || {}).length, 0);
    const tunnelRooms = graphStats.tunnel_rooms != null ? graphStats.tunnel_rooms : 0;

    const kgStats = data.kg_stats || {};
    const currentFacts =
      kgStats.current_facts != null
        ? kgStats.current_facts
        : kgStats.total_triples != null
        ? kgStats.total_triples
        : 0;

    const recentEvents = Array.isArray(data.recent_events) ? data.recent_events : [];
    const tipSeq = data.events_tip
      ? `#${data.events_tip.seq || recentEvents.length}`
      : String(recentEvents.length);

    // Populate tab-integrated live counts & disclosure pills
    document.getElementById('metric-drawers').textContent = formatCompact(totalDrawers);
    document.getElementById('metric-rooms').textContent = formatNumber(totalRooms);
    document.getElementById('metric-tunnels-sub').textContent = formatNumber(tunnelRooms);
    document.getElementById('metric-kg-facts').textContent = formatCompact(currentFacts);
    document.getElementById('metric-events').textContent = tipSeq;

    updateEngineBadge(status.backend || 'sqlite_exact', totalDrawers, Boolean(data.vector_warm));

    // Poll once or twice if background vector warmup is still running
    clearTimeout(state.warmupPollTimer);
    if (!data.vector_warm) {
      state.warmupPollTimer = setTimeout(async () => {
        const health = await apiPost('/api/overview', { workspacePath });
        if (health && health.ok && health.data) {
          updateEngineBadge(
            (health.data.status && health.data.status.backend) || 'sqlite_exact',
            totalDrawers,
            Boolean(health.data.vector_warm)
          );
        }
      }, 3500);
    }

    // Update topbar Workspace Wing pill
    const wsPill = document.getElementById('workspace-wing-pill');
    if (state.workspaceWing) {
      const wsInfo = wingEntries.find((w) => w.wing === state.workspaceWing);
      const wsCount = wsInfo ? wsInfo.count : 0;
      wsPill.textContent = `★ ${state.workspaceWing} · ${formatCompact(wsCount)}`;
      wsPill.classList.remove('hidden');
      syncWorkspacePillActive();
    } else {
      wsPill.classList.add('hidden');
    }

    populateWingSelects();
    renderWingQuickBar('search-wings-bar', document.getElementById('search-wing').value);
    renderWingQuickBar('palace-wings-bar', document.getElementById('palace-wing-select').value);
    renderPalaceSubstrips();
  }

  function syncWorkspacePillActive() {
    const wsPill = document.getElementById('workspace-wing-pill');
    if (!wsPill || !state.workspaceWing) return;
    const activeWing =
      state.activeTab === 'palace'
        ? document.getElementById('palace-wing-select').value
        : document.getElementById('search-wing').value;
    const isActive = activeWing === state.workspaceWing;
    wsPill.classList.toggle('active', isActive);
    wsPill.setAttribute('aria-pressed', isActive ? 'true' : 'false');
  }

  function populateWingSelects() {
    const wingSelectIds = ['search-wing', 'palace-wing-select', 'diary-wing'];
    wingSelectIds.forEach((id) => {
      const sel = document.getElementById(id);
      if (!sel) return;
      const prev = sel.value;
      sel.innerHTML = `<option value="">All Wings (${state.wingTotals.length})</option>`;
      state.wingTotals.forEach(({ wing, count }) => {
        const opt = document.createElement('option');
        opt.value = wing;
        const star = wing === state.workspaceWing ? '★ ' : '';
        opt.textContent = `${star}${wing} (${formatNumber(count)})`;
        sel.appendChild(opt);
      });
      if (prev && state.taxonomy[prev]) {
        sel.value = prev;
      }
    });

    populateRoomSelect('search-room', document.getElementById('search-wing').value);
    populateRoomSelect('palace-room-select', document.getElementById('palace-wing-select').value);
  }

  function populateRoomSelect(selectId, selectedWing) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const prev = sel.value;
    const roomCounts = {};
    if (selectedWing && state.taxonomy[selectedWing]) {
      Object.entries(state.taxonomy[selectedWing]).forEach(([r, c]) => {
        roomCounts[r] = (roomCounts[r] || 0) + Number(c || 0);
      });
    } else {
      Object.values(state.taxonomy).forEach((roomsMap) => {
        Object.entries(roomsMap || {}).forEach(([r, c]) => {
          roomCounts[r] = (roomCounts[r] || 0) + Number(c || 0);
        });
      });
    }
    const sortedRooms = Object.entries(roomCounts).sort((a, b) => b[1] - a[1]);
    sel.innerHTML = `<option value="">All Rooms (${sortedRooms.length})</option>`;
    sortedRooms.forEach(([room, count]) => {
      const opt = document.createElement('option');
      opt.value = room;
      opt.textContent = `${room} (${formatNumber(count)})`;
      sel.appendChild(opt);
    });
    if (prev && roomCounts[prev] != null) {
      sel.value = prev;
    }
  }

  // Render distilled horizontal Quick Wing Bar (top 7 wings + pinned workspace wing)
  function renderWingQuickBar(containerId, currentWing) {
    const bar = document.getElementById(containerId);
    if (!bar) return;
    const totalDrawers =
      (state.overview && state.overview.status && state.overview.status.total_drawers) || 0;

    const items = [];
    if (state.workspaceWing) {
      const wsEntry = state.wingTotals.find((w) => w.wing === state.workspaceWing);
      if (wsEntry) items.push({ ...wsEntry, isWorkspace: true });
    }
    for (const w of state.wingTotals) {
      if (w.wing === state.workspaceWing) continue;
      items.push(w);
      if (items.length >= 7) break;
    }

    const allActive = !currentWing;
    let html = `<button class="chip ${allActive ? 'active' : ''}" type="button" aria-pressed="${allActive}" data-quick-wing="">All <span class="chip-count">${formatCompact(totalDrawers)}</span></button>`;
    for (const item of items) {
      const isActive = currentWing === item.wing;
      const wsClass = item.isWorkspace ? 'chip-workspace' : '';
      const prefix = item.isWorkspace ? '★ ' : '';
      html += `<button class="chip ${wsClass} ${isActive ? 'active' : ''}" type="button" aria-pressed="${isActive}" data-quick-wing="${escapeHtml(item.wing)}" title="${escapeHtml(item.wing)}: ${formatNumber(item.count)} drawers">${prefix}${escapeHtml(item.wing)} <span class="chip-count">${formatCompact(item.count)}</span></button>`;
    }
    bar.innerHTML = html;

    bar.querySelectorAll('[data-quick-wing]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pickedWing = btn.dataset.quickWing || '';
        if (containerId === 'search-wings-bar') {
          const wingSel = document.getElementById('search-wing');
          wingSel.value = pickedWing;
          populateRoomSelect('search-room', pickedWing);
          renderWingQuickBar('search-wings-bar', pickedWing);
          syncWorkspacePillActive();
          if (document.getElementById('search-query').value.trim()) {
            runSearch();
          }
        } else {
          const wingSel = document.getElementById('palace-wing-select');
          wingSel.value = pickedWing;
          populateRoomSelect('palace-room-select', pickedWing);
          renderWingQuickBar('palace-wings-bar', pickedWing);
          renderPalaceSubstrips();
          syncWorkspacePillActive();
          state.palaceOffset = 0;
          loadPalaceDrawers();
        }
      });
    });
  }

  // ── TAB 1: RECALL (SEARCH — Instant FTS5 + Hybrid) ─────────────────

  async function runSearch(overrideStrategy = '') {
    const queryInput = document.getElementById('search-query');
    const query = (queryInput.value || '').trim();
    const wing = document.getElementById('search-wing').value;
    const room = document.getElementById('search-room').value;
    const strategy = overrideStrategy || document.getElementById('search-strategy').value || 'union';

    const metaBar = document.getElementById('search-meta-bar');
    const listEl = document.getElementById('search-results');

    if (!query) {
      metaBar.classList.add('hidden');
      listEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">Search your memory palace</p>
          <p class="empty-sub">Start typing for sub-10ms FTS5 lexical recall, or press Enter for full Hybrid (Vector + BM25) semantic recall. Use j/k to navigate cards and Enter to inspect.</p>
          <div class="quick-chips">
            <button class="chip" type="button" data-quick-search="architectural decisions">architectural decisions</button>
            <button class="chip" type="button" data-quick-search="antigravity plugin">antigravity plugin</button>
            <button class="chip" type="button" data-quick-search="coordination protocol">coordination protocol</button>
            <button class="chip" type="button" data-quick-search="performance optimization">performance optimization</button>
          </div>
        </div>`;
      bindQuickSearchChips();
      return;
    }

    const mySeq = ++state.searchSeq;
    metaBar.classList.remove('hidden');
    const modeLabel = strategy === 'lexical' ? 'FTS5 lexical' : strategy === 'vector' ? 'Vector' : 'Hybrid';
    metaBar.innerHTML = `<span>Searching <strong>"${escapeHtml(query)}"</strong> (${modeLabel})…</span>`;

    const res = await apiPost('/api/search', {
      query,
      wing,
      room,
      strategy,
      limit: 25,
    });

    if (mySeq !== state.searchSeq) return; // superseded by newer keystroke

    if (!res || !res.ok) {
      metaBar.innerHTML = `<span style="color:var(--rose-fg)">Search error: ${escapeHtml((res && res.error) || 'failed')}</span>`;
      return;
    }

    const data = res.data || {};
    if (data.vector_warm && state.overview && state.overview.status) {
      updateEngineBadge(state.overview.status.backend, state.overview.status.total_drawers, true);
    }

    const results = Array.isArray(data.results) ? data.results : [];
    const elapsedMs = res.elapsed_ms != null ? res.elapsed_ms : res._clientMs;
    const usedStrategy = data.strategy === 'lexical' || strategy === 'lexical' ? 'FTS5' : strategy;

    metaBar.innerHTML = `
      <span><strong>${results.length}</strong> matches for <strong>"${escapeHtml(query)}"</strong>${wing ? ` in <span class="badge badge-wing">${escapeHtml(wing)}</span>` : ''}${room ? ` / <span class="badge badge-room">${escapeHtml(room)}</span>` : ''}</span>
      <span class="badge badge-subtle">${escapeHtml(usedStrategy)} · ${elapsedMs}ms</span>
    `;

    if (results.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No verbatim matches found</p>
          <p class="empty-sub">Try clearing the Wing/Room filter or pressing Enter for Hybrid semantic recall.</p>
        </div>`;
      return;
    }

    renderDrawerCards(listEl, results, true, query);
  }

  function renderDrawerCards(container, items, isSearchResult = false, highlightQuery = '') {
    state.kbCardIndex = -1;
    let prevGroupKey = '';

    container.innerHTML = items
      .map((item, idx) => {
        const wing = item.wing || 'unknown';
        const room = item.room || 'general';
        const srcFile = item.source_file || '';
        const srcBase = basename(srcFile);
        const canOpenFile = isResolvableFilePath(srcFile);
        const drawerId = item.id || item.drawer_id || '';
        const filedAt = (item.filed_at || '').slice(0, 16).replace('T', ' ');
        const text = item.text != null ? item.text : item.content || '';
        const visualLines = Math.max(text.split('\n').length, Math.ceil(text.length / 68));
        const isLong = visualLines > 4 || text.length > 290;
        const isTruncated = Boolean(item.is_truncated) || text.length >= 790;
        const expandId = `exp-${isSearchResult ? 's' : 'd'}-${idx}`;
        const expandLabel = `Show full drawer (${formatNumber(text.length)}${isTruncated ? '+' : ''} chars)`;

        const groupKey = `${wing}::${room}::${srcBase}`;
        const isGrouped = groupKey === prevGroupKey && Boolean(srcBase);
        prevGroupKey = groupKey;

        // Score badges: support both vector similarity and BM25 score
        let scoreBadges = '';
        if (item.similarity != null) {
          const pct = Math.round(Number(item.similarity) * 100);
          scoreBadges += `<span class="badge badge-score" title="Vector semantic similarity">${pct}%</span>`;
        }
        if (item.bm25_score != null) {
          scoreBadges += `<span class="badge badge-bm25" title="FTS5 BM25 lexical relevance">BM25 ${Number(item.bm25_score).toFixed(1)}</span>`;
        }
        const viaCloset = item.via_closet
          ? `<span class="badge badge-subtle" title="Matched via summarized closet">closet</span>`
          : '';

        const renderedContent = highlightQuery
          ? highlightQueryHtml(text, highlightQuery)
          : escapeHtml(text);

        const headerBadges = isGrouped
          ? `<span class="card-thread-label">↳ same source (${escapeHtml(wing)} / ${escapeHtml(room)})</span>${scoreBadges}${viaCloset}`
          : `<button class="badge badge-wing" type="button" data-jump-wing="${escapeHtml(wing)}" title="Filter Palace by wing: ${escapeHtml(wing)}">${escapeHtml(wing)}</button>
             <span class="badge badge-room">${escapeHtml(room)}</span>
             ${scoreBadges}
             ${viaCloset}`;

        return `
          <article class="card ${isGrouped ? 'card-grouped' : ''}" data-card-idx="${idx}">
            <div class="card-header">
              <div class="card-badges">
                ${headerBadges}
              </div>
              <div class="card-meta-right">
                ${filedAt ? `<span>${escapeHtml(filedAt)}</span>` : ''}
              </div>
            </div>
            <div class="card-content" data-inspect-drawer="${idx}" title="Click to inspect full verbatim drawer">
              <div class="card-content-text ${isLong ? 'clamped' : ''}" data-expand-box="${expandId}">${renderedContent}</div>
            </div>
            ${
              isLong
                ? `<button class="expand-toggle-btn" type="button" data-expand-btn="${expandId}" data-expand-idx="${idx}" data-expand-label="${escapeHtml(expandLabel)}">${escapeHtml(expandLabel)}</button>`
                : ''
            }
            <div class="card-footer">
              <span class="card-source" title="${escapeHtml(srcFile || drawerId)}">${escapeHtml(srcBase || drawerId)}</span>
              <div class="card-actions">
                ${
                  canOpenFile
                    ? `<button class="btn btn-secondary btn-xs" type="button" data-open-file="${escapeHtml(srcFile)}" aria-label="Open source file ${escapeHtml(srcBase)}">Open File</button>`
                    : ''
                }
                <button class="btn btn-secondary btn-xs" type="button" data-inspect-drawer="${idx}" aria-label="Inspect drawer ${escapeHtml(srcBase || drawerId)}">Inspect</button>
                <button class="btn btn-secondary btn-xs" type="button" data-copy-idx="${idx}" aria-label="Copy drawer ${escapeHtml(srcBase || drawerId)}">Copy</button>
                <button class="btn btn-cite btn-xs" type="button" data-cite-idx="${idx}" aria-label="Cite drawer ${escapeHtml(srcBase || drawerId)} in Chat">Cite in Chat</button>
              </div>
            </div>
          </article>`;
      })
      .join('');

    // Wire expand/collapse toggle buttons (fetches full drawer on truncated items!)
    container.querySelectorAll('[data-expand-btn]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const targetId = btn.dataset.expandBtn;
        const idx = Number(btn.dataset.expandIdx);
        const item = items[idx];
        const box = container.querySelector(`[data-expand-box="${targetId}"]`);
        if (!box) return;
        const isExpanding = box.classList.contains('clamped');
        if (isExpanding) {
          box.classList.remove('clamped');
          box.classList.add('expanded');
          btn.textContent = 'Collapse drawer';
          const drawerId = item && (item.id || item.drawer_id);
          if (drawerId && !item._fullLoaded && (item.is_truncated || (item.content || item.text || '').length >= 790)) {
            btn.textContent = 'Loading full verbatim drawer…';
            const res = await apiPost('/api/drawer', { drawer_id: drawerId });
            if (res && res.ok && res.data && res.data.content) {
              item.content = res.data.content;
              item._fullLoaded = true;
              box.innerHTML = highlightQuery
                ? highlightQueryHtml(res.data.content, highlightQuery)
                : escapeHtml(res.data.content);
              const updatedLabel = `Show full drawer (${formatNumber(res.data.content.length)} chars)`;
              btn.dataset.expandLabel = updatedLabel;
            }
            btn.textContent = 'Collapse drawer';
          }
        } else {
          box.classList.add('clamped');
          box.classList.remove('expanded');
          btn.textContent = btn.dataset.expandLabel || 'Show full drawer';
        }
      });
    });

    // Wire Wing jump badges
    container.querySelectorAll('[data-jump-wing]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const w = btn.dataset.jumpWing;
        const wingSel = document.getElementById('palace-wing-select');
        if (wingSel) {
          wingSel.value = w;
          populateRoomSelect('palace-room-select', w);
          renderWingQuickBar('palace-wings-bar', w);
          renderPalaceSubstrips();
          syncWorkspacePillActive();
          state.palaceOffset = 0;
        }
        switchTab('palace');
      });
    });

    // Wire Open File buttons
    container.querySelectorAll('[data-open-file]').forEach((btn) => {
      btn.addEventListener('click', () => openSourceFile(btn.dataset.openFile));
    });

    // Wire Inspect (card content click + Inspect button)
    container.querySelectorAll('[data-inspect-drawer]').forEach((el) => {
      el.addEventListener('click', async () => {
        // Don't hijack text selection inside card-content
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;

        const item = items[Number(el.dataset.inspectDrawer)];
        if (!item) return;
        const drawerId = item.id || item.drawer_id || '';
        let fullText = item.text != null ? item.text : item.content || '';

        if (drawerId && !item._fullLoaded) {
          const res = await apiPost('/api/drawer', { drawer_id: drawerId });
          if (res && res.ok && res.data && res.data.content) {
            fullText = res.data.content;
            item.content = fullText;
            item._fullLoaded = true;
          }
        }

        const badgesHtml = `
          <span class="badge badge-wing">${escapeHtml(item.wing || 'unknown')}</span>
          <span class="badge badge-room">${escapeHtml(item.room || 'general')}</span>
          ${drawerId ? `<span class="badge badge-subtle">${escapeHtml(drawerId)}</span>` : ''}
          ${item.source_file ? `<span class="badge badge-subtle">${escapeHtml(basename(item.source_file))}</span>` : ''}
        `;
        openInspector({
          title: basename(item.source_file) || drawerId || 'Verbatim Drawer',
          badgesHtml,
          body: fullText,
          citeTitle: basename(item.source_file) || drawerId || 'Drawer',
          citeLocus: `${item.wing || 'unknown'}/${item.room || 'general'}`,
        });
      });
    });

    // Wire Copy buttons
    container.querySelectorAll('[data-copy-idx]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const item = items[Number(btn.dataset.copyIdx)];
        if (!item) return;
        copyToClipboard(item.text != null ? item.text : item.content || '');
      });
    });

    // Wire Cite in Chat buttons
    container.querySelectorAll('[data-cite-idx]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const item = items[Number(btn.dataset.citeIdx)];
        if (!item) return;
        const text = item.text != null ? item.text : item.content || '';
        const locus = `${item.wing || 'unknown'}/${item.room || 'general'}`;
        const label = basename(item.source_file) || item.id || 'Drawer';
        citeInChat(label, text, locus);
      });
    });
  }

  // ── TAB 2: PALACE BROWSER (WINGS -> ROOMS -> DRAWERS) ──────────────

  function updatePalaceFilterResetVisibility() {
    const wing = document.getElementById('palace-wing-select').value;
    const room = document.getElementById('palace-room-select').value;
    const resetBtn = document.getElementById('palace-reset-filters-btn');
    if (resetBtn) {
      resetBtn.classList.toggle('hidden', !wing && !room);
    }
  }

  function renderPalaceSubstrips() {
    const selectedWing = document.getElementById('palace-wing-select').value;
    const selectedRoom = document.getElementById('palace-room-select').value;
    const roomsStrip = document.getElementById('palace-rooms-strip');
    const tunnelsStrip = document.getElementById('palace-tunnels-strip');
    const roomsToggleBtn = document.getElementById('btn-toggle-rooms');
    const tunnelsToggleBtn = document.getElementById('btn-toggle-tunnels');

    updatePalaceFilterResetVisibility();

    // Aggregate rooms for selected wing (or top rooms across palace)
    const roomMap = {};
    if (selectedWing && state.taxonomy[selectedWing]) {
      Object.entries(state.taxonomy[selectedWing]).forEach(([r, c]) => {
        roomMap[r] = (roomMap[r] || 0) + Number(c || 0);
      });
    } else {
      Object.values(state.taxonomy).forEach((rObj) => {
        Object.entries(rObj || {}).forEach(([r, c]) => {
          roomMap[r] = (roomMap[r] || 0) + Number(c || 0);
        });
      });
    }

    const sortedRooms = Object.entries(roomMap).sort((a, b) => b[1] - a[1]);
    const roomsCountEl = document.getElementById('metric-rooms');
    if (roomsCountEl) roomsCountEl.textContent = formatNumber(sortedRooms.length);

    const topRooms = sortedRooms.slice(0, 18);
    const allRoomsActive = !selectedRoom;
    roomsStrip.innerHTML = [
      `<button class="chip ${allRoomsActive ? 'active' : ''}" type="button" aria-pressed="${allRoomsActive}" data-room-chip="">All Rooms</button>`,
      ...topRooms.map(([r, c]) => {
        const isAct = selectedRoom === r;
        return `<button class="chip ${isAct ? 'active' : ''}" type="button" aria-pressed="${isAct}" data-room-chip="${escapeHtml(r)}">${escapeHtml(r)} <span class="chip-count">${formatCompact(c)}</span></button>`;
      }),
    ].join('');

    roomsStrip.querySelectorAll('[data-room-chip]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const picked = chip.dataset.roomChip || '';
        document.getElementById('palace-room-select').value = picked;
        state.palaceOffset = 0;
        renderPalaceSubstrips();
        loadPalaceDrawers();
      });
    });

    // Tunnels strip (cross-wing rooms)
    const topTunnels =
      (state.overview && state.overview.graph_stats && state.overview.graph_stats.top_tunnels) || [];
    const relevantTunnels = selectedWing
      ? topTunnels.filter((t) => Array.isArray(t.wings) && t.wings.includes(selectedWing))
      : topTunnels.slice(0, 10);

    const tunnelsCountEl = document.getElementById('metric-tunnels-sub');
    if (tunnelsCountEl) tunnelsCountEl.textContent = formatNumber(relevantTunnels.length);

    if (relevantTunnels.length > 0) {
      tunnelsStrip.innerHTML =
        `<span class="tunnels-label">Tunnels:</span>` +
        relevantTunnels
          .slice(0, 10)
          .map((t) => {
            const wList = Array.isArray(t.wings) ? t.wings.join(', ') : '';
            const isAct = selectedRoom === t.room;
            return `<button class="chip chip-tunnel ${isAct ? 'active' : ''}" type="button" aria-pressed="${isAct}" data-tunnel-room="${escapeHtml(t.room)}" title="Bridges wings: ${escapeHtml(wList)}">⇄ ${escapeHtml(t.room)} <span class="chip-count">${ (t.wings || []).length }w</span></button>`;
          })
          .join('');

      tunnelsStrip.querySelectorAll('[data-tunnel-room]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const room = btn.dataset.tunnelRoom;
          populateRoomSelect('palace-room-select', document.getElementById('palace-wing-select').value);
          document.getElementById('palace-room-select').value = room;
          state.palaceOffset = 0;
          renderPalaceSubstrips();
          loadPalaceDrawers();
        });
      });
    } else {
      tunnelsStrip.innerHTML = `<span class="tunnels-label">No cross-wing tunnels in this scope</span>`;
    }

    // Apply progressive-disclosure visibility
    roomsStrip.classList.toggle('hidden', !state.roomsExpanded);
    if (roomsToggleBtn) roomsToggleBtn.setAttribute('aria-expanded', state.roomsExpanded ? 'true' : 'false');

    tunnelsStrip.classList.toggle('hidden', !state.tunnelsExpanded);
    if (tunnelsToggleBtn) tunnelsToggleBtn.setAttribute('aria-expanded', state.tunnelsExpanded ? 'true' : 'false');
  }

  async function loadPalaceDrawers() {
    const wing = document.getElementById('palace-wing-select').value;
    const room = document.getElementById('palace-room-select').value;
    const sort = document.getElementById('palace-sort-select').value || 'recent';
    const summaryEl = document.getElementById('palace-drawers-summary');
    const listEl = document.getElementById('palace-drawers-list');

    summaryEl.textContent = 'Loading drawers…';

    const res = await apiPost('/api/drawers', {
      wing,
      room,
      sort,
      limit: state.palaceLimit,
      offset: state.palaceOffset,
    });

    if (!res || !res.ok) {
      summaryEl.textContent = `Error: ${(res && res.error) || 'failed'}`;
      return;
    }

    const data = res.data || {};
    const drawers = Array.isArray(data.drawers) ? data.drawers : [];
    state.palaceTotal = data.total != null ? data.total : drawers.length;
    const elapsedMs = res.elapsed_ms != null ? res.elapsed_ms : res._clientMs;

    const startIdx = drawers.length > 0 ? state.palaceOffset + 1 : 0;
    const endIdx = state.palaceOffset + drawers.length;
    summaryEl.innerHTML = `Showing <strong>${formatNumber(startIdx)}–${formatNumber(endIdx)}</strong> of <strong>${formatNumber(state.palaceTotal)}</strong> <span class="badge badge-subtle">${elapsedMs}ms</span>`;

    const currentPage = Math.floor(state.palaceOffset / state.palaceLimit) + 1;
    const totalPages = Math.max(1, Math.ceil(state.palaceTotal / state.palaceLimit));
    const pageInput = document.getElementById('palace-page-input');
    const pageTotal = document.getElementById('palace-page-total');
    if (pageInput) {
      pageInput.max = String(totalPages);
      pageInput.value = String(currentPage);
    }
    if (pageTotal) {
      pageTotal.textContent = `of ${formatNumber(totalPages)}`;
    }
    document.getElementById('palace-prev-btn').disabled = state.palaceOffset <= 0;
    document.getElementById('palace-next-btn').disabled = !data.has_more && endIdx >= state.palaceTotal;

    if (drawers.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No drawers in this locus</p>
          <p class="empty-sub">Select another Wing or Room above.</p>
        </div>`;
      return;
    }

    renderDrawerCards(listEl, drawers, false, '');
  }

  // ── TAB 3: KNOWLEDGE GRAPH ─────────────────────────────────────────

  async function loadKnowledgeGraph() {
    const entity = (document.getElementById('kg-entity-input').value || '').trim();
    const direction = document.getElementById('kg-direction').value;
    const asOf = (document.getElementById('kg-as-of').value || '').trim();
    const metaBar = document.getElementById('kg-meta-bar');
    const listEl = document.getElementById('kg-triples-list');

    metaBar.textContent = 'Loading Knowledge Graph…';

    const res = await apiPost('/api/kg', {
      entity,
      direction,
      as_of: asOf,
      limit: 80,
    });

    if (!res || !res.ok) {
      metaBar.textContent = `Error: ${(res && res.error) || 'failed'}`;
      return;
    }

    const data = res.data || {};
    const stats = data.stats || {};
    const timelineObj = data.timeline || {};
    const queryResult = data.query_result || null;
    const entitiesSample = Array.isArray(data.entities_sample) ? data.entities_sample : [];

    // Render entity quick chips with aria-pressed
    const chipsEl = document.getElementById('kg-entity-chips');
    if (entitiesSample.length > 0) {
      chipsEl.innerHTML = [
        `<button class="chip ${!entity ? 'active' : ''}" type="button" aria-pressed="${!entity}" data-kg-entity="">All Entities</button>`,
        ...entitiesSample.slice(0, 14).map((ent) => {
          const isAct = entity === ent;
          return `<button class="chip ${isAct ? 'active' : ''}" type="button" aria-pressed="${isAct}" data-kg-entity="${escapeHtml(ent)}">${escapeHtml(ent)}</button>`;
        }),
      ].join('');
      chipsEl.querySelectorAll('[data-kg-entity]').forEach((chip) => {
        chip.addEventListener('click', () => {
          document.getElementById('kg-entity-input').value = chip.dataset.kgEntity || '';
          loadKnowledgeGraph();
        });
      });
    } else {
      chipsEl.innerHTML = '';
    }

    const triples = queryResult && Array.isArray(queryResult.facts)
      ? queryResult.facts
      : Array.isArray(timelineObj.timeline)
      ? timelineObj.timeline
      : [];

    metaBar.innerHTML = `
      <span><strong>${formatNumber(triples.length)}</strong> facts shown${entity ? ` for <strong>${escapeHtml(entity)}</strong>` : ''}</span>
      <span>Total: ${formatNumber(stats.current_facts || stats.total_triples || 0)} active · ${formatNumber(stats.entities || 0)} entities</span>
    `;

    renderKgSvg(triples, entity);
    renderKgTriplesList(listEl, triples);
  }

  function renderKgSvg(triples, focusEntity) {
    const svg = document.getElementById('kg-svg');
    const titleEl = document.getElementById('kg-graph-title');
    if (!svg) return;

    if (!triples || triples.length === 0) {
      titleEl.textContent = 'Entity Neighborhood (empty)';
      svg.innerHTML = `<text x="280" y="110" text-anchor="middle" fill="var(--muted-foreground)" font-size="12">No temporal triples found for this filter.</text>`;
      return;
    }

    const degree = {};
    triples.forEach((t) => {
      const s = String(t.subject || '').trim();
      const o = String(t.object || '').trim();
      if (s) degree[s] = (degree[s] || 0) + 1;
      if (o) degree[o] = (degree[o] || 0) + 1;
    });

    const center =
      focusEntity && degree[focusEntity]
        ? focusEntity
        : Object.entries(degree).sort((a, b) => b[1] - a[1])[0]?.[0] || '';

    titleEl.textContent = center ? `Neighborhood: ${center} (🟢 active · 🔴 ended)` : 'Entity Neighborhood';

    const neighbors = [];
    const edges = [];
    for (const t of triples) {
      const s = String(t.subject || '').trim();
      const o = String(t.object || '').trim();
      const p = String(t.predicate || '').trim();
      if (s === center && o && o !== center) {
        if (!neighbors.includes(o) && neighbors.length < 10) neighbors.push(o);
        edges.push({ from: center, to: o, predicate: p, current: t.current !== false });
      } else if (o === center && s && s !== center) {
        if (!neighbors.includes(s) && neighbors.length < 10) neighbors.push(s);
        edges.push({ from: s, to: center, predicate: p, current: t.current !== false });
      }
    }

    const cx = 280;
    const cy = 110;
    const rx = 195;
    const ry = 76;

    const coords = { [center]: { x: cx, y: cy } };
    neighbors.forEach((n, idx) => {
      const angle = (2 * Math.PI * idx) / Math.max(1, neighbors.length) - Math.PI / 2;
      coords[n] = {
        x: Math.round(cx + rx * Math.cos(angle)),
        y: Math.round(cy + ry * Math.sin(angle)),
      };
    });

    let linesHtml = '';
    edges.slice(0, 14).forEach((e) => {
      const a = coords[e.from];
      const b = coords[e.to];
      if (!a || !b) return;
      const mx = Math.round((a.x + b.x) / 2);
      const my = Math.round((a.y + b.y) / 2);
      const stroke = e.current ? 'var(--emerald-fg)' : 'var(--rose-fg)';
      const dash = e.current ? '' : 'stroke-dasharray="4 3"';
      linesHtml += `
        <line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${stroke}" stroke-opacity="0.55" stroke-width="1.5" ${dash} />
        <text x="${mx}" y="${my - 4}" text-anchor="middle" fill="var(--muted-foreground)" font-size="11">${escapeHtml(e.predicate.slice(0, 18))}</text>
      `;
    });

    let nodesHtml = '';
    Object.entries(coords).forEach(([name, pt]) => {
      const isCenter = name === center;
      const r = isCenter ? 16 : 11;
      const fill = isCenter ? 'var(--primary)' : 'var(--card-elevated)';
      const stroke = isCenter ? 'var(--gold-fg)' : 'var(--primary)';
      const label = name.length > 20 ? name.slice(0, 19) + '…' : name;
      nodesHtml += `
        <g class="kg-node" tabindex="0" role="button" aria-label="Pivot Knowledge Graph on ${escapeHtml(name)}" data-svg-entity="${escapeHtml(name)}">
          <circle cx="${pt.x}" cy="${pt.y}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="1.8" />
          <text x="${pt.x}" y="${pt.y + r + 13}" text-anchor="middle" fill="var(--foreground)" font-size="11" font-weight="${isCenter ? '600' : '500'}">${escapeHtml(label)}</text>
        </g>
      `;
    });

    svg.innerHTML = linesHtml + nodesHtml;
    svg.querySelectorAll('[data-svg-entity]').forEach((node) => {
      const triggerPivot = () => {
        const picked = node.dataset.svgEntity || '';
        document.getElementById('kg-entity-input').value = picked;
        loadKnowledgeGraph();
      };
      node.addEventListener('click', triggerPivot);
      node.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          triggerPivot();
        }
      });
    });
  }

  function renderKgTriplesList(container, triples) {
    if (!triples || triples.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No Knowledge Graph facts</p>
          <p class="empty-sub">Add facts with <code>palace_exec KG ADD</code> or clear the entity filter.</p>
        </div>`;
      return;
    }

    container.innerHTML = triples
      .map((t, idx) => {
        const isCurrent = t.current !== false && !t.valid_to;
        const statusBadge = isCurrent
          ? `<span class="badge badge-score">current</span>`
          : `<span class="badge badge-warn">ended ${escapeHtml(String(t.valid_to || '').slice(0, 10))}</span>`;
        const validFrom = t.valid_from ? `from ${String(t.valid_from).slice(0, 10)}` : '';
        return `
          <article class="card" data-card-idx="${idx}">
            <div class="card-header">
              <div class="kg-triple-row">
                <button class="kg-subject" type="button" data-pivot-entity="${escapeHtml(t.subject || '')}">${escapeHtml(t.subject || '?')}</button>
                <span class="kg-predicate">→ ${escapeHtml(t.predicate || 'related_to')} →</span>
                <button class="kg-object" type="button" data-pivot-entity="${escapeHtml(t.object || '')}">${escapeHtml(t.object || '?')}</button>
              </div>
              <div class="card-meta-right">
                ${statusBadge}
              </div>
            </div>
            <div class="card-footer">
              <span class="card-source">${escapeHtml(validFrom)} ${t.confidence != null ? `· conf ${t.confidence}` : ''}</span>
              <div class="card-actions">
                <button class="btn btn-secondary btn-xs" type="button" data-copy-kg="${idx}">Copy</button>
                <button class="btn btn-cite btn-xs" type="button" data-cite-kg="${idx}">Cite in Chat</button>
              </div>
            </div>
          </article>`;
      })
      .join('');

    container.querySelectorAll('[data-pivot-entity]').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.getElementById('kg-entity-input').value = btn.dataset.pivotEntity || '';
        loadKnowledgeGraph();
      });
    });

    container.querySelectorAll('[data-copy-kg]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const t = triples[Number(btn.dataset.copyKg)];
        if (!t) return;
        const line = `${t.subject} -> ${t.predicate} -> ${t.object} (valid_from=${t.valid_from || 'n/a'}, valid_to=${t.valid_to || 'current'})`;
        copyToClipboard(line);
      });
    });

    container.querySelectorAll('[data-cite-kg]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const t = triples[Number(btn.dataset.citeKg)];
        if (!t) return;
        const line = `${t.subject} —[${t.predicate}]→ ${t.object}\nvalid_from: ${t.valid_from || 'n/a'}\nvalid_to: ${t.valid_to || 'current'}`;
        citeInChat(`KG Fact: ${t.subject} ${t.predicate}`, line, 'KG');
      });
    });
  }

  // ── TAB 4: LOGSTREAM & DIARIES ─────────────────────────────────────

  async function loadLogstreamEvents() {
    const room = document.getElementById('events-room').value;
    const type = document.getElementById('events-type').value;
    const status = document.getElementById('events-status').value;
    const toAgent = (document.getElementById('events-to-agent').value || '').trim();
    const correlationId = (document.getElementById('events-correlation').value || '').trim();

    const metaBar = document.getElementById('events-meta-bar');
    const listEl = document.getElementById('events-list');

    metaBar.textContent = 'Loading coordination logstream…';

    const res = await apiPost('/api/events', {
      room,
      type,
      status,
      to_agent: toAgent,
      correlation_id: correlationId,
      limit: 50,
      preview: false,
    });

    if (!res || !res.ok) {
      metaBar.textContent = `Error: ${(res && res.error) || 'failed'}`;
      return;
    }

    const data = res.data || {};
    const events = Array.isArray(data.events) ? data.events : [];
    const tip = data.tip_event_id || (data.tip && data.tip.event_id) || '—';

    metaBar.innerHTML = `
      <span><strong>${events.length}</strong> events</span>
      <span title="Logstream cursor tip">tip: <code>${escapeHtml(String(tip).slice(0, 22))}</code></span>
    `;

    if (events.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No logstream events match</p>
          <p class="empty-sub">Try another preset filter above or delegate a task with <code>palace_coordinate</code>.</p>
        </div>`;
      return;
    }

    listEl.innerHTML = events
      .map((ev, idx) => {
        const evType = ev.type || 'event';
        const evRoom = ev.room || 'status';
        const evStatus = ev.status || '';
        const writer = ev.writer || ev.from_agent || 'unknown';
        const to = ev.to_agent || '*';
        const ts = String(ev.created_at || '').slice(0, 19).replace('T', ' ');
        const bodyText =
          typeof ev.body === 'string'
            ? ev.body
            : ev.body_preview || JSON.stringify(ev.body || {}, null, 2);
        const corr = ev.correlation_id || '';
        const patchSha =
          (ev.metadata && ev.metadata.patch_sha256) ||
          (ev.body && typeof ev.body === 'object' && ev.body.patch_sha256) ||
          '';

        const statusBadge = evStatus
          ? `<span class="badge ${evStatus === 'open' || evStatus === 'claimed' ? 'badge-wing' : evStatus === 'applied' ? 'badge-score' : 'badge-warn'}">${escapeHtml(evStatus)}</span>`
          : '';

        return `
          <article class="card" data-card-idx="${idx}">
            <div class="card-header">
              <div class="card-badges">
                <span class="badge badge-room">${escapeHtml(evType)}</span>
                <span class="badge badge-subtle">${escapeHtml(evRoom)}</span>
                ${statusBadge}
                ${corr ? `<button class="badge badge-subtle" type="button" data-filter-corr="${escapeHtml(corr)}" title="Filter by correlation_id">${escapeHtml(corr.slice(0, 16))}</button>` : ''}
              </div>
              <div class="card-meta-right">
                <span>${escapeHtml(ts)}</span>
              </div>
            </div>
            <div class="card-content" data-inspect-event="${idx}" title="Click to inspect full event">
              <div class="card-content-text clamped">${escapeHtml(bodyText)}</div>
            </div>
            <div class="card-footer">
              <span class="card-source" title="${escapeHtml(writer)} → ${escapeHtml(to)}">${escapeHtml(writer)} → ${escapeHtml(to)}</span>
              <div class="card-actions">
                ${patchSha ? `<button class="btn btn-secondary btn-xs" type="button" data-inspect-artifact="${escapeHtml(patchSha)}">View Patch</button>` : ''}
                <button class="btn btn-secondary btn-xs" type="button" data-inspect-event="${idx}">Inspect</button>
                <button class="btn btn-cite btn-xs" type="button" data-cite-event="${idx}">Cite in Chat</button>
              </div>
            </div>
          </article>`;
      })
      .join('');

    listEl.querySelectorAll('[data-filter-corr]').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.getElementById('events-correlation').value = btn.dataset.filterCorr || '';
        loadLogstreamEvents();
      });
    });

    listEl.querySelectorAll('[data-inspect-event]').forEach((el) => {
      el.addEventListener('click', () => {
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;
        const ev = events[Number(el.dataset.inspectEvent)];
        if (!ev) return;
        const bodyStr = JSON.stringify(ev, null, 2);
        openInspector({
          title: `Event ${ev.event_id || ev.type}`,
          badgesHtml: `<span class="badge badge-room">${escapeHtml(ev.type || '')}</span><span class="badge badge-subtle">${escapeHtml(ev.writer || '')}</span>`,
          body: bodyStr,
          citeTitle: `Logstream Event ${ev.event_id || ''}`,
          citeLocus: `logstream/${ev.room || 'status'}`,
        });
      });
    });

    listEl.querySelectorAll('[data-inspect-artifact]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const sha = btn.dataset.inspectArtifact;
        const res = await apiPost('/api/artifact', { sha256: sha });
        if (!res || !res.ok) {
          showToast('Unable to load artifact');
          return;
        }
        const art = res.data || {};
        openInspector({
          title: `Patch Artifact ${sha.slice(0, 12)}…`,
          badgesHtml: `<span class="badge badge-score">sha256:${escapeHtml(sha.slice(0, 16))}</span>`,
          body: art.content || JSON.stringify(art, null, 2),
          citeTitle: `Patch ${sha.slice(0, 12)}`,
          citeLocus: 'artifact',
        });
      });
    });

    listEl.querySelectorAll('[data-cite-event]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const ev = events[Number(btn.dataset.citeEvent)];
        if (!ev) return;
        const bodyStr = typeof ev.body === 'string' ? ev.body : JSON.stringify(ev, null, 2);
        citeInChat(
          `Event ${ev.event_id || ev.type} (${ev.writer || ''} -> ${ev.to_agent || '*'})`,
          bodyStr,
          `logstream/${ev.room || 'status'}`
        );
      });
    });
  }

  async function loadAgentDiary() {
    const agentName = (document.getElementById('diary-agent').value || '').trim();
    const wing = document.getElementById('diary-wing').value;
    const metaBar = document.getElementById('diary-meta-bar');
    const listEl = document.getElementById('diary-list');

    metaBar.textContent = 'Loading diary entries…';

    const res = await apiPost('/api/diary', {
      agent_name: agentName,
      wing,
      last_n: 25,
    });

    if (!res || !res.ok) {
      metaBar.textContent = `Error: ${(res && res.error) || 'failed'}`;
      return;
    }

    const data = res.data || {};
    const inner = data.result || {};
    const entries = Array.isArray(inner.entries) ? inner.entries : [];

    metaBar.innerHTML = `
      <span><strong>${entries.length}</strong> diary entries${agentName ? ` for <strong>${escapeHtml(agentName)}</strong>` : ''}</span>
      <span>Total: ${formatNumber(inner.total != null ? inner.total : entries.length)}</span>
    `;

    if (entries.length === 0) {
      listEl.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No diary entries found</p>
          <p class="empty-sub">Agents record journal entries via <code>palace_exec DIARY WRITE</code>.</p>
        </div>`;
      return;
    }

    listEl.innerHTML = entries
      .map((entry, idx) => {
        const topic = entry.topic || 'diary';
        const dateStr = entry.date || String(entry.timestamp || '').slice(0, 10);
        const wingTag = entry.wing || wing || 'diary';
        const content = entry.content || '';
        return `
          <article class="card" data-card-idx="${idx}">
            <div class="card-header">
              <div class="card-badges">
                <span class="badge badge-wing">${escapeHtml(wingTag)}</span>
                <span class="badge badge-room">${escapeHtml(topic)}</span>
              </div>
              <div class="card-meta-right">
                <span>${escapeHtml(dateStr)}</span>
              </div>
            </div>
            <div class="card-content" data-inspect-diary="${idx}" title="Click to inspect full diary entry">
              <div class="card-content-text clamped">${escapeHtml(content)}</div>
            </div>
            <div class="card-footer">
              <span class="card-source">${escapeHtml(entry.drawer_id || data.agent || '')}</span>
              <div class="card-actions">
                <button class="btn btn-secondary btn-xs" type="button" data-inspect-diary="${idx}">Inspect</button>
                <button class="btn btn-secondary btn-xs" type="button" data-copy-diary="${idx}">Copy</button>
                <button class="btn btn-cite btn-xs" type="button" data-cite-diary="${idx}">Cite in Chat</button>
              </div>
            </div>
          </article>`;
      })
      .join('');

    listEl.querySelectorAll('[data-inspect-diary]').forEach((el) => {
      el.addEventListener('click', () => {
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;
        const entry = entries[Number(el.dataset.inspectDiary)];
        if (!entry) return;
        openInspector({
          title: `Diary · ${entry.topic || 'entry'} (${entry.date || ''})`,
          badgesHtml: `<span class="badge badge-wing">${escapeHtml(entry.wing || 'diary')}</span><span class="badge badge-room">${escapeHtml(entry.topic || 'diary')}</span>`,
          body: entry.content || '',
          citeTitle: `Diary (${entry.date || ''})`,
          citeLocus: `${entry.wing || 'diary'}/diary`,
        });
      });
    });

    listEl.querySelectorAll('[data-copy-diary]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const entry = entries[Number(btn.dataset.copyDiary)];
        if (entry) copyToClipboard(entry.content || '');
      });
    });

    listEl.querySelectorAll('[data-cite-diary]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const entry = entries[Number(btn.dataset.citeDiary)];
        if (entry) {
          citeInChat(`Diary ${entry.date || ''}`, entry.content || '', `${entry.wing || 'diary'}/diary`);
        }
      });
    });
  }

  // ── Event Listeners & Initialization ───────────────────────────────

  function bindQuickSearchChips() {
    document.querySelectorAll('[data-quick-search]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const q = chip.dataset.quickSearch || '';
        document.getElementById('search-query').value = q;
        runSearch();
      });
    });
  }

  function applyEventPreset(preset) {
    document.querySelectorAll('[data-event-preset]').forEach((btn) => {
      const isAct = btn.dataset.eventPreset === preset;
      btn.classList.toggle('active', isAct);
      btn.setAttribute('aria-pressed', isAct ? 'true' : 'false');
    });
    const roomEl = document.getElementById('events-room');
    const typeEl = document.getElementById('events-type');
    const statusEl = document.getElementById('events-status');
    const toAgentEl = document.getElementById('events-to-agent');
    const corrEl = document.getElementById('events-correlation');

    roomEl.value = '';
    typeEl.value = '';
    statusEl.value = '';
    toAgentEl.value = '';
    corrEl.value = '';

    if (preset === 'open_tasks') {
      typeEl.value = 'task.request';
      statusEl.value = 'open';
    } else if (preset === 'claimed') {
      statusEl.value = 'claimed';
    } else if (preset === 'patches') {
      typeEl.value = 'patch.ready';
    } else if (preset === 'inbox') {
      const ws = state.workspaceWing || 'mempalace';
      toAgentEl.value = `windows:antigravity:${ws}`;
    }
    loadLogstreamEvents();
  }

  function getActiveCardListContainer() {
    if (state.activeTab === 'recall') return document.getElementById('search-results');
    if (state.activeTab === 'palace') return document.getElementById('palace-drawers-list');
    if (state.activeTab === 'kg') return document.getElementById('kg-triples-list');
    if (state.activeTab === 'logstream') {
      return state.activeSubtab === 'events'
        ? document.getElementById('events-list')
        : document.getElementById('diary-list');
    }
    return null;
  }

  function navigateCardsByKeyboard(delta) {
    const container = getActiveCardListContainer();
    if (!container) return;
    const cards = Array.from(container.querySelectorAll('.card'));
    if (!cards.length) return;
    state.kbCardIndex = Math.max(0, Math.min(cards.length - 1, state.kbCardIndex + delta));
    cards.forEach((c, idx) => c.classList.toggle('kb-active', idx === state.kbCardIndex));
    const activeCard = cards[state.kbCardIndex];
    if (activeCard) {
      activeCard.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  function initEventListeners() {
    // Primary Tabs + Keyboard Arrow Navigation
    const tabButtons = Array.from(document.querySelectorAll('.tab-btn'));
    tabButtons.forEach((btn, idx) => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
      btn.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          const nextIdx =
            e.key === 'ArrowRight'
              ? (idx + 1) % tabButtons.length
              : (idx - 1 + tabButtons.length) % tabButtons.length;
          tabButtons[nextIdx].focus();
          switchTab(tabButtons[nextIdx].dataset.tab);
        }
      });
    });

    // Workspace Wing Pill in Header
    document.getElementById('workspace-wing-pill').addEventListener('click', () => {
      if (!state.workspaceWing) return;
      if (state.activeTab === 'palace') {
        const sel = document.getElementById('palace-wing-select');
        const nextVal = sel.value === state.workspaceWing ? '' : state.workspaceWing;
        sel.value = nextVal;
        populateRoomSelect('palace-room-select', nextVal);
        renderWingQuickBar('palace-wings-bar', nextVal);
        renderPalaceSubstrips();
        syncWorkspacePillActive();
        state.palaceOffset = 0;
        loadPalaceDrawers();
      } else {
        switchTab('recall');
        const sel = document.getElementById('search-wing');
        const nextVal = sel.value === state.workspaceWing ? '' : state.workspaceWing;
        sel.value = nextVal;
        populateRoomSelect('search-room', nextVal);
        renderWingQuickBar('search-wings-bar', nextVal);
        syncWorkspacePillActive();
        if (document.getElementById('search-query').value.trim()) {
          runSearch();
        }
      }
    });

    // Refresh & Setup Retry
    document.getElementById('btn-refresh').addEventListener('click', async () => {
      showToast('Refreshing palace overview…');
      await loadOverview(true);
      if (state.activeTab === 'palace') loadPalaceDrawers();
      if (state.activeTab === 'kg') loadKnowledgeGraph();
      if (state.activeTab === 'logstream') loadLogstreamEvents();
    });

    document.getElementById('btn-retry-bridge').addEventListener('click', () => loadOverview(true));
    document.getElementById('btn-ask-agent-setup').addEventListener('click', () => {
      const errText = document.getElementById('setup-banner-msg').textContent || '';
      citeInChat(
        'Please help configure or repair my MemPalace environment for the Antigravity sidecar',
        errText,
        'setup'
      );
    });

    // Search form (Enter -> full selected strategy; typing -> 150ms debounced instant FTS5 lexical)
    document.getElementById('search-form').addEventListener('submit', (e) => {
      e.preventDefault();
      clearTimeout(state.searchDebounceTimer);
      runSearch();
    });

    document.getElementById('search-query').addEventListener('input', () => {
      clearTimeout(state.searchDebounceTimer);
      const q = (document.getElementById('search-query').value || '').trim();
      if (!q) {
        runSearch();
        return;
      }
      state.searchDebounceTimer = setTimeout(() => {
        if (q.length >= 2) {
          runSearch('lexical');
        }
      }, 150);
    });

    document.getElementById('search-wing').addEventListener('change', (e) => {
      const w = e.target.value;
      populateRoomSelect('search-room', w);
      renderWingQuickBar('search-wings-bar', w);
      syncWorkspacePillActive();
      if (document.getElementById('search-query').value.trim()) runSearch();
    });

    document.getElementById('search-room').addEventListener('change', () => {
      if (document.getElementById('search-query').value.trim()) runSearch();
    });

    document.getElementById('search-strategy').addEventListener('change', () => {
      if (document.getElementById('search-query').value.trim()) runSearch();
    });

    bindQuickSearchChips();

    // Palace Controls & Progressive Disclosure Toggles
    document.getElementById('palace-wing-select').addEventListener('change', (e) => {
      const w = e.target.value;
      populateRoomSelect('palace-room-select', w);
      state.palaceOffset = 0;
      renderWingQuickBar('palace-wings-bar', w);
      renderPalaceSubstrips();
      syncWorkspacePillActive();
      loadPalaceDrawers();
    });

    document.getElementById('palace-room-select').addEventListener('change', () => {
      state.palaceOffset = 0;
      renderPalaceSubstrips();
      loadPalaceDrawers();
    });

    document.getElementById('palace-sort-select').addEventListener('change', () => {
      state.palaceOffset = 0;
      loadPalaceDrawers();
    });

    document.getElementById('palace-reset-filters-btn').addEventListener('click', () => {
      document.getElementById('palace-wing-select').value = '';
      populateRoomSelect('palace-room-select', '');
      document.getElementById('palace-room-select').value = '';
      state.palaceOffset = 0;
      renderWingQuickBar('palace-wings-bar', '');
      renderPalaceSubstrips();
      syncWorkspacePillActive();
      loadPalaceDrawers();
    });

    document.getElementById('btn-toggle-rooms').addEventListener('click', () => {
      state.roomsExpanded = !state.roomsExpanded;
      renderPalaceSubstrips();
    });

    document.getElementById('btn-toggle-tunnels').addEventListener('click', () => {
      state.tunnelsExpanded = !state.tunnelsExpanded;
      renderPalaceSubstrips();
    });

    document.getElementById('palace-prev-btn').addEventListener('click', () => {
      state.palaceOffset = Math.max(0, state.palaceOffset - state.palaceLimit);
      loadPalaceDrawers();
    });

    document.getElementById('palace-next-btn').addEventListener('click', () => {
      state.palaceOffset += state.palaceLimit;
      loadPalaceDrawers();
    });

    document.getElementById('palace-page-input').addEventListener('change', (e) => {
      const totalPages = Math.max(1, Math.ceil(state.palaceTotal / state.palaceLimit));
      const targetPage = Math.max(1, Math.min(totalPages, Number(e.target.value) || 1));
      e.target.value = String(targetPage);
      state.palaceOffset = (targetPage - 1) * state.palaceLimit;
      loadPalaceDrawers();
    });

    // Knowledge Graph Controls
    document.getElementById('kg-form').addEventListener('submit', (e) => {
      e.preventDefault();
      loadKnowledgeGraph();
    });
    document.getElementById('kg-direction').addEventListener('change', loadKnowledgeGraph);
    document.getElementById('kg-as-of').addEventListener('change', loadKnowledgeGraph);
    document.getElementById('kg-clear-btn').addEventListener('click', () => {
      document.getElementById('kg-entity-input').value = '';
      document.getElementById('kg-as-of').value = '';
      document.getElementById('kg-direction').value = 'both';
      loadKnowledgeGraph();
    });

    // Logstream & Diaries Controls
    document.querySelectorAll('.subtab-btn').forEach((btn) => {
      btn.addEventListener('click', () => switchSubtab(btn.dataset.subtab));
    });

    document.querySelectorAll('[data-event-preset]').forEach((btn) => {
      btn.addEventListener('click', () => applyEventPreset(btn.dataset.eventPreset));
    });

    document.getElementById('events-filter-btn').addEventListener('click', loadLogstreamEvents);
    document.getElementById('events-room').addEventListener('change', loadLogstreamEvents);
    document.getElementById('events-type').addEventListener('change', loadLogstreamEvents);
    document.getElementById('events-status').addEventListener('change', loadLogstreamEvents);
    document.getElementById('events-clear-btn').addEventListener('click', () => applyEventPreset('all'));

    document.getElementById('events-live-checkbox').addEventListener('change', (e) => {
      clearInterval(state.livePollTimer);
      if (e.target.checked) {
        state.livePollTimer = setInterval(() => {
          if (state.activeTab === 'logstream' && state.activeSubtab === 'events') {
            loadLogstreamEvents();
          }
        }, 5000);
        showToast('Live logstream polling enabled (5s)');
      } else {
        showToast('Live logstream polling paused');
      }
    });

    document.getElementById('diary-form').addEventListener('submit', (e) => {
      e.preventDefault();
      loadAgentDiary();
    });

    // Inspector Modal Actions + Focus Trap + Keyboard Shortcuts
    document.querySelectorAll('[data-close-inspector]').forEach((el) => {
      el.addEventListener('click', closeInspector);
    });
    document.getElementById('inspector-copy-btn').addEventListener('click', () => {
      if (state.inspectorPayload) copyToClipboard(state.inspectorPayload.body);
    });
    document.getElementById('inspector-cite-btn').addEventListener('click', () => {
      if (state.inspectorPayload) {
        citeInChat(
          state.inspectorPayload.citeTitle,
          state.inspectorPayload.body,
          state.inspectorPayload.citeLocus
        );
      }
    });

    window.addEventListener('keydown', (e) => {
      const modal = document.getElementById('inspector-modal');
      const modalOpen = modal && !modal.classList.contains('hidden');

      if (e.key === 'Escape' && modalOpen) {
        e.preventDefault();
        closeInspector();
        return;
      }

      // Focus trap inside Inspector Modal when open
      if (modalOpen && e.key === 'Tab') {
        const focusables = Array.from(
          modal.querySelectorAll('button:not([disabled]), [tabindex]:not([tabindex="-1"])')
        );
        if (focusables.length > 0) {
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
        return;
      }

      const tag = document.activeElement && document.activeElement.tagName;
      const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
      if (inInput || modalOpen) return;

      if (e.key === '/') {
        e.preventDefault();
        switchTab('recall');
        document.getElementById('search-query').focus();
      } else if (e.key === 'j') {
        e.preventDefault();
        navigateCardsByKeyboard(1);
      } else if (e.key === 'k') {
        e.preventDefault();
        navigateCardsByKeyboard(-1);
      } else if (e.key === 'Enter' && state.kbCardIndex >= 0) {
        const container = getActiveCardListContainer();
        const activeCard = container && container.querySelectorAll('.card')[state.kbCardIndex];
        const inspectBtn = activeCard && activeCard.querySelector('[data-inspect-drawer], [data-inspect-event], [data-inspect-diary]');
        if (inspectBtn) {
          e.preventDefault();
          inspectBtn.click();
        }
      } else if (e.key === 'c' && !e.ctrlKey && !e.metaKey && state.kbCardIndex >= 0) {
        const container = getActiveCardListContainer();
        const activeCard = container && container.querySelectorAll('.card')[state.kbCardIndex];
        const copyBtn = activeCard && activeCard.querySelector('[data-copy-idx], [data-copy-kg], [data-copy-diary]');
        if (copyBtn) {
          e.preventDefault();
          copyBtn.click();
        }
      }
    });

    // Observe host theme CSS variable changes on <html>
    const observer = new MutationObserver(() => syncThemeMode());
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['style', 'class'],
    });
  }

  // Boot
  document.addEventListener('DOMContentLoaded', () => {
    syncThemeMode();
    initEventListeners();
    loadOverview(false);
  });
})();
