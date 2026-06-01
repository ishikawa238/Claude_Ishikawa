// screening.js — Screening ページのフィルタ・ソート・ブックマーク

var _urlFilter = new URLSearchParams(window.location.search).get('filter');
var _validLabels = ['candidate', 'watch', 'skip', 'all'];
var filterLabel = (_urlFilter && _validLabels.indexOf(_urlFilter) >= 0) ? _urlFilter : 'candidate';
var filterMarket = 'all';
var filterPattern = 'all';
var filterBookmarkOnly = false;
var sortAsc = false;
var sortPriceAsc = false;
var bookmarkTickers = new Set();

// URL パラメータで初期フィルタが指定された場合、ボタンのアクティブ状態を同期
if (_urlFilter && _validLabels.indexOf(_urlFilter) >= 0) {
  document.querySelectorAll('.filter-group')[1].querySelectorAll('.filter-btn').forEach(function(b) {
    b.classList.remove('active');
    if ((_urlFilter === 'candidate' && b.textContent === '候補') ||
        (_urlFilter === 'watch' && b.textContent === '監視') ||
        (_urlFilter === 'skip' && b.textContent === 'スキップ') ||
        (_urlFilter === 'all' && b.textContent === 'すべて')) {
      b.classList.add('active');
    }
  });
}

function syncBookmarkButtons() {
  document.querySelectorAll('.bookmark-btn').forEach(function(btn) {
    var t = (btn.dataset.ticker || '').toUpperCase();
    var on = bookmarkTickers.has(t);
    btn.classList.toggle('bookmark-btn-on', on);
    btn.textContent = on ? '★' : '☆';
    btn.title = on ? 'ブックマーク解除' : 'ブックマークに追加';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    var tr = btn.closest('tr');
    if (tr) tr.dataset.bookmarked = on ? '1' : '0';
  });
  var cntEl = document.getElementById('bookmarkCount');
  if (cntEl) cntEl.textContent = String(bookmarkTickers.size);
}

function loadBookmarksFromPage() {
  document.querySelectorAll('#screeningTable tbody tr').forEach(function(tr) {
    if (tr.dataset.bookmarked === '1') {
      var btn = tr.querySelector('.bookmark-btn');
      if (btn && btn.dataset.ticker) {
        bookmarkTickers.add(btn.dataset.ticker.toUpperCase());
      }
    }
  });
}

function toggleBookmarkFilter(btn) {
  filterBookmarkOnly = !filterBookmarkOnly;
  btn.classList.toggle('active', filterBookmarkOnly);
  applyFilters();
}

function setFilter(type, value, btn) {
  if (type === 'label') filterLabel = value;
  else if (type === 'market') filterMarket = value;
  else if (type === 'pattern') filterPattern = value;
  btn.parentElement.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  applyFilters();
}

function getPriceFilterBounds() {
  var minEl = document.getElementById('filterPriceMin');
  var maxEl = document.getElementById('filterPriceMax');
  var min = minEl && minEl.value !== '' ? parseFloat(minEl.value) : null;
  var max = maxEl && maxEl.value !== '' ? parseFloat(maxEl.value) : null;
  if (min !== null && isNaN(min)) min = null;
  if (max !== null && isNaN(max)) max = null;
  return { min: min, max: max };
}

function clearPriceFilter() {
  var minEl = document.getElementById('filterPriceMin');
  var maxEl = document.getElementById('filterPriceMax');
  if (minEl) minEl.value = '';
  if (maxEl) maxEl.value = '';
  applyFilters();
}

function applyFilters() {
  var rows = document.querySelectorAll('#screeningTable tbody tr');
  var priceBounds = getPriceFilterBounds();
  var shown = 0;
  rows.forEach(function(tr) {
    var matchLabel = (filterLabel === 'all' || tr.dataset.label === filterLabel);
    var matchMarket = (filterMarket === 'all' || tr.dataset.market === filterMarket);
    var matchPattern = (filterPattern === 'all' || tr.dataset.pattern === filterPattern);
    var matchBookmark = !filterBookmarkOnly || tr.dataset.bookmarked === '1';
    var matchPrice = true;
    if (priceBounds.min !== null || priceBounds.max !== null) {
      var raw = tr.dataset.priceJpy;
      var priceJpy = raw !== '' && raw !== undefined ? parseFloat(raw) : NaN;
      if (isNaN(priceJpy)) {
        matchPrice = false;
      } else {
        if (priceBounds.min !== null && priceJpy < priceBounds.min) matchPrice = false;
        if (priceBounds.max !== null && priceJpy > priceBounds.max) matchPrice = false;
      }
    }
    if (matchLabel && matchMarket && matchPattern && matchBookmark && matchPrice) {
      tr.style.display = '';
      shown++;
    } else {
      tr.style.display = 'none';
    }
  });
  document.getElementById('filterCount').textContent = shown + ' / ' + rows.length + ' 件表示中';
}

function onBookmarkClick(btn) {
  var ticker = (btn.dataset.ticker || '').trim();
  if (!ticker) return;

  btn.disabled = true;
  fetch('/api/screening-bookmarks/toggle', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ticker: ticker,
      name: btn.dataset.name || '',
      market: btn.dataset.market || '',
    }),
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.success) {
        alert(data.error || 'ブックマークの更新に失敗しました');
        return;
      }
      bookmarkTickers = new Set((data.tickers || []).map(function(t) { return t.toUpperCase(); }));
      syncBookmarkButtons();
      applyFilters();
    })
    .catch(function(err) {
      alert('通信エラー: ' + err.message);
    })
    .finally(function() {
      btn.disabled = false;
    });
}

document.getElementById('screeningTable').addEventListener('click', function(e) {
  var btn = e.target.closest('.bookmark-btn');
  if (btn) {
    e.preventDefault();
    onBookmarkClick(btn);
  }
});

function sortTableRows(compareFn) {
  var tbody = document.querySelector('#screeningTable tbody');
  var rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort(compareFn);
  rows.forEach(function(r) { tbody.appendChild(r); });
  applyFilters();
}

function parsePriceJpyForSort(tr, asc) {
  var raw = tr.dataset.priceJpy;
  if (raw === '' || raw === undefined) return asc ? Infinity : -Infinity;
  var v = parseFloat(raw);
  return isNaN(v) ? (asc ? Infinity : -Infinity) : v;
}

// Score ソート
document.getElementById('sortScore').addEventListener('click', function() {
  sortAsc = !sortAsc;
  sortTableRows(function(a, b) {
    var va = parseFloat(a.dataset.score) || 0;
    var vb = parseFloat(b.dataset.score) || 0;
    return sortAsc ? va - vb : vb - va;
  });
});

// 株価（円）ソート
document.getElementById('sortPriceJpy').addEventListener('click', function() {
  sortPriceAsc = !sortPriceAsc;
  sortTableRows(function(a, b) {
    var va = parsePriceJpyForSort(a, sortPriceAsc);
    var vb = parsePriceJpyForSort(b, sortPriceAsc);
    return sortPriceAsc ? va - vb : vb - va;
  });
});

function buildPromptUrl(tickers, theme) {
  var list = (tickers || []).filter(function(t) { return t; });
  if (!list.length) return null;
  var params = new URLSearchParams();
  params.set('from', 'screening');
  params.set('theme', theme || 'buy');
  params.set('tickers', list.join(','));
  return '/prompt-builder?' + params.toString();
}

function openPromptForTickers(tickers) {
  var url = buildPromptUrl(tickers, 'buy');
  if (url) {
    window.location.href = url;
  } else {
    alert('Prompt に送る銘柄がありません。フィルタを緩めるか、銘柄を選んでください。');
  }
}

function getVisibleTickers() {
  var tickers = [];
  document.querySelectorAll('#screeningTable tbody tr').forEach(function(tr) {
    if (tr.style.display === 'none') return;
    var t = tr.dataset.ticker;
    if (t) tickers.push(t);
  });
  return tickers;
}

function openPromptForVisible() {
  openPromptForTickers(getVisibleTickers());
}

function openPromptForBookmarks() {
  openPromptForTickers(Array.from(bookmarkTickers));
}

loadBookmarksFromPage();
syncBookmarkButtons();
applyFilters();
