// screening.js — Screening ページのフィルタとソート

var _urlFilter = new URLSearchParams(window.location.search).get('filter');
var _validLabels = ['candidate', 'watch', 'skip', 'all'];
var filterLabel = (_urlFilter && _validLabels.indexOf(_urlFilter) >= 0) ? _urlFilter : 'candidate';
var filterMarket = 'all';
var filterPattern = 'all';
var sortAsc = false;

// URL パラメータで初期フィルタが指定された場合、ボタンのアクティブ状態を同期
if (_urlFilter && _validLabels.indexOf(_urlFilter) >= 0) {
  document.querySelectorAll('.filter-group')[0].querySelectorAll('.filter-btn').forEach(function(b) {
    b.classList.remove('active');
    if ((_urlFilter === 'candidate' && b.textContent === '候補') ||
        (_urlFilter === 'watch' && b.textContent === '監視') ||
        (_urlFilter === 'skip' && b.textContent === 'スキップ') ||
        (_urlFilter === 'all' && b.textContent === 'すべて')) {
      b.classList.add('active');
    }
  });
}

function setFilter(type, value, btn) {
  if (type === 'label') filterLabel = value;
  else if (type === 'market') filterMarket = value;
  else if (type === 'pattern') filterPattern = value;
  btn.parentElement.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  applyFilters();
}

function applyFilters() {
  var rows = document.querySelectorAll('#screeningTable tbody tr');
  var shown = 0;
  rows.forEach(function(tr) {
    var matchLabel = (filterLabel === 'all' || tr.dataset.label === filterLabel);
    var matchMarket = (filterMarket === 'all' || tr.dataset.market === filterMarket);
    var matchPattern = (filterPattern === 'all' || tr.dataset.pattern === filterPattern);
    if (matchLabel && matchMarket && matchPattern) {
      tr.style.display = '';
      shown++;
    } else {
      tr.style.display = 'none';
    }
  });
  document.getElementById('filterCount').textContent = shown + ' / ' + rows.length + ' 件表示中';
}

// Score ソート
document.getElementById('sortScore').addEventListener('click', function() {
  var tbody = document.querySelector('#screeningTable tbody');
  var rows = Array.from(tbody.querySelectorAll('tr'));
  sortAsc = !sortAsc;
  rows.sort(function(a, b) {
    var va = parseFloat(a.dataset.score) || 0;
    var vb = parseFloat(b.dataset.score) || 0;
    return sortAsc ? va - vb : vb - va;
  });
  rows.forEach(function(r) { tbody.appendChild(r); });
  applyFilters();
});

// 初期表示
applyFilters();
