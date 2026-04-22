(function () {
  const bootstrapNode = document.getElementById('bookkeepingOfflineBootstrap');
  if (!bootstrapNode) {
    return;
  }

  let embeddedBootstrap;
  try {
    embeddedBootstrap = JSON.parse(bootstrapNode.textContent || '{}');
  } catch (error) {
    return;
  }

  const DB_NAME = 'kumbu-bookkeeping-offline';
  const DB_VERSION = 1;
  const BOOTSTRAP_STORE = 'bootstrap';
  const QUEUE_STORE = 'queue';
  const WORKBOOK_PAGE_SIZE = 30;

  const state = {
    bootstrap: embeddedBootstrap,
    db: null,
    queue: [],
    registration: null,
    isSyncing: false,
    lastSyncAt: '',
    activeWorkspacePeriodKey: '',
    pagination: {
      workspacePage: 1,
      serverPage: 1,
    },
  };

  const elements = {
    connectivityBadge: document.getElementById('connectivityBadge'),
    offlineReadyBadge: document.getElementById('offlineReadyBadge'),
    syncNowBtn: document.getElementById('syncNowBtn'),
    refreshServerBtn: document.getElementById('refreshServerBtn'),
    lastSyncNote: document.getElementById('lastSyncNote'),
    summaryMetrics: document.getElementById('summaryMetrics'),
    topCategoryRow: document.getElementById('topCategoryRow'),
    workspaceWorkbookForm: document.getElementById('workspaceWorkbookForm'),
    workspaceWorkbookMeta: document.getElementById('workspaceWorkbookMeta'),
    workspaceGridHead: document.getElementById('workspaceGridHead'),
    workspaceGridBody: document.getElementById('workspaceGridBody'),
    offlineWorkspaceTableScroll: document.getElementById('offlineWorkspaceTableScroll'),
    offlineWorkspaceScrollLeft: document.getElementById('offlineWorkspaceScrollLeft'),
    offlineWorkspaceScrollRight: document.getElementById('offlineWorkspaceScrollRight'),
    workspacePagination: document.getElementById('workspacePagination'),
    workspacePaginationSummary: document.getElementById('workspacePaginationSummary'),
    workspacePaginationStatus: document.getElementById('workspacePaginationStatus'),
    workspacePrevPageBtn: document.getElementById('workspacePrevPageBtn'),
    workspaceNextPageBtn: document.getElementById('workspaceNextPageBtn'),
    offlineCameraInput: document.getElementById('offlineCameraInput'),
    offlineFileInput: document.getElementById('offlineFileInput'),
    offlineDocumentDate: document.getElementById('offlineDocumentDate'),
    offlineImportIntoWorkspace: document.getElementById('offlineImportIntoWorkspace'),
    offlineCombinePages: document.getElementById('offlineCombinePages'),
    queueCountBadge: document.getElementById('queueCountBadge'),
    syncStatusMessage: document.getElementById('syncStatusMessage'),
    queueList: document.getElementById('queueList'),
    serverRowCount: document.getElementById('serverRowCount'),
    workspaceTemplateMeta: document.getElementById('workspaceTemplateMeta'),
    serverRowsHead: document.getElementById('serverRowsHead'),
    serverRowsBody: document.getElementById('serverRowsBody'),
    serverPagination: document.getElementById('serverPagination'),
    serverPaginationSummary: document.getElementById('serverPaginationSummary'),
    serverPaginationStatus: document.getElementById('serverPaginationStatus'),
    serverPrevPageBtn: document.getElementById('serverPrevPageBtn'),
    serverNextPageBtn: document.getElementById('serverNextPageBtn'),
  };

  function bootstrapKey() {
    const cboId = state.bootstrap && state.bootstrap.cbo ? state.bootstrap.cbo.id : 'unknown';
    return 'cbo-' + String(cboId);
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

  function workbookColumnClass(columnName) {
    const column = String(columnName || '').trim().toLowerCase();
    if (column.indexOf('date') !== -1) {
      return 'bookkeeping-table__col--date';
    }
    if (column.indexOf('sign') !== -1) {
      return 'bookkeeping-table__col--signature';
    }
    if (
      ['row', 'no', 'no.', 'sn', 's/n', 'm/f', 'sex', 'gender', 'days'].indexOf(column) !== -1 ||
      column.indexOf('days') !== -1 ||
      column.indexOf('qty') !== -1 ||
      column.indexOf('quantity') !== -1
    ) {
      return 'bookkeeping-table__col--short';
    }
    if (column.indexOf('name') !== -1 || column.indexOf('tool') !== -1) {
      return 'bookkeeping-table__col--name';
    }
    if (
      column.indexOf('amount') !== -1 ||
      column.indexOf('fee') !== -1 ||
      column.indexOf('revenue') !== -1 ||
      column.indexOf('cost') !== -1 ||
      column.indexOf('price') !== -1 ||
      column.indexOf('balance') !== -1
    ) {
      return 'bookkeeping-table__col--amount';
    }
    if (column.indexOf('phone') !== -1 || column.indexOf('id') !== -1) {
      return 'bookkeeping-table__col--id';
    }
    return 'bookkeeping-table__col--text';
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

  function formatCurrency(value) {
    const amount = Number(value || 0);
    return 'KSh ' + amount.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
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

  function openDatabase() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(BOOTSTRAP_STORE)) {
          db.createObjectStore(BOOTSTRAP_STORE, { keyPath: 'key' });
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
      return Promise.reject(new Error('Offline storage is not ready.'));
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
        return String(left.createdAt || '').localeCompare(String(right.createdAt || ''));
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

  function setPillState(element, message, stateName) {
    if (!element) {
      return;
    }
    element.textContent = message;
    element.className = 'bookkeeping-offline-pill';
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

  function renderOfflineReady(readyMessage, readyState) {
    setPillState(elements.offlineReadyBadge, readyMessage, readyState);
  }

  function currentSummary() {
    return (state.bootstrap && state.bootstrap.summary) || {};
  }

  function currentWorkspace() {
    return (state.bootstrap && state.bootstrap.workspace) || {};
  }

  function currentWorkspaceColumns() {
    const workspace = currentWorkspace();
    return Array.isArray(workspace.columns) ? workspace.columns : [];
  }

  function currentWorkspacePeriods() {
    const workspace = currentWorkspace();
    return Array.isArray(workspace.periods) ? workspace.periods : [];
  }

  function currentWorkspaceTypeGroups() {
    const workspace = currentWorkspace();
    return Array.isArray(workspace.type_groups) ? workspace.type_groups : [];
  }

  function defaultWorkspaceTypeKey() {
    const workspace = currentWorkspace();
    return String(
      workspace.default_type_key ||
      (currentWorkspaceTypeGroups()[0] && currentWorkspaceTypeGroups()[0].key) ||
      'manual_general'
    ).trim();
  }

  function workspaceTypeLabel(typeKey) {
    const normalizedKey = String(typeKey || '').trim();
    const match = currentWorkspaceTypeGroups().find(function (group) {
      return String(group && group.key ? group.key : '').trim() === normalizedKey;
    });
    return match && match.label ? String(match.label) : (normalizedKey || 'General workbook');
  }

  function workspacePeriodStorageKey() {
    return bootstrapKey() + '-workspace-period';
  }

  function formatPeriodLabel(periodKey) {
    const value = String(periodKey || '').trim();
    if (!/^\d{4}-\d{2}$/.test(value)) {
      return value || 'Undated';
    }
    const parsed = new Date(value + '-01T00:00:00');
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleDateString(undefined, {
      month: 'long',
      year: 'numeric',
    });
  }

  function periodKeyFromValue(value) {
    const normalized = String(value || '').trim();
    if (/^\d{4}-\d{2}$/.test(normalized)) {
      return normalized;
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
      return normalized.slice(0, 7);
    }
    const parsed = parseIso(normalized);
    if (!parsed) {
      return '';
    }
    const year = parsed.getUTCFullYear();
    const month = String(parsed.getUTCMonth() + 1).padStart(2, '0');
    return String(year) + '-' + month;
  }

  function readStoredWorkspacePeriod(defaultValue) {
    try {
      return window.sessionStorage.getItem(workspacePeriodStorageKey()) || defaultValue || '';
    } catch (error) {
      return defaultValue || '';
    }
  }

  function writeStoredWorkspacePeriod(periodKey) {
    try {
      window.sessionStorage.setItem(workspacePeriodStorageKey(), String(periodKey || ''));
    } catch (error) {
      /* Ignore session storage failures. */
    }
  }

  function activeWorkspacePeriodKey() {
    const periods = currentWorkspacePeriods();
    const defaultKey = String(currentWorkspace().default_period_key || (periods[0] && periods[0].key) || periodKeyFromValue(new Date().toISOString()) || '').trim();
    const availableKeys = periods.map(function (period) {
      return String(period && period.key ? period.key : '');
    }).filter(Boolean);

    let nextKey = String(state.activeWorkspacePeriodKey || readStoredWorkspacePeriod(defaultKey) || defaultKey || '').trim();
    if (availableKeys.length && availableKeys.indexOf(nextKey) === -1) {
      nextKey = defaultKey || availableKeys[0] || '';
    }
    if (!nextKey) {
      nextKey = defaultKey;
    }
    state.activeWorkspacePeriodKey = nextKey;
    writeStoredWorkspacePeriod(nextKey);
    return state.activeWorkspacePeriodKey;
  }

  function activeWorkspacePeriodMeta() {
    const periodKey = activeWorkspacePeriodKey();
    const period = currentWorkspacePeriods().find(function (item) {
      return String(item && item.key ? item.key : '') === periodKey;
    });
    return period || {
      key: periodKey,
      label: formatPeriodLabel(periodKey),
      row_count: 0,
      manual_row_count: 0,
      imported_row_count: 0,
    };
  }

  function renderSummary() {
    if (!elements.summaryMetrics) {
      return;
    }
    const summary = currentSummary();
    const workspace = currentWorkspace();
    const metrics = [
      { label: 'Documents', value: Number(summary.document_count || 0) },
      { label: 'Entries', value: Number(summary.entry_count || 0) },
      { label: 'Audit Flags', value: Number(summary.audit_issue_count || 0) },
      { label: 'Income', value: formatCurrency(summary.income_total || 0) },
      { label: 'Expenses', value: formatCurrency(summary.expense_total || 0) },
      { label: 'Saved Rows', value: Number(workspace.row_count || 0) },
    ];

    elements.summaryMetrics.innerHTML = metrics.map(function (metric) {
      return [
        '<article class="bookkeeping-offline-metric">',
        '<span>' + escapeHtml(metric.label) + '</span>',
        '<strong>' + escapeHtml(metric.value) + '</strong>',
        '</article>',
      ].join('');
    }).join('');

    const categories = Array.isArray(summary.top_categories) ? summary.top_categories : [];
    if (!elements.topCategoryRow) {
      return;
    }
    if (!categories.length) {
      elements.topCategoryRow.innerHTML = '';
      return;
    }

    elements.topCategoryRow.innerHTML = categories.slice(0, 6).map(function (category) {
      return '<span class="bookkeeping-offline-pill">' + escapeHtml(category.label || 'Other') + ' · ' + escapeHtml(formatCurrency(category.amount || 0)) + '</span>';
    }).join('');
  }

  function workbookRowHasValues(values) {
    return Object.keys(values || {}).some(function (column) {
      return String(values[column] || '').trim();
    });
  }

  function normalizeWorkbookEntry(row, columns) {
    const values = row && row.values ? row.values : {};
    const entrySource = row && (row.entry_source || row.entrySource) ? (row.entry_source || row.entrySource) : 'manual';
    const rowId = row && (row.row_id || row.rowId) ? String(row.row_id || row.rowId) : '';
    const createdAt = row && (row.created_at || row.createdAt) ? String(row.created_at || row.createdAt) : '';
    const updatedAt = row && (row.updated_at || row.updatedAt) ? String(row.updated_at || row.updatedAt) : '';
    const sourceDocumentId = row && (row.source_document_id || row.sourceDocumentId) ? String(row.source_document_id || row.sourceDocumentId) : '';
    const sourceRowNumber = Number(row && (row.source_row_number || row.sourceRowNumber) ? (row.source_row_number || row.sourceRowNumber) : 0) || 0;
    const workspaceDocumentTypeKey = row && (row.workspace_document_type_key || row.workspaceDocumentTypeKey)
      ? String(row.workspace_document_type_key || row.workspaceDocumentTypeKey)
      : defaultWorkspaceTypeKey();
    const workspacePeriodKey = row && (row.workspace_period_key || row.workspacePeriodKey)
      ? String(row.workspace_period_key || row.workspacePeriodKey)
      : (periodKeyFromValue(createdAt) || activeWorkspacePeriodKey());
    const createdAtDisplay = row && row.created_at_display
      ? String(row.created_at_display)
      : (entrySource === 'document_import' && sourceRowNumber
        ? 'Document row ' + String(sourceRowNumber)
        : formatDateTime(createdAt));

    return {
      row_id: rowId,
      created_at: createdAt,
      updated_at: updatedAt,
      entry_source: entrySource,
      source_document_id: sourceDocumentId,
      source_row_number: sourceRowNumber,
      workspace_document_type_key: workspaceDocumentTypeKey,
      workspace_document_type_label: workspaceTypeLabel(workspaceDocumentTypeKey),
      workspace_period_key: workspacePeriodKey,
      workspace_period_label: formatPeriodLabel(workspacePeriodKey),
      created_at_display: createdAtDisplay,
      values: columns.reduce(function (result, column) {
        result[column] = String(values[column] || '');
        return result;
      }, {}),
    };
  }

  function captureWorkbookRows() {
    if (!elements.workspaceGridBody) {
      return [];
    }

    return Array.from(elements.workspaceGridBody.querySelectorAll('tr[data-row-index]')).map(function (row) {
      const values = {};
      row.querySelectorAll('input[data-column]').forEach(function (input) {
        values[input.dataset.column] = String(input.value || '');
      });

      const rowIdInput = row.querySelector('input[data-role="row-id"]');
      const createdAtInput = row.querySelector('input[data-role="created-at"]');
      const updatedAtInput = row.querySelector('input[data-role="updated-at"]');
      const sourceInput = row.querySelector('input[data-role="entry-source"]');
      const documentInput = row.querySelector('input[data-role="source-document-id"]');
      const documentRowInput = row.querySelector('input[data-role="source-row-number"]');
      const documentTypeInput = row.querySelector('input[data-role="workspace-document-type"]');
      const periodInput = row.querySelector('input[data-role="workspace-period"]');

      const rowId = rowIdInput ? String(rowIdInput.value || '').trim() : '';
      if (!workbookRowHasValues(values) && !rowId) {
        return null;
      }

      return {
        row_id: rowId,
        created_at: createdAtInput ? String(createdAtInput.value || '').trim() : '',
        updated_at: updatedAtInput ? String(updatedAtInput.value || '').trim() : '',
        entry_source: sourceInput ? String(sourceInput.value || 'manual').trim() || 'manual' : 'manual',
        source_document_id: documentInput ? String(documentInput.value || '').trim() : '',
        source_row_number: documentRowInput ? String(documentRowInput.value || '').trim() : '',
        workspace_document_type_key: documentTypeInput ? String(documentTypeInput.value || '').trim() : String(row.dataset.workspaceType || ''),
        workspace_period_key: periodInput ? String(periodInput.value || '').trim() : String(row.dataset.workspacePeriod || ''),
        values: values,
      };
    }).filter(Boolean);
  }

  function renderWorkbookMeta() {
    if (!elements.workspaceWorkbookMeta) {
      return;
    }

    const workspace = currentWorkspace();
    const activePeriod = activeWorkspacePeriodMeta();
    const fragments = [];
    if (workspace.generated_at) {
      fragments.push('<span class="bookkeeping-offline-pill">Template refreshed ' + escapeHtml(formatDateTime(workspace.generated_at)) + '</span>');
    }
    if (workspace.primary_document_type) {
      fragments.push('<span class="bookkeeping-offline-pill">Layout: ' + escapeHtml(workspace.primary_document_type) + '</span>');
    }
    if (Array.isArray(workspace.source_document_types) && workspace.source_document_types.length) {
      fragments.push('<span class="bookkeeping-offline-pill">Source docs: ' + escapeHtml(workspace.source_document_types.join(', ')) + '</span>');
    }
    if (Array.isArray(workspace.custom_fields) && workspace.custom_fields.length) {
      fragments.push('<span class="bookkeeping-offline-pill">Custom fields: ' + escapeHtml(workspace.custom_fields.join(', ')) + '</span>');
    }
    fragments.push([
      '<label class="bookkeeping-offline-period-control">',
      '<span>Workbook month</span>',
      '<select data-workspace-period-select>',
      currentWorkspacePeriods().map(function (period) {
        const periodKey = String(period && period.key ? period.key : '');
        const periodLabel = String(period && period.label ? period.label : formatPeriodLabel(periodKey));
        const rowCount = Number(period && period.row_count ? period.row_count : 0);
        return '<option value="' + escapeHtml(periodKey) + '" data-period-label="' + escapeHtml(periodLabel) + '"' + (periodKey === activePeriod.key ? ' selected' : '') + '>' + escapeHtml(periodLabel) + ' · ' + escapeHtml(String(rowCount)) + ' row' + (rowCount === 1 ? '' : 's') + '</option>';
      }).join(''),
      '</select>',
      '</label>',
    ].join(''));
    fragments.push('<span class="bookkeeping-offline-pill" data-workspace-period-summary>' + escapeHtml(activePeriod.label) + ' · ' + escapeHtml(String(activePeriod.row_count || 0)) + ' rows</span>');
    elements.workspaceWorkbookMeta.innerHTML = fragments.join('');
  }

  function paginateTableRows(options) {
    const body = options.body;
    if (!body) {
      return 1;
    }

    const rows = Array.from(body.querySelectorAll(options.rowSelector));
    const filteredRows = typeof options.filter === 'function'
      ? rows.filter(options.filter)
      : rows;
    const rowCount = filteredRows.length;
    const pageSize = Math.max(1, Number(options.pageSize || WORKBOOK_PAGE_SIZE) || WORKBOOK_PAGE_SIZE);
    const pageCount = Math.max(1, Math.ceil(rowCount / pageSize));
    const normalizedPage = Math.min(Math.max(1, Number(options.page || 1) || 1), pageCount);
    const startIndex = (normalizedPage - 1) * pageSize;
    const endIndex = startIndex + pageSize;

    const visibleRows = new Set(filteredRows.slice(startIndex, endIndex));
    rows.forEach(function (row) {
      const isVisible = visibleRows.has(row);
      row.hidden = !isVisible;
      row.setAttribute('aria-hidden', String(!isVisible));
    });

    if (options.controls) {
      options.controls.hidden = rowCount <= pageSize;
    }
    if (options.summary) {
      options.summary.textContent = rowCount
        ? 'Showing ' + String(startIndex + 1) + '-' + String(Math.min(endIndex, rowCount)) + ' of ' + String(rowCount) + ' rows'
        : (options.emptySummary || 'No rows');
    }
    if (options.status) {
      options.status.textContent = 'Page ' + String(normalizedPage) + ' of ' + String(pageCount);
    }
    if (options.prevButton) {
      options.prevButton.disabled = rowCount <= pageSize || normalizedPage <= 1;
    }
    if (options.nextButton) {
      options.nextButton.disabled = rowCount <= pageSize || normalizedPage >= pageCount;
    }

    return normalizedPage;
  }

  function renderWorkspacePagination() {
    state.pagination.workspacePage = paginateTableRows({
      body: elements.workspaceGridBody,
      rowSelector: 'tr[data-row-index]',
      page: state.pagination.workspacePage,
      pageSize: WORKBOOK_PAGE_SIZE,
      filter: function (row) {
        return String(row.dataset.workspacePeriod || '').trim() === activeWorkspacePeriodKey();
      },
      emptySummary: 'No rows in this month yet',
      controls: elements.workspacePagination,
      summary: elements.workspacePaginationSummary,
      status: elements.workspacePaginationStatus,
      prevButton: elements.workspacePrevPageBtn,
      nextButton: elements.workspaceNextPageBtn,
    });

    const summary = elements.workspaceWorkbookMeta ? elements.workspaceWorkbookMeta.querySelector('[data-workspace-period-summary]') : null;
    if (summary) {
      const activePeriod = activeWorkspacePeriodMeta();
      const visibleCount = Array.from(elements.workspaceGridBody ? elements.workspaceGridBody.querySelectorAll('tr[data-row-index]') : []).filter(function (row) {
        return String(row.dataset.workspacePeriod || '').trim() === activePeriod.key;
      }).length;
      summary.textContent = activePeriod.label + ' · ' + String(visibleCount) + ' row' + (visibleCount === 1 ? '' : 's');
    }
  }

  function renderServerPagination() {
    state.pagination.serverPage = paginateTableRows({
      body: elements.serverRowsBody,
      rowSelector: 'tr[data-server-row-index]',
      page: state.pagination.serverPage,
      pageSize: WORKBOOK_PAGE_SIZE,
      controls: elements.serverPagination,
      summary: elements.serverPaginationSummary,
      status: elements.serverPaginationStatus,
      prevButton: elements.serverPrevPageBtn,
      nextButton: elements.serverNextPageBtn,
    });
  }

  function renderWorkbookEditor(draftRows) {
    const columns = currentWorkspaceColumns();
    const currentPeriodKey = activeWorkspacePeriodKey();
    renderWorkbookMeta();

    if (!elements.workspaceGridHead || !elements.workspaceGridBody) {
      return;
    }

    if (!columns.length) {
      elements.workspaceGridHead.innerHTML = '';
      elements.workspaceGridBody.innerHTML = '<tr><td class="bookkeeping-offline-empty">No bookkeeping workspace template has been generated for this CBO yet.</td></tr>';
      renderWorkspacePagination();
      return;
    }

    const sourceRows = Array.isArray(draftRows) && draftRows.length
      ? draftRows.map(function (row) { return normalizeWorkbookEntry(row, columns); })
      : ((Array.isArray(currentWorkspace().entries) ? currentWorkspace().entries : []).map(function (row) {
          return normalizeWorkbookEntry(row, columns);
        }));

    const blankRows = 8;
    elements.workspaceGridHead.innerHTML = [
      '<tr>',
      '<th class="bookkeeping-table__col bookkeeping-table__col--rownum">Record</th>',
      columns.map(function (column) {
        return '<th class="bookkeeping-table__col ' + escapeHtml(workbookColumnClass(column)) + '">' + escapeHtml(column) + '</th>';
      }).join(''),
      '</tr>',
    ].join('');

    const rows = sourceRows.slice();
    for (let index = 0; index < blankRows; index += 1) {
      rows.push({
        row_id: '',
        created_at: '',
        updated_at: '',
        created_at_display: 'Ready for entry',
        entry_source: 'manual',
        source_document_id: '',
        source_row_number: '',
        workspace_document_type_key: defaultWorkspaceTypeKey(),
        workspace_period_key: currentPeriodKey,
        values: columns.reduce(function (result, column) {
          result[column] = '';
          return result;
        }, {}),
      });
    }

    elements.workspaceGridBody.innerHTML = rows.map(function (row, index) {
      const entrySource = row.entry_source || 'manual';
      const rowMode = !row.row_id ? 'new' : (entrySource === 'document_import' ? 'imported' : 'manual');
      const stamp = row.created_at_display || (!row.row_id ? 'Ready for entry' : formatDateTime(row.created_at));
      const rowTypeKey = String(row.workspace_document_type_key || defaultWorkspaceTypeKey() || '').trim();
      const rowPeriodKey = String(row.workspace_period_key || currentPeriodKey || '').trim();
      return [
        '<tr data-row-index="' + String(index) + '" data-workspace-period="' + escapeHtml(rowPeriodKey) + '" data-workspace-type="' + escapeHtml(rowTypeKey) + '" class="bookkeeping-database-row bookkeeping-database-row--' + escapeHtml(rowMode) + '">',
        '<td class="bookkeeping-table__col bookkeeping-table__col--rownum bookkeeping-database-row__meta">',
        '<span class="bookkeeping-database-row__index">' + escapeHtml(index + 1) + '</span>',
        '<span class="bookkeeping-database-row__tag bookkeeping-database-row__tag--' + escapeHtml(rowMode) + '">' + escapeHtml(rowMode === 'imported' ? 'Imported' : rowMode === 'manual' ? 'Saved' : 'New') + '</span>',
        '<span class="bookkeeping-database-row__stamp">' + escapeHtml(stamp) + '</span>',
        '<input type="hidden" data-role="row-id" value="' + escapeHtml(row.row_id || '') + '">',
        '<input type="hidden" data-role="created-at" value="' + escapeHtml(row.created_at || '') + '">',
        '<input type="hidden" data-role="updated-at" value="' + escapeHtml(row.updated_at || '') + '">',
        '<input type="hidden" data-role="entry-source" value="' + escapeHtml(entrySource) + '">',
        '<input type="hidden" data-role="source-document-id" value="' + escapeHtml(row.source_document_id || '') + '">',
        '<input type="hidden" data-role="source-row-number" value="' + escapeHtml(row.source_row_number || '') + '">',
        '<input type="hidden" data-role="workspace-document-type" value="' + escapeHtml(rowTypeKey) + '">',
        '<input type="hidden" data-role="workspace-period" value="' + escapeHtml(rowPeriodKey) + '">',
        '</td>',
        columns.map(function (column) {
          return '<td class="bookkeeping-table__col ' + escapeHtml(workbookColumnClass(column)) + '"><input class="bookkeeping-sheet__input" type="text" data-column="' + escapeHtml(column) + '" value="' + escapeHtml(row.values[column] || '') + '" autocomplete="off"></td>';
        }).join(''),
        '</tr>',
      ].join('');
    }).join('');

    renderWorkspacePagination();
  }

  function renderWorkspaceSnapshot() {
    const workspace = currentWorkspace();
    const columns = Array.isArray(workspace.columns) ? workspace.columns : [];
    const rows = Array.isArray(workspace.entries) ? workspace.entries : [];

    if (elements.serverRowCount) {
      elements.serverRowCount.textContent = String(rows.length) + ' rows';
    }

    if (elements.workspaceTemplateMeta) {
      const meta = [];
      if (workspace.generated_at) {
        meta.push('<span class="bookkeeping-offline-pill">Template refreshed ' + escapeHtml(formatDateTime(workspace.generated_at)) + '</span>');
      }
      if (workspace.primary_document_type) {
        meta.push('<span class="bookkeeping-offline-pill">Layout: ' + escapeHtml(workspace.primary_document_type) + '</span>');
      }
      if (Array.isArray(workspace.source_document_types) && workspace.source_document_types.length) {
        meta.push('<span class="bookkeeping-offline-pill">Source docs: ' + escapeHtml(workspace.source_document_types.join(', ')) + '</span>');
      }
      if (Array.isArray(workspace.custom_fields) && workspace.custom_fields.length) {
        meta.push('<span class="bookkeeping-offline-pill">Custom fields: ' + escapeHtml(workspace.custom_fields.join(', ')) + '</span>');
      }
      elements.workspaceTemplateMeta.innerHTML = meta.join('');
    }

    if (!elements.serverRowsHead || !elements.serverRowsBody) {
      return;
    }

    if (!columns.length) {
      elements.serverRowsHead.innerHTML = '';
      elements.serverRowsBody.innerHTML = '<tr><td class="bookkeeping-offline-empty">No workspace template available yet.</td></tr>';
      renderServerPagination();
      return;
    }

    elements.serverRowsHead.innerHTML = [
      '<tr>',
      '<th>Record</th>',
      columns.map(function (column) {
        return '<th>' + escapeHtml(column) + '</th>';
      }).join(''),
      '</tr>',
    ].join('');

    if (!rows.length) {
      elements.serverRowsBody.innerHTML = '<tr><td colspan="' + String(columns.length + 1) + '" class="bookkeeping-offline-empty">No rows have synced to the server yet.</td></tr>';
      renderServerPagination();
      return;
    }

    elements.serverRowsBody.innerHTML = rows.map(function (row, index) {
      const normalizedRow = normalizeWorkbookEntry(row, columns);
      const values = normalizedRow.values;
      const entrySource = normalizedRow.entry_source || 'manual';
      const rowMode = entrySource === 'document_import' ? 'imported' : 'manual';
      return [
        '<tr data-server-row-index="' + String(index) + '" class="bookkeeping-offline-workbook-row bookkeeping-offline-workbook-row--' + escapeHtml(rowMode) + '">',
        '<td class="bookkeeping-offline-workbook-table__meta"><span class="bookkeeping-offline-workbook-table__index">' + escapeHtml(String(index + 1)) + '</span><span class="bookkeeping-offline-workbook-table__tag bookkeeping-offline-workbook-table__tag--' + escapeHtml(rowMode) + '">' + escapeHtml(rowMode === 'imported' ? 'Imported' : 'Saved') + '</span><span class="bookkeeping-offline-workbook-table__stamp">' + escapeHtml(normalizedRow.created_at_display || formatDateTime(normalizedRow.created_at)) + '</span></td>',
        columns.map(function (column) {
          return '<td>' + escapeHtml(values[column] || '');
        }).join(''),
        '</tr>',
      ].join('');
    }).join('');

    renderServerPagination();
  }

  function queueItemStatusLabel(item) {
    if (item.status === 'syncing') {
      return 'Syncing';
    }
    if (item.status === 'failed') {
      return 'Needs attention';
    }
    return 'Queued';
  }

  function queueItemTitle(item) {
    if (item.kind === 'workspace_grid') {
      return 'Workbook snapshot';
    }
    if (item.kind === 'workspace') {
      return 'Workspace row';
    }
    return item.fileName || 'Bookkeeping upload';
  }

  function queueItemDescription(item) {
    if (item.kind === 'workspace_grid') {
      const rowCount = Number(item.rowCount || 0);
      const columnCount = Number(item.columnCount || 0);
      return String(rowCount) + ' row(s) across ' + String(columnCount) + ' column(s)';
    }
    if (item.kind === 'workspace') {
      const values = item.values || {};
      const fragments = Object.keys(values).filter(function (column) {
        return String(values[column] || '').trim();
      }).slice(0, 4).map(function (column) {
        return column + ': ' + values[column];
      });
      return fragments.length ? fragments.join(' | ') : 'Blank row payload';
    }

    const fragments = [];
    fragments.push(formatFileSize(item.fileSize || 0));
    if (item.combineRelatedPages) {
      fragments.push('aligned batch');
    }
    if (item.documentDate) {
      fragments.push('document date ' + item.documentDate);
    }
    fragments.push(item.includeInWorkspace ? 'imports into live workbook' : 'digitize only');
    if (item.sourceLabel) {
      fragments.push(item.sourceLabel);
    }
    return fragments.join(' · ');
  }

  function renderQueue() {
    if (!elements.queueList || !elements.queueCountBadge) {
      return;
    }
    const queue = state.queue || [];
    const failedCount = queue.filter(function (item) {
      return item.status === 'failed';
    }).length;
    elements.queueCountBadge.textContent = String(queue.length) + ' queued';

    if (!queue.length) {
      elements.queueList.innerHTML = '<div class="bookkeeping-offline-empty">Nothing is waiting in the outbox.</div>';
      return;
    }

    elements.queueList.innerHTML = queue.map(function (item) {
      const kindLabel = item.kind === 'workspace_grid'
        ? 'Workbook'
        : (item.kind === 'workspace' ? 'Workspace' : 'Upload');
      return [
        '<article class="bookkeeping-offline-queue-item is-' + escapeHtml(item.status || 'queued') + '">',
        '<div>',
        '<span class="bookkeeping-offline-queue-item__kind">' + escapeHtml(kindLabel) + '</span>',
        '<h3 class="bookkeeping-offline-queue-item__title">' + escapeHtml(queueItemTitle(item)) + '</h3>',
        '<p class="bookkeeping-offline-queue-item__meta">' + escapeHtml(queueItemDescription(item)) + '</p>',
        '<p class="bookkeeping-offline-queue-item__meta">Queued ' + escapeHtml(formatDateTime(item.createdAt)) + '</p>',
        item.error ? '<p class="bookkeeping-offline-queue-item__error">' + escapeHtml(item.error) + '</p>' : '',
        '</div>',
        '<div class="bookkeeping-offline-queue-item__actions">',
        '<span class="bookkeeping-offline-pill ' + escapeHtml(item.status === 'failed' ? 'is-error' : item.status === 'syncing' ? 'is-ready' : '') + '">' + escapeHtml(queueItemStatusLabel(item)) + '</span>',
        item.status === 'failed' ? '<button type="button" class="btn btn-outline btn-sm" data-retry-id="' + escapeHtml(item.id) + '">Retry</button>' : '',
        '<button type="button" class="btn btn-outline btn-sm" data-remove-id="' + escapeHtml(item.id) + '">Remove</button>',
        '</div>',
        '</article>',
      ].join('');
    }).join('');

    if (state.isSyncing) {
      setStatusMessage('Syncing queued bookkeeping data now.', 'info');
    } else if (failedCount) {
      setStatusMessage(String(failedCount) + ' queued item(s) need attention before they can finish syncing.', 'warning');
    } else if (navigator.onLine) {
      setStatusMessage(String(queue.length) + ' queued item(s) will sync automatically.', 'info');
    } else {
      setStatusMessage('Offline. New bookkeeping rows and images will stay here until the connection returns.', 'warning');
    }
  }

  function renderLastSync() {
    if (!elements.lastSyncNote) {
      return;
    }
    if (state.lastSyncAt) {
      elements.lastSyncNote.textContent = 'Last sync finished ' + formatDateTime(state.lastSyncAt) + '.';
      return;
    }
    if (state.bootstrap && state.bootstrap.generated_at) {
      elements.lastSyncNote.textContent = 'Server snapshot captured ' + formatDateTime(state.bootstrap.generated_at) + '.';
      return;
    }
    elements.lastSyncNote.textContent = 'Server snapshot not refreshed yet.';
  }

  async function reloadQueue() {
    state.queue = await loadQueueItems();
    renderQueue();
  }

  async function applyBootstrap(nextBootstrap, options) {
    if (!nextBootstrap || nextBootstrap.ok === false) {
      return;
    }
    const preserveDraft = !options || options.preserveDraft !== false;
    const draftRows = preserveDraft ? captureWorkbookRows() : [];
    state.bootstrap = nextBootstrap;
    renderSummary();
    renderWorkbookEditor(draftRows);
    renderWorkspaceSnapshot();
    renderLastSync();
    if (state.db) {
      await saveBootstrap(nextBootstrap);
    }
  }

  function extractBootstrap(payload) {
    if (!payload) {
      return null;
    }
    return payload.bootstrap || payload;
  }

  function isRetryableStatus(statusCode) {
    return statusCode === 0 || statusCode === 408 || statusCode === 425 || statusCode === 429 || statusCode >= 500;
  }

  async function parseSyncResponse(response, fallbackMessage) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (response.ok && payload && payload.ok !== false) {
      return {
        ok: true,
        payload: payload,
      };
    }

    const message = payload && (payload.message || (payload.failure_messages || []).join(' '))
      ? payload.message || (payload.failure_messages || []).join(' ')
      : fallbackMessage;

    return {
      ok: false,
      payload: payload,
      message: message,
      retryable: isRetryableStatus(response.status),
      stopSync: response.status === 401 || response.status === 403,
    };
  }

  async function syncWorkspaceItem(item) {
    try {
      const payload = item.kind === 'workspace_grid'
        ? {
            submission_id: item.id,
            rows: item.rows || [],
          }
        : {
            submission_id: item.id,
            row_id: item.rowId,
            created_at: item.createdAt,
            workspace_document_type_key: item.workspaceDocumentTypeKey || '',
            workspace_period_key: item.workspacePeriodKey || '',
            values: item.values || {},
          };
      const response = await fetch(state.bootstrap.sync.workspace_url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify(payload),
      });
      return parseSyncResponse(response, item.kind === 'workspace_grid' ? 'Could not sync the workbook snapshot.' : 'Could not sync the workspace row.');
    } catch (error) {
      return {
        ok: false,
        message: item.kind === 'workspace_grid'
          ? 'Connection dropped before the workbook snapshot synced.'
          : 'Connection dropped before the workspace row synced.',
        retryable: true,
        stopSync: false,
      };
    }
  }

  async function syncUploadItem(item) {
    try {
      const formData = new FormData();
      formData.append('submission_id', item.id);
      formData.append('group_id', item.groupId || item.id);
      if (item.combineRelatedPages) {
        formData.append('combine_related_pages', 'true');
      }
      formData.append('document_date', item.documentDate || '');
      formData.append('import_into_workspace', item.includeInWorkspace ? 'true' : 'false');
      formData.append('bookkeeping_image', item.fileBlob, item.fileName || 'bookkeeping-upload');

      const response = await fetch(state.bootstrap.sync.uploads_url, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: formData,
      });
      return parseSyncResponse(response, 'Could not sync the queued upload.');
    } catch (error) {
      return {
        ok: false,
        message: 'Connection dropped before the upload finished.',
        retryable: true,
        stopSync: false,
      };
    }
  }

  async function scheduleBackgroundSync() {
    if (!state.registration || !state.registration.sync || typeof state.registration.sync.register !== 'function') {
      return;
    }
    try {
      await state.registration.sync.register('kumbu-bookkeeping-outbox');
    } catch (error) {
      /* Background Sync is optional; online/visibility listeners still flush. */
    }
  }

  async function flushQueue(reason) {
    if (state.isSyncing || !state.db) {
      return;
    }
    if (!navigator.onLine) {
      renderConnectivity();
      if (reason) {
        setStatusMessage('Offline. The outbox will retry automatically when the connection returns.', 'warning');
      }
      return;
    }

    await reloadQueue();
    if (!state.queue.length) {
      setStatusMessage('All queued bookkeeping data has already synced.', 'success');
      return;
    }

    state.isSyncing = true;
    if (elements.syncNowBtn) {
      elements.syncNowBtn.disabled = true;
    }
    renderQueue();

    for (const item of state.queue.slice()) {
      const syncingItem = Object.assign({}, item, {
        status: 'syncing',
        updatedAt: new Date().toISOString(),
      });
      await saveQueueItem(syncingItem);
      await reloadQueue();

      const result = syncingItem.kind === 'workspace' || syncingItem.kind === 'workspace_grid'
        ? await syncWorkspaceItem(syncingItem)
        : await syncUploadItem(syncingItem);

      if (result.ok) {
        await removeQueueItem(syncingItem.id);
        state.lastSyncAt = new Date().toISOString();
        const nextBootstrap = extractBootstrap(result.payload);
        if (nextBootstrap) {
          await applyBootstrap(nextBootstrap);
        }
        continue;
      }

      const nextItem = Object.assign({}, syncingItem, {
        status: result.retryable ? 'queued' : 'failed',
        updatedAt: new Date().toISOString(),
        attempts: Number(syncingItem.attempts || 0) + 1,
        error: result.message || 'Sync failed.',
      });
      await saveQueueItem(nextItem);
      if (result.stopSync) {
        setStatusMessage(result.message || 'The session needs attention before syncing can continue.', 'error');
        break;
      }
    }

    state.isSyncing = false;
    if (elements.syncNowBtn) {
      elements.syncNowBtn.disabled = false;
    }
    await reloadQueue();
    renderLastSync();

    if (!state.queue.length) {
      setStatusMessage('All queued bookkeeping data has synced.', 'success');
    }
  }

  async function refreshBootstrap(options) {
    if (!navigator.onLine) {
      if (!options || !options.quiet) {
        setStatusMessage('Offline. Using the most recent cached bookkeeping snapshot.', 'warning');
      }
      return;
    }

    try {
      const response = await fetch(state.bootstrap.sync.bootstrap_url, {
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const result = await parseSyncResponse(response, 'Could not refresh the server snapshot.');
      if (!result.ok) {
        throw new Error(result.message || 'Could not refresh the server snapshot.');
      }
      state.lastSyncAt = new Date().toISOString();
      await applyBootstrap(extractBootstrap(result.payload));
      if (!options || !options.quiet) {
        setStatusMessage('Server snapshot refreshed.', 'success');
      }
    } catch (error) {
      if (!options || !options.quiet) {
        setStatusMessage(error.message || 'Could not refresh the server snapshot.', 'error');
      }
    }
  }

  function buildLocalWorkbookEntries(rows) {
    const columns = currentWorkspaceColumns();
    return rows.filter(function (row) {
      return workbookRowHasValues(row.values || {});
    }).map(function (row) {
      const normalizedRow = normalizeWorkbookEntry(row, columns);
      const entrySource = normalizedRow.entry_source || 'manual';
      const createdAt = normalizedRow.created_at || new Date().toISOString();
      return {
        row_id: normalizedRow.row_id || makeId('row'),
        created_at: createdAt,
        updated_at: new Date().toISOString(),
        created_at_display: entrySource === 'document_import' && normalizedRow.source_row_number
          ? 'Document row ' + String(normalizedRow.source_row_number)
          : formatDateTime(createdAt),
        entry_source: entrySource,
        source_document_id: normalizedRow.source_document_id || '',
        source_row_number: Number(normalizedRow.source_row_number || 0) || 0,
        workspace_document_type_key: normalizedRow.workspace_document_type_key || defaultWorkspaceTypeKey(),
        workspace_period_key: normalizedRow.workspace_period_key || activeWorkspacePeriodKey(),
        values: normalizedRow.values,
      };
    });
  }

  async function queueWorkbookSnapshot(event) {
    event.preventDefault();
    if (!state.db) {
      setStatusMessage('Offline storage is not ready yet.', 'error');
      return;
    }

    const rows = captureWorkbookRows();
    const visibleRows = rows.filter(function (row) {
      return workbookRowHasValues(row.values || {});
    });

    if (!rows.length) {
      setStatusMessage('Enter at least one value in the workbook before queueing a snapshot.', 'warning');
      return;
    }

    const createdAt = new Date().toISOString();
    await saveQueueItem({
      id: makeId('workbook'),
      kind: 'workspace_grid',
      status: 'queued',
      attempts: 0,
      createdAt: createdAt,
      updatedAt: createdAt,
      rows: rows,
      rowCount: visibleRows.length,
      columnCount: currentWorkspaceColumns().length,
      error: '',
    });

    const nextBootstrap = JSON.parse(JSON.stringify(state.bootstrap || {}));
    nextBootstrap.workspace = nextBootstrap.workspace || {};
    nextBootstrap.workspace.entries = buildLocalWorkbookEntries(rows);
    nextBootstrap.workspace.row_count = nextBootstrap.workspace.entries.length;
    await applyBootstrap(nextBootstrap, { preserveDraft: false });
    await reloadQueue();
    setStatusMessage('Workbook snapshot added to the outbox.', 'success');
    await scheduleBackgroundSync();
    await flushQueue('workspace');
  }

  async function queueFiles(fileList, sourceLabel) {
    if (!state.db) {
      setStatusMessage('Offline storage is not ready yet.', 'error');
      return;
    }
    const files = Array.from(fileList || []).filter(Boolean);
    if (!files.length) {
      return;
    }

    const maxFiles = Number((state.bootstrap.sync && state.bootstrap.sync.max_files) || 5);
    if (files.length > maxFiles) {
      setStatusMessage('Queue up to ' + String(maxFiles) + ' files at a time.', 'warning');
      return;
    }

    const documentDate = String(elements.offlineDocumentDate ? elements.offlineDocumentDate.value : '').trim();
    if (!documentDate) {
      setStatusMessage('Choose the document date before queueing files so Kumbu can place the document in the correct workbook month.', 'warning');
      if (elements.offlineDocumentDate) {
        elements.offlineDocumentDate.focus();
      }
      return;
    }

    const includeInWorkspace = Boolean(elements.offlineImportIntoWorkspace && elements.offlineImportIntoWorkspace.checked);

    const createdAt = new Date().toISOString();
    const combineRelatedPages = Boolean(elements.offlineCombinePages && elements.offlineCombinePages.checked);
    const groupId = combineRelatedPages ? makeId('batch') : '';

    for (const file of files) {
      const submissionId = makeId('upload');
      await saveQueueItem({
        id: submissionId,
        kind: 'upload',
        status: 'queued',
        attempts: 0,
        createdAt: createdAt,
        updatedAt: createdAt,
        combineRelatedPages: combineRelatedPages,
        groupId: groupId || submissionId,
        fileName: file.name || 'bookkeeping-upload',
        fileType: file.type || 'application/octet-stream',
        fileSize: Number(file.size || 0),
        fileBlob: file,
        documentDate: documentDate,
        includeInWorkspace: includeInWorkspace,
        sourceLabel: sourceLabel,
        error: '',
      });
    }

    await reloadQueue();
    setStatusMessage(String(files.length) + ' file(s) added to the outbox.', 'success');
    await scheduleBackgroundSync();
    await flushQueue('upload');
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
      renderOfflineReady('Offline caching unavailable', 'is-error');
      return;
    }

    try {
      state.registration = await navigator.serviceWorker.register(state.bootstrap.sync.sw_url, {
        scope: state.bootstrap.sync.app_url,
      });
      await navigator.serviceWorker.ready;
      if (navigator.serviceWorker.controller) {
        renderOfflineReady('Offline ready', 'is-ready');
      } else {
        renderOfflineReady('Cached for next reopen', 'is-warning');
      }
      navigator.serviceWorker.addEventListener('message', function (event) {
        if (!event.data || event.data.type !== 'BOOKKEEPING_OFFLINE_SYNC') {
          return;
        }
        flushQueue('background');
      });
      await scheduleBackgroundSync();
    } catch (error) {
      renderOfflineReady('Offline cache failed', 'is-error');
    }
  }

  async function handleQueueAction(event) {
    const retryButton = event.target.closest('[data-retry-id]');
    if (retryButton) {
      const itemId = retryButton.getAttribute('data-retry-id');
      const item = state.queue.find(function (candidate) {
        return candidate.id === itemId;
      });
      if (!item) {
        return;
      }
      item.status = 'queued';
      item.error = '';
      item.updatedAt = new Date().toISOString();
      await saveQueueItem(item);
      await reloadQueue();
      await scheduleBackgroundSync();
      await flushQueue('retry');
      return;
    }

    const removeButton = event.target.closest('[data-remove-id]');
    if (!removeButton) {
      return;
    }
    const itemId = removeButton.getAttribute('data-remove-id');
    await removeQueueItem(itemId);
    await reloadQueue();
    if (!state.queue.length) {
      setStatusMessage('The outbox is empty.', 'info');
    }
  }

  async function initialize() {
    renderConnectivity();
    renderSummary();
    renderWorkbookEditor([]);
    renderWorkspaceSnapshot();
    renderLastSync();

    if (!window.indexedDB) {
      renderOfflineReady('Offline storage unsupported', 'is-error');
      setStatusMessage('This browser does not support IndexedDB, so offline bookkeeping cannot be enabled here.', 'error');
      return;
    }

    try {
      state.db = await openDatabase();
      await saveBootstrap(state.bootstrap);
      const cachedBootstrap = await loadBootstrap();
      if (cachedBootstrap) {
        const cachedDate = parseIso(cachedBootstrap.generated_at);
        const currentDate = parseIso(state.bootstrap.generated_at);
        if (cachedDate && (!currentDate || cachedDate > currentDate)) {
          await applyBootstrap(cachedBootstrap, { preserveDraft: false });
        }
      }
      await reloadQueue();
    } catch (error) {
      renderOfflineReady('Offline storage failed', 'is-error');
      setStatusMessage('IndexedDB could not be initialized for offline bookkeeping.', 'error');
      return;
    }

    if (elements.workspaceWorkbookForm) {
      elements.workspaceWorkbookForm.addEventListener('submit', queueWorkbookSnapshot);
    }
    if (elements.workspaceWorkbookMeta) {
      elements.workspaceWorkbookMeta.addEventListener('change', function (event) {
        const select = event.target.closest('[data-workspace-period-select]');
        if (!select) {
          return;
        }
        state.activeWorkspacePeriodKey = String(select.value || '').trim();
        writeStoredWorkspacePeriod(state.activeWorkspacePeriodKey);
        state.pagination.workspacePage = 1;
        renderWorkbookEditor(captureWorkbookRows());
      });
    }
    if (elements.workspacePrevPageBtn) {
      elements.workspacePrevPageBtn.addEventListener('click', function () {
        state.pagination.workspacePage = Math.max(1, state.pagination.workspacePage - 1);
        renderWorkspacePagination();
      });
    }
    if (elements.workspaceNextPageBtn) {
      elements.workspaceNextPageBtn.addEventListener('click', function () {
        state.pagination.workspacePage += 1;
        renderWorkspacePagination();
      });
    }
    if (elements.offlineWorkspaceScrollLeft && elements.offlineWorkspaceTableScroll) {
      elements.offlineWorkspaceScrollLeft.addEventListener('click', function () {
        elements.offlineWorkspaceTableScroll.scrollBy({ left: -240, behavior: 'smooth' });
      });
    }
    if (elements.offlineWorkspaceScrollRight && elements.offlineWorkspaceTableScroll) {
      elements.offlineWorkspaceScrollRight.addEventListener('click', function () {
        elements.offlineWorkspaceTableScroll.scrollBy({ left: 240, behavior: 'smooth' });
      });
    }
    if (elements.queueList) {
      elements.queueList.addEventListener('click', handleQueueAction);
    }
    if (elements.syncNowBtn) {
      elements.syncNowBtn.addEventListener('click', function () {
        flushQueue('manual');
      });
    }
    if (elements.refreshServerBtn) {
      elements.refreshServerBtn.addEventListener('click', function () {
        refreshBootstrap({ quiet: false });
      });
    }
    if (elements.serverPrevPageBtn) {
      elements.serverPrevPageBtn.addEventListener('click', function () {
        state.pagination.serverPage = Math.max(1, state.pagination.serverPage - 1);
        renderServerPagination();
      });
    }
    if (elements.serverNextPageBtn) {
      elements.serverNextPageBtn.addEventListener('click', function () {
        state.pagination.serverPage += 1;
        renderServerPagination();
      });
    }
    if (elements.offlineCameraInput) {
      elements.offlineCameraInput.addEventListener('change', async function (event) {
        await queueFiles(event.target.files, 'camera capture');
        event.target.value = '';
      });
    }
    if (elements.offlineFileInput) {
      elements.offlineFileInput.addEventListener('change', async function (event) {
        await queueFiles(event.target.files, 'file picker');
        event.target.value = '';
      });
    }

    window.addEventListener('online', function () {
      renderConnectivity();
      setStatusMessage('Connection restored. Syncing the outbox now.', 'success');
      flushQueue('online');
    });
    window.addEventListener('offline', function () {
      renderConnectivity();
      setStatusMessage('Offline. New bookkeeping rows and images will stay in the outbox until the connection returns.', 'warning');
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && navigator.onLine) {
        refreshBootstrap({ quiet: true });
        flushQueue('visible');
      }
    });

    await registerServiceWorker();
    await refreshBootstrap({ quiet: true });
    await flushQueue('startup');
  }

  initialize();
})();