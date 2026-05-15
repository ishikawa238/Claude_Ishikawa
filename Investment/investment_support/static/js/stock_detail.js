// stock_detail.js — 個別銘柄ページのチャート描画

function initStockCharts(ticker) {
  // 価格チャート
  fetch('/api/price-history?ticker=' + ticker + '&days=60').then(function(r) { return r.json(); }).then(function(data) {
    new Chart(document.getElementById('priceChart'), {
      type: 'line',
      data: {
        labels: data.dates,
        datasets: [
          { label: '終値', data: data.close, borderColor: ChartTheme.primary(), borderWidth: 2, pointRadius: 0, tension: 0.2 },
          { label: 'MA25', data: data.ma25, borderColor: ChartTheme.maLine(), borderWidth: 1, borderDash: [4, 4], pointRadius: 0, tension: 0.2 },
          { label: 'MA75', data: data.ma75, borderColor: ChartTheme.maSecondary(), borderWidth: 1, borderDash: [2, 2], pointRadius: 0, tension: 0.2 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { size: 10 }, color: ChartTheme.textColor() } } },
        scales: {
          x: { ticks: { maxTicksLimit: 8, font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } },
          y: { ticks: { font: { size: 10 }, color: ChartTheme.textColor() }, grid: { color: ChartTheme.gridColor() } }
        }
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
  });
}
