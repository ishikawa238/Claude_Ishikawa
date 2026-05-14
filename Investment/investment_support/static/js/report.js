// report.js — Dashboard ページのチャート描画

// フォールバック表示ヘルパー
function showChartError(canvasId, msg) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const parent = canvas.parentElement;
  canvas.style.display = 'none';
  const el = document.createElement('div');
  el.style.cssText = 'display:flex;align-items:center;justify-content:center;height:100%;color:#999;font-size:0.85rem';
  el.textContent = msg || 'データ取得失敗';
  parent.appendChild(el);
}

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
          borderColor: '#1976D2',
          backgroundColor: 'rgba(25,118,210,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 1
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 } } },
          y: { ticks: { callback: function(v) { return v + '%'; } } }
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
            backgroundColor: 'rgba(25,118,210,0.2)',
            borderColor: '#1976D2',
            pointBackgroundColor: '#1976D2'
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { r: { min: 0, max: 100, ticks: { stepSize: 20, font: { size: 9 } }, pointLabels: { font: { size: 9 } } } }
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
          backgroundColor: ['#1976D2', '#4CAF50', '#FF9800', '#D32F2F', '#9C27B0', '#00BCD4', '#795548', '#607D8B', '#E91E63', '#3F51B5']
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'right', labels: { font: { size: 10 } } } }
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
          { label: 'MA25', data: maData, borderColor: '#FF9800', borderWidth: 1, borderDash: [4, 4], pointRadius: 0, tension: 0.2 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { size: 10 } } } },
        scales: { x: { ticks: { maxTicksLimit: 6, font: { size: 10 } } }, y: { ticks: { font: { size: 10 } } } }
      }
    });
  }
  makeIndexChart('n225Chart', mhN225, mhN225Ma, '日経平均', '#D32F2F');
  makeIndexChart('sp500Chart', mhSp500, mhSp500Ma, 'S&P500', '#1976D2');

  // --- スクリーニングファネル ---
  fetch('/api/screening-funnel').then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function(data) {
    if (data.error) return;
    var f = data.funnel;
    document.getElementById('funnelDate').textContent = '(' + data.date + ')';

    new Chart(document.getElementById('funnelChart'), {
      type: 'bar',
      data: {
        labels: ['ユニバース', '流動性フィルタ通過', 'candidate', 'watch', '最終通知'],
        datasets: [{
          data: [f.universe, f.screened, f.candidate, f.watch, f.notified],
          backgroundColor: ['#90CAF9', '#42A5F5', '#4CAF50', '#FF9800', '#D32F2F'],
          borderRadius: 4
        }]
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function(ctx) { var v = ctx.raw; var pct = f.universe > 0 ? (v / f.universe * 100).toFixed(1) : '0'; return v.toLocaleString() + ' 件 (' + pct + '%)'; } } }
        },
        scales: { x: { type: 'logarithmic', ticks: { font: { size: 10 } } }, y: { ticks: { font: { size: 11 } } } }
      }
    });

    // パターンA
    var pa = data.pattern_a;
    var aLabels = [['MA200上昇', 'ma200_rising'], ['MA整列', 'ma_aligned'], ['52週高値近傍', 'high_52w_near'], ['出来高急増', 'volume_surge'], ['RSI適正', 'rsi_ok'], ['MA25上方', 'above_ma25']];
    var aHtml = '<table class="table table-sm table-bordered mb-0" style="font-size:0.8rem"><tbody>';
    aLabels.forEach(function(pair) {
      var cnt = pa[pair[1]]; var pct = pa.total > 0 ? (cnt / pa.total * 100).toFixed(1) : '0';
      var w = pa.total > 0 ? Math.round(cnt / pa.total * 100) : 0;
      aHtml += '<tr><td style="width:45%">' + pair[0] + '</td><td><div style="background:#E3F2FD;border-radius:4px;overflow:hidden"><div style="width:' + w + '%;background:#42A5F5;height:16px;border-radius:4px"></div></div></td><td style="width:25%;text-align:right">' + cnt + ' <span style="color:#999">(' + pct + '%)</span></td></tr>';
    });
    aHtml += '</tbody></table><div style="color:#999;font-size:0.7rem;margin-top:4px">対象: ' + pa.total.toLocaleString() + ' 銘柄</div>';
    document.getElementById('condATable').innerHTML = aHtml;

    // パターンB
    var pb = data.pattern_b;
    var bLabels = [['MA200上昇', 'ma200_rising'], ['MA整列', 'ma_aligned'], ['押し目タッチ', 'pullback_touched'], ['反発確認', 'bounced'], ['MA25上昇', 'ma25_rising'], ['RSI適正', 'rsi_ok']];
    var bHtml = '<table class="table table-sm table-bordered mb-0" style="font-size:0.8rem"><tbody>';
    bLabels.forEach(function(pair) {
      var cnt = pb[pair[1]]; var pct = pb.total > 0 ? (cnt / pb.total * 100).toFixed(1) : '0';
      var w = pb.total > 0 ? Math.round(cnt / pb.total * 100) : 0;
      bHtml += '<tr><td style="width:45%">' + pair[0] + '</td><td><div style="background:#FFF3E0;border-radius:4px;overflow:hidden"><div style="width:' + w + '%;background:#FF9800;height:16px;border-radius:4px"></div></div></td><td style="width:25%;text-align:right">' + cnt + ' <span style="color:#999">(' + pct + '%)</span></td></tr>';
    });
    bHtml += '</tbody></table><div style="color:#999;font-size:0.7rem;margin-top:4px">対象: ' + pb.total.toLocaleString() + ' 銘柄</div>';
    document.getElementById('condBTable').innerHTML = bHtml;

    // スコア分布
    var sd = data.score_distribution;
    var buckets = ['0-19', '20-39', '40-59', '60-79', '80-100'];
    var bucketColors = ['#BDBDBD', '#90CAF9', '#42A5F5', '#FF9800', '#4CAF50'];
    new Chart(document.getElementById('scoreDistChart'), {
      type: 'bar',
      data: {
        labels: buckets,
        datasets: [{
          data: buckets.map(function(b) { return sd[b] || 0; }),
          backgroundColor: bucketColors,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { font: { size: 10 } } }, y: { ticks: { font: { size: 10 }, stepSize: 1 } } }
      }
    });
  }).catch(function() {
    showChartError('funnelChart', 'ファネルデータの取得に失敗しました');
    document.getElementById('condATable').textContent = 'データ取得失敗';
    document.getElementById('condBTable').textContent = 'データ取得失敗';
    showChartError('scoreDistChart', 'データ取得失敗');
  });
}
