"""
moomoo 注文実行モジュール

Slack 通知のリンクから呼び出される注文ロジック。
OpenD ローカルデーモンを介して moomoo に成行注文を発注する。
"""

import hashlib
import hmac
from urllib.parse import quote, urlencode

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 設定読み込み
# ============================================================

_MOOMOO_CFG = CONFIG.get("moomoo", {})
_HOST = _MOOMOO_CFG.get("host", "127.0.0.1")
_PORT = int(_MOOMOO_CFG.get("port", 11111))
_SECURITY_FIRM_STR = _MOOMOO_CFG.get("security_firm", "")
_TRADE_PWD_MD5 = _MOOMOO_CFG.get("trade_password_md5", "")

_SERVER_CFG = CONFIG.get("order_server", {})
_SERVER_HOST = _SERVER_CFG.get("host", "127.0.0.1")
_SERVER_PORT = int(_SERVER_CFG.get("port", 5100))
_SECRET = _SERVER_CFG.get("secret", "")

_GUI_UNLOCK_DISABLED_MARKERS = (
    "unlock api has been disabled in the gui version",
    "use the unlock button in the interface",
)

_TRADE_LOCKED_MARKERS = (
    "unlock trade",
    "unlock trading",
    "trade is locked",
    "trading is locked",
)


# ============================================================
# URL 署名・検証
# ============================================================

def _make_sig(action: str, ticker: str, market: str) -> str:
    """注文パラメータの HMAC 署名を生成する。"""
    if not _SECRET:
        return ""
    msg = f"{action}:{ticker}:{market}"
    return hmac.new(
        _SECRET.encode(), msg.encode(), hashlib.sha256,
    ).hexdigest()[:16]


def verify_order_signature(action: str, ticker: str, market: str, sig: str) -> bool:
    """Slack リンクの署名を検証する。"""
    if not _SECRET:
        return False
    expected = _make_sig(action, ticker, market)
    return hmac.compare_digest(expected, sig)


def generate_order_url(
    action: str,
    ticker: str,
    market: str,
    qty: float,
    price: float = 0,
    stop: float = 0,
    name: str = "",
) -> str | None:
    """
    Slack 通知に埋め込む注文確認 URL を生成する。
    order_server の secret が未設定の場合は None を返す。
    """
    if not _SECRET:
        return None

    sig = _make_sig(action, ticker, market)
    params = urlencode({
        "action": action,
        "ticker": ticker,
        "market": market,
        "qty": qty,
        "price": price,
        "stop": stop,
        "name": name,
        "sig": sig,
    }, quote_via=quote)
    return f"http://{_SERVER_HOST}:{_SERVER_PORT}/order?{params}"


# ============================================================
# 取引パスワードのハッシュ生成ヘルパー
# ============================================================

def hash_trade_password(password: str) -> str:
    """
    moomoo 取引パスワードの MD5 ハッシュを生成する。
    結果を config.json の moomoo.trade_password_md5 に設定すること。

    使い方:
        python -c "from src.order_executor import hash_trade_password; print(hash_trade_password('your_password'))"
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest()


# ============================================================
# 注文実行
# ============================================================

def _resolve_security_firm(ft):
    """config の security_firm 文字列を SDK の SecurityFirm 値に変換する。"""
    if not _SECURITY_FIRM_STR:
        return ft.SecurityFirm.FUTUSECURITIES
    val = getattr(ft.SecurityFirm, _SECURITY_FIRM_STR, None)
    if val is None:
        logger.warning(f"SecurityFirm '{_SECURITY_FIRM_STR}' が不明。デフォルトを使用")
        return ft.SecurityFirm.FUTUSECURITIES
    return val


def _normalize_error_message(message) -> str:
    if message is None:
        return ""
    return str(message).strip()


def _is_gui_unlock_disabled_message(message) -> bool:
    normalized = _normalize_error_message(message).lower()
    return any(marker in normalized for marker in _GUI_UNLOCK_DISABLED_MARKERS)


def _is_trade_unlock_required_message(message) -> bool:
    normalized = _normalize_error_message(message).lower()
    return any(marker in normalized for marker in _TRADE_LOCKED_MARKERS)


def _manual_unlock_guidance() -> str:
    return (
        "GUI版 OpenD では API で取引ロック解除できません。"
        " moomoo の画面で取引ロックを解除してから、もう一度注文を実行してください"
    )


def place_order(
    action: str,
    ticker: str,
    market: str,
    qty: float,
    trd_env: str = "REAL",
    jp_acc_type: str = "JP_TOKUTEI",
) -> dict:
    """
    moomoo OpenD を通じて成行注文を発注する。

    引数:
        action:      "buy" または "sell"
        ticker:      銘柄コード ("NVDA", "7203")
        market:      "US" または "JP"
        qty:         注文数量
        trd_env:     "REAL" または "SIMULATE"
        jp_acc_type: 日本株口座タイプ ("JP_TOKUTEI", "JP_GENERAL" 等)

    返り値:
        {"success": bool, "order_id": str|None, "message": str, "details": dict|None}
    """
    try:
        import moomoo as ft
    except ImportError:
        return _fail("moomoo SDK がインストールされていません (pip install futu-api)")

    code = f"{market}.{ticker}"
    trd_side = ft.TrdSide.BUY if action == "buy" else ft.TrdSide.SELL
    env = ft.TrdEnv.REAL if trd_env == "REAL" else ft.TrdEnv.SIMULATE
    trd_market = getattr(ft.TrdMarket, market, ft.TrdMarket.US)
    sub_acc = getattr(ft.SubAccType, jp_acc_type, ft.SubAccType.JP_TOKUTEI)

    ctx = None
    gui_unlock_disabled = False
    try:
        ctx = ft.OpenSecTradeContext(
            filter_trdmarket=trd_market,
            host=_HOST,
            port=_PORT,
            security_firm=_resolve_security_firm(ft),
        )

        # REAL 環境は取引ロック解除が必要
        if env == ft.TrdEnv.REAL:
            if _TRADE_PWD_MD5:
                ret, msg = ctx.unlock_trade(password_md5=_TRADE_PWD_MD5)
                if ret != ft.RET_OK:
                    if _is_gui_unlock_disabled_message(msg):
                        gui_unlock_disabled = True
                        logger.info("GUI版 OpenD のため API unlock をスキップし、手動解除前提で注文を継続")
                    else:
                        return _fail(f"取引ロック解除失敗: {msg}")
            else:
                logger.info("moomoo.trade_password_md5 未設定のため API unlock をスキップ")

        # 成行注文を発注
        ret, data = ctx.place_order(
            price=0,
            qty=qty,
            code=code,
            trd_side=trd_side,
            order_type=ft.OrderType.MARKET,
            trd_env=env,
            jp_acc_type=sub_acc,
            remark="InvestSupport",
        )

        if ret == ft.RET_OK:
            info = data.to_dict("records")[0] if len(data) > 0 else {}
            order_id = str(info.get("order_id", ""))
            side_str = "買い" if action == "buy" else "売り"
            logger.info(f"注文成功: {side_str} {ticker} x{qty} (ID: {order_id})")
            return {
                "success": True,
                "order_id": order_id,
                "message": f"注文が発注されました (注文ID: {order_id})",
                "details": info,
            }
        else:
            error_message = _normalize_error_message(data)
            logger.error(f"注文失敗: {error_message}")

            if env == ft.TrdEnv.REAL and (
                gui_unlock_disabled or _is_gui_unlock_disabled_message(error_message)
            ):
                return _fail(f"{_manual_unlock_guidance()}。詳細: {error_message}")

            if env == ft.TrdEnv.REAL and not _TRADE_PWD_MD5 and _is_trade_unlock_required_message(error_message):
                return _fail(
                    "取引ロックが解除されていません。"
                    " GUI版 OpenD なら moomoo の画面で解除し、通常版 OpenD なら "
                    "config.json の moomoo.trade_password_md5 を設定してください。"
                    f" 詳細: {error_message}"
                )

            if env == ft.TrdEnv.REAL and _is_trade_unlock_required_message(error_message):
                return _fail(f"取引ロックが解除されていません。{error_message}")

            return _fail(f"注文失敗: {error_message}")

    except Exception as e:
        logger.error(f"注文実行エラー: {e}", exc_info=True)
        return _fail(f"エラー: {e}")
    finally:
        if ctx:
            ctx.close()


def _fail(message: str) -> dict:
    return {"success": False, "order_id": None, "message": message, "details": None}
