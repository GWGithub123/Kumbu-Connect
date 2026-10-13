(function () {
  'use strict';

  /* ── Info tips ─────────────────────────────────────────────── */
  var openTip = null;

  function placeTip(tip) {
    var bubble = tip.querySelector('.info-tip__bubble');
    if (!bubble) {
      return;
    }
    bubble.style.removeProperty('--tip-shift');
    tip.classList.remove('info-tip--below');
    var rect = bubble.getBoundingClientRect();
    var margin = 10;
    var shift = 0;
    if (rect.left < margin) {
      shift = margin - rect.left;
    } else if (rect.right > window.innerWidth - margin) {
      shift = (window.innerWidth - margin) - rect.right;
    }
    if (shift) {
      bubble.style.setProperty('--tip-shift', shift + 'px');
    }
    if (rect.top < margin + 56) {
      tip.classList.add('info-tip--below');
    }
  }

  function closeTip(tip) {
    if (!tip) {
      return;
    }
    tip.classList.remove('is-open');
    var trigger = tip.querySelector('.info-tip__trigger');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
    }
    if (openTip === tip) {
      openTip = null;
    }
  }

  function openTipFor(tip) {
    if (openTip && openTip !== tip) {
      closeTip(openTip);
    }
    tip.classList.add('is-open');
    var trigger = tip.querySelector('.info-tip__trigger');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'true');
    }
    openTip = tip;
    placeTip(tip);
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest ? event.target.closest('.info-tip__trigger') : null;
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      var tip = trigger.closest('[data-info-tip]');
      if (tip.classList.contains('is-open')) {
        closeTip(tip);
      } else {
        openTipFor(tip);
      }
      return;
    }
    if (openTip && !(event.target.closest && event.target.closest('[data-info-tip]'))) {
      closeTip(openTip);
    }
  }, true);

  document.addEventListener('mouseover', function (event) {
    var tip = event.target.closest ? event.target.closest('[data-info-tip]') : null;
    if (tip && window.matchMedia('(hover: hover)').matches) {
      placeTip(tip);
    }
  });

  document.addEventListener('focusin', function (event) {
    var tip = event.target.closest ? event.target.closest('[data-info-tip]') : null;
    if (tip) {
      placeTip(tip);
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && openTip) {
      var trigger = openTip.querySelector('.info-tip__trigger');
      closeTip(openTip);
      if (trigger) {
        trigger.focus();
      }
    }
  });

  window.addEventListener('scroll', function () {
    if (openTip) {
      closeTip(openTip);
    }
  }, { passive: true });

  /* ── Interface translation ─────────────────────────────────── */
  var root = document.documentElement;
  var seedNode = document.getElementById('kumbu-i18n-seed');
  var seed = null;
  try {
    seed = seedNode ? JSON.parse(seedNode.textContent || '{}') : null;
  } catch (error) {
    seed = null;
  }

  function reveal() {
    root.removeAttribute('data-i18n-pending');
  }

  var lang = (seed && seed.lang) || '';
  if (!lang || lang === 'en') {
    reveal();
    try {
      localStorage.setItem('kumbu.lang', 'en');
    } catch (error) { /* storage unavailable */ }
    return;
  }

  try {
    localStorage.setItem('kumbu.lang', lang);
  } catch (error) { /* storage unavailable */ }

  var STORAGE_KEY = 'kumbu.i18n.' + lang;
  var dictionary = {};
  try {
    dictionary = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {};
  } catch (error) {
    dictionary = {};
  }
  var seedMap = (seed && seed.map) || {};
  Object.keys(seedMap).forEach(function (key) {
    dictionary[key] = seedMap[key];
  });
  var outputs = new Set(Object.keys(dictionary).map(function (key) { return dictionary[key]; }));

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, CODE: 1, PRE: 1, svg: 1, SVG: 1, IFRAME: 1, CANVAS: 1 };
  var ATTRIBUTES = ['placeholder', 'title', 'aria-label', 'alt', 'data-unsaved-label', 'data-saved-label', 'data-confirm'];
  var HAS_LETTER = /[A-Za-z]/;
  var URLISH = /^(https?:\/\/|www\.|[\w.+-]+@[\w-]+\.[\w.]+$)/i;
  var pending = new Map();
  var requested = new Set();
  var flushTimer = null;

  function normalize(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function translatable(text) {
    return text && text.length <= 1500 && HAS_LETTER.test(text) && !URLISH.test(text);
  }

  function excluded(element) {
    for (var node = element; node && node.nodeType === 1; node = node.parentNode) {
      if (SKIP_TAGS[node.tagName]) {
        return true;
      }
      if (node.getAttribute('translate') === 'no' || (node.classList && node.classList.contains('notranslate'))) {
        return true;
      }
      if (node.isContentEditable) {
        return true;
      }
    }
    return false;
  }

  function queue(text, apply) {
    if (!pending.has(text)) {
      pending.set(text, []);
    }
    pending.get(text).push(apply);
  }

  function handleText(node) {
    var raw = node.nodeValue;
    var text = normalize(raw);
    if (!translatable(text) || outputs.has(text)) {
      return;
    }
    var apply = function (translated) {
      if (!translated || node.nodeValue !== raw) {
        return;
      }
      var lead = raw.match(/^\s*/)[0];
      var tail = raw.match(/\s*$/)[0];
      node.nodeValue = lead + translated + tail;
    };
    if (dictionary[text]) {
      apply(dictionary[text]);
    } else {
      queue(text, apply);
    }
  }

  function handleAttributes(element) {
    ATTRIBUTES.forEach(function (name) {
      if (!element.hasAttribute(name)) {
        return;
      }
      var raw = element.getAttribute(name);
      var text = normalize(raw);
      if (!translatable(text) || outputs.has(text)) {
        return;
      }
      var apply = function (translated) {
        if (translated && element.getAttribute(name) === raw) {
          element.setAttribute(name, translated);
        }
      };
      if (dictionary[text]) {
        apply(dictionary[text]);
      } else {
        queue(text, apply);
      }
    });
    if (element.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(element.type) && element.value) {
      var rawValue = element.value;
      var valueText = normalize(rawValue);
      if (translatable(valueText) && !outputs.has(valueText)) {
        var applyValue = function (translated) {
          if (translated && element.value === rawValue) {
            element.value = translated;
          }
        };
        if (dictionary[valueText]) {
          applyValue(dictionary[valueText]);
        } else {
          queue(valueText, applyValue);
        }
      }
    }
  }

  function walk(start) {
    if (!start) {
      return;
    }
    if (start.nodeType === 3) {
      if (start.parentNode && !excluded(start.parentNode)) {
        handleText(start);
      }
      return;
    }
    if (start.nodeType !== 1 || excluded(start)) {
      return;
    }
    handleAttributes(start);
    var walker = document.createTreeWalker(start, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (node.nodeType === 1) {
          return excluded(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var node = walker.nextNode();
    while (node) {
      if (node.nodeType === 3) {
        handleText(node);
      } else {
        handleAttributes(node);
      }
      node = walker.nextNode();
    }
  }

  function persist() {
    try {
      var keys = Object.keys(dictionary);
      if (keys.length > 4000) {
        var trimmed = {};
        keys.slice(keys.length - 4000).forEach(function (key) { trimmed[key] = dictionary[key]; });
        dictionary = trimmed;
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(dictionary));
    } catch (error) { /* storage full or unavailable */ }
  }

  function learn(map) {
    Object.keys(map || {}).forEach(function (source) {
      var translated = map[source];
      if (!translated) {
        return;
      }
      dictionary[source] = translated;
      outputs.add(translated);
      var appliers = pending.get(source);
      if (appliers) {
        observer.disconnect();
        appliers.forEach(function (apply) { apply(translated); });
        observe();
        pending.delete(source);
      }
    });
    persist();
  }

  function flush() {
    flushTimer = null;
    var batch = [];
    pending.forEach(function (_, text) {
      if (!requested.has(text) && batch.length < 60) {
        batch.push(text);
      }
    });
    if (!batch.length || !navigator.onLine) {
      return;
    }
    batch.forEach(function (text) { requested.add(text); });
    fetch('/i18n/translate', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang: lang, texts: batch }),
    }).then(function (response) {
      return response.ok ? response.json() : { translations: {} };
    }).then(function (data) {
      learn(data.translations || {});
      batch.forEach(function (text) {
        if (!dictionary[text]) {
          pending.delete(text);
        }
      });
      if (pending.size) {
        scheduleFlush();
      }
    }).catch(function () {
      batch.forEach(function (text) { requested.delete(text); });
    });
  }

  function scheduleFlush() {
    if (!flushTimer) {
      flushTimer = setTimeout(flush, 120);
    }
  }

  var observer = new MutationObserver(function (mutations) {
    observer.disconnect();
    mutations.forEach(function (mutation) {
      if (mutation.type === 'characterData') {
        walk(mutation.target);
      } else if (mutation.type === 'attributes') {
        if (!excluded(mutation.target)) {
          handleAttributes(mutation.target);
        }
      } else {
        mutation.addedNodes.forEach(walk);
      }
    });
    observe();
    if (pending.size) {
      scheduleFlush();
    }
  });

  function observe() {
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRIBUTES,
    });
  }

  function start() {
    walk(document.body);
    reveal();
    observe();
    if (pending.size) {
      scheduleFlush();
    }
    window.addEventListener('online', scheduleFlush);
  }

  if (document.body) {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start);
  }
})();
