(function () {
  const bootstrapNode = document.getElementById('intakeOfflineBootstrap');
  if (!bootstrapNode) {
    return;
  }

  let embeddedBootstrap;
  try {
    embeddedBootstrap = JSON.parse(bootstrapNode.textContent || '{}');
  } catch (error) {
    return;
  }

  const DB_NAME = 'kumbu-intake-offline';
  const DB_VERSION = 1;
  const BOOTSTRAP_STORE = 'bootstrap';
  const DRAFT_STORE = 'drafts';
  const FILE_STORE = 'files';
  const QUEUE_STORE = 'queue';
  const SYNC_REQUEST_TIMEOUT_MS = 45000;

  const state = {
    bootstrap: embeddedBootstrap,
    db: null,
    draft: {
      key: '',
      formValues: {},
      uploadsByField: {},
      updatedAt: '',
    },
    queue: [],
    registration: null,
    isSyncing: false,
    lastSyncAt: '',
    saveTimer: null,
    retryTimer: null,
  };

  const elements = {
    form: document.getElementById('intakeOfflineForm'),
    connectivityBadge: document.getElementById('intakeConnectivityBadge'),
    offlineReadyBadge: document.getElementById('intakeOfflineReadyBadge'),
    queueCountBadge: document.getElementById('intakeQueueCountBadge'),
    lastSyncNote: document.getElementById('intakeLastSyncNote'),
    queueSubmitBtn: document.getElementById('intakeQueueSubmitBtn'),
    syncStatusMessage: document.getElementById('intakeSyncStatusMessage'),
    queueList: document.getElementById('intakeQueueList'),
  };

  function bootstrapKey() {
    const cboId = state.bootstrap && state.bootstrap.cbo ? state.bootstrap.cbo.id : 'unknown';
    return 'cbo-' + String(cboId);
  }

  function lastSyncStorageKey() {
    return 'kumbu-intake-last-sync-' + bootstrapKey();
  }

  function makeId(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return prefix + '-' + window.crypto.randomUUID();
    }
    return prefix + '-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function parseIso(value) {
    if (!value) {
      return null;
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatDateTime(value) {
    const parsed = parseIso(value);
    if (!parsed) {
      return value || 'Not available';
    }
    return parsed.toLocaleString();
  }

  function formatFileSize(value) {
    const size = Number(value || 0);
    if (!size) {
      return '0 B';
    }
    if (size >= 1024 * 1024) {
      return (size / (1024 * 1024)).toFixed(1) + ' MB';
    }
    if (size >= 1024) {
      return Math.round(size / 1024) + ' KB';
    }
    return size + ' B';
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
    const fileName = String(file && file.name || '').trim();
    const declaredMimeType = normalizeMimeType(file && file.type);
    const guessedMimeType = guessMimeTypeFromName(fileName);
    const arrayBuffer = await file.arrayBuffer();
    const sniffedMimeType = sniffMimeType(arrayBuffer);
    const mimeType = sniffedMimeType || declaredMimeType || guessedMimeType || 'application/octet-stream';
    const extension = extensionForMimeType(mimeType);
    const finalFileName = fileName
      ? (/\.[a-z0-9]{1,8}$/i.test(fileName) || !extension ? fileName : fileName + extension)
      : String(fallbackBaseName || 'upload') + extension;
    const blob = new Blob([arrayBuffer], {
      type: mimeType,
    });
    return {
      blob: blob,
      fileName: finalFileName,
      mimeType: mimeType,
      size: Number(blob.size || file.size || 0),
    };
  }

  function requestToPromise(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error || new Error('IndexedDB request failed.'));
      };
    });
  }

  function fetchWithSyncTimeout(url, options) {
    if (typeof AbortController !== 'function') {
      return fetch(url, options);
    }

    const controller = new AbortController();
    const requestOptions = Object.assign({}, options || {}, {
      signal: controller.signal,
    });
    const timeoutId = window.setTimeout(function () {
      controller.abort();
    }, SYNC_REQUEST_TIMEOUT_MS);

    return fetch(url, requestOptions).catch(function (error) {
      if (error && error.name === 'AbortError') {
        const timeoutError = new Error('Kumbu Connect did not respond in time. This submission will stay queued and retry automatically.');
        timeoutError.retryable = true;
        throw timeoutError;
      }
      throw error;
    }).finally(function () {
      window.clearTimeout(timeoutId);
    });
  }

  function recoverInterruptedQueueItems(items) {
    return (items || []).map(function (item) {
      if (!item || item.status !== 'syncing') {
        return item;
      }
      return Object.assign({}, item, {
        status: 'queued',
        lastError: item.lastError || 'The previous sync attempt was interrupted. Kumbu will retry when this device is online.',
      });
    });
  }

  function openDatabase() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(BOOTSTRAP_STORE)) {
          db.createObjectStore(BOOTSTRAP_STORE, { keyPath: 'key' });
        }
        if (!db.objectStoreNames.contains(DRAFT_STORE)) {
          db.createObjectStore(DRAFT_STORE, { keyPath: 'key' });
        }
        if (!db.objectStoreNames.contains(FILE_STORE)) {
          db.createObjectStore(FILE_STORE, { keyPath: 'id' });
        }
        if (!db.objectStoreNames.contains(QUEUE_STORE)) {
          db.createObjectStore(QUEUE_STORE, { keyPath: 'id' });
        }
      };
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error || new Error('Could not open IndexedDB.'));
      };
    });
  }

  function storeRequest(storeName, mode, action) {
    if (!state.db) {
      return Promise.reject(new Error('Offline storage is not ready yet.'));
    }
    const transaction = state.db.transaction(storeName, mode);
    const store = transaction.objectStore(storeName);
    return action(store, transaction);
  }

  function saveBootstrap(payload) {
    return storeRequest(BOOTSTRAP_STORE, 'readwrite', function (store) {
      return requestToPromise(store.put({
        key: bootstrapKey(),
        payload: payload,
        updatedAt: new Date().toISOString(),
      }));
    });
  }

  function loadBootstrap() {
    return storeRequest(BOOTSTRAP_STORE, 'readonly', function (store) {
      return requestToPromise(store.get(bootstrapKey()));
    }).then(function (record) {
      return record && record.payload ? record.payload : null;
    });
  }

  function emptyDraft() {
    return {
      key: bootstrapKey(),
      formValues: {},
      uploadsByField: {},
      updatedAt: new Date().toISOString(),
    };
  }

  function saveDraft(draft) {
    return storeRequest(DRAFT_STORE, 'readwrite', function (store) {
      return requestToPromise(store.put(draft));
    });
  }

  function loadDraft() {
    return storeRequest(DRAFT_STORE, 'readonly', function (store) {
      return requestToPromise(store.get(bootstrapKey()));
    }).then(function (record) {
      return record || null;
    });
  }

  function saveFileRecord(fileRecord) {
    return storeRequest(FILE_STORE, 'readwrite', function (store) {
      return requestToPromise(store.put(fileRecord));
    });
  }

  function loadFileRecord(fileId) {
    return storeRequest(FILE_STORE, 'readonly', function (store) {
      return requestToPromise(store.get(fileId));
    }).then(function (record) {
      return record || null;
    });
  }

  function removeFileRecord(fileId) {
    return storeRequest(FILE_STORE, 'readwrite', function (store) {
      return requestToPromise(store.delete(fileId));
    });
  }

  function loadAllFileRecords() {
    return storeRequest(FILE_STORE, 'readonly', function (store) {
      return requestToPromise(store.getAll());
    }).then(function (items) {
      return items || [];
    });
  }

  function saveQueueItem(item) {
    return storeRequest(QUEUE_STORE, 'readwrite', function (store) {
      return requestToPromise(store.put(item));
    });
  }

  function removeQueueItem(id) {
    return storeRequest(QUEUE_STORE, 'readwrite', function (store) {
      return requestToPromise(store.delete(id));
    });
  }

  function loadQueueItems() {
    return storeRequest(QUEUE_STORE, 'readonly', function (store) {
      return requestToPromise(store.getAll());
    }).then(function (items) {
      return (items || []).sort(function (left, right) {
        return String(right.createdAt || '').localeCompare(String(left.createdAt || ''));
      });
    });
  }

  function setStatusMessage(message, tone) {
    if (!elements.syncStatusMessage) {
      return;
    }
    elements.syncStatusMessage.textContent = message;
    elements.syncStatusMessage.dataset.tone = tone || 'info';
  }

  function clearRetryTimer() {
    if (!state.retryTimer) {
      return;
    }
    window.clearTimeout(state.retryTimer);
    state.retryTimer = null;
  }

  function queuedRetryItems() {
    return (state.queue || []).filter(function (item) {
      return item && item.status === 'queued';
    });
  }

  function scheduleRetry(reason) {
    clearRetryTimer();
    if (!navigator.onLine || state.isSyncing) {
      return;
    }

    const pendingItems = queuedRetryItems();
    if (!pendingItems.length) {
      return;
    }

    const maxAttempts = pendingItems.reduce(function (maxAttemptsValue, item) {
      return Math.max(maxAttemptsValue, Number(item.attempts || 0));
    }, 0);
    const delayMs = Math.min(15000, Math.max(3000, (maxAttempts + 1) * 3000));
    state.retryTimer = window.setTimeout(function () {
      state.retryTimer = null;
      syncQueue();
    }, delayMs);

    if (reason === 'transient-failure') {
      setStatusMessage('Connection is back, but sync is still settling. Kumbu will retry automatically.', 'warning');
    }
  }

  function setPillState(element, message, stateName) {
    if (!element) {
      return;
    }
    element.textContent = message;
    element.className = 'intake-offline-pill';
    if (stateName) {
      element.classList.add(stateName);
    }
  }

  function renderConnectivity() {
    if (navigator.onLine) {
      setPillState(elements.connectivityBadge, 'Online', 'is-online');
      return;
    }
    setPillState(elements.connectivityBadge, 'Offline', 'is-offline');
  }

  function renderOfflineReady(message, tone) {
    setPillState(elements.offlineReadyBadge, message, tone);
  }

  function renderLastSync() {
    if (!elements.lastSyncNote) {
      return;
    }
    if (!state.lastSyncAt) {
      elements.lastSyncNote.textContent = 'No sync completed on this device yet.';
      return;
    }
    elements.lastSyncNote.textContent = 'Last successful sync on this device: ' + formatDateTime(state.lastSyncAt);
  }

  function intakeFields() {
    return Array.isArray(state.bootstrap.form && state.bootstrap.form.fields)
      ? state.bootstrap.form.fields
      : [];
  }

  function intakeUploadFields() {
    return Array.isArray(state.bootstrap.form && state.bootstrap.form.upload_fields)
      ? state.bootstrap.form.upload_fields
      : [];
  }

  function captureFormValues() {
    const values = {};
    if (!elements.form) {
      return values;
    }
    elements.form.querySelectorAll('[data-field-id]').forEach(function (field) {
      values[field.dataset.fieldId] = String(field.value || '');
    });
    return values;
  }

  function fillFormValues(values) {
    if (!elements.form) {
      return;
    }
    elements.form.querySelectorAll('[data-field-id]').forEach(function (field) {
      field.value = String(values && values[field.dataset.fieldId] ? values[field.dataset.fieldId] : '');
    });
  }

  function queueCountLabel() {
    const count = Array.isArray(state.queue) ? state.queue.length : 0;
    return String(count) + ' queued';
  }

  function renderQueueCount() {
    if (!elements.queueCountBadge) {
      return;
    }
    const count = Array.isArray(state.queue) ? state.queue.length : 0;
    const tone = count ? (state.queue.some(function (item) { return item.status === 'failed'; }) ? 'is-warning' : 'is-ready') : '';
    setPillState(elements.queueCountBadge, queueCountLabel(), tone);
  }

  function draftFileRefs(fieldId) {
    const uploadsByField = state.draft && state.draft.uploadsByField ? state.draft.uploadsByField : {};
    return Array.isArray(uploadsByField[fieldId]) ? uploadsByField[fieldId] : [];
  }

  function renderDraftUploads() {
    intakeUploadFields().forEach(function (uploadField) {
      const container = document.getElementById('draftFiles-' + String(uploadField.id || ''));
      if (!container) {
        return;
      }
      const refs = draftFileRefs(String(uploadField.id || ''));
      if (!refs.length) {
        container.innerHTML = '<p class="intake-offline-empty">No files saved on this device yet.</p>';
        return;
      }

      container.innerHTML = refs.map(function (fileRef) {
        return [
          '<article class="intake-offline-file-chip">',
          '<div>',
          '<p class="intake-offline-file-chip__title">' + escapeHtml(fileRef.fileName || 'Upload') + '</p>',
          '<p class="intake-offline-file-chip__hint">Saved locally · ' + escapeHtml(formatFileSize(fileRef.size || 0)) + (fileRef.mimeType ? ' · ' + escapeHtml(fileRef.mimeType) : '') + '</p>',
          '</div>',
          '<div class="intake-offline-file-chip__meta">',
          '<button type="button" class="btn btn-danger btn-xs" data-draft-file-remove="' + escapeHtml(fileRef.fileId || '') + '" data-draft-field="' + escapeHtml(uploadField.id || '') + '">Remove</button>',
          '</div>',
          '</article>',
        ].join('');
      }).join('');
    });
  }

  function queueStatusLabel(item) {
    if (item.status === 'syncing') {
      return 'Syncing';
    }
    if (item.status === 'queued' && item.lastError) {
      return 'Retrying';
    }
    if (item.status === 'failed') {
      return 'Needs attention';
    }
    return 'Queued';
  }

  function queueTitle(item) {
    const values = item.formValues || {};
    return values.cbo_name || values.full_name || values.email_address || 'CBO intake submission';
  }

  function renderQueue() {
    renderQueueCount();
    if (!elements.queueList) {
      return;
    }
    if (!state.queue.length) {
      elements.queueList.innerHTML = '<p class="intake-offline-empty">No queued submissions yet.</p>';
      return;
    }

    elements.queueList.innerHTML = state.queue.map(function (item) {
      const itemClass = item.status === 'failed'
        ? 'intake-offline-queue-item is-failed'
        : item.status === 'syncing'
          ? 'intake-offline-queue-item is-syncing'
          : 'intake-offline-queue-item';
      const uploadCount = Array.isArray(item.uploads) ? item.uploads.length : 0;
      const answerCount = Object.keys(item.formValues || {}).filter(function (fieldId) {
        return String(item.formValues[fieldId] || '').trim();
      }).length;
      return [
        '<article class="' + itemClass + '">',
        '<div>',
        '<span class="intake-offline-chip">' + escapeHtml(queueStatusLabel(item)) + '</span>',
        '<h3 class="intake-offline-queue-item__title">' + escapeHtml(queueTitle(item)) + '</h3>',
        '<p class="intake-offline-queue-item__meta">',
        escapeHtml(String(answerCount) + ' answered field' + (answerCount === 1 ? '' : 's')),
        ' · ',
        escapeHtml(String(uploadCount) + ' upload' + (uploadCount === 1 ? '' : 's')),
        ' · Queued ',
        escapeHtml(formatDateTime(item.createdAt)),
        '</p>',
        item.lastError ? '<p class="intake-offline-queue-item__error">' + escapeHtml(item.lastError) + '</p>' : '',
        '</div>',
        '<div class="intake-offline-queue-item__actions">',
        item.status === 'failed' ? '<button type="button" class="btn btn-outline btn-xs" data-queue-retry="' + escapeHtml(item.id || '') + '">Retry</button>' : '',
        '<button type="button" class="btn btn-danger btn-xs" data-queue-remove="' + escapeHtml(item.id || '') + '">Discard</button>',
        '</div>',
        '</article>',
      ].join('');
    }).join('');
  }

  function saveDraftState(showStatus) {
    const draft = {
      key: bootstrapKey(),
      formValues: captureFormValues(),
      uploadsByField: state.draft && state.draft.uploadsByField ? state.draft.uploadsByField : {},
      updatedAt: new Date().toISOString(),
    };
    state.draft = draft;
    return saveDraft(draft).then(function () {
      if (showStatus) {
        setStatusMessage('Draft saved on this device.', 'success');
      }
    }).catch(function (error) {
      setStatusMessage(error.message || 'Could not save this draft locally.', 'error');
      throw error;
    });
  }

  function scheduleDraftSave() {
    if (state.saveTimer) {
      window.clearTimeout(state.saveTimer);
    }
    state.saveTimer = window.setTimeout(function () {
      saveDraftState(false).catch(function () {});
    }, 260);
  }

  function referencedFileIds() {
    const keep = new Set();
    const uploadsByField = state.draft && state.draft.uploadsByField ? state.draft.uploadsByField : {};
    Object.keys(uploadsByField).forEach(function (fieldId) {
      (uploadsByField[fieldId] || []).forEach(function (fileRef) {
        if (fileRef && fileRef.fileId) {
          keep.add(String(fileRef.fileId));
        }
      });
    });
    state.queue.forEach(function (item) {
      (item.uploads || []).forEach(function (upload) {
        if (upload && upload.fileId) {
          keep.add(String(upload.fileId));
        }
      });
    });
    return keep;
  }

  function cleanupUnusedFiles() {
    const keep = referencedFileIds();
    return loadAllFileRecords().then(function (records) {
      return Promise.all(records.filter(function (record) {
        return !keep.has(String(record.id || ''));
      }).map(function (record) {
        return removeFileRecord(record.id);
      }));
    });
  }

  function removeDraftFile(fieldId, fileId) {
    const nextUploads = Object.assign({}, state.draft.uploadsByField || {});
    nextUploads[fieldId] = (nextUploads[fieldId] || []).filter(function (fileRef) {
      return String(fileRef.fileId || '') !== String(fileId || '');
    });
    if (!nextUploads[fieldId].length) {
      delete nextUploads[fieldId];
    }
    state.draft.uploadsByField = nextUploads;
    return saveDraftState(false).then(function () {
      return cleanupUnusedFiles();
    }).then(function () {
      renderDraftUploads();
      setStatusMessage('Removed the local file from this draft.', 'warning');
    }).catch(function (error) {
      setStatusMessage(error.message || 'Could not remove the local file.', 'error');
    });
  }

  function persistSelectedFiles(fieldId, fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) {
      return Promise.resolve();
    }
    const existing = draftFileRefs(fieldId).slice();
    return Promise.all(files.map(async function (file, index) {
      const normalizedFile = await canonicalizeSelectedFile(file, String(fieldId || 'upload') + '-' + String(Date.now()) + '-' + String(index + 1));
      const fileId = makeId('file');
      existing.push({
        fileId: fileId,
        fileName: normalizedFile.fileName || 'upload',
        mimeType: normalizedFile.mimeType || '',
        size: normalizedFile.size || 0,
      });
      return saveFileRecord({
        id: fileId,
        fieldId: fieldId,
        fileName: normalizedFile.fileName || 'upload',
        mimeType: normalizedFile.mimeType || '',
        size: normalizedFile.size || 0,
        blob: normalizedFile.blob,
        createdAt: new Date().toISOString(),
      });
    })).then(function () {
      const nextUploads = Object.assign({}, state.draft.uploadsByField || {});
      nextUploads[fieldId] = existing;
      state.draft.uploadsByField = nextUploads;
      return saveDraftState(false);
    }).then(function () {
      renderDraftUploads();
      setStatusMessage('Saved file' + (files.length === 1 ? '' : 's') + ' locally for offline sync.', 'success');
    }).catch(function (error) {
      setStatusMessage(error.message || 'Could not save selected files locally.', 'error');
    });
  }

  function validationErrors(formValues) {
    const errors = [];
    intakeFields().forEach(function (field) {
      const value = String(formValues[field.id] || '').trim();
      if (field.required && !value) {
        errors.push(String(field.title || field.id || 'This field') + ' is required.');
      }
    });
    return errors;
  }

  function clearDraftFromUi() {
    fillFormValues({});
    state.draft = emptyDraft();
    return saveDraft(state.draft).then(function () {
      renderDraftUploads();
    });
  }

  function queueCurrentSubmission() {
    const formValues = captureFormValues();
    const errors = validationErrors(formValues);
    if (errors.length) {
      setStatusMessage(errors[0], 'error');
      return Promise.resolve();
    }

    const uploads = [];
    intakeUploadFields().forEach(function (uploadField) {
      draftFileRefs(String(uploadField.id || '')).forEach(function (fileRef) {
        uploads.push({
          uploadId: makeId('upload'),
          fieldId: String(uploadField.id || ''),
          fileId: String(fileRef.fileId || ''),
          fileName: fileRef.fileName || 'upload',
          mimeType: fileRef.mimeType || '',
          size: Number(fileRef.size || 0),
        });
      });
    });

    const queueItem = {
      id: makeId('queued'),
      submissionId: makeId('submission'),
      createdAt: new Date().toISOString(),
      status: 'queued',
      attempts: 0,
      formValues: formValues,
      uploads: uploads,
      lastError: '',
    };

    return saveQueueItem(queueItem).then(function () {
      return loadQueueItems();
    }).then(function (items) {
      state.queue = items;
      return clearDraftFromUi();
    }).then(function () {
      renderQueue();
      setStatusMessage('Submission queued on this device. It will sync automatically when a connection is available.', 'success');
      return registerBackgroundSync();
    }).then(function () {
      if (navigator.onLine) {
        return syncQueue();
      }
      return null;
    }).catch(function (error) {
      setStatusMessage(error.message || 'Could not queue this submission locally.', 'error');
    });
  }

  function updateQueueItem(item, updates) {
    const nextItem = Object.assign({}, item, updates || {});
    return saveQueueItem(nextItem).then(function () {
      return loadQueueItems();
    }).then(function (items) {
      state.queue = items;
      renderQueue();
      return nextItem;
    });
  }

  function discardQueueItem(itemId) {
    return removeQueueItem(itemId).then(function () {
      return loadQueueItems();
    }).then(function (items) {
      state.queue = items;
      if (!queuedRetryItems().length) {
        clearRetryTimer();
      }
      return cleanupUnusedFiles();
    }).then(function () {
      renderQueue();
      setStatusMessage('Removed the queued submission from this device.', 'warning');
    }).catch(function (error) {
      setStatusMessage(error.message || 'Could not remove that queued submission.', 'error');
    });
  }

  function retryQueueItem(itemId) {
    const item = (state.queue || []).find(function (candidate) {
      return String(candidate.id || '') === String(itemId || '');
    });
    if (!item) {
      return Promise.resolve();
    }

    return updateQueueItem(item, {
      status: 'queued',
      lastError: '',
    }).then(function () {
      if (navigator.onLine) {
        return syncQueue();
      }
      setStatusMessage('Submission moved back into the retry queue.', 'warning');
      return null;
    });
  }

  function isRetryableStatus(statusCode) {
    return statusCode === 0 || statusCode === 408 || statusCode === 425 || statusCode === 429 || statusCode >= 500;
  }

  function unexpectedHtmlMessage(text) {
    const normalized = String(text || '').toLowerCase();
    if (!normalized || (normalized.indexOf('<html') === -1 && normalized.indexOf('<!doctype') === -1)) {
      return '';
    }
    if (normalized.indexOf('login') !== -1) {
      return 'The server returned a login page instead of the intake sync response.';
    }
    return 'The server returned an HTML page instead of the intake sync response.';
  }

  function parseSubmitResponse(response) {
    return response.text().then(function (text) {
      let payload = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (error) {
          payload = null;
        }
      }

      if (response.ok && payload && payload.ok !== false) {
        return {
          ok: true,
          payload: payload,
        };
      }

      const htmlMessage = unexpectedHtmlMessage(text);
      const message = (payload && (payload.message || (Array.isArray(payload.errors) && payload.errors[0])))
        || htmlMessage
        || 'Could not sync this intake submission.';

      return {
        ok: false,
        message: message,
        retryable: isRetryableStatus(response.status),
        stopSync: response.status === 401 || response.status === 403 || response.status === 404 || response.status === 410,
      };
    });
  }

  function submitQueueItem(item) {
    const formData = new FormData();
    formData.append('metadata', JSON.stringify({
      submission_id: item.submissionId,
      created_at: item.createdAt,
      form_values: item.formValues || {},
      uploads: (item.uploads || []).map(function (upload) {
        return {
          field_id: upload.fieldId,
          file_id: upload.fileId,
          upload_id: upload.uploadId,
        };
      }),
    }));

    return Promise.all((item.uploads || []).map(function (upload) {
      return loadFileRecord(upload.fileId).then(function (fileRecord) {
        if (!fileRecord || !fileRecord.blob) {
          const missingFileError = new Error('One of the saved upload files is missing from this device.');
          missingFileError.retryable = false;
          throw missingFileError;
        }
        formData.append('upload::' + String(upload.uploadId || upload.fileId || ''), fileRecord.blob, fileRecord.fileName || upload.fileName || 'upload');
      });
    })).then(function () {
      return fetchWithSyncTimeout(state.bootstrap.sync.submit_url, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
    }).then(function (response) {
      return parseSubmitResponse(response);
    }).catch(function (error) {
      return {
        ok: false,
        message: error.message || 'Connection returned, but the sync request still failed to load.',
        retryable: typeof error.retryable === 'boolean' ? error.retryable : true,
        stopSync: Boolean(error.stopSync),
      };
    });
  }

  function registerBackgroundSync() {
    if (!state.registration || !state.registration.sync || typeof state.registration.sync.register !== 'function') {
      return Promise.resolve();
    }
    return state.registration.sync.register('kumbu-intake-outbox').catch(function () {
      return null;
    });
  }

  function syncQueue() {
    if (state.isSyncing) {
      return Promise.resolve();
    }
    if (!navigator.onLine) {
      renderConnectivity();
      setStatusMessage('Offline. New submissions will stay queued until the device reconnects.', 'warning');
      return Promise.resolve();
    }
    return loadQueueItems().then(function (items) {
      const recoveredQueue = recoverInterruptedQueueItems(items);
      const recoveryWrites = recoveredQueue.reduce(function (writes, item, index) {
        const original = items[index];
        if (original && original.status === 'syncing' && item.status === 'queued') {
          writes.push(saveQueueItem(item));
        }
        return writes;
      }, []);

      return Promise.all(recoveryWrites).then(function () {
        state.queue = recoveredQueue;
        renderQueue();
      });
    }).then(function () {
      if (!state.queue.length) {
        clearRetryTimer();
        setStatusMessage('Everything on this device is already synced.', 'success');
        return null;
      }

      clearRetryTimer();
      state.isSyncing = true;
      setStatusMessage('Syncing queued intake submissions to Kumbu Connect...', 'warning');

      let syncedCount = 0;
      const warnings = [];

      function syncNext(index) {
        if (index >= state.queue.length) {
          return Promise.resolve();
        }
        const item = state.queue[index];
        return updateQueueItem(item, {
          status: 'syncing',
          lastError: '',
        }).then(function () {
          return submitQueueItem(item);
        }).then(function (result) {
          if (!result || result.ok === false) {
            return updateQueueItem(item, {
              status: result && result.retryable ? 'queued' : 'failed',
              attempts: Number(item.attempts || 0) + 1,
              lastError: (result && result.message) || 'Could not sync this submission.',
            }).then(function () {
              if (result && result.stopSync) {
                return Promise.resolve();
              }
              return syncNext(index + 1);
            });
          }

          const payload = result.payload || {};
          syncedCount += 1;
          (payload.warnings || []).forEach(function (warning) {
            warnings.push(String(warning));
          });
          return removeQueueItem(item.id).then(function () {
            return loadQueueItems();
          }).then(function (items) {
            state.queue = items;
            renderQueue();
          }).then(function () {
            return syncNext(index);
          });
        });
      }

      return syncNext(0).then(function () {
        state.isSyncing = false;
        if (syncedCount > 0) {
          state.lastSyncAt = new Date().toISOString();
          window.localStorage.setItem(lastSyncStorageKey(), state.lastSyncAt);
          renderLastSync();
        }
        return cleanupUnusedFiles();
      }).then(function () {
        if (queuedRetryItems().length && navigator.onLine) {
          scheduleRetry('transient-failure');
          if (syncedCount > 0) {
            setStatusMessage('Synced ' + String(syncedCount) + ' submission' + (syncedCount === 1 ? '' : 's') + '. Remaining queued submissions will retry automatically.', 'warning');
          }
          return;
        }
        if (syncedCount > 0 && state.queue.some(function (queueItem) { return queueItem.status === 'failed'; })) {
          setStatusMessage('Synced ' + String(syncedCount) + ' submission' + (syncedCount === 1 ? '' : 's') + ', but some queued items still need attention.', 'warning');
          return;
        }
        if (syncedCount > 0 && warnings.length) {
          setStatusMessage('Synced ' + String(syncedCount) + ' submission' + (syncedCount === 1 ? '' : 's') + '. Follow-up notes: ' + warnings[0], 'warning');
          return;
        }
        if (syncedCount > 0) {
          setStatusMessage('Synced ' + String(syncedCount) + ' queued submission' + (syncedCount === 1 ? '' : 's') + ' to Kumbu Connect.', 'success');
          return;
        }
        if (state.queue.some(function (queueItem) { return queueItem.status === 'failed'; })) {
          setStatusMessage('Some queued submissions still need attention. They will retry again on the next sync.', 'warning');
          return;
        }
        setStatusMessage('Everything on this device is already synced.', 'success');
      });
    }).catch(function (error) {
      state.isSyncing = false;
      setStatusMessage(error.message || 'Could not finish syncing queued submissions.', 'error');
    });
  }

  function bindEvents() {
    if (elements.form) {
      elements.form.addEventListener('input', function () {
        scheduleDraftSave();
      });
      elements.form.addEventListener('change', function (event) {
        const uploadFieldId = event.target && event.target.dataset ? event.target.dataset.uploadField : '';
        if (uploadFieldId) {
          persistSelectedFiles(String(uploadFieldId), event.target.files || []).then(function () {
            event.target.value = '';
          });
          return;
        }
        scheduleDraftSave();
      });
    }

    if (elements.queueSubmitBtn) {
      elements.queueSubmitBtn.addEventListener('click', function () {
        queueCurrentSubmission();
      });
    }
    if (elements.queueList) {
      elements.queueList.addEventListener('click', function (event) {
        const draftRemoveButton = event.target.closest('[data-draft-file-remove]');
        if (draftRemoveButton) {
          removeDraftFile(String(draftRemoveButton.dataset.draftField || ''), String(draftRemoveButton.dataset.draftFileRemove || ''));
          return;
        }
        const queueRetryButton = event.target.closest('[data-queue-retry]');
        if (queueRetryButton) {
          retryQueueItem(String(queueRetryButton.dataset.queueRetry || ''));
          return;
        }
        const queueRemoveButton = event.target.closest('[data-queue-remove]');
        if (queueRemoveButton) {
          discardQueueItem(String(queueRemoveButton.dataset.queueRemove || ''));
        }
      });
    }

    document.body.addEventListener('click', function (event) {
      const removeButton = event.target.closest('[data-draft-file-remove]');
      if (removeButton) {
        removeDraftFile(String(removeButton.dataset.draftField || ''), String(removeButton.dataset.draftFileRemove || ''));
      }
    });

    window.addEventListener('online', function () {
      renderConnectivity();
      syncQueue();
    });
    window.addEventListener('offline', function () {
      clearRetryTimer();
      renderConnectivity();
      setStatusMessage('Offline. Drafts and uploads remain on this device until the connection returns.', 'warning');
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && navigator.onLine) {
        syncQueue();
      }
    });
    window.addEventListener('focus', function () {
      if (navigator.onLine) {
        syncQueue();
      }
    });
    window.addEventListener('pageshow', function () {
      if (navigator.onLine) {
        syncQueue();
      }
    });
    window.addEventListener('beforeunload', function () {
      clearRetryTimer();
      if (state.saveTimer) {
        window.clearTimeout(state.saveTimer);
        state.saveTimer = null;
      }
      saveDraftState(false).catch(function () {});
    });
  }

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
      renderOfflineReady('Offline cache unavailable', 'is-warning');
      return Promise.resolve();
    }
    return navigator.serviceWorker.register(state.bootstrap.sync.sw_url, {
      scope: state.bootstrap.sync.app_url,
    }).then(function (registration) {
      state.registration = registration;
      return navigator.serviceWorker.ready.then(function () {
        if (navigator.serviceWorker.controller) {
          renderOfflineReady('Offline ready', 'is-ready');
        } else {
          renderOfflineReady('Cached for next reopen', 'is-warning');
        }
        navigator.serviceWorker.addEventListener('message', function (event) {
          if (event.data && event.data.type === 'INTAKE_OFFLINE_SYNC') {
            syncQueue();
          }
        });
        return registerBackgroundSync();
      });
    }).catch(function () {
      renderOfflineReady('Offline cache limited', 'is-warning');
      return null;
    });
  }

  function initializeDraft(record) {
    state.draft = record || emptyDraft();
    fillFormValues(state.draft.formValues || {});
    renderDraftUploads();
  }

  function initializeQueue(items) {
    state.queue = items || [];
    renderQueue();
  }

  function initialize() {
    renderConnectivity();
    renderLastSync();
    renderOfflineReady('Preparing offline storage', 'is-warning');

    state.lastSyncAt = window.localStorage.getItem(lastSyncStorageKey()) || '';
    renderLastSync();

    openDatabase().then(function (db) {
      state.db = db;
      return saveBootstrap(state.bootstrap).then(function () {
        return Promise.all([
          loadBootstrap(),
          loadDraft(),
          loadQueueItems(),
        ]);
      });
    }).then(function (results) {
      const draft = results[1];
      const queue = recoverInterruptedQueueItems(results[2]);
      // Keep the bootstrap embedded in the active page. Cached bootstrap records are keyed
      // by CBO and can belong to an older token, so they must not override the current sync URLs.
      initializeDraft(draft);
      initializeQueue(queue);
      renderOfflineReady('Offline ready', 'is-ready');
      bindEvents();
      return registerServiceWorker();
    }).then(function () {
      if (navigator.onLine && state.queue.length) {
        return syncQueue();
      }
      if (navigator.onLine) {
        setStatusMessage('This device is ready for offline intake capture.', 'success');
      } else {
        setStatusMessage('Offline mode active. Drafts and uploads will stay on this device until connection returns.', 'warning');
      }
      return null;
    }).catch(function (error) {
      renderOfflineReady('Offline storage failed', 'is-error');
      setStatusMessage(error.message || 'Could not initialize offline storage for this intake app.', 'error');
    });
  }

  initialize();
})();