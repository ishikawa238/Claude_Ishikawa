// report.js — Dashboard ページのチャート描画

// 折りたたみパネルの開閉状態を localStorage に保存
function initDashboardPanels() {
  var STORAGE_PREFIX = 'dashboard.panel.';
  document.querySelectorAll('details.dashboard-panel[data-panel]').forEach(function(panel) {
    var key = STORAGE_PREFIX + panel.getAttribute('data-panel');
    var stored = localStorage.getItem(key);
    if (stored === 'open') panel.open = true;
    else if (stored === 'closed') panel.open = false;
    panel.addEventListener('toggle', function() {
      localStorage.setItem(key, panel.open ? 'open' : 'closed');
    });
  });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initDashboardPanels);
} else {
  initDashboardPanels();
}

// フォールバック表示ヘルパー
function showChartError(canvasId, msg) {
  var canvas = document.getElementById(canvasId);
  if (!canvas) return;
  var parent = canvas.parentElement;
  canvas.style.display = 'none';
  var el = document.createElement('div');
  el.style.cssText = 'display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:0.85rem';
  el.textContent = msg || 'データ取得失敗';
  parent.appendChild(el);
}

// ポートフォリオ リスクサマリー
function loadPortfolioRisk() {
  fetch('/api/portfolio-risk').then(function(r) { return r.json(); }).then(function(data) {
    if (!data || data.total_positions === 0) return;
    var widget = document.getElementById('riskWidget');
    if (widget) widget.style.display = '';

    // VaR
    var varEl = document.getElementById('riskVar');
    if (varEl && data.var_99_1d !== null) {
      varEl.textContent = '-' + data.var_99_1d + '%';
      varEl.className = 'value text-loss';
    }

    // Beta
    var betaEl = document.getElementById('riskBeta');
    if (betaEl && data.beta !== null) {
      betaEl.textContent = data.beta.toFixed(2);
      var hint = document.getElementById('riskBetaHint');
      if (data.beta > 1.2) {
        betaEl.className = 'value text-loss';
        if (hint) hint.textContent = '市場より変動が大きい';
      } else if (data.beta < 0.8) {
        betaEl.className = 'value text-profit';
        if (hint) hint.textContent = '市場より安定';
      } else {
        betaEl.className = 'value';
        if (hint) hint.textContent = '市場と同程度';
      }
    }

    // Sharpe
    var sharpeEl = document.getElementById('riskSharpe');
    if (sharpeEl && data.sharpe !== null) {
      sharpeEl.textContent = data.sharpe.toFixed(2);
      var shint = document.getElementById('riskSharpeHint');
      if (data.sharpe >= 1.0) {
        sharpeEl.className = 'value text-profit';
        if (shint) shint.textContent = '良好なリスク効率';
      } else if (data.sharpe >= 0) {
        sharpeEl.className = 'value';
        if (shint) shint.textContent = '改善の余地あり';
      } else {
        sharpeEl.className = 'value text-loss';
        if (shint) shint.textContent = 'リスクに見合わない';
      }
    }
  }).catch(function() {});
}
loadPortfolioRisk();

// 決算カレンダー
function loadEarningsCalendar() {
  fetch('/api/earnings-calendar').then(function(r) { return r.json(); }).then(function(data) {
    if (!data.events || data.events.length === 0) return;
    var widget = document.getElementById('earningsCalendar');
    var content = document.getElementById('earningsContent');
    if (!widget || !content) return;
    widget.style.display = '';

    var html = '<div class="d-flex flex-wrap gap-2">';
    data.events.forEach(function(e) {
      var cls = 'action-info';
      var urgency = '';
      if (e.days_until <= 3) {
        cls = 'action-danger';
        urgency = e.days_until === 0 ? '本日' : e.days_until + '日後';
      } else if (e.days_until <= 7) {
        cls = 'action-warn';
        urgency = e.days_until + '日後';
      } else {
        urgency = e.days_until + '日後';
      }
      html += '<div class="action-item ' + cls + '" style="flex:0 0 auto;min-width:180px">'
        + '<strong>' + e.ticker + '</strong> ' + e.name
        + '<br><span class="meta-small">' + e.date + '（' + urgency + '）</span>'
        + '</div>';
    });
    html += '</div>';
    content.innerHTML = html;
  }).catch(function() {});
}
loadEarningsCalendar();

// SL 今日の行動ワンクリック更新
(function() {
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.sl-apply-btn');
    if (!btn) return;
    btn.disabled = true;
    var ticker = btn.dataset.ticker;
    var newSl = parseFloat(btn.dataset.sl);
    btn.textContent = '更新中...';
    fetch('/api/holdings/update-sl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker, stop_loss_price: newSl })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.success) {
        btn.textContent = '変更済み ✔';
        btn.style.background = 'var(--success, #28a745)';
        btn.style.borderColor = 'var(--success, #28a745)';
        // 保有銘柄テーブルのSLセルも更新
        var slCell = document.querySelector('.sl-cell[data-ticker="' + ticker + '"]');
        if (slCell) {
          slCell.dataset.sl = newSl;
          var fmt = newSl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
          var display = slCell.querySelector('.sl-display');
          if (display) display.childNodes[0].textContent = fmt + ' ';
          var hint = slCell.querySelector('.sl-hint');
          if (hint) hint.style.display = 'none';
          slCell.querySelector('.sl-input').value = newSl;
        }
      } else {
        btn.textContent = '失敗: ' + (data.error || '不明');
        btn.style.background = 'var(--danger, #dc3545)';
        btn.style.borderColor = 'var(--danger, #dc3545)';
        btn.disabled = false;
      }
    })
    .catch(function() {
      btn.textContent = '通信エラー';
      btn.disabled = false;
    });
  });
})();

// SL インライン編集
(function() {
  document.addEventListener('click', function(e) {
    var display = e.target.closest('.sl-display');
    if (!display) return;
    var cell = display.closest('.sl-cell');
    if (!cell) return;
    display.style.display = 'none';
    cell.querySelector('.sl-edit').style.display = '';
    var input = cell.querySelector('.sl-input');
    input.focus();
    input.select();
  });
  document.addEventListener('click', function(e) {
    var cancelBtn = e.target.closest('.sl-cancel-btn');
    if (!cancelBtn) return;
    var cell = cancelBtn.closest('.sl-cell');
    _slRestore(cell);
  });
  document.addEventListener('click', function(e) {
    var saveBtn = e.target.closest('.sl-save-btn');
    if (!saveBtn) return;
    var cell = saveBtn.closest('.sl-cell');
    _slSave(cell);
  });
  document.addEventListener('keydown', function(e) {
    if (!e.target.classList.contains('sl-input')) return;
    var cell = e.target.closest('.sl-cell');
    if (e.key === 'Enter') { e.preventDefault(); _slSave(cell); }
    if (e.key === 'Escape') { _slRestore(cell); }
  });

  function _slRestore(cell) {
    var input = cell.querySelector('.sl-input');
    input.value = cell.dataset.sl;
    cell.querySelector('.sl-edit').style.display = 'none';
    cell.querySelector('.sl-display').style.display = '';
  }

  function _slSave(cell) {
    var input = cell.querySelector('.sl-input');
    var val = parseFloat(input.value);
    if (isNaN(val) || val <= 0) { input.style.borderColor = 'red'; return; }
    cell.classList.add('sl-saving');
    fetch('/api/holdings/update-sl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: cell.dataset.ticker, stop_loss_price: val })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      cell.classList.remove('sl-saving');
      if (data.success) {
        cell.dataset.sl = val;
        var fmt = val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        cell.querySelector('.sl-display').childNodes[0].textContent = fmt + ' ';
        var hint = cell.querySelector('.sl-hint');
        if (hint && parseFloat(hint.textContent.replace('↑','')) <= val) hint.style.display = 'none';
        cell.querySelector('.sl-edit').style.display = 'none';
        cell.querySelector('.sl-display').style.display = '';
        cell.classList.add('sl-ok');
        setTimeout(function() { cell.classList.remove('sl-ok'); }, 700);
      } else {
        alert(data.error || '更新に失敗しました');
      }
    })
    .catch(function() {
      cell.classList.remove('sl-saving');
      alert('通信エラーが発生しました');
    });
  }
})();

function initReportCharts(sectorLabels, sectorValues, mhDates, mhN225, mhN225Ma, mhSp500, mhSp500Ma, mhModes) {
  // --- 損益推移チャート ---
  fetch('/api/portfolio-pnl').then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function(data) {
    new Chart(document.getElementById('pnlChart'), {
      type: 'line',
      data: {
        labels: data.dates,
        datasets: [{
          label: '損益 (%)',
          data: data.pnl_pct,
          borderColor: ChartTheme.primary(),
          backgroundColor: ChartTheme.primaryAlpha(0.1),
          fill: true,
          tension: 0.3,
          pointRadius: 1
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
          y: { ticks: { callback: function(v) { return v + '%'; }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
        }
      }
    });
  }).catch(function() { showChartError('pnlChart', '損益データの取得に失敗しました'); });

  // --- レーダーチャート ---
  window.radarChartInstance = null;
  window.loadRadar = function() {
    var ticker = document.getElementById('radarTicker');
    if (!ticker) return;
    ticker = ticker.value || '';
    if (!ticker) return;
    fetch('/api/score-radar?ticker=' + ticker).then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function(data) {
      if (window.radarChartInstance) window.radarChartInstance.destroy();
      window.radarChartInstance = new Chart(document.getElementById('radarChart'), {
        type: 'radar',
        data: {
          labels: [['トレンド'], ['パターンA', '(ブレイクアウト)'], ['パターンB', '(押し目買い)'], ['流動性']],
          datasets: [{
            data: [data.trend, data.pattern_a, data.pattern_b, data.liquidity],
            backgroundColor: ChartTheme.primaryAlpha(0.15),
            borderColor: ChartTheme.primary(),
            pointBackgroundColor: ChartTheme.primary()
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { r: { min: 0, max: 100, ticks: { stepSize: 20, font: { size: 9 }, color: ChartTheme.textColor() }, pointLabels: { font: { size: 9 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } } }
        }
      });
    }).catch(function() { showChartError('radarChart', 'スコアデータの取得に失敗しました'); });
  };
  loadRadar();

  // --- セクターチャート ---
  if (sectorLabels.length > 0) {
    new Chart(document.getElementById('sectorChart'), {
      type: 'doughnut',
      data: {
        labels: sectorLabels,
        datasets: [{
          data: sectorValues,
          backgroundColor: ChartTheme.sectorPalette()
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'right', labels: { font: { size: 10 }, color: ChartTheme.textColor() } } }
      }
    });
  }

  // --- 市場環境チャート ---
  function makeIndexChart(canvasId, data, maData, label, color) {
    new Chart(document.getElementById(canvasId), {
      type: 'line',
      data: {
        labels: mhDates,
        datasets: [
          { label: label, data: data, borderColor: color, borderWidth: 2, pointRadius: 0, tension: 0.2 },
          { label: 'MA25', data: maData, borderColor: ChartTheme.maLine(), borderWidth: 1, borderDash: [4, 4], pointRadius: 0, tension: 0.2 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { size: 10 }, color: ChartTheme.textColor() } } },
        scales: {
          x: { ticks: { maxTicksLimit: 6, font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
          y: { ticks: { font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
        }
      }
    });
  }
  makeIndexChart('n225Chart', mhN225, mhN225Ma, '日経平均', ChartTheme.danger());
  makeIndexChart('sp500Chart', mhSp500, mhSp500Ma, 'S&P500', ChartTheme.primary());

  // --- スクリーニングファネル（スイング） ---
  fetch('/api/screening-funnel').then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function(data) {
    if (data.error) return;
    var f = data.funnel;
    var sw = data.swing || {};
    document.getElementById('funnelDate').textContent = '(' + data.date + ')';

    new Chart(document.getElementById('funnelChart'), {
      type: 'bar',
      data: {
        labels: ['ユニバース', '流動性通過', '①トレンド', '②RSI', '③MACD', '候補', 'あと1条件'],
        datasets: [{
          data: [
            f.universe,
            f.screened,
            sw.uptrend || 0,
            sw.rsi_oversold || 0,
            sw.macd_confirm || 0,
            f.candidate,
            f.watch
          ],
          backgroundColor: ChartTheme.funnelPalette(),
          borderRadius: 4
        }]
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function(ctx) { var v = ctx.raw; var pct = f.universe > 0 ? (v / f.universe * 100).toFixed(1) : '0'; return v.toLocaleString() + ' 件 (' + pct + '%)'; } } }
        },
        scales: {
          x: { type: 'logarithmic', ticks: { font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
          y: { ticks: { font: { size: 11 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
        }
      }
    });

    var swingLabels = [['EMA25>EMA75', 'uptrend'], ['RSI売られ過ぎ', 'rsi_oversold'], ['MACD確認', 'macd_confirm']];
    var total = sw.total || f.screened || 1;
    var sHtml = '<table class="table table-sm table-bordered mb-0" style="font-size:0.8rem"><tbody>';
    swingLabels.forEach(function(pair) {
      var cnt = sw[pair[1]] || 0;
      var pct = total > 0 ? (cnt / total * 100).toFixed(1) : '0';
      var w = total > 0 ? Math.round(cnt / total * 100) : 0;
      sHtml += '<tr><td style="width:45%">' + pair[0] + '</td><td><div class="funnel-a-bg" style="border-radius:4px;overflow:hidden"><div class="funnel-a-bar" style="width:' + w + '%;height:16px;border-radius:4px"></div></div></td><td style="width:25%;text-align:right">' + cnt + ' <span class="text-muted-custom">(' + pct + '%)</span></td></tr>';
    });
    sHtml += '</tbody></table><div class="meta-small" style="margin-top:4px">対象: ' + total.toLocaleString() + ' 銘柄（スイング戦略）</div>';
    document.getElementById('swingCondTable').innerHTML = sHtml;

    var cm = sw.cond_met || {};
    var metBuckets = ['0', '1', '2', '3'];
    new Chart(document.getElementById('condMetChart'), {
      type: 'bar',
      data: {
        labels: metBuckets.map(function(b) { return b + '/3'; }),
        datasets: [{
          data: metBuckets.map(function(b) { return cm[b] || 0; }),
          backgroundColor: ['#94a3b8', '#fbbf24', '#fb923c', '#22c55e'],
          borderRadius: 4
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
          y: { ticks: { font: { size: 10 }, stepSize: 1, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
        }
      }
    });
  }).catch(function() {
    showChartError('funnelChart', 'ファネルデータの取得に失敗しました');
    document.getElementById('swingCondTable').textContent = 'データ取得失敗';
    showChartError('condMetChart', 'データ取得失敗');
  });
}
