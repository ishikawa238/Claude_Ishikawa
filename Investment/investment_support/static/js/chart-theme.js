// chart-theme.js — Chart.js 用カラーパレットの一元管理
// CSS 変数から動的に色を取得する

var ChartTheme = (function() {
  function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  return {
    primary: function() { return getCSSVar('--chart-primary') || '#2563EB'; },
    primaryAlpha: function(a) { return 'rgba(37, 99, 235, ' + (a || 0.1) + ')'; },
    success: function() { return getCSSVar('--chart-success') || '#16A34A'; },
    successAlpha: function(a) { return 'rgba(22, 163, 74, ' + (a || 0.7) + ')'; },
    danger: function() { return getCSSVar('--chart-danger') || '#DC2626'; },
    dangerAlpha: function(a) { return 'rgba(220, 38, 38, ' + (a || 0.7) + ')'; },
    warning: function() { return getCSSVar('--chart-warning') || '#D97706'; },
    maLine: function() { return getCSSVar('--chart-ma-line') || '#F59E0B'; },
    maSecondary: function() { return getCSSVar('--chart-ma-secondary') || '#94A3B8'; },
    gridColor: function() { return getCSSVar('--chart-grid') || '#E2E8F0'; },
    textColor: function() { return getCSSVar('--chart-text') || '#64748B'; },

    // セクターチャート用 10色パレット
    sectorPalette: function() {
      return ['#2563EB', '#16A34A', '#D97706', '#DC2626', '#7C3AED',
              '#0891B2', '#92400E', '#475569', '#DB2777', '#4F46E5'];
    },

    // ファネルチャート用
    funnelPalette: function() {
      return ['#93C5FD', '#3B82F6', '#16A34A', '#D97706', '#DC2626'];
    },

    // スコア分布用
    scoreBuckets: function() {
      return ['#94A3B8', '#93C5FD', '#3B82F6', '#D97706', '#16A34A'];
    },

    // 損益の色を返す (正=success, 負=danger)
    pnlColor: function(v) {
      return v >= 0 ? this.successAlpha(0.7) : this.dangerAlpha(0.7);
    },

    // 共通スケールオプション
    defaultScaleOptions: function() {
      var clr = this.textColor();
      return {
        ticks: { font: { size: 10 }, color: clr },
        grid: { color: this.gridColor() }
      };
    }
  };
})();
