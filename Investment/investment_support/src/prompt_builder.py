"""
プロンプトビルダーモジュール

ダッシュボードの「プロンプト作成」ページ用。
テーマ（売却相談・購入判断・ポートフォリオ全体・市場環境・カスタム）に応じて
ChatGPT 等の外部 AI に貼り付けるためのプロンプトを自動生成する。

llm_advisor.py の既存フォーマッタを再利用し、テーマ別の質問テンプレートと
初心者向け用語解説を付加する。
"""

from datetime import date

from src.config import CONFIG
from src.llm_advisor import (
    _estimate_tokens,
    _fetch_index_mas,
    _fetch_mode_history,
    _format_entry_sets_rich,
    _format_holdings_rich,
    _format_ma_alignment,
    _format_market_env_rich,
    _HOLDING_LABELS,
    _MODE_LABELS,
    _PATTERN_NAMES,
    _pct,
)
from src.logger import get_logger

logger = get_logger(__name__)

# テーマ定義
THEMES = {
    "sell": {"label": "売却相談", "icon": "trending-down"},
    "buy": {"label": "購入判断", "icon": "shopping-cart"},
    "portfolio": {"label": "ポートフォリオ全体", "icon": "pie-chart"},
    "market": {"label": "市場環境", "icon": "globe"},
    "custom": {"label": "カスタム", "icon": "pen-tool"},
}

_CAPITAL_CFG = CONFIG.get("capital", {})
# 購入相談プロンプトに含める候補の上限（トークン肥大化防止）
_MAX_BUY_PROMPT_TICKERS = 10

# テクニカル用語の解説（初心者向けオプション）
_GLOSSARY = (
    "## 用語解説\n"
    "- MA（移動平均線）: 過去N日間の終値の平均。MA25 = 25日移動平均。"
    "短期MA > 長期MA の順（MA5 > MA25 > MA75 > MA200）で並ぶ状態を「パーフェクトオーダー」と呼び、上昇トレンドの強さを示す\n"
    "- RSI（相対力指数）: 0〜100 の範囲で売られ過ぎ/買われ過ぎを測る指標。"
    "一般に 30 以下は売られ過ぎ、70 以上は買われ過ぎ\n"
    "- ATR（平均真幅）: 過去14日の値動きの大きさを示す指標。損切り幅の設定に使う\n"
    "- 損切りライン（SL）: これ以上下がったら損失を確定して手放す価格。"
    "損失を限定するための最重要ルール\n"
    "- 利確目安: 利益確定の目標価格。リスクリワード比 2:1 = 損切り幅の2倍を目標に設定\n"
    "- 出来高: 1日に取引された株数。急増は関心の高まりを示す\n"
    "- 52週高値: 過去1年の最高値。ブレイクアウト戦略ではこの更新が買いシグナルになる"
)


# ============================================================
# 公開 API
# ============================================================

def generate_prompt(
    theme: str,
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> dict:
    """テーマに基づいてプロンプトを生成する。

    Args:
        theme: "sell" | "buy" | "portfolio" | "market" | "custom"
        tickers: 選択された銘柄ティッカーのリスト
        holdings: 保有銘柄の enriched データリスト
        candidates: 買い候補の enriched データリスト
        market_env: 市場環境 dict
        options: {"beginner_glossary": bool, "custom_question": str}

    Returns:
        {"prompt": str, "estimated_tokens": int}
    """
    builders = {
        "sell": _build_sell_prompt,
        "buy": _build_buy_prompt,
        "portfolio": _build_portfolio_prompt,
        "market": _build_market_prompt,
        "custom": _build_custom_prompt,
    }
    builder = builders.get(theme)
    if not builder:
        return {"prompt": f"不明なテーマ: {theme}", "estimated_tokens": 0}

    prompt = builder(tickers, holdings, candidates, market_env, options)
    return {
        "prompt": prompt,
        "estimated_tokens": _estimate_tokens(prompt),
    }


# ============================================================
# テーマ別ビルダー
# ============================================================

def _build_sell_prompt(
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> str:
    """保有銘柄の売却相談プロンプト"""
    target_ticker = tickers[0] if tickers else None
    holding = None
    if target_ticker:
        holding = next((h for h in holdings if h.get("ticker") == target_ticker), None)

    sections = [_system_instruction("sell")]

    # 市場環境
    sections.append(_format_market_env_rich(market_env))

    # 対象銘柄データ
    if holding:
        sections.append(_format_holdings_rich([holding]))
    else:
        sections.append("## 対象銘柄\n銘柄が選択されていません。")

    # 用語解説
    if options.get("beginner_glossary", True):
        sections.append(_GLOSSARY)

    # 質問
    custom_q = options.get("custom_question", "").strip()
    question = (
        "## 質問\n"
        "上記の保有銘柄について、以下の観点で分析してください：\n"
        "1. 現在の損益状況とテクニカル指標から、今売却すべきか継続保有すべきか\n"
        "2. 損切りラインと利確目標に対する現在の位置\n"
        "3. 市場環境を踏まえたリスク要因\n"
        "4. 初心者が見落としがちな注意点\n"
    )
    if custom_q:
        question += f"\n追加の質問: {custom_q}\n"
    question += "\n具体的な根拠を挙げて、分かりやすい日本語で回答してください。投資助言ではなく、判断材料の整理としてお願いします。"
    sections.append(question)

    return "\n\n".join(sections)


def _build_buy_prompt(
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> str:
    """買い候補の購入判断プロンプト（複数銘柄の同時相談に対応）"""
    selected, truncated = _pick_items_by_tickers(
        candidates, tickers, _MAX_BUY_PROMPT_TICKERS,
    )
    multi = len(selected) > 1

    sections = [_system_instruction("buy", multi=multi, count=len(selected))]

    # 市場環境
    sections.append(_format_market_env_rich(market_env))

    if options.get("from_screening"):
        sections.append(
            "## 出典\n"
            "Screening List で選んだ銘柄です（daily_signals の最新スコア・テクニカルに基づく）。"
        )

    # 対象候補データ
    if selected:
        sections.append(_format_entry_sets_rich(selected))
        if truncated:
            sections.append(
                f"※ 選択銘柄が {_MAX_BUY_PROMPT_TICKERS} 件を超えたため、"
                f"先頭 {_MAX_BUY_PROMPT_TICKERS} 件のみ掲載しています。"
            )
    else:
        sections.append("## 対象候補\n候補が選択されていません。")

    # 現在の保有状況（文脈として）
    if holdings:
        sections.append(
            f"## 現在の保有状況\n"
            f"保有銘柄数: {len(holdings)}件 / "
            f"上限: {_CAPITAL_CFG.get('max_concurrent_holdings', '?')}件"
        )

    # 用語解説
    if options.get("beginner_glossary", True):
        sections.append(_GLOSSARY)

    # 質問
    custom_q = options.get("custom_question", "").strip()
    if multi:
        question = (
            "## 質問\n"
            f"上記の買い候補 {len(selected)} 銘柄について、以下の観点で分析してください：\n"
            "1. 各銘柄のテクニカル指標（MA配置・RSI・出来高）から見た買いシグナルの信頼性\n"
            "2. 各銘柄のエントリー価格・損切りライン・利確目標の妥当性\n"
            "3. 推奨投入額と現在のポートフォリオバランスへの影響\n"
            "4. 購入を見送るべき警戒要因がある銘柄はどれか\n"
            "5. 複数銘柄を同時に検討する場合の優先順位と資金配分の考え方\n"
        )
    else:
        question = (
            "## 質問\n"
            "上記の買い候補について、以下の観点で分析してください：\n"
            "1. テクニカル指標（MA配置・RSI・出来高）から見た買いシグナルの信頼性\n"
            "2. エントリー価格・損切りライン・利確目標の妥当性\n"
            "3. 推奨投入額と現在のポートフォリオバランスへの影響\n"
            "4. 購入を見送るべき警戒要因があるか\n"
        )
    if custom_q:
        question += f"\n追加の質問: {custom_q}\n"
    question += "\n具体的な根拠を挙げて、分かりやすい日本語で回答してください。投資助言ではなく、判断材料の整理としてお願いします。"
    sections.append(question)

    return "\n\n".join(sections)


def _build_portfolio_prompt(
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> str:
    """ポートフォリオ全体の見直しプロンプト"""
    sections = [_system_instruction("portfolio")]

    # 市場環境
    sections.append(_format_market_env_rich(market_env))

    # 全保有銘柄
    sections.append(_format_holdings_rich(holdings))

    # ポートフォリオサマリー
    if holdings:
        jp_count = sum(1 for h in holdings if h.get("market") == "JP")
        us_count = sum(1 for h in holdings if h.get("market") == "US")
        max_hold = _CAPITAL_CFG.get("max_concurrent_holdings", "?")
        total_pnl = sum(h.get("pnl_pct", 0) for h in holdings)
        avg_pnl = total_pnl / len(holdings) if holdings else 0
        sections.append(
            f"## ポートフォリオ概要\n"
            f"保有銘柄数: {len(holdings)}件（JP: {jp_count}, US: {us_count}）/ "
            f"上限: {max_hold}件\n"
            f"平均損益率: {avg_pnl:+.1f}%"
        )

    # 直近の候補（参考情報）
    if candidates:
        sections.append(
            f"## 直近の買い候補（参考）\n"
            f"過去7日間の候補数: {len(candidates)}件"
        )

    # 用語解説
    if options.get("beginner_glossary", True):
        sections.append(_GLOSSARY)

    # 質問
    custom_q = options.get("custom_question", "").strip()
    question = (
        "## 質問\n"
        "ポートフォリオ全体について、以下の観点で分析してください：\n"
        "1. 保有銘柄の全体的なバランス（市場・セクター分散）\n"
        "2. 損益の偏り（大きな含み損や含み益のある銘柄への対応）\n"
        "3. 市場環境を踏まえた全体のリスク水準\n"
        "4. 今後の運用で注意すべき点\n"
    )
    if custom_q:
        question += f"\n追加の質問: {custom_q}\n"
    question += "\n具体的な根拠を挙げて、分かりやすい日本語で回答してください。投資助言ではなく、判断材料の整理としてお願いします。"
    sections.append(question)

    return "\n\n".join(sections)


def _build_market_prompt(
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> str:
    """市場環境の確認プロンプト"""
    sections = [_system_instruction("market")]

    # 市場環境（詳細）
    sections.append(_format_market_env_rich(market_env))

    # 保有状況サマリー
    if holdings:
        positive = sum(1 for h in holdings if h.get("pnl_pct", 0) > 0)
        negative = len(holdings) - positive
        sections.append(
            f"## 現在の保有状況\n"
            f"保有銘柄数: {len(holdings)}件（含み益: {positive}件, 含み損: {negative}件）"
        )
    else:
        sections.append("## 現在の保有状況\n保有銘柄なし")

    # 用語解説
    if options.get("beginner_glossary", True):
        sections.append(_GLOSSARY)

    # 質問
    custom_q = options.get("custom_question", "").strip()
    question = (
        "## 質問\n"
        "現在の市場環境について、以下の観点で分析してください：\n"
        "1. 日経平均・S&P500 の各移動平均線との位置関係が示すトレンド\n"
        "2. 市場モードの推移から読み取れる今後の見通し\n"
        "3. 今の環境で新規買いを入れてよいか、控えるべきか\n"
        "4. 保有銘柄がある場合、市場環境を踏まえた注意点\n"
    )
    if custom_q:
        question += f"\n追加の質問: {custom_q}\n"
    question += "\n具体的な根拠を挙げて、分かりやすい日本語で回答してください。投資助言ではなく、判断材料の整理としてお願いします。"
    sections.append(question)

    return "\n\n".join(sections)


def _build_custom_prompt(
    tickers: list[str],
    holdings: list[dict],
    candidates: list[dict],
    market_env: dict,
    options: dict,
) -> str:
    """カスタム相談プロンプト"""
    sections = [_system_instruction("custom")]

    # 市場環境
    sections.append(_format_market_env_rich(market_env))

    # 選択された銘柄のデータを含める
    selected_holdings = [h for h in holdings if h.get("ticker") in tickers]
    selected_candidates = [c for c in candidates if c.get("ticker") in tickers]

    if selected_holdings:
        sections.append(_format_holdings_rich(selected_holdings))
    if selected_candidates:
        sections.append(_format_entry_sets_rich(selected_candidates))
    if not selected_holdings and not selected_candidates and tickers:
        sections.append(f"## 選択された銘柄\n{', '.join(tickers)}（データなし）")

    # 用語解説
    if options.get("beginner_glossary", True):
        sections.append(_GLOSSARY)

    # 質問
    custom_q = options.get("custom_question", "").strip()
    if custom_q:
        sections.append(f"## 質問\n{custom_q}")
    else:
        sections.append(
            "## 質問\n"
            "上記のデータについて分析してください。\n"
            "具体的な根拠を挙げて、分かりやすい日本語で回答してください。"
            "投資助言ではなく、判断材料の整理としてお願いします。"
        )

    return "\n\n".join(sections)


# ============================================================
# ヘルパー
# ============================================================

def _pick_items_by_tickers(
    items: list[dict],
    tickers: list[str],
    max_items: int | None = None,
) -> tuple[list[dict], bool]:
    """tickers の順序で items を抽出する。max_items 超過時は切り詰め、truncated=True。"""
    if not tickers:
        return [], False
    by_ticker = {item.get("ticker"): item for item in items if item.get("ticker")}
    ordered = [by_ticker[t] for t in tickers if t in by_ticker]
    if max_items is not None and len(ordered) > max_items:
        return ordered[:max_items], True
    return ordered, False


def _system_instruction(
    theme: str,
    *,
    multi: bool = False,
    count: int = 0,
) -> str:
    """テーマに応じたシステム指示を生成する。"""
    today_str = date.today().strftime("%Y-%m-%d")
    base = (
        f"あなたは初心者投資家向けの投資教育アシスタントです。（日付: {today_str}）\n"
        "投資助言ではなく、テクニカル指標の読み方や注意点を教える立場で回答してください。\n"
        "回答は日本語で、初心者にも分かりやすく具体的にお願いします。"
    )
    theme_hints = {
        "sell": "これから保有銘柄の売却判断について相談されます。",
        "buy": "これから買い候補の購入判断について相談されます。",
        "portfolio": "これからポートフォリオ全体の見直しについて相談されます。",
        "market": "これから市場環境の分析について相談されます。",
        "custom": "これから投資に関する質問を受けます。",
    }
    hint = theme_hints.get(theme, "")
    if theme == "buy" and multi and count > 1:
        hint = (
            f"これから買い候補 {count} 銘柄の購入判断をまとめて相談されます。"
            "銘柄ごとの分析に加え、比較・優先順位・資金配分の観点も含めてください。"
        )
    return f"## システム指示\n{base}\n{hint}"
