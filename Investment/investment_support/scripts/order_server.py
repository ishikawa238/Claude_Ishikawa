"""
日次レポート Web サーバー

Slack 通知のレポートリンクをクリック → ブラウザで保有銘柄・シグナル履歴・市場環境を確認する。

実行方法（investment_support/ から）:
    python -m scripts.order_server
"""

import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask
from markupsafe import escape

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_SERVER_CFG = CONFIG.get("report_server", {})
_HOST = _SERVER_CFG.get("host", "127.0.0.1")
_PORT = int(_SERVER_CFG.get("port", 5100))

app = Flask(__name__)


# ============================================================
# ルーティング
# ============================================================

@app.route("/health")
def health():
    return "OK", 200


@app.route("/report")
def report():
    """日次詳細レポートページ"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        market_env = fetch_one(
            conn,
            "SELECT * FROM market_environment ORDER BY date DESC LIMIT 1",
        )
        holdings = fetch_all(
            conn,
            """
            SELECT h.ticker, h.name, h.market, h.entry_price, h.entry_date,
                   h.quantity, h.stop_loss_price,
                   (SELECT adj_close FROM price_history
                    WHERE ticker = h.ticker AND date <= ? AND is_holiday = 0
                    ORDER BY date DESC LIMIT 1) AS current_price,
                   (SELECT target_price FROM trade_signal_log
                    WHERE ticker = h.ticker ORDER BY signal_date DESC LIMIT 1) AS target_price
            FROM holdings h
            WHERE h.is_closed = 0
            ORDER BY h.entry_date
            """,
            (today_str,),
        )
        candidates = fetch_all(
            conn,
            """
            SELECT ticker, name, market, pattern, entry_price, stop_loss_price,
                   stop_loss_pct, target_price, recommended_amount, signal_date
            FROM trade_signal_log
            WHERE signal_date >= date(?, '-7 days')
            ORDER BY signal_date DESC, ticker
            """,
            (today_str,),
        )

    return _render_report(today_str, market_env, holdings, candidates)


# ============================================================
# HTML レンダリング
# ============================================================

def _render_report(today_str, market_env, holdings, candidates):
    _MODE_LABEL = {"normal": "通常モード", "caution": "警戒モード", "full_caution": "全面警戒モード"}
    _MODE_COLOR = {"normal": "#4CAF50", "caution": "#FF9800", "full_caution": "#D32F2F"}
    _PATTERN_NAME = {"A": "モメンタムブレイクアウト", "B": "押し目買い"}

    # sqlite3.Row → dict に変換
    market_env = dict(market_env) if market_env else {}
    holdings   = [dict(h) for h in holdings]
    candidates = [dict(c) for c in candidates]

    mode = (market_env or {}).get("mode", "unknown")
    mode_label = _MODE_LABEL.get(mode, mode)
    mode_color = _MODE_COLOR.get(mode, "#999")

    # 市場環境カード
    if market_env:
        n225   = market_env.get("n225_close")
        n225m  = market_env.get("n225_ma25")
        sp500  = market_env.get("sp500_close")
        sp500m = market_env.get("sp500_ma25")
        jp_str = f"日経平均: {n225:,.0f}（MA25: {n225m:,.0f}）" if n225 and n225m else "データなし"
        us_str = f"S&amp;P500: {sp500:,.0f}（MA25: {sp500m:,.0f}）" if sp500 and sp500m else "データなし"
        env_date = market_env.get("date", "")
    else:
        jp_str = us_str = "データなし"
        env_date = ""

    # 保有銘柄テーブル行
    holding_rows = ""
    for h in holdings:
        ep = float(h["entry_price"] or 0)
        cp = float(h["current_price"] or 0) if h["current_price"] else None
        sl = float(h["stop_loss_price"] or 0)
        tp = float(h["target_price"] or 0) if h["target_price"] else None
        qty = h["quantity"]

        pnl_str = "—"
        pnl_color = ""
        if cp and ep:
            pnl_pct = (cp - ep) / ep * 100
            pnl_color = "color:#4CAF50" if pnl_pct >= 0 else "color:#D32F2F"
            pnl_str = f"{pnl_pct:+.1f}%"

        target_str = "—"
        if tp and cp:
            dist = (tp - cp) / cp * 100
            target_str = f"{tp:,.2f}（{dist:+.1f}%）"

        days_str = "—"
        if h["entry_date"]:
            try:
                from datetime import datetime
                entry_dt = datetime.strptime(h["entry_date"], "%Y-%m-%d").date()
                days_str = f"{(date.today() - entry_dt).days}日"
            except ValueError:
                pass

        holding_rows += f"""
        <tr>
          <td>{escape(h['ticker'])}</td>
          <td>{escape(h['name'] or '')}</td>
          <td>{escape(h['market'] or '')}</td>
          <td>{f'{cp:,.2f}' if cp else '—'}</td>
          <td style="{pnl_color}">{pnl_str}</td>
          <td>{sl:,.2f}</td>
          <td>{target_str}</td>
          <td>{days_str}</td>
          <td>{f'{qty:.0f}' if qty else '—'}</td>
        </tr>"""

    # シグナル履歴テーブル行
    signal_rows = ""
    for s in candidates:
        sl_p = float(s["stop_loss_pct"] or 0)
        tg_p = sl_p * 2
        amt = s["recommended_amount"]
        currency = "円" if s["market"] == "JP" else "USD"
        amt_str = f"{amt:,.0f} {currency}" if amt else "—"
        signal_rows += f"""
        <tr>
          <td>{escape(s['signal_date'] or '')}</td>
          <td>{escape(s['ticker'])}</td>
          <td>{escape(s['name'] or '')}</td>
          <td>{escape(s['market'] or '')}</td>
          <td>{escape(_PATTERN_NAME.get(s['pattern'], s['pattern']))}</td>
          <td>{float(s['entry_price']):,.2f}</td>
          <td>{float(s['stop_loss_price']):,.2f}（-{sl_p:.1f}%）</td>
          <td>{float(s['target_price']):,.2f}（+{tg_p:.1f}%）</td>
          <td>{amt_str}</td>
        </tr>"""

    holding_empty = "<tr><td colspan='9' style='text-align:center;color:#999'>保有銘柄なし</td></tr>" if not holdings else ""
    signal_empty  = "<tr><td colspan='9' style='text-align:center;color:#999'>直近7日間のシグナルなし</td></tr>" if not candidates else ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日次レポート {today_str}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<style>
  body {{ padding: 24px; font-size: 14px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; }}
  .mode-badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px;
                 color: #fff; font-weight: 600; background: {mode_color}; }}
  th {{ white-space: nowrap; }}
  .section {{ margin-bottom: 32px; }}
</style>
</head>
<body>
<h1 class="h4 mb-4">📊 日次レポート <span class="text-muted fs-6">{today_str}</span></h1>

<div class="section">
  <h2>市場環境 <span class="text-muted fw-normal fs-6">({env_date})</span></h2>
  <div class="card p-3" style="max-width:480px">
    <div class="mb-2"><span class="mode-badge">{mode_label}</span></div>
    <div>{jp_str}</div>
    <div>{us_str}</div>
  </div>
</div>

<div class="section">
  <h2>保有銘柄（{len(holdings)}件）</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm table-hover">
      <thead class="table-light">
        <tr>
          <th>Ticker</th><th>銘柄名</th><th>市場</th><th>現在値</th>
          <th>損益</th><th>損切りSL</th><th>利確目安</th><th>保有日数</th><th>数量</th>
        </tr>
      </thead>
      <tbody>{holding_rows}{holding_empty}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>直近7日間のシグナル履歴（{len(candidates)}件）</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm table-hover">
      <thead class="table-light">
        <tr>
          <th>日付</th><th>Ticker</th><th>銘柄名</th><th>市場</th><th>パターン</th>
          <th>エントリー</th><th>損切りSL</th><th>利確目安</th><th>推奨投入額</th>
        </tr>
      </thead>
      <tbody>{signal_rows}{signal_empty}</tbody>
    </table>
  </div>
</div>

</body>
</html>"""


# ============================================================
# エントリーポイント
# ============================================================

def main():
    print(f"🚀 レポートサーバーを起動します: http://{_HOST}:{_PORT}/report")
    print("   Ctrl+C で停止")
    app.run(host=_HOST, port=_PORT, debug=False)


if __name__ == "__main__":
    main()
