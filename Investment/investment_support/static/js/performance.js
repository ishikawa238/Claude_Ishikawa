// performance.js — パフォーマンスページのチャートとフォーム

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
