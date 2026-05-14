// stock_detail.js — 個別銘柄ページのチャート描画

function initStockCharts(ticker) {
  // 価格チャート
  fetch('/api/price-history?ticker=' + ticker + '&days=60').then(function(r) { return r.json(); }).then(function(data) {
    new Chart(document.getElementById('priceChart'), {
      type: 'line',
      data: {
        labels: data.dates,
        datasets: [
          { label: '終値', data: data.close, borderColor: '#1976D2', borderWidth: 2, pointRadius: 0, tension: 0.2 },
          { label: 'MA25', data: data.ma25, borderColor: '#FF9800', borderWidth: 1, borderDash: [4, 4], pointRadius: 0, tension: 0.2 },
          { label: 'MA75', data: data.ma75, borderColor: '#9E9E9E', borderWidth: 1, borderDash: [2, 2], pointRadius: 0, tension: 0.2 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { size: 10 } } } },
        scales: { x: { ticks: { maxTicksLimit: 8, font: { size: 10 } } }, y: { ticks: { font: { size: 10 } } } }
      }
    });
  });

  // レーダーチャート
  fetch('/api/score-radar?ticker=' + ticker).then(function(r) { return r.json(); }).then(function(data) {
    new Chart(document.getElementById('stockRadar'), {
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
  });
}
