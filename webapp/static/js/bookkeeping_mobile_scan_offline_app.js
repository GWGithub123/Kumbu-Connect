(function () {
  const bootstrapNode = document.getElementById('bookkeepingMobileScanBootstrap');
  if (!bootstrapNode) {
    return;
  }

  let embeddedBootstrap = null;
  try {
    embeddedBootstrap = JSON.parse(bootstrapNode.textContent || '{}');
  } catch (error) {
    console.error('Unable to parse bookkeeping mobile scan bootstrap payload.', error);
    return;
  }

  const DB_NAME = 'kumbu-bookkeeping-mobile-scan';
  const DB_VERSION = 1;
  const QUEUE_STORE = 'queue';
  const LAST_SYNC_PREFIX = 'kumbu-bookkeeping-mobile-scan-last-sync:';
  const RETRY_BASE_DELAY_MS = 15000;
  const RETRY_MAX_DELAY_MS = 120000;

  const state = {
    bootstrap: embeddedBootstrap,
    db: null,
    queue: [],
    pendingFiles: [],
    isSyncing: false,
    retryTimer: null,
    registration: null,
    activeGroupId: '',
    previewUrls: new Map(),
  };

  const elements = {
    form: document.getElementById('mobileScanForm'),
    connectivityBadge: document.getElementById('mobileScanConnectivityBadge'),
    offlineReadyBadge: document.getElementById('mobileScanOfflineReadyBadge'),
    queueBadge: document.getElementById('mobileScanQueueBadge'),
    lastSyncNote: document.getElementById('mobileScanLastSyncNote'),
    metricDocuments: document.getElementById('mobileScanMetricDocuments'),
    metricEntries: document.getElementById('mobileScanMetricEntries'),
    metricNet: document.getElementById('mobileScanMetricNet'),
    nativeCameraPicker: document.getElementById('nativeCameraPicker'),
    filePicker: document.getElementById('filePicker'),
    submitBtn: document.getElementById('mobileScanSubmitBtn'),
    pendingNote: document.getElementById('mobileScanPendingNote'),
    documentDateInput: document.getElementById('mobileScanDocumentDate'),
    importIntoWorkspace: document.getElementById('mobileScanImportIntoWorkspace'),
    combineRelatedPages: document.getElementById('combineRelatedPages'),
    queueWrap: document.getElementById('mobileScanQueueWrap'),
    queueCount: document.getElementById('mobileScanQueueCount'),
    queue: document.getElementById('mobileScanQueue'),
    status: document.getElementById('mobileScanStatus'),
  };

  function currentQueueKey() {
    return String(state.bootstrap?.cbo?.id || 'default');
  }

  function lastSyncStorageKey() {
    return LAST_SYNC_PREFIX + currentQueueKey();
  }

  function requestToPromise(request) {
    return new Promise((resolve, reject) => {
      request.addEventListener('success', () => resolve(request.result));
      request.addEventListener('error', () => reject(request.error || new Error('IndexedDB request failed.')));
    });
  }

  function setBadge(element, text, badgeState) {
    if (!element) {
      return;
    }
    element.textContent = text;
    element.dataset.state = badgeState;
  }

  function setStatusMessage(message, tone) {
    if (!elements.status) {
      return;
    }
    elements.status.textContent = message;
    elements.status.dataset.tone = tone || 'warning';
  }

  function formatDateTime(value) {
    if (!value) {
      return 'just now';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return 'just now';
    }
    return parsed.toLocaleString([], {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }

  function formatFileSize(bytes) {
    const size = Number(bytes || 0);
    if (size <= 0) {
      return '0 B';
    }
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = size;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function formatCurrency(value) {
    const number = Number(value || 0);
    return `UGX ${new Intl.NumberFormat('en-US', {
      maximumFractionDigits: 0,
    }).format(number)}`;
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeMimeType(value) {
    const mimeType = String(value || '').trim().toLowerCase();
    if (mimeType === 'image/jpg') {
      return 'image/jpeg';
    }
    return mimeType;
  }

  function guessMimeTypeFromName(fileName) {
    const normalized = String(fileName || '').trim().toLowerCase();
    if (normalized.endsWith('.pdf')) {
      return 'application/pdf';
    }
    if (normalized.endsWith('.png')) {
      return 'image/png';
    }
    if (normalized.endsWith('.webp')) {
      return 'image/webp';
    }
    if (normalized.endsWith('.heic')) {
      return 'image/heic';
    }
    if (normalized.endsWith('.heif')) {
      return 'image/heif';
    }
    if (normalized.endsWith('.jpg') || normalized.endsWith('.jpeg')) {
      return 'image/jpeg';
    }
    return '';
  }

  function extensionForMimeType(mimeType) {
    return {
      'application/pdf': '.pdf',
      'image/jpeg': '.jpg',
      'image/png': '.png',
      'image/webp': '.webp',
      'image/heic': '.heic',
      'image/heif': '.heif',
    }[String(mimeType || '').trim().toLowerCase()] || '';
  }

  function sniffMimeType(arrayBuffer) {
    const header = new Uint8Array(arrayBuffer || 0);
    if (header.length >= 4 && header[0] === 0x25 && header[1] === 0x50 && header[2] === 0x44 && header[3] === 0x46) {
      return 'application/pdf';
    }
    if (header.length >= 3 && header[0] === 0xff && header[1] === 0xd8 && header[2] === 0xff) {
      return 'image/jpeg';
    }
    if (
      header.length >= 8
      && header[0] === 0x89
      && header[1] === 0x50
      && header[2] === 0x4e
      && header[3] === 0x47
      && header[4] === 0x0d
      && header[5] === 0x0a
      && header[6] === 0x1a
      && header[7] === 0x0a
    ) {
      return 'image/png';
    }
    if (
      header.length >= 12
      && header[0] === 0x52
      && header[1] === 0x49
      && header[2] === 0x46
      && header[3] === 0x46
      && header[8] === 0x57
      && header[9] === 0x45
      && header[10] === 0x42
      && header[11] === 0x50
    ) {
      return 'image/webp';
    }
    if (
      header.length >= 12
      && header[4] === 0x66
      && header[5] === 0x74
      && header[6] === 0x79
      && header[7] === 0x70
    ) {
      const brand = String.fromCharCode(header[8], header[9], header[10], header[11]).toLowerCase();
      if (brand === 'heic' || brand === 'heix' || brand === 'hevc' || brand === 'hevx') {
        return 'image/heic';
      }
      if (brand === 'heif' || brand === 'mif1' || brand === 'msf1') {
        return 'image/heif';
      }
    }
    return '';
  }

  async function canonicalizeSelectedFile(file, fallbackBaseName) {
    const originalName = String(file && file.name || '').trim();
    const declaredMimeType = normalizeMimeType(file && file.type);
    const guessedMimeType = guessMimeTypeFromName(originalName);
    const arrayBuffer = await file.arrayBuffer();
    const sniffedMimeType = sniffMimeType(arrayBuffer);
    const mimeType = sniffedMimeType || declaredMimeType || guessedMimeType || 'application/octet-stream';
    const extension = extensionForMimeType(mimeType);
    const fileName = originalName
      ? (/\.[a-z0-9]{1,8}$/i.test(originalName) || !extension ? originalName : originalName + extension)
      : String(fallbackBaseName || 'capture') + extension;
    const blob = new Blob([arrayBuffer], {
      type: mimeType,
    });
    return {
      blob,
      fileName,
      fileType: mimeType,
      fileSize: Number(blob.size || file.size || 0),
    };
  }

  function renderPendingSelection() {
    if (elements.submitBtn) {
      elements.submitBtn.disabled = !state.db || !state.pendingFiles.length;
    }
    if (!elements.pendingNote) {
      return;
    }
    if (!state.pendingFiles.length) {
      elements.pendingNote.textContent = 'No files selected yet. Choose a photo or PDF, then tap Submit.';
      return;
    }
    const previewNames = state.pendingFiles.slice(0, 2).map((item) => item.fileName || 'upload');
    const remainder = state.pendingFiles.length - previewNames.length;
    const suffix = remainder > 0 ? ` and ${remainder} more` : '';
    elements.pendingNote.textContent = `${state.pendingFiles.length} file${state.pendingFiles.length === 1 ? '' : 's'} ready to submit: ${previewNames.join(', ')}${suffix}.`;
  }

  function renderSummary() {
    const summary = state.bootstrap?.summary || {};
    if (elements.metricDocuments) {
      elements.metricDocuments.textContent = String(Number(summary.document_count || 0));
    }
    if (elements.metricEntries) {
      elements.metricEntries.textContent = String(Number(summary.entry_count || 0));
    }
    if (elements.metricNet) {
      elements.metricNet.textContent = formatCurrency(summary.net_total || 0);
    }
  }

  function renderLastSyncNote() {
    if (!elements.lastSyncNote) {
      return;
    }
    const storedValue = window.localStorage.getItem(lastSyncStorageKey());
    if (!storedValue) {
      elements.lastSyncNote.textContent = 'No sync has completed on this device yet.';
      return;
    }
    elements.lastSyncNote.textContent = `Last synced ${formatDateTime(storedValue)}.`;
  }

  function markLastSyncNow() {
    window.localStorage.setItem(lastSyncStorageKey(), new Date().toISOString());
    renderLastSyncNote();
  }

  function renderConnectivity() {
    if (navigator.onLine) {
      setBadge(elements.connectivityBadge, 'Online', 'online');
      return;
    }
    setBadge(elements.connectivityBadge, 'Offline', 'offline');
  }

  function renderOfflineReady(text, badgeState) {
    setBadge(elements.offlineReadyBadge, text, badgeState);
  }

  function clearPreviewUrls() {
    state.previewUrls.forEach((previewUrl) => URL.revokeObjectURL(previewUrl));
    state.previewUrls.clear();
  }

  function queueStatusLabel(status) {
    if (status === 'syncing') {
      return 'Syncing';
    }
    if (status === 'failed') {
      return 'Needs attention';
    }
    return 'Queued';
  }

  function queueStatusClass(status) {
    if (status === 'syncing') {
      return 'bookkeeping-mobile-queue-item bookkeeping-mobile-queue-item--syncing';
    }
    if (status === 'failed') {
      return 'bookkeeping-mobile-queue-item bookkeeping-mobile-queue-item--failed';
    }
    return 'bookkeeping-mobile-queue-item bookkeeping-mobile-queue-item--queued';
  }

  function refreshQueueBadges() {
    const queueCount = state.queue.length;
    const label = `${queueCount} queued`;
    if (queueCount > 0) {
      setBadge(elements.queueBadge, label, 'pending');
    } else {
      setBadge(elements.queueBadge, label, 'ready');
    }
    if (elements.queueCount) {
      elements.queueCount.textContent = `${queueCount} file${queueCount === 1 ? '' : 's'} queued`;
    }
    if (elements.queueWrap) {
      elements.queueWrap.hidden = queueCount === 0;
    }
  }

  function renderQueue() {
    if (!elements.queue) {
      return;
    }

    clearPreviewUrls();
    refreshQueueBadges();

    if (!state.queue.length) {
      elements.queue.innerHTML = '<div class="bookkeeping-mobile-queue-empty">No files are queued on this device.</div>';
      return;
    }

    elements.queue.innerHTML = state.queue.map((item) => {
      let previewMarkup = '<div class="bookkeeping-mobile-queue-preview">PDF</div>';
      if (item.fileBlob && String(item.fileType || '').startsWith('image/')) {
        const previewUrl = URL.createObjectURL(item.fileBlob);
        state.previewUrls.set(item.id, previewUrl);
        previewMarkup = `<img src="${previewUrl}" alt="Queued preview for ${escapeHtml(item.fileName)}">`;
      }
      const attempts = Number(item.attempts || 0);
      const metaParts = [
        escapeHtml(item.sourceLabel || 'device capture'),
        escapeHtml(formatFileSize(item.fileSize)),
        `Saved ${escapeHtml(formatDateTime(item.createdAt))}`,
      ];
      if (attempts > 0) {
        metaParts.push(`Retries ${attempts}`);
      }
      if (item.combineRelatedPages) {
        metaParts.push('Grouped document');
      }
      if (item.documentDate) {
        metaParts.push(`Document date ${escapeHtml(item.documentDate)}`);
      }
      metaParts.push(item.includeInWorkspace ? 'Imports into live workbook' : 'Digitize only');

      return `
        <article class="${queueStatusClass(item.status)}" data-queue-id="${escapeHtml(item.id)}">
          <div class="bookkeeping-mobile-queue-preview-wrap">
            ${previewMarkup}
          </div>
          <div class="bookkeeping-mobile-queue-body">
            <div class="bookkeeping-mobile-queue-item__top">
              <div>
                <p class="bookkeeping-mobile-queue-item__kind">${escapeHtml(item.sourceLabel || 'Device capture')}</p>
                <h4>${escapeHtml(item.fileName || 'Untitled upload')}</h4>
              </div>
              <span class="bookkeeping-mobile-queue-item__status">${escapeHtml(queueStatusLabel(item.status))}</span>
            </div>
            <p class="bookkeeping-mobile-queue-item__meta">${metaParts.join(' · ')}</p>
            ${item.error ? `<div class="bookkeeping-mobile-queue-item__error">${escapeHtml(item.error)}</div>` : ''}
            <div class="bookkeeping-mobile-queue-item__actions">
              ${item.status === 'failed' ? '<button type="button" class="btn btn-outline" data-queue-action="retry">Retry</button>' : ''}
              <button type="button" class="btn btn-outline" data-queue-action="remove">Remove</button>
            </div>
          </div>
        </article>
      `;
    }).join('');
  }

  function makeId(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }

  function getQueueStore(mode) {
    return state.db.transaction(QUEUE_STORE, mode).objectStore(QUEUE_STORE);
  }

  async function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = window.indexedDB.open(DB_NAME, DB_VERSION);
      request.addEventListener('upgradeneeded', () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(QUEUE_STORE)) {
          database.createObjectStore(QUEUE_STORE, { keyPath: 'id' });
        }
      });
      request.addEventListener('success', () => resolve(request.result));
      request.addEventListener('error', () => reject(request.error || new Error('Unable to open offline queue.')));
    });
  }

  async function loadQueueItems() {
    const queueItems = await requestToPromise(getQueueStore('readonly').getAll());
    state.queue = queueItems
      .filter((item) => String(item.queueKey || '') === currentQueueKey())
      .sort((left, right) => String(left.createdAt || '').localeCompare(String(right.createdAt || '')));
    restoreCombineGroup();
    renderQueue();
    return state.queue;
  }

  async function getQueueItem(itemId) {
    return requestToPromise(getQueueStore('readonly').get(itemId));
  }

  async function saveQueueItem(item) {
    return requestToPromise(getQueueStore('readwrite').put(item));
  }

  async function updateQueueItem(itemId, updates) {
    const existing = await getQueueItem(itemId);
    if (!existing) {
      return null;
    }
    const merged = {
      ...existing,
      ...updates,
    };
    await saveQueueItem(merged);
    return merged;
  }

  async function deleteQueueItem(itemId) {
    return requestToPromise(getQueueStore('readwrite').delete(itemId));
  }

  async function removeQueueItem(itemId) {
    await deleteQueueItem(itemId);
    await loadQueueItems();
    setStatusMessage('Removed the file from this device queue.', 'warning');
  }

  async function stageFiles(files, sourceLabel) {
    const selectedFiles = Array.from(files || []).filter(Boolean);
    if (!selectedFiles.length) {
      return;
    }
    const normalizedFiles = [];
    for (let index = 0; index < selectedFiles.length; index += 1) {
      const safeSource = String(sourceLabel || 'capture').replace(/[^a-z0-9]+/gi, '-').replace(/(^-|-$)/g, '').toLowerCase() || 'capture';
      const normalized = await canonicalizeSelectedFile(selectedFiles[index], `${safeSource}-${Date.now()}-${index + 1}`);
      normalizedFiles.push({
        ...normalized,
        sourceLabel,
      });
    }

    state.pendingFiles = state.pendingFiles.concat(normalizedFiles);
    renderPendingSelection();

    if (elements.nativeCameraPicker) {
      elements.nativeCameraPicker.value = '';
    }
    if (elements.filePicker) {
      elements.filePicker.value = '';
    }

    setStatusMessage(`Selected ${normalizedFiles.length} file${normalizedFiles.length === 1 ? '' : 's'}. Tap Submit to add ${normalizedFiles.length === 1 ? 'it' : 'them'} to the outbox.`, 'success');
  }

  async function submitPendingFiles() {
    if (!state.pendingFiles.length) {
      setStatusMessage('Choose at least one file before submitting.', 'warning');
      return;
    }
    if (!state.db) {
      setStatusMessage('Offline storage is unavailable on this device.', 'error');
      return;
    }

    const maxFiles = Number(state.bootstrap?.sync?.max_files || 5);
    if (state.pendingFiles.length > maxFiles) {
      setStatusMessage(`Add ${maxFiles} files or fewer at a time.`, 'warning');
      return;
    }

    const documentDate = String(elements.documentDateInput?.value || '').trim();
    if (!documentDate) {
      setStatusMessage('Choose the document date before submitting so Kumbu can place the document in the correct workbook month.', 'warning');
      elements.documentDateInput?.focus();
      return;
    }

    const includeInWorkspace = Boolean(elements.importIntoWorkspace && elements.importIntoWorkspace.checked);
    const combineRelatedPages = Boolean(elements.combineRelatedPages && elements.combineRelatedPages.checked);
    const groupId = combineRelatedPages ? (state.activeGroupId || makeId('group')) : '';
    if (combineRelatedPages && !state.activeGroupId) {
      state.activeGroupId = groupId;
    }
    if (!combineRelatedPages) {
      state.activeGroupId = '';
    }

    const createdAt = new Date().toISOString();
    for (const pendingFile of state.pendingFiles) {
      const submissionId = makeId('upload');
      await saveQueueItem({
        id: submissionId,
        queueKey: currentQueueKey(),
        submissionId,
        groupId: groupId || submissionId,
        combineRelatedPages,
        createdAt,
        updatedAt: createdAt,
        status: 'queued',
        attempts: 0,
        error: '',
        sourceLabel: pendingFile.sourceLabel,
        documentDate,
        includeInWorkspace,
        fileName: pendingFile.fileName,
        fileType: pendingFile.fileType,
        fileSize: pendingFile.fileSize,
        fileBlob: pendingFile.blob,
      });
    }

    state.pendingFiles = [];
    renderPendingSelection();

    await loadQueueItems();
    await registerBackgroundSync();

    if (navigator.onLine) {
      setStatusMessage('Files were saved locally. Uploading to Kumbu Connect now.', 'success');
      await syncQueue('file-added');
    } else {
      setStatusMessage('Files were saved locally and will sync automatically when the connection returns.', 'success');
    }
  }

  async function retryQueueItem(itemId) {
    await updateQueueItem(itemId, {
      status: 'queued',
      error: '',
      updatedAt: new Date().toISOString(),
    });
    await loadQueueItems();
    if (navigator.onLine) {
      setStatusMessage('Retrying the selected file now.', 'warning');
      await syncQueue('manual-retry');
      return;
    }
    setStatusMessage('Marked the file for retry. It will sync automatically when you are back online.', 'warning');
  }

  function isRetryableStatus(statusCode) {
    return [0, 408, 425, 429, 500, 502, 503, 504].includes(Number(statusCode || 0));
  }

  function unexpectedHtmlMessage(text, statusCode) {
    if (!text || !/(<html|<!doctype html)/i.test(text)) {
      return '';
    }
    if (statusCode === 404 || /not found|404/i.test(text)) {
      return 'This mobile scan link is unavailable or has expired. Open a fresh link and try again.';
    }
    if (statusCode === 410 || /expired/i.test(text)) {
      return 'This mobile scan link has expired. Open a fresh link and try again.';
    }
    if (/sign in|login/i.test(text)) {
      return 'The upload session is no longer available. Open a fresh mobile scan link and try again.';
    }
    return 'The server returned a page instead of an upload response.';
  }

  async function parseSyncResponse(response) {
    const contentType = String(response.headers.get('content-type') || '').toLowerCase();
    let payload = null;
    let text = '';

    if (contentType.includes('application/json')) {
      try {
        payload = await response.json();
      } catch (error) {
        payload = null;
      }
    } else {
      try {
        text = await response.text();
      } catch (error) {
        text = '';
      }
    }

    if (response.ok && payload && payload.ok !== false) {
      return {
        ok: true,
        payload,
        message: String(payload.message || 'Upload synced successfully.'),
      };
    }

    const fallbackMessage = unexpectedHtmlMessage(text, response.status);
    const payloadMessage = payload && typeof payload.message === 'string' ? payload.message.trim() : '';
    const message = payloadMessage || fallbackMessage || `Upload failed with status ${response.status}.`;
    const stopSync = response.status === 404 || response.status === 410;
    return {
      ok: false,
      payload,
      message,
      retryable: !stopSync && isRetryableStatus(response.status),
      stopSync,
    };
  }

  function queuedRetryItems() {
    return state.queue.filter((item) => item.status === 'queued');
  }

  function clearRetryTimer() {
    if (state.retryTimer) {
      window.clearTimeout(state.retryTimer);
      state.retryTimer = null;
    }
  }

  function scheduleRetry(reason) {
    clearRetryTimer();
    if (!navigator.onLine || state.isSyncing) {
      return;
    }
    const queuedItems = queuedRetryItems();
    if (!queuedItems.length) {
      return;
    }
    const maxAttempts = queuedItems.reduce((highest, item) => {
      return Math.max(highest, Number(item.attempts || 0));
    }, 0);
    const delay = Math.min(RETRY_BASE_DELAY_MS * Math.max(1, 2 ** Math.max(0, maxAttempts - 1)), RETRY_MAX_DELAY_MS);
    state.retryTimer = window.setTimeout(() => {
      state.retryTimer = null;
      void syncQueue(reason || 'scheduled-retry');
    }, delay);
  }

  function updateBootstrap(payload) {
    if (!payload || !payload.bootstrap) {
      return;
    }
    state.bootstrap = payload.bootstrap;
    renderSummary();
  }

  function queueFileFromItem(item) {
    if (item.fileBlob instanceof File) {
      return item.fileBlob;
    }
    return new File(
      [item.fileBlob],
      item.fileName || `queued-upload-${Date.now()}.jpg`,
      { type: item.fileType || 'application/octet-stream' }
    );
  }

  async function submitQueueItem(item) {
    const formData = new FormData();
    formData.append('submission_id', item.submissionId || item.id);
    formData.append('group_id', item.groupId || item.submissionId || item.id);
    if (item.combineRelatedPages) {
      formData.append('combine_related_pages', 'true');
    }
    formData.append('document_date', item.documentDate || '');
    formData.append('import_into_workspace', item.includeInWorkspace ? 'true' : 'false');
    formData.append('bookkeeping_image', queueFileFromItem(item), item.fileName || 'bookkeeping-upload.jpg');

    try {
      const response = await fetch(state.bootstrap.sync.submit_url, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      return parseSyncResponse(response);
    } catch (error) {
      return {
        ok: false,
        message: error && error.message ? error.message : 'Connection dropped before the queued file could upload.',
        retryable: true,
        stopSync: false,
      };
    }
  }

  async function syncQueue(reason) {
    if (state.isSyncing) {
      return;
    }
    if (!navigator.onLine) {
      setStatusMessage('Offline. Files remain safely queued on this device.', 'warning');
      return;
    }

    await loadQueueItems();

    const syncableItems = state.queue.filter((item) => item.status !== 'failed');
    if (!syncableItems.length) {
      if (state.queue.length) {
        setStatusMessage('Some queued files still need attention before they can retry.', 'warning');
      } else {
        setStatusMessage('No queued files to sync.', 'success');
      }
      return;
    }

    clearRetryTimer();
    state.isSyncing = true;
    renderQueue();
    setStatusMessage(`Syncing ${syncableItems.length} queued file${syncableItems.length === 1 ? '' : 's'}...`, 'warning');

    let processedCount = 0;
    let sawRetryableFailure = false;

    for (const queuedItem of syncableItems) {
      const liveItem = state.queue.find((item) => item.id === queuedItem.id);
      if (!liveItem || liveItem.status === 'failed') {
        continue;
      }

      await updateQueueItem(liveItem.id, {
        status: 'syncing',
        error: '',
        updatedAt: new Date().toISOString(),
      });
      await loadQueueItems();

      const syncingItem = state.queue.find((item) => item.id === liveItem.id) || liveItem;
      const result = await submitQueueItem(syncingItem);

      if (result.ok) {
        processedCount += 1;
        updateBootstrap(result.payload);
        await deleteQueueItem(syncingItem.id);
        await loadQueueItems();
        markLastSyncNow();
        continue;
      }

      const attempts = Number(syncingItem.attempts || 0) + 1;
      sawRetryableFailure = sawRetryableFailure || Boolean(result.retryable);
      await updateQueueItem(syncingItem.id, {
        status: result.retryable ? 'queued' : 'failed',
        attempts,
        error: result.message,
        updatedAt: new Date().toISOString(),
      });
      await loadQueueItems();
      setStatusMessage(result.message, result.retryable ? 'warning' : 'error');

      if (result.stopSync) {
        break;
      }
    }

    state.isSyncing = false;
    renderQueue();

    if (!state.queue.length) {
      setStatusMessage('All queued files have been synced.', 'success');
      return;
    }

    if (!navigator.onLine) {
      setStatusMessage('Offline. Files remain safely queued until the connection returns.', 'warning');
      return;
    }

    if (state.queue.some((item) => item.status === 'failed')) {
      setStatusMessage('Some queued files need attention. Retry them from the outbox after fixing the issue.', 'warning');
    } else if (processedCount > 0) {
      setStatusMessage('Some files remain queued and will retry automatically.', 'warning');
    } else {
      setStatusMessage('Queued files are waiting for the next retry window.', 'warning');
    }

    if (sawRetryableFailure && state.queue.some((item) => item.status === 'queued')) {
      scheduleRetry(reason || 'retry');
    }
  }

  async function registerBackgroundSync() {
    if (!state.registration || !state.queue.length) {
      return;
    }
    if (!('sync' in state.registration)) {
      return;
    }
    try {
      await state.registration.sync.register('bookkeeping-mobile-scan-sync');
    } catch (error) {
      console.warn('Unable to register background sync.', error);
    }
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
      renderOfflineReady('Offline cache unavailable', 'error');
      return;
    }
    try {
      state.registration = await navigator.serviceWorker.register(state.bootstrap.sync.sw_url, {
        scope: state.bootstrap.sync.app_url,
      });
      await navigator.serviceWorker.ready;
      if (navigator.serviceWorker.controller) {
        renderOfflineReady('Offline ready', 'ready');
      } else {
        renderOfflineReady('Cached for next reopen', 'pending');
      }
      await registerBackgroundSync();
      navigator.serviceWorker.addEventListener('message', (event) => {
        if (event.data && event.data.type === 'BOOKKEEPING_MOBILE_SCAN_SYNC') {
          void syncQueue('service-worker');
        }
      });
    } catch (error) {
      console.error('Unable to register mobile scan service worker.', error);
      renderOfflineReady('Offline cache unavailable', 'error');
    }
  }

  function restoreCombineGroup() {
    const latestGroupedItem = [...state.queue]
      .reverse()
      .find((item) => item.combineRelatedPages && item.status !== 'failed');
    state.activeGroupId = latestGroupedItem ? String(latestGroupedItem.groupId || '') : '';
  }

  function bindEvents() {
    if (elements.nativeCameraPicker) {
      elements.nativeCameraPicker.addEventListener('change', (event) => {
        void stageFiles(event.target.files, 'phone camera');
      });
    }
    if (elements.filePicker) {
      elements.filePicker.addEventListener('change', (event) => {
        void stageFiles(event.target.files, 'library file');
      });
    }
    if (elements.form) {
      elements.form.addEventListener('submit', (event) => {
        event.preventDefault();
        void submitPendingFiles();
      });
    }
    if (elements.combineRelatedPages) {
      elements.combineRelatedPages.addEventListener('change', () => {
        if (!elements.combineRelatedPages.checked) {
          state.activeGroupId = '';
        }
      });
    }
    if (elements.queue) {
      elements.queue.addEventListener('click', (event) => {
        const target = event.target.closest('button[data-queue-action]');
        if (!target) {
          return;
        }
        const itemElement = target.closest('[data-queue-id]');
        if (!itemElement) {
          return;
        }
        const itemId = itemElement.getAttribute('data-queue-id');
        if (!itemId) {
          return;
        }
        const action = target.getAttribute('data-queue-action');
        if (action === 'retry') {
          void retryQueueItem(itemId);
        } else if (action === 'remove') {
          void removeQueueItem(itemId);
        }
      });
    }

    window.addEventListener('online', () => {
      renderConnectivity();
      setStatusMessage('Connection restored. Syncing queued files now.', 'success');
      void syncQueue('online');
    });

    window.addEventListener('offline', () => {
      clearRetryTimer();
      renderConnectivity();
      setStatusMessage('Offline. Files remain safely queued on this device until the connection returns.', 'warning');
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && navigator.onLine && state.queue.length) {
        void syncQueue('visible');
      }
    });

    window.addEventListener('focus', () => {
      if (navigator.onLine && state.queue.length) {
        void syncQueue('focus');
      }
    });

    window.addEventListener('pageshow', () => {
      if (navigator.onLine && state.queue.length) {
        void syncQueue('pageshow');
      }
    });

    window.addEventListener('beforeunload', () => {
      clearRetryTimer();
      clearPreviewUrls();
    });
  }

  async function initialize() {
    bindEvents();
    renderSummary();
    renderConnectivity();
    renderLastSyncNote();
    renderPendingSelection();
    renderOfflineReady('Preparing offline cache', 'pending');

    if (!window.indexedDB) {
      renderOfflineReady('Offline storage unavailable', 'error');
      setStatusMessage('This browser cannot save captures offline. Use a newer mobile browser for this page.', 'error');
      return;
    }

    try {
      state.db = await openDatabase();
      await loadQueueItems();
    } catch (error) {
      console.error('Unable to initialize the mobile scan queue.', error);
      renderOfflineReady('Offline storage unavailable', 'error');
      setStatusMessage('The device could not open offline storage for the mobile scanner.', 'error');
      return;
    }

    await registerServiceWorker();

    if (navigator.onLine && state.queue.some((item) => item.status === 'queued')) {
      setStatusMessage('Queued files detected. Syncing now.', 'warning');
      await syncQueue('startup');
      return;
    }

    if (!state.queue.length) {
      setStatusMessage('Ready to capture. Choose files and tap Submit to add them to the outbox.', 'success');
    } else if (!navigator.onLine) {
      setStatusMessage('Offline. Queued files will stay on this device until the connection returns.', 'warning');
    } else {
      setStatusMessage('Queued files are ready to sync and will retry automatically while this page stays open.', 'warning');
    }
  }

  void initialize();
})();