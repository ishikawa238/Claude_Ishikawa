"""
注文確認 Web サーバー

Slack 通知のリンクをクリック → ブラウザで注文内容を確認 → 確認ボタンで発注

実行方法（investment_support/ から）:
    python -m scripts.order_server

前提条件:
    - OpenD が起動していること
    - config.json に moomoo.trade_password_md5 が設定されていること
    - config.json に order_server.secret が設定されていること
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, request, abort
from markupsafe import escape

from src.config import CONFIG
from src.logger import get_logger
from src.order_executor import place_order, verify_order_signature

logger = get_logger(__name__)

_SERVER_CFG = CONFIG.get("order_server", {})
_HOST = _SERVER_CFG.get("host", "127.0.0.1")
_PORT = int(_SERVER_CFG.get("port", 5100))

app = Flask(__name__)


# ============================================================
# ルーティング
# ============================================================

@app.route("/order")
def order_confirm():
    """GET: Slack リンクから遷移 → 注文確認ページを表示"""
    action = request.args.get("action", "")
    ticker = request.args.get("ticker", "")
    market = request.args.get("market", "")
    qty    = request.args.get("qty", "0")
    price  = request.args.get("price", "0")
    stop   = request.args.get("stop", "0")
    name   = request.args.get("name", "")
    sig    = request.args.get("sig", "")

    if not action or not ticker or not market:
        abort(400)
    if not verify_order_signature(action, ticker, market, sig):
        logger.warning(f"署名検証失敗: {action} {ticker} {market}")
        abort(403)

    return _render_confirm(action, ticker, market, qty, price, stop, name, sig)


@app.route("/order/execute", methods=["POST"])
def order_execute():
    """POST: 確認ページから送信 → 注文を実行"""
    action      = request.form.get("action", "")
    ticker      = request.form.get("ticker", "")
    market      = request.form.get("market", "")
    qty_str     = request.form.get("qty", "0")
    sig         = request.form.get("sig", "")
    trd_env     = request.form.get("trd_env", "REAL")
    jp_acc_type = request.form.get("jp_acc_type", "JP_TOKUTEI")

    if not verify_order_signature(action, ticker, market, sig):
        abort(403)

    try:
        qty = float(qty_str)
    except ValueError:
        return _render_result(False, "数量が不正です", action, ticker, market)

    if qty <= 0:
        return _render_result(False, "数量は0より大きい値を指定してください", action, ticker, market)

    # 注文実行
    result = place_order(
        action=action,
        ticker=ticker,
        market=market,
        qty=qty,
        trd_env=trd_env,
        jp_acc_type=jp_acc_type,
    )

    return _render_result(
        result["success"],
        result["message"],
        action, ticker, market,
        order_id=result.get("order_id"),
    )


@app.route("/health")
def health():
    return "OK", 200


# ============================================================
# HTML レンダリング
# ============================================================

def _render_confirm(action, ticker, market, qty, price, stop, name, sig):
    action_label = "買い注文（成行）" if action == "buy" else "売り注文（成行）"
    action_color = "#1976D2" if action == "buy" else "#D32F2F"
    action_emoji = "🛒" if action == "buy" else "💰"
    currency = "円" if market == "JP" else "USD"

    price_f = float(price) if price else 0
    stop_f = float(stop) if stop else 0
    qty_f = float(qty) if qty else 0

    price_row = ""
    if price_f > 0:
        price_row = f"""
        <tr><td class="label">参考価格</td><td>{price_f:,.2f} {currency}</td></tr>"""
    stop_row = ""
    if stop_f > 0:
        stop_row = f"""
        <tr><td class="label">損切りライン</td><td>{stop_f:,.2f} {currency}</td></tr>"""

    # JP特定口座タイプ選択（全銘柄共通 - moomoo Japan は US株も JP口座で取引）
    jp_acc_options = """
        <option value="JP_TOKUTEI" selected>特定口座</option>
        <option value="JP_GENERAL">一般口座</option>
        <option value="JP_NISA_TSUMITATE">つみたてNISA</option>
        <option value="JP_NISA_GENERAL">一般NISA</option>
    """

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(action_label)} - {escape(ticker)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', 'Yu Gothic UI', sans-serif; background: #f5f5f5; padding: 40px 20px; }}
  .card {{ max-width: 480px; margin: 0 auto; background: #fff; border-radius: 12px;
           box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden; }}
  .header {{ background: {action_color}; color: #fff; padding: 20px 24px; font-size: 18px; font-weight: 600; }}
  .body {{ padding: 24px; }}
  .ticker {{ font-size: 28px; font-weight: 700; margin-bottom: 4px; }}
  .name {{ color: #666; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
  td {{ padding: 8px 0; border-bottom: 1px solid #eee; }}
  td.label {{ color: #888; width: 120px; }}
  .form-group {{ margin-bottom: 16px; }}
  .form-group label {{ display: block; font-size: 13px; color: #666; margin-bottom: 4px; }}
  .form-group input, .form-group select {{ width: 100%; padding: 10px 12px; border: 1px solid #ddd;
           border-radius: 6px; font-size: 16px; }}
  .form-group input:focus, .form-group select:focus {{ border-color: {action_color}; outline: none; }}
  .buttons {{ display: flex; gap: 12px; margin-top: 24px; }}
  .btn {{ flex: 1; padding: 14px; border: none; border-radius: 8px; font-size: 16px;
          font-weight: 600; cursor: pointer; text-align: center; text-decoration: none; }}
  .btn-primary {{ background: {action_color}; color: #fff; }}
  .btn-primary:hover {{ opacity: 0.9; }}
  .btn-cancel {{ background: #eee; color: #666; }}
  .btn-cancel:hover {{ background: #ddd; }}
  .warn {{ background: #FFF3E0; border-left: 4px solid #FF9800; padding: 12px; margin-bottom: 16px;
           border-radius: 0 6px 6px 0; font-size: 13px; color: #E65100; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">{action_emoji} {escape(action_label)}</div>
  <div class="body">
    <div class="ticker">{escape(ticker)}</div>
    <div class="name">{escape(name)} ({escape(market)})</div>

    <table>
      <tr><td class="label">注文種別</td><td>成行注文</td></tr>{price_row}{stop_row}
    </table>

    <div class="warn">
      ⚠️ 成行注文は市場価格で即座に約定します。内容を確認してから実行してください。
    </div>

    <div class="warn">
      ℹ️ GUI版 OpenD を使っている場合、注文前に moomoo の画面側で取引ロックを解除しておく必要があります。
    </div>

    <form method="POST" action="/order/execute">
      <input type="hidden" name="action" value="{escape(action)}">
      <input type="hidden" name="ticker" value="{escape(ticker)}">
      <input type="hidden" name="market" value="{escape(market)}">
      <input type="hidden" name="sig" value="{escape(sig)}">

      <div class="form-group">
        <label>数量</label>
        <input type="number" name="qty" value="{qty_f}" step="any" min="0.0001" required>
      </div>

      <div class="form-group">
        <label>口座タイプ</label>
        <select name="jp_acc_type">{jp_acc_options}</select>
      </div>

      <div class="form-group">
        <label>取引環境</label>
        <select name="trd_env">
          <option value="REAL" selected>本番（REAL）</option>
          <option value="SIMULATE">デモ（SIMULATE）</option>
        </select>
      </div>

      <div class="buttons">
        <a href="javascript:window.close()" class="btn btn-cancel">キャンセル</a>
        <button type="submit" class="btn btn-primary">注文を実行する</button>
      </div>
    </form>
  </div>
</div>
</body>
</html>"""


def _render_result(success, message, action, ticker, market, order_id=None):
    if success:
        status_emoji = "✅"
        status_label = "注文完了"
        status_color = "#4CAF50"
        extra = ""
        if order_id:
            extra = f'<p style="color:#666;margin-top:8px;">注文ID: {escape(str(order_id))}</p>'
        if action == "buy":
            extra += """
            <div class="note">
              💡 約定後、<code>python -m scripts.sync_from_moomoo</code> で holdings.csv を同期してください。
            </div>"""
    else:
        status_emoji = "❌"
        status_label = "注文失敗"
        status_color = "#D32F2F"
        extra = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{status_label} - {escape(ticker)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', 'Yu Gothic UI', sans-serif; background: #f5f5f5; padding: 40px 20px; }}
  .card {{ max-width: 480px; margin: 0 auto; background: #fff; border-radius: 12px;
           box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden; }}
  .header {{ background: {status_color}; color: #fff; padding: 20px 24px; font-size: 18px; font-weight: 600; }}
  .body {{ padding: 24px; text-align: center; }}
  .emoji {{ font-size: 48px; margin-bottom: 16px; }}
  .message {{ font-size: 15px; color: #333; margin-bottom: 16px; word-break: break-all; }}
  .note {{ background: #E3F2FD; border-radius: 8px; padding: 12px; margin-top: 16px;
           font-size: 13px; color: #1565C0; text-align: left; }}
  code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  .btn {{ display: inline-block; padding: 12px 32px; border: none; border-radius: 8px;
          font-size: 15px; font-weight: 600; cursor: pointer; text-decoration: none;
          background: #eee; color: #666; margin-top: 20px; }}
  .btn:hover {{ background: #ddd; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">{status_emoji} {status_label}</div>
  <div class="body">
    <div class="emoji">{status_emoji}</div>
    <div class="message">{escape(str(message))}</div>
    {extra}
    <a href="javascript:window.close()" class="btn">閉じる</a>
  </div>
</div>
</body>
</html>"""


# ============================================================
# エントリーポイント
# ============================================================

def main():
    secret = _SERVER_CFG.get("secret", "")
    pwd_md5 = CONFIG.get("moomoo", {}).get("trade_password_md5", "")

    if not secret:
        print("⚠️  config.json の order_server.secret が未設定です。")
        print("   以下のコマンドでランダムな秘密鍵を生成できます:")
        print('   python -c "import secrets; print(secrets.token_hex(16))"')
        sys.exit(1)

    if not pwd_md5:
        print("⚠️  config.json の moomoo.trade_password_md5 が未設定です。")
        print("   GUI版 OpenD を使う場合は、moomoo の画面で手動解除すれば注文できることがあります。")
        print("   通常版 OpenD で API unlock を使う場合は、以下のコマンドで MD5 を生成してください:")
        print('   python -c "from src.order_executor import hash_trade_password; '
          "print(hash_trade_password('あなたのパスワード'))\"")
        print("   (SIMULATE 環境のみ使用する場合は空のままでも OK)")

    print(f"🚀 注文確認サーバーを起動します: http://{_HOST}:{_PORT}")
    print("   Ctrl+C で停止")
    app.run(host=_HOST, port=_PORT, debug=False)


if __name__ == "__main__":
    main()
