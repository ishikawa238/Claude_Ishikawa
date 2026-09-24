"""
投資委員会のプロンプト定義と入力テキスト整形

各エージェントには計算済みの数値だけを渡し、LLM には指標の計算をさせない。
出力はすべて JSON（スキーマは docs/investment_committee_requirements.md 4 章）。
"""

import json

from src.fundamentals_fetcher import FUNDAMENTAL_FIELDS

SCHEMA_VERSION = 1

ROLE_NAMES = {
    "macro": "マクロエコノミスト",
    "fundamental": "ファンダメンタル・アナリスト",
    "technical": "テクニカル・アナリスト",
    "risk": "リスク・マネージャー",
    "pm": "ポートフォリオ・マネージャー",
}

_HOLDING_LABELS = {
    "forced_stop": "強制損切り",
    "sl_near": "損切り接近",
    "sell_watch": "売り検討",
    "hold": "ホールド",
}

_COMMON_RULES = (
    "与えられた数値だけを根拠にし、数値が無い項目は推測せず「データなし」と扱うこと。"
    "自分で指標を計算しないこと。日本語で簡潔に述べること。"
    "出力は指定の JSON オブジェクトのみとし、前後に説明文やコードブロックを付けないこと。"
)

_ANALYST_SCHEMA = (
    '{"stance": "bullish | neutral | bearish", "confidence": 0.0〜1.0, '
    '"key_points": ["最大3件"], "risks": ["最大2件"], "summary": "200文字以内"}'
)

_MACRO_SCHEMA = (
    '{"jp": {"stance": "bullish | neutral | bearish", "confidence": 0.0〜1.0, "summary": "日本株への影響 150文字以内"}, '
    '"us": {"stance": "bullish | neutral | bearish", "confidence": 0.0〜1.0, "summary": "米国株への影響 150文字以内"}, '
    '"key_points": ["両市場に共通する注目点 最大3件"], "risks": ["最大2件"]}'
)

_ROLE_FOCUS = {
    "macro": "金利・為替・株価指数のトレンド・市場ニュースから、日本株と米国株それぞれについて"
             "買いに対する追い風か逆風かを個別に評価する。"
             "日本株と米国株は別の市場であり、一方の指数の動きをもう一方の評価に流用しないこと。",
    "fundamental": "財務指標（PER/PBR/ROE/成長率/利益率/負債）から企業価値と割安・割高を評価する。",
    "technical": "移動平均・RSI・出来高・52週高値との距離などから、売買タイミングとして妥当かを評価する。",
    "risk": "ボラティリティ・損切り幅・VaR・Beta・決算接近・ポートフォリオ集中度から、下振れリスクを評価する。"
            "楽観的な見方には懐疑的な立場を取ること。",
}

# PM がルール判定を追認するだけにならないようにするための指示
_PM_RULES = (
    "各メンバーの意見・反論・応答を読み、最終判定を下す。"
    "多数決ではなく論拠の強さで判断し、採用しなかった少数意見を必ず dissent に残すこと。"
    "判定は委員会自身の結論である。提示されたルール判定は前提情報にすぎず、"
    "それを追認したり、判定の根拠として引用してはならない。"
    "rationale には各メンバーが挙げた具体的な数値と論拠だけを書くこと。"
    "慎重・弱気な論拠が優勢な場合は、ルール判定がホールドや買いであっても "
    "REDUCE / SELL / WAIT / SKIP を選ぶこと。"
    "強制ルール（損切りライン到達・新規買い停止）のみは委員会の判定で覆せないが、"
    "それに該当しない限り委員会の判断を優先すること。"
)


def system_prompt(role: str) -> str:
    """エージェントのシステムプロンプトを返す。"""
    base = f"あなたは運用会社の投資委員会に所属する{ROLE_NAMES[role]}です。"
    if role == "pm":
        return base + _PM_RULES + _COMMON_RULES
    return base + _ROLE_FOCUS[role] + _COMMON_RULES


# ============================================================
# 入力テキスト整形
# ============================================================

def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "データなし"
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}"
    return str(value)


def format_subject(subject: dict) -> str:
    """審議対象の基本情報を整形する。"""
    item = subject["item"]
    ticker = item.get("ticker", "?")
    name = item.get("name") or ""
    market = item.get("market", "")
    if subject["subject_type"] == "holding":
        label = _HOLDING_LABELS.get(item.get("label"), item.get("label"))
        reasons = "、".join(item.get("reasons") or []) or "なし"
        lines = [
            f"## 審議対象（保有銘柄）: {ticker} {name}（{market}）",
            f"ルール判定: {label}（理由: {reasons}）",
            f"取得単価: {_fmt(item.get('entry_price'))} 現在値: {_fmt(item.get('current_price'))} "
            f"損益: {_fmt(item.get('pnl_pct'), 1)}% 保有日数: {item.get('holding_days', 'データなし')}",
            f"損切りライン: {_fmt(item.get('stop_loss_price'))} 利確目標: {_fmt(item.get('target_price'))}",
        ]
    else:
        conditions = item.get("conditions_met") or item.get("_conditions_met") or []
        if isinstance(conditions, str):
            try:
                conditions = json.loads(conditions)
            except (json.JSONDecodeError, TypeError):
                conditions = [conditions]
        lines = [
            f"## 審議対象（新規買い候補）: {ticker} {name}（{market}）",
            f"スコア: {_fmt(item.get('final_score') or item.get('_final_score'), 1)} "
            f"ランク: {item.get('recommend_rank') or item.get('_recommend_rank') or 'データなし'}",
            f"エントリー: {_fmt(item.get('entry_price'))} 損切り: {_fmt(item.get('stop_loss_price'))} "
            f"（幅 {_fmt(item.get('stop_loss_pct'), 1)}%） 目標: {_fmt(item.get('target_price'))}",
            f"推奨投入額: {_fmt(item.get('recommended_amount'), 0)}",
            f"充足条件: {', '.join(str(c) for c in conditions) if conditions else 'なし'}",
        ]
        if item.get("_skip_reason"):
            lines.append(f"ルール上の注意: {item['_skip_reason']}")
    return "\n".join(lines)


_TECH_FIELDS = {
    "ma5": "MA5", "ma25": "MA25", "ma75": "MA75", "ma200": "MA200",
    "ema25": "EMA25", "ema75": "EMA75", "rsi14": "RSI14",
    "macd_diff": "MACD(DIFF)", "macd_dea": "MACDシグナル(DEA)",
    "macd_hist": "MACDヒストグラム", "macd_hist_prev": "MACDヒストグラム(前日)",
    "atr14": "ATR14", "vol_20d_avg": "20日平均出来高", "high_52w": "52週高値", "low_52w": "52週安値",
    "support_level": "サポート", "resistance_level": "レジスタンス",
    "return_5d": "5日リターン", "return_20d": "20日リターン", "rs_score": "RSスコア",
}


def format_technical(tech_row: dict | None, item: dict) -> str:
    """テクニカル・アナリスト向けの入力を整形する。"""
    lines = ["## テクニカル指標"]
    close = item.get("_close") or item.get("current_price") or item.get("entry_price")
    lines.append(f"- 直近終値: {_fmt(close)}")
    if tech_row:
        lines.append(f"基準日: {tech_row.get('date', 'データなし')}")
        for key, label in _TECH_FIELDS.items():
            if tech_row.get(key) is not None:
                lines.append(f"- {label}: {_fmt(tech_row[key])}")
    else:
        lines.append("technical_snapshot: データなし")
    if item.get("_vol_ratio") is not None:
        lines.append(f"- 出来高比(20日平均比): {_fmt(item['_vol_ratio'], 1)}倍")
    return "\n".join(lines)


def format_fundamentals(fund: dict, jq_row: dict | None) -> str:
    """ファンダメンタル・アナリスト向けの入力を整形する。"""
    lines = ["## 財務指標"]
    if jq_row:
        lines.append(f"### J-Quants（{jq_row.get('fiscal_year', '')} {jq_row.get('fiscal_quarter', '')}）")
        for key, label in (("revenue", "売上高"), ("operating_income", "営業利益"), ("net_income", "純利益"),
                           ("eps", "EPS"), ("bps", "BPS"), ("per", "PER"), ("pbr", "PBR"),
                           ("dividend_yield", "配当利回り")):
            if jq_row.get(key) is not None:
                lines.append(f"- {label}: {_fmt(jq_row[key])}")
    data = fund.get("data") or {}
    if data:
        stale = "（※最新取得に失敗したため過去データ）" if fund.get("stale") else ""
        lines.append(f"### yfinance（取得日 {fund.get('fetched_date')}）{stale}")
        for key, label in FUNDAMENTAL_FIELDS.items():
            if key in data:
                lines.append(f"- {label}: {_fmt(data[key])}")
    if len(lines) == 1:
        lines.append("財務データなし。データ不足であることを前提に評価すること。")
    return "\n".join(lines)


def format_risk(item: dict, tech_row: dict | None, risk_row: dict | None, portfolio: dict) -> str:
    """リスク・マネージャー向けの入力を整形する。"""
    lines = ["## リスク指標"]
    if risk_row:
        lines.append(f"- VaR(99%, 1日): {_fmt(risk_row.get('var_99_1d'))}")
        lines.append(f"- Beta(60日): {_fmt(risk_row.get('beta_60d'))}")
        lines.append(f"- Sharpe(60日): {_fmt(risk_row.get('sharpe_60d'))}")
    if tech_row:
        lines.append(f"- 20日ボラティリティ: {_fmt(tech_row.get('volatility_20d'))}")
        lines.append(f"- ATR14: {_fmt(tech_row.get('atr14'))}")
    if item.get("stop_loss_pct") is not None:
        lines.append(f"- 損切り幅: {_fmt(item['stop_loss_pct'], 1)}%")
    if item.get("_warn_wide"):
        lines.append("- 警告: 損切り幅が広い")
    if item.get("_warn_earnings") or item.get("earnings_days") is not None:
        lines.append(f"- 決算接近: {item.get('earnings_date') or 'あり'}（残り{item.get('earnings_days', '?')}日）")
    lines.append(
        f"- ポートフォリオ: 保有 {portfolio.get('holdings_count', 0)} 銘柄 / "
        f"上限 {portfolio.get('max_holdings', 'データなし')} 銘柄"
    )
    return "\n".join(lines)


def format_news(news: list[dict] | None, ticker: str | None = None, limit: int = 5) -> str:
    """ニュースを整形する。ticker 指定時はその銘柄のニュースのみ。"""
    items = [n for n in (news or []) if n.get("ticker") == ticker][:limit]
    if not items:
        return ""
    lines = ["## 直近ニュース"]
    for n in items:
        lines.append(f"- {n.get('title', '')}（{n.get('published', '')}）")
    return "\n".join(lines)


# ============================================================
# プロンプト構築
# ============================================================

def _json_instruction(schema: str) -> str:
    return f"次の JSON 形式のみで回答してください:\n{schema}"


def build_macro_prompt(market_env_text: str, usdjpy: float | None, market_news: str) -> str:
    parts = [market_env_text, f"## 為替\nUSD/JPY: {_fmt(usdjpy)}"]
    if market_news:
        parts.append(market_news)
    parts.append(
        "日本株と米国株のそれぞれについて、本日の市場環境が新規の買いにとって"
        "追い風か逆風かを個別に評価してください。"
        "日経平均は日本株の評価にのみ、S&P500 は米国株の評価にのみ使うこと。"
        "一方の指数しかデータがない場合、もう一方は「データなし」として中立寄りに評価すること。"
    )
    parts.append(_json_instruction(_MACRO_SCHEMA))
    return "\n\n".join(parts)


def build_analyst_prompt(subject_text: str, data_text: str, news_text: str = "") -> str:
    parts = [subject_text, data_text]
    if news_text:
        parts.append(news_text)
    parts.append(_json_instruction(_ANALYST_SCHEMA))
    return "\n\n".join(parts)


def _opinion_lines(opinions: dict) -> str:
    lines = []
    for role, op in opinions.items():
        points = " / ".join(op.get("key_points") or [])
        lines.append(
            f"- {ROLE_NAMES[role]}: {op.get('stance')}（確信度 {op.get('confidence')}）"
            f" {op.get('summary', '')} 論点: {points}"
        )
    return "\n".join(lines)


def build_rebuttal_prompt(subject_text: str, opinions: dict, targets: list[str]) -> str:
    target_names = "、".join(f"{t}（{ROLE_NAMES[t]}）" for t in targets)
    schema = (
        '{"rebuttals": [{"target": "' + " | ".join(targets) + '", "point": "反論 150文字以内"}], '
        '"summary": "全体のダウンサイド評価 200文字以内"}'
    )
    return "\n\n".join([
        subject_text,
        "## Round 1 の意見\n" + _opinion_lines(opinions),
        f"強気意見を述べた {target_names} の論点に対し、悪魔の代弁者として反論してください。"
        "各 target につき反論は 1 件まで。",
        _json_instruction(schema),
    ])


def build_response_prompt(subject_text: str, own_opinion: dict, rebuttal_point: str) -> str:
    schema = '{"response": "150文字以内", "stance_after": "bullish | neutral | bearish"}'
    return "\n\n".join([
        subject_text,
        f"## あなたの Round 1 の意見\n{json.dumps(own_opinion, ensure_ascii=False)}",
        f"## リスク・マネージャーからの反論\n{rebuttal_point}",
        "反論に 1 回だけ応答してください。反論が妥当なら立場を修正して構いません。",
        _json_instruction(schema),
    ])


def build_pm_prompt(
    subject_type: str,
    subject_text: str,
    opinions: dict,
    rebuttal: dict | None,
    responses: dict,
    absent: list[str],
) -> str:
    if subject_type == "holding":
        verdicts = "HOLD | REDUCE | SELL"
        size = "keep | half | exit"
    else:
        verdicts = "BUY | WAIT | SKIP"
        size = "full | half | skip"
    schema = (
        f'{{"verdict": "{verdicts}", "confidence": 0.0〜1.0, "position_size_adj": "{size}", '
        '"rationale": "300文字以内", "dissent": "採用しなかった少数意見 100文字以内"}'
    )
    parts = [subject_text, "## Round 1 の意見\n" + (_opinion_lines(opinions) or "なし")]
    if rebuttal:
        rebuttal_lines = [f"- {ROLE_NAMES.get(r.get('target'), r.get('target'))} への反論: {r.get('point')}"
                          for r in rebuttal.get("rebuttals") or []]
        parts.append("## Round 2: リスク・マネージャーの反論\n" + "\n".join(rebuttal_lines)
                     + f"\n総括: {rebuttal.get('summary', '')}")
    if responses:
        parts.append("## Round 2: 反論への応答\n" + "\n".join(
            f"- {ROLE_NAMES[role]}: {r.get('response')}（修正後の立場: {r.get('stance_after')}）"
            for role, r in responses.items()
        ))
    if absent:
        parts.append("## 欠席\n" + "、".join(ROLE_NAMES.get(a, a) for a in absent)
                     + " は応答できなかった。その観点が欠けていることを考慮すること。")
    parts.append("以上を踏まえて最終判定を下してください。")
    parts.append(_json_instruction(schema))
    return "\n\n".join(parts)
