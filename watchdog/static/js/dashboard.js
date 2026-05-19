// dashboard.js — Watchdog Dashboard: auto-refresh + theme toggle

(function () {
  'use strict';

  var STORAGE_KEY = 'watchdog-dashboard-theme';
  var REFRESH_INTERVAL_MS = 60000;
  var _timer = null;

  // ===========================================================================
  // Theme
  // ===========================================================================
  function getPreferredTheme() {
    var stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'dark' || stored === 'light') return stored;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark';
    }
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    var btn = document.getElementById('themeToggle');
    if (btn) {
      var icon = btn.querySelector('[data-lucide]');
      if (icon) {
        icon.setAttribute('data-lucide', theme === 'dark' ? 'sun' : 'moon');
        if (window.lucide) lucide.createIcons();
      }
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    localStorage.setItem(STORAGE_KEY, next);
    applyTheme(next);
  }

  applyTheme(getPreferredTheme());
  window.toggleTheme = toggleTheme;

  // ===========================================================================
  // Status Labels
  // ===========================================================================
  var STATUS_LABELS = {
    success:         'OK',
    failed:          'NG',
    running:         'RUN',
    missing:         'MISS',
    partial_failure: 'WARN',
    pending:         '---',
    not_registered:  'N/A'
  };

  var STATUS_ICONS = {
    success:         'check-circle',
    failed:          'x-circle',
    running:         'loader',
    missing:         'help-circle',
    partial_failure: 'alert-triangle',
    pending:         'minus-circle',
    not_registered:  'alert-triangle'
  };

  // Task Scheduler result code → status mapping
  var TASK_RESULT_OK = 0;
  var TASK_RESULT_RUNNING = 267009;
  var TASK_RESULT_NOT_YET_RUN = 267011;
  var TASK_RESULT_TERMINATED = 267014;

  function taskResultToStatus(task) {
    if (task.state === 'Running') return 'running';
    if (task.state === 'NotRegistered') return 'not_registered';
    if (task.last_result === TASK_RESULT_OK) return 'success';
    if (task.last_result === TASK_RESULT_RUNNING) return 'running';
    if (task.last_result === TASK_RESULT_NOT_YET_RUN) return 'pending';
    if (task.last_result === TASK_RESULT_TERMINATED) return 'pending';
    return 'failed';
  }

  function formatTaskName(name) {
    // "Ishikawa238_WatchdogDashboard" → "WatchdogDashboard"
    // "Investment_MorningBatch_ALL" → "MorningBatch_ALL"
    var prefixes = ['Ishikawa238_', 'Investment_', 'FreelanceScout_',
                    'AiNewsDigest_', 'EnglishDaily_', 'PaperWatch_',
                    'MorningEnglish'];
    for (var p = 0; p < prefixes.length; p++) {
      if (name.indexOf(prefixes[p]) === 0) {
        return name.substring(prefixes[p].length) || name;
      }
    }
    return name;
  }

  // ===========================================================================
  // Render
  // ===========================================================================
  function renderProjects(data) {
    var grid = document.getElementById('projectGrid');
    var badge = document.getElementById('overallBadge');
    var checkedAt = document.getElementById('checkedAt');
    var indicator = document.getElementById('refreshIndicator');

    // Overall badge
    badge.className = 'overall-badge ' + (data.all_ok ? 'ok' : 'ng');
    badge.innerHTML = data.all_ok
      ? '<i data-lucide="shield-check" style="width:14px;height:14px"></i> All Systems OK'
      : '<i data-lucide="shield-alert" style="width:14px;height:14px"></i> ' +
        data.projects.filter(function (p) { return !p.ok; }).length + ' Issues';

    // Checked at
    if (data.checked_at) {
      checkedAt.textContent = 'Last checked: ' + data.checked_at;
    }

    // Refresh indicator
    indicator.textContent = 'Auto-refresh 60s';

    // Cards
    var html = '';
    data.projects.forEach(function (proj, i) {
      var cardClass = proj.discovered ? 'card-discovered'
        : proj.scheduler_only ? (proj.ok ? 'card-scheduler' : 'card-ng')
        : (proj.ok ? 'card-ok' : 'card-ng');
      var iconName = proj.icon || 'box';

      html += '<div class="col-12 col-md-6 col-xl-4">';
      html += '<div class="project-card ' + cardClass + ' animate-in" style="animation-delay:' + (i * 50) + 'ms">';

      // Header with icon
      html += '<div class="card-header-row">';
      html += '<div class="d-flex align-items-center gap-2">';
      html += '<i data-lucide="' + iconName + '" class="card-icon" style="width:18px;height:18px"></i>';
      if (proj.url) {
        html += '<a href="' + escapeHtml(proj.url) + '" class="card-title-link" target="_blank">';
        html += '<h3 class="card-title mb-0">' + escapeHtml(proj.label) + '</h3>';
        html += '</a>';
      } else {
        html += '<h3 class="card-title mb-0">' + escapeHtml(proj.label) + '</h3>';
      }
      html += '</div>';
      if (proj.discovered) {
        html += '<span class="discovered-badge">未登録</span>';
      }
      if (proj.scheduler_only) {
        html += '<span class="scheduler-badge">Scheduler</span>';
      }
      html += '</div>';

      // Description
      if (proj.description) {
        html += '<p class="card-description">' + escapeHtml(proj.description) + '</p>';
      }

      if (proj.scheduler_only && proj.scheduler_tasks_status) {
        // --- Scheduler-only: タスクスケジューラ状態リスト ---
        html += '<ul class="batch-list">';
        proj.scheduler_tasks_status.forEach(function (t) {
          var status = taskResultToStatus(t);
          var statusLabel = STATUS_LABELS[status] || status;
          var statusIcon = STATUS_ICONS[status] || 'help-circle';
          html += '<li class="batch-item">';
          html += '<span class="batch-name">' + escapeHtml(formatTaskName(t.name)) + '</span>';
          html += '<span class="status-badge ' + status + '">';
          html += '<i data-lucide="' + statusIcon + '" style="width:12px;height:12px"></i> ' + statusLabel;
          html += '</span>';
          html += '<span class="batch-detail">' + (t.last_run || '---') + '</span>';
          html += '</li>';
        });
        html += '</ul>';
      } else if (proj.batches && proj.batches.length > 0) {
        // --- DB 監視: バッチ状態リスト ---
        html += '<ul class="batch-list">';
        proj.batches.forEach(function (b) {
          var status = b.status || 'missing';
          var statusLabel = STATUS_LABELS[status] || status;
          var statusIcon = STATUS_ICONS[status] || 'help-circle';
          var batchDisplayName = b.label || b.batch_type;
          html += '<li class="batch-item">';
          html += '<span class="batch-name">' + escapeHtml(batchDisplayName) + '</span>';
          if (b.schedule) {
            html += '<span class="batch-schedule">' + escapeHtml(b.schedule) + '</span>';
          }
          html += '<span class="status-badge ' + status + '">';
          html += '<i data-lucide="' + statusIcon + '" style="width:12px;height:12px"></i> ' + statusLabel;
          html += '</span>';
          html += '<span class="batch-detail">' + extractTime(b.detail) + '</span>';
          html += '</li>';
        });
        html += '</ul>';
      }

      // DB unavailable (DB 監視のみ)
      if (!proj.scheduler_only && !proj.db_available) {
        html += '<div class="error-samples"><p>DB unavailable</p></div>';
      }

      // Footer
      html += '<div class="card-footer-info">';

      if (!proj.scheduler_only) {
        // DB 監視: fatal errors + log file
        var fe = proj.fatal_errors || {};
        var feClass = fe.ok !== false ? 'ok' : 'ng';
        html += '<span class="footer-item ' + feClass + '">';
        html += '<i data-lucide="' + (fe.ok !== false ? 'check' : 'alert-circle') + '" style="width:12px;height:12px"></i> ';
        html += 'Fatal: ' + (fe.count || 0);
        html += '</span>';

        var lf = proj.log_file || {};
        var lfClass = lf.ok !== false ? 'ok' : 'ng';
        html += '<span class="footer-item ' + lfClass + '">';
        html += '<i data-lucide="' + (lf.ok !== false ? 'file-check' : 'file-x') + '" style="width:12px;height:12px"></i> ';
        html += 'Log: ' + (lf.ok !== false ? 'OK' : 'Missing');
        html += '</span>';
      }

      // Scheduler task count (DB 監視の場合のみ — scheduler-only は上で詳細表示済み)
      var tasks = proj.scheduler_tasks || [];
      if (!proj.scheduler_only && tasks.length > 0) {
        html += '<span class="footer-item" title="' + escapeHtml(tasks.join(', ')) + '">';
        html += '<i data-lucide="clock" style="width:12px;height:12px"></i> ';
        html += 'Tasks: ' + tasks.length;
        html += '</span>';
      }

      html += '</div>';

      // Error samples (DB 監視のみ)
      var fe = proj.fatal_errors || {};
      if (!proj.scheduler_only && fe.samples && fe.samples.length > 0) {
        html += '<div class="error-samples">';
        fe.samples.forEach(function (s) {
          html += '<p>' + escapeHtml(s) + '</p>';
        });
        html += '</div>';
      }

      html += '</div></div>';
    });

    grid.innerHTML = html;

    // Re-render Lucide icons
    if (window.lucide) lucide.createIcons();
  }

  function extractTime(detail) {
    if (!detail) return '';
    var match = detail.match(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}/);
    return match ? match[0] : detail;
  }

  function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // ===========================================================================
  // Fetch
  // ===========================================================================
  function fetchStatus() {
    fetch('/api/status')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(renderProjects)
      .catch(function (err) {
        console.error('Failed to fetch status:', err);
        var badge = document.getElementById('overallBadge');
        badge.className = 'overall-badge ng';
        badge.textContent = 'Fetch error';
      });
  }

  // ===========================================================================
  // Init
  // ===========================================================================
  document.addEventListener('DOMContentLoaded', function () {
    if (window.lucide) lucide.createIcons();
    fetchStatus();
    _timer = setInterval(fetchStatus, REFRESH_INTERVAL_MS);
  });
})();
