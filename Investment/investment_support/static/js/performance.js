// performance.js — パフォーマンスページのチャートとフォーム

var _benchmarkChart = null;

function _fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  var sign = v >= 0 ? '+' : '';
  return sign + Number(v).toFixed(2) + '%';
}

function _setTrendClass(el, v) {
  if (!el || v == null || isNaN(v)) {
    el.classList.remove('trend-up', 'trend-down');
    return;
  }
  el.classList.toggle('trend-up', v >= 0);
  el.classList.toggle('trend-down', v < 0);
}

function loadBenchmarkComparison(days) {
  fetch('/api/benchmark-comparison?days=' + encodeURIComponent(days))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var emptyEl = document.getElementById('benchmarkEmpty');
      var panel = document.getElementById('benchmarkPanel');
      if (!data.dates || data.dates.length < 2) {
        emptyEl.textContent = (data.meta && data.meta.message)
          || '比較データがまだありません。朝・夜バッチ実行後に portfolio_snapshot が蓄積されます。';
        emptyEl.style.display = 'block';
        panel.style.display = 'none';
        if (_benchmarkChart) {
          _benchmarkChart.destroy();
          _benchmarkChart = null;
        }
        return;
      }
      emptyEl.style.display = 'none';
      panel.style.display = 'block';

      var s = data.summary || {};
      var pr = document.getElementById('bmPortfolioReturn');
      var pa = document.getElementById('bmPortfolioAdjReturn');
      var sp = document.getElementById('bmSp500Return');
      var ex = document.getElementById('bmExcessReturn');
      pr.textContent = _fmtPct(s.portfolio_return_pct);
      pa.textContent = _fmtPct(s.portfolio_adjusted_return_pct);
      sp.textContent = _fmtPct(s.sp500_return_pct);
      var excess = s.excess_adjusted_return_pct != null ? s.excess_adjusted_return_pct : s.excess_return_pct;
      ex.textContent = _fmtPct(excess);
      _setTrendClass(pr, s.portfolio_return_pct);
      _setTrendClass(pa, s.portfolio_adjusted_return_pct);
      _setTrendClass(sp, s.sp500_return_pct);
      _setTrendClass(ex, excess);

      var beatEl = document.getElementById('bmBeatLabel');
      if (s.beating_sp500 === true) {
        beatEl.textContent = 'この期間は S&P500 を上回っています';
      } else if (s.beating_sp500 === false) {
        beatEl.textContent = 'この期間は S&P500 を下回っています';
      } else {
        beatEl.textContent = '';
      }

      var meta = document.getElementById('benchmarkMeta');
      if (s.start_date && s.end_date) {
        meta.textContent = s.start_date + ' 〜 ' + s.end_date
          + ' ｜ 期間入金概算: ' + Number(s.estimated_deposits_jpy || 0).toLocaleString() + '円'
          + '（毎月 ' + Number(s.monthly_deposit_jpy || 0).toLocaleString() + '円）';
      }

      var ctx = document.getElementById('benchmarkChart');
      if (_benchmarkChart) _benchmarkChart.destroy();
      _benchmarkChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: data.dates,
          datasets: [
            {
              label: 'あなたの資産（入金控除後）',
              data: data.portfolio_adjusted_index,
              borderColor: ChartTheme.primary(),
              backgroundColor: ChartTheme.primaryAlpha(0.08),
              fill: false,
              tension: 0.25,
              pointRadius: 0,
              borderWidth: 2.5,
            },
            {
              label: 'S&P500',
              data: data.sp500_index,
              borderColor: ChartTheme.maSecondary(),
              fill: false,
              tension: 0.25,
              pointRadius: 0,
              borderWidth: 2,
              borderDash: [6, 4],
            },
            {
              label: 'あなたの資産（総資産・入金込み）',
              data: data.portfolio_index,
              borderColor: ChartTheme.primaryAlpha(0.45),
              fill: false,
              tension: 0.25,
              pointRadius: 0,
              borderWidth: 1.5,
              borderDash: [2, 3],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: ChartTheme.textColor(), boxWidth: 12 } },
            tooltip: {
              callbacks: {
                label: function(ctx) {
                  var v = ctx.parsed.y;
                  return ctx.dataset.label + ': ' + (v != null ? v.toFixed(1) : '—');
                },
              },
            },
          },
          scales: {
            x: {
              ticks: { maxTicksLimit: 8, font: { size: 10 }, color: ChartTheme.textColor() },
              grid: { color: ChartTheme.gridColor() },
            },
            y: {
              ticks: { callback: function(v) { return v; }, color: ChartTheme.textColor() },
              grid: { color: ChartTheme.gridColor() },
            },
          },
        },
      });
    })
    .catch(function(err) {
      document.getElementById('benchmarkEmpty').textContent = '読み込みエラー: ' + err.message;
      document.getElementById('benchmarkEmpty').style.display = 'block';
      document.getElementById('benchmarkPanel').style.display = 'none';
    });
}

(function initBenchmark() {
  var sel = document.getElementById('benchmarkDays');
  if (!sel) return;
  loadBenchmarkComparison(sel.value);
  sel.addEventListener('change', function() {
    loadBenchmarkComparison(sel.value);
  });
})();

// 累積損益
fetch('/api/perf-cumulative').then(function(r) { return r.json(); }).then(function(data) {
  var _cumJpy = data.cumulative_pnl_jpy || [];
  new Chart(document.getElementById('cumChart'), {
    type: 'line',
    data: {
      labels: data.dates,
      datasets: [{
        label: '累積損益 (%)',
        data: data.cumulative_pnl,
        borderColor: ChartTheme.primary(),
        backgroundColor: ChartTheme.primaryAlpha(0.1),
        fill: true, tension: 0.3, pointRadius: 2
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            afterLabel: function(ctx) {
              var jpy = _cumJpy[ctx.dataIndex];
              return jpy != null ? '円: ' + Number(jpy).toLocaleString() + '円' : null;
            }
          }
        }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 8, font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
        y: { ticks: { callback: function(v) { return v + '%'; }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
      }
    }
  });
});

// 月別損益
fetch('/api/perf-monthly').then(function(r) { return r.json(); }).then(function(data) {
  var _monthJpy = data.pnl_jpy || [];
  new Chart(document.getElementById('monthlyChart'), {
    type: 'bar',
    data: {
      labels: data.months,
      datasets: [{
        label: '月別損益 (%)',
        data: data.pnl,
        backgroundColor: data.pnl.map(function(v) { return ChartTheme.pnlColor(v); })
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            afterLabel: function(ctx) {
              var jpy = _monthJpy[ctx.dataIndex];
              return jpy != null ? '円: ' + Number(jpy).toLocaleString() + '円' : null;
            }
          }
        }
      },
      scales: {
        x: { ticks: { font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
        y: { ticks: { callback: function(v) { return v + '%'; }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
      }
    }
  });
});

// --- トレード記録フォーム ---
document.getElementById('tf_exit_date').value = new Date().toISOString().slice(0, 10);

var _sigTimer = null;
document.getElementById('tf_ticker').addEventListener('input', function() {
  clearTimeout(_sigTimer);
  var t = this.value.trim().toUpperCase();
  if (t.length < 1) return;
  _sigTimer = setTimeout(function() {
    fetch('/api/recent-signals?ticker=' + encodeURIComponent(t))
      .then(function(r) { return r.json(); })
      .then(function(signals) {
        var sel = document.getElementById('tf_signal');
        sel.innerHTML = '<option value="">シグナルなし（手動エントリー）</option>';
        signals.forEach(function(s) {
          var opt = document.createElement('option');
          opt.value = s.id;
          opt.textContent = '[' + s.signal_date + '] パターン' + s.pattern + ' エントリー' + s.entry_price;
          sel.appendChild(opt);
        });
        if (signals.length > 0) sel.value = signals[0].id;
      });
  }, 400);
});

document.getElementById('tradeForm').addEventListener('submit', function(e) {
  e.preventDefault();
  var resultDiv = document.getElementById('tf_result');
  resultDiv.style.display = 'none';

  var body = {
    ticker: document.getElementById('tf_ticker').value,
    signal_id: document.getElementById('tf_signal').value || null,
    actual_entry_price: document.getElementById('tf_entry_price').value || null,
    actual_entry_date: document.getElementById('tf_entry_date').value || null,
    actual_exit_price: document.getElementById('tf_exit_price').value,
    actual_exit_date: document.getElementById('tf_exit_date').value,
    exit_reason: document.getElementById('tf_reason').value,
    memo: document.getElementById('tf_memo').value,
  };

  fetch('/api/trade-result', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.success) {
      resultDiv.innerHTML = '<div class="alert alert-success">' + data.message + '</div>';
      resultDiv.style.display = 'block';
      setTimeout(function() { location.reload(); }, 1200);
    } else {
      resultDiv.innerHTML = '<div class="alert alert-danger">' + (data.error || 'エラーが発生しました') + '</div>';
      resultDiv.style.display = 'block';
    }
  })
  .catch(function(err) {
    resultDiv.innerHTML = '<div class="alert alert-danger">通信エラー: ' + err.message + '</div>';
    resultDiv.style.display = 'block';
  });
});
