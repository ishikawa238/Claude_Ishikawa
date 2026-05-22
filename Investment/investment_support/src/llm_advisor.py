"""
LLM アドバイザーモジュール

買い候補・保有銘柄・市場環境のデータを複数の LLM（OpenAI / Anthropic / Gemini）に送信し、
投資判断のフィードバックを取得する。バッチ実行時に自動で呼び出され、
結果は Slack 通知に含まれる。

設定:
    config.json の "llm" セクションで有効化・APIキーを設定する。
    llm.enabled = false（デフォルト）の場合は何も行わない。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

from src.config import CONFIG
from src.data_store import fetch_all, get_connection, insert_one, log_error
from src.logger import get_logger

logger = get_logger(__name__)

_LLM_CONFIG: dict = CONFIG.get("llm", {})

# プロンプトに含める最大件数
_MAX_ENTRY_SETS = 3
_MAX_HOLDINGS = 5

# Slack セクション上限（3000文字）対策
_MAX_RESPONSE_CHARS = 2800

# プロンプトのトークン上限（各プロバイダーの最小入力枠を目安に安全マージン込み）
_MAX_PROMPT_TOKENS = 80_000

# パターン名の日本語表記
_PATTERN_NAMES = {"A": "モメンタムブレイクアウト", "B": "押し目買い", "C": "サポートバウンス"}

# 市場モードの日本語表記
_MODE_LABELS = {
    "normal": "通常",
    "caution": "警戒",
    "full_caution": "全面警戒",
}

_SYSTEM_PROMPT = (
    "あなたは初心者投資家向けの投資教育アシスタントです。\n"
    "日本株・米国株のテクニカル分析に基づく売買シグナルと保有銘柄の判定結果を確認し、"
    "教育的なフィードバックを日本語で簡潔に提供してください。\n"
    "投資助言ではなく、テクニカル指標の読み方や注意点を教える立場で回答してください。"
)


# ============================================================
# 公開 API
# ============================================================

def get_llm_feedback(
    entry_sets: list[dict],
    holdings: list[dict],
    market_env: dict,
    batch_type: str = "evening",
    news: list[dict] | None = None,
) -> list[dict]:
    """
    有効な LLM プロバイダーに並列でフィードバックを要求する。

    返り値:
        [{"provider": "openai", "model": "...", "text": "...", "error": None}, ...]
        LLM 機能が無効 / API キー未設定の場合は空リストを返す。
    """
    if not _LLM_CONFIG.get("enabled", False):
        logger.info("LLM アドバイザー: 無効（config.llm.enabled = false）")
        return []

    today_str = date.today().strftime("%Y-%m-%d")

    # 同日・同バッチタイプのキャッシュ確認
    try:
        with get_connection() as conn:
            cached = fetch_all(
                conn,
                "SELECT provider, model, response_text FROM llm_feedback_log "
                "WHERE feedback_date = ? AND batch_type = ? AND response_text != ''",
                (today_str, batch_type),
            )
        if cached:
            logger.info(f"LLM アドバイザー: 同日キャッシュあり（{len(cached)}件）。API呼び出しをスキップ")
            return [
                {"provider": r["provider"], "model": r["model"], "text": r["response_text"], "error": None}
                for r in cached
            ]
    except Exception:
        logger.debug("LLM キャッシュ確認スキップ")

    user_prompt = _build_prompt(entry_sets, holdings, market_env, news=news)

    providers: list[tuple[str, dict]] = []
    for name in ("openai", "anthropic", "gemini"):
        if _is_enabled(name):
            providers.append((name, _LLM_CONFIG[name]))

    if not providers:
        logger.info("LLM アドバイザー: 有効なプロバイダーなし")
        return []

    logger.info(f"LLM アドバイザー: {len(providers)} プロバイダーに問い合わせ中...")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        _CALLERS = {
            "openai": _call_openai,
            "anthropic": _call_anthropic,
            "gemini": _call_gemini,
        }
        futures = {}
        for name, cfg in providers:
            caller = _CALLERS[name]
            fut = pool.submit(caller, _SYSTEM_PROMPT, user_prompt, cfg)
            futures[fut] = (name, cfg.get("model", "unknown"))

        for future in as_completed(futures):
            provider_name, model = futures[future]
            try:
                result = future.result()
                result["provider"] = provider_name
                result["model"] = model
                results.append(result)

                # DB に保存
                _save_feedback(result, batch_type, today_str)

                if result.get("error"):
                    logger.warning(f"LLM [{provider_name}] エラー: {result['error']}")
                else:
                    logger.info(f"LLM [{provider_name}] フィードバック取得完了 "
                                f"({result.get('latency_ms', '?')}ms)")
            except Exception as e:
                logger.warning(f"LLM [{provider_name}] 予期しないエラー: {e}")
                results.append({
                    "provider": provider_name,
                    "model": model,
                    "text": "",
                    "error": str(e),
                })

    # 先頭の結果にプロンプト全文を添付（Slack 通知用）
    if results:
        results[0]["_prompt"] = user_prompt

    return results


# ============================================================
# トークン推計
# ============================================================

def _estimate_tokens(text: str) -> int:
    """テキストのトークン数をヒューリスティックに推計する。

    日本語は平均3文字/トークン、ASCII は平均4文字/トークン、
    マークダウン構造のオーバーヘッドを10%加算する。
    """
    jp_chars = sum(1 for c in text if ord(c) > 0x2FFF)
    ascii_chars = len(text) - jp_chars
    raw = jp_chars / 3 + ascii_chars / 4
    return int(raw * 1.1)


# ============================================================
# プロンプト構築
# ============================================================

def _build_prompt(
    entry_sets: list[dict],
    holdings: list[dict],
    market_env: dict,
    news: list[dict] | None = None,
) -> str:
    """データをプロンプト文字列に整形する。"""
    sections = []

    # 市場環境（MA75/200 + モード推移を含む拡充版）
    sections.append(_format_market_env_rich(market_env))

    # 買い候補
    sections.append(_format_entry_sets_rich(entry_sets[:_MAX_ENTRY_SETS]))

    # 保有銘柄
    sections.append(_format_holdings_rich(holdings[:_MAX_HOLDINGS]))

    # ニュース
    if news:
        sections.append(_format_news(news))

    # 回答指示
    if news:
        sections.append(
            "以下の4項目について、各200文字以内で回答してください：\n"
            "1. 買い候補の分析（テクニカル的に説得力があるか？注意すべき点は？）\n"
            "2. 保有銘柄の判断（ホールド/売り判定に同意するか？見落としている観点は？）\n"
            "3. ニュースの影響（直近ニュースが候補・保有銘柄に与える影響は？）\n"
            "4. 初心者向け学びポイント（今日のデータから学べる知識）"
        )
    else:
        sections.append(
            "以下の3項目について、各200文字以内で回答してください：\n"
            "1. 買い候補の分析（テクニカル的に説得力があるか？注意すべき点は？）\n"
            "2. 保有銘柄の判断（ホールド/売り判定に同意するか？見落としている観点は？）\n"
            "3. 初心者向け学びポイント（今日のデータから学べるテクニカル分析の知識）"
        )

    prompt = "\n\n".join(sections)

    # トークン上限ガード: 超過する場合はニュース→保有→候補の順に削減
    est = _estimate_tokens(prompt)
    if est > _MAX_PROMPT_TOKENS:
        logger.warning(f"プロンプト推計 {est} トークン > 上限 {_MAX_PROMPT_TOKENS}。ニュースを削除して再構築します")
        sections_trimmed = [s for s in sections if not s.startswith("## 直近の市場ニュース")]
        prompt = "\n\n".join(sections_trimmed)
        est = _estimate_tokens(prompt)
        if est > _MAX_PROMPT_TOKENS:
            logger.warning(f"ニュース削除後も推計 {est} トークン。情報量を縮小します")
            sections_trimmed = []
            sections_trimmed.append(_format_market_env_rich(market_env))
            sections_trimmed.append(_format_entry_sets_rich(entry_sets[:1]))
            sections_trimmed.append(_format_holdings_rich(holdings[:2]))
            sections_trimmed.append(sections[-1])  # 回答指示は残す
            prompt = "\n\n".join(sections_trimmed)
    else:
        logger.debug(f"プロンプト推計: {est} トークン")

    return prompt


def _format_market_env_rich(market_env: dict) -> str:
    """市場環境を拡充テキストに変換する（MA75/200 + モード推移付き）。"""
    mode = market_env.get("mode", "unknown")
    label = _MODE_LABELS.get(mode, mode)

    # 直近3日のモード推移を DB から取得
    mode_history = _fetch_mode_history()
    if mode_history:
        history_str = " → ".join(mode_history)
        header = f"## 市場環境\nモード: {label}（推移: {history_str}）"
    else:
        header = f"## 市場環境\nモード: {label}"

    parts = [header]

    # 日経平均（MA25/75/200）
    n225 = market_env.get("n225_close")
    n225_ma25 = market_env.get("n225_ma25")
    if n225 and n225_ma25:
        n225_ma75, n225_ma200 = _fetch_index_mas("N225")
        ma_parts = [f"MA25比 {_pct(n225, n225_ma25)}"]
        if n225_ma75:
            ma_parts.append(f"MA75比 {_pct(n225, n225_ma75)}")
        if n225_ma200:
            ma_parts.append(f"MA200比 {_pct(n225, n225_ma200)}")
        parts.append(f"日経平均: {n225:,.0f}（{', '.join(ma_parts)}）")

    # S&P500（MA25/75/200）
    sp500 = market_env.get("sp500_close")
    sp500_ma25 = market_env.get("sp500_ma25")
    if sp500 and sp500_ma25:
        sp500_ma75, sp500_ma200 = _fetch_index_mas("SP500")
        ma_parts = [f"MA25比 {_pct(sp500, sp500_ma25)}"]
        if sp500_ma75:
            ma_parts.append(f"MA75比 {_pct(sp500, sp500_ma75)}")
        if sp500_ma200:
            ma_parts.append(f"MA200比 {_pct(sp500, sp500_ma200)}")
        parts.append(f"S&P500: {sp500:,.0f}（{', '.join(ma_parts)}）")

    return "\n".join(parts)


def _format_entry_sets_rich(entry_sets: list[dict]) -> str:
    """エントリーセットを拡充テキストに変換する。"""
    if not entry_sets:
        return "## 本日の買い候補\nなし"

    lines = [f"## 本日の買い候補（{len(entry_sets)}件）"]
    for es in entry_sets:
        ticker = es.get("ticker", "?")
        name = es.get("name", es.get("_name", ""))
        pattern = es.get("pattern", "?")
        pattern_name = _PATTERN_NAMES.get(pattern, pattern)
        score = es.get("final_score") or es.get("_final_score", "?")
        rank = es.get("recommend_rank") or es.get("_recommend_rank", "?")
        rsi = es.get("_rsi14") or es.get("rsi14", "?")
        entry = es.get("entry_price", "?")
        target = es.get("target_price", "?")
        sl_pct = es.get("stop_loss_pct", "?")

        # サブスコア
        trend_s = es.get("_trend_score")
        pat_a_s = es.get("_pattern_a_score")
        pat_b_s = es.get("_pattern_b_score")
        pat_c_s = es.get("_pattern_c_score")
        liq_s = es.get("_liquidity_score")
        sub_parts = []
        if trend_s is not None:
            sub_parts.append(f"トレンド:{trend_s:.0f}")
        pat_score = {"A": pat_a_s, "B": pat_b_s, "C": pat_c_s}.get(pattern)
        if pat_score is not None:
            sub_parts.append(f"パターン:{pat_score:.0f}")
        if liq_s is not None:
            sub_parts.append(f"流動性:{liq_s:.0f}")
        sub_str = f"｜{' '.join(sub_parts)}" if sub_parts else ""

        lines.append(
            f"- {ticker} {name}（{pattern_name}）"
            f" スコア:{score} ランク:{rank}{sub_str}"
        )

        # テクニカル詳細行
        detail_parts = [f"RSI:{rsi}"]
        vol_ratio = es.get("_vol_ratio")
        if vol_ratio is not None:
            detail_parts.append(f"出来高:{vol_ratio:.1f}倍")
        high_52w = es.get("_high_52w")
        close = es.get("_close")
        if high_52w and close and high_52w > 0:
            dist_pct = (high_52w - close) / high_52w * 100
            detail_parts.append(f"52週高値まで:{dist_pct:.1f}%")
        lines.append(f"  {' '.join(detail_parts)}")

        # MA配置
        ma5 = es.get("_ma5")
        ma25 = es.get("_ma25")
        ma75 = es.get("_ma75")
        ma200 = es.get("_ma200")
        ma_line = _format_ma_alignment(ma5, ma25, ma75, ma200)
        if ma_line:
            lines.append(f"  {ma_line}")

        # エントリー/目標/損切り + 推奨投入額
        entry_line = f"  エントリー:{entry} → 目標:{target}（SL幅:{sl_pct}%）"
        amount = es.get("recommended_amount")
        if amount:
            entry_line += f" 推奨投入額:{amount:,.0f}円"
        skip = es.get("_skip_reason")
        if skip:
            entry_line += f" ※{skip}"
        lines.append(entry_line)

        # 充足条件
        conditions = es.get("conditions_met") or es.get("_conditions_met", "")
        if isinstance(conditions, str):
            try:
                conditions = json.loads(conditions)
            except (json.JSONDecodeError, TypeError):
                pass
        if conditions and isinstance(conditions, list):
            lines.append(f"  充足条件: {', '.join(str(c) for c in conditions)}")

        # 警告
        warns = []
        if es.get("_warn_wide"):
            warns.append("損切り幅が広い")
        if es.get("_warn_earnings"):
            warns.append("決算接近")
        if warns:
            lines.append(f"  ⚠ {', '.join(warns)}")

    return "\n".join(lines)


def _format_holdings_rich(holdings: list[dict]) -> str:
    """保有銘柄を拡充テキストに変換する。"""
    if not holdings:
        return "## 保有銘柄\nなし"

    # 総資金（ポートフォリオ比率計算用）
    cap_cfg = CONFIG.get("capital", {})
    total_cap_jpy = float(cap_cfg.get("total_capital_jpy", 0))
    total_cap_usd = float(cap_cfg.get("total_capital_usd", 0))

    lines = [f"## 保有銘柄（{len(holdings)}件）"]
    for h in holdings:
        ticker = h.get("ticker", "?")
        name = h.get("name", "")
        pnl_pct = h.get("pnl_pct", 0)
        days = h.get("holding_days", "?")
        label = h.get("label", "?")
        label_jp = _HOLDING_LABELS.get(label, label)
        quantity = h.get("quantity")

        qty_str = f" {quantity:g}株" if quantity else ""
        lines.append(
            f"- {ticker} {name}{qty_str}"
            f" 損益:{pnl_pct:+.1f}%"
            f" 保有:{days}日"
            f" 判定:{label_jp}"
        )

        # 評価額・ポートフォリオ比率
        current = h.get("current_price")
        if quantity and current:
            market = h.get("market", "")
            value = quantity * current
            total_cap = total_cap_jpy if market == "JP" else total_cap_usd
            currency = "円" if market == "JP" else "USD"
            val_str = f"評価額:{value:,.0f}{currency}"
            if total_cap > 0:
                weight = value / total_cap * 100
                val_str += f"（ポートフォリオ比率:{weight:.1f}%）"
            lines.append(f"  {val_str}")

        # 現在値・目標・損切りの距離
        target = h.get("target_price")
        sl = h.get("stop_loss_price")
        price_parts = []
        if current:
            price_parts.append(f"現在値:{current:,.0f}")
        if target and current and current > 0:
            target_dist = (target - current) / current * 100
            price_parts.append(f"目標:{target:,.0f}（残り{target_dist:+.1f}%）")
        if sl and current and current > 0:
            sl_dist = (current - sl) / current * 100
            price_parts.append(f"損切り:{sl:,.0f}（余裕{sl_dist:.1f}%）")
        if price_parts:
            lines.append(f"  {' '.join(price_parts)}")

        # MA + RSI
        ma5 = h.get("tech_ma5")
        ma25 = h.get("tech_ma25")
        ma200 = h.get("tech_ma200")
        rsi = h.get("tech_rsi14", "?")
        tech_parts = []
        if ma5:
            tech_parts.append(f"MA5:{ma5:,.0f}")
        if ma25:
            tech_parts.append(f"MA25:{ma25:,.0f}")
        if ma200:
            tech_parts.append(f"MA200:{ma200:,.0f}")
        tech_parts.append(f"RSI:{rsi}")
        lines.append(f"  {' '.join(tech_parts)}")

        # 決算接近アラート
        earnings_date = h.get("earnings_date")
        earnings_days = h.get("earnings_days")
        if earnings_date and earnings_days is not None:
            lines.append(f"  決算発表まで{earnings_days}日（{earnings_date}）")

        # 理由
        reasons = h.get("reasons", [])
        reason_text = "、".join(reasons) if reasons else "なし"
        lines.append(f"  理由: {reason_text}")

    return "\n".join(lines)


def _format_news(news: list[dict]) -> str:
    """ニュースリストをプロンプト用テキストに変換する。"""
    if not news:
        return ""

    lines = [f"## 直近の市場ニュース（{len(news)}件）"]
    for item in news:
        title = item.get("title", "")
        pub = item.get("published", "")
        source = item.get("source", "")
        ticker = item.get("ticker")

        if ticker:
            prefix = f"[{ticker}] "
        else:
            prefix = ""

        date_str = f"（{pub}）" if pub else ""
        source_str = f" - {source}" if source else ""
        lines.append(f"- {prefix}{title}{date_str}{source_str}")

    return "\n".join(lines)


# ============================================================
# プロンプト用ヘルパー
# ============================================================

_HOLDING_LABELS = {
    "forced_stop": "強制損切り",
    "sl_near": "損切り接近",
    "sell_watch": "売り検討",
    "hold": "ホールド",
}


def _pct(value: float, base: float) -> str:
    """パーセント差を +X.X% 形式で返す。"""
    if not base:
        return "N/A"
    return f"{(value / base - 1) * 100:+.1f}%"


def _format_ma_alignment(
    ma5: float | None,
    ma25: float | None,
    ma75: float | None,
    ma200: float | None,
) -> str:
    """MA配置を「MA5 > MA25 > MA75 > MA200 整列」形式で返す。"""
    mas = []
    if ma5 is not None:
        mas.append(("MA5", ma5))
    if ma25 is not None:
        mas.append(("MA25", ma25))
    if ma75 is not None:
        mas.append(("MA75", ma75))
    if ma200 is not None:
        mas.append(("MA200", ma200))

    if len(mas) < 2:
        return ""

    parts = [f"{name}({val:,.0f})" for name, val in mas]
    # 整列判定: 全て降順ならパーフェクトオーダー
    values = [val for _, val in mas]
    aligned = all(values[i] >= values[i + 1] for i in range(len(values) - 1))
    suffix = " 整列" if aligned else ""

    separators = []
    for i in range(len(values) - 1):
        separators.append(" > " if values[i] >= values[i + 1] else " < ")

    result = parts[0]
    for i, sep in enumerate(separators):
        result += sep + parts[i + 1]
    return f"MA配置: {result}{suffix}"


def _fetch_index_mas(index_code: str) -> tuple[float | None, float | None]:
    """index_snapshot から最新の MA75, MA200 を取得する。"""
    try:
        with get_connection() as conn:
            rows = fetch_all(
                conn,
                "SELECT ma75, ma200 FROM index_snapshot "
                "WHERE index_code = ? AND ma75 IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                (index_code,),
            )
        if rows:
            return rows[0].get("ma75"), rows[0].get("ma200")
    except Exception as e:
        logger.warning(f"index_snapshot MA取得失敗 ({index_code}): {e}")
    return None, None


def _fetch_mode_history(days: int = 3) -> list[str]:
    """直近N日の市場モード推移を取得する。"""
    try:
        today = date.today()
        start = today - timedelta(days=days + 5)  # 営業日考慮で余裕を持つ
        with get_connection() as conn:
            rows = fetch_all(
                conn,
                "SELECT date, mode FROM market_environment "
                "WHERE date >= ? AND date < ? "
                "ORDER BY date DESC LIMIT ?",
                (start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), days),
            )
        if rows:
            # 古い順に返す
            return [_MODE_LABELS.get(r["mode"], r["mode"]) for r in reversed(rows)]
    except Exception as e:
        logger.warning(f"モード推移取得失敗: {e}")
    return []


# ============================================================
# LLM API 呼び出し
# ============================================================

_MAX_RETRIES = 2
_RETRY_WAIT_SEC = 3

_QUOTA_KEYWORDS = ["quota", "billing", "insufficient_quota", "exceeded"]


def _is_quota_error(body_text: str) -> bool:
    """429 がレート制限ではなく課金枠不足かどうか判定する。"""
    lower = body_text.lower()
    return any(kw in lower for kw in _QUOTA_KEYWORDS)


def _make_error(status: int, body_text: str, latency_ms: int) -> dict:
    """エラー結果を生成する。課金枠不足の場合は分かりやすいメッセージを付加。"""
    if status == 429 and _is_quota_error(body_text):
        hint = "（課金枠不足: APIダッシュボードでクレジットを追加してください）"
    elif status == 429:
        hint = "（レート制限: リトライ後も失敗）"
    else:
        hint = ""
    return {
        "text": "",
        "error": f"HTTP {status}{hint}: {body_text[:200]}",
        "latency_ms": latency_ms,
        "prompt_tokens": None,
        "output_tokens": None,
    }


def _call_openai(system_prompt: str, user_prompt: str, config: dict) -> dict:
    """OpenAI Chat Completions API を呼び出す。"""
    api_key = config["api_key"]
    model = config.get("model", "gpt-4.1")
    max_tokens = config.get("max_tokens", 800)
    temperature = config.get("temperature", 0.3)
    timeout = config.get("timeout_sec", 30)

    for attempt in range(_MAX_RETRIES + 1):
        start = time.time()
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            usage = body.get("usage", {})
            text = body["choices"][0]["message"]["content"]
            return {
                "text": text[:_MAX_RESPONSE_CHARS],
                "error": None,
                "latency_ms": latency_ms,
                "prompt_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            }

        # 429 かつ課金枠不足 → リトライしても無駄
        if resp.status_code == 429 and _is_quota_error(resp.text):
            return _make_error(resp.status_code, resp.text, latency_ms)

        # 429 (レート制限) or 5xx → リトライ
        if resp.status_code in (429, 500, 502, 503) and attempt < _MAX_RETRIES:
            logger.info(f"LLM [openai] HTTP {resp.status_code} → {_RETRY_WAIT_SEC}秒後リトライ ({attempt+1}/{_MAX_RETRIES})")
            time.sleep(_RETRY_WAIT_SEC)
            continue

        return _make_error(resp.status_code, resp.text, latency_ms)

    return _make_error(resp.status_code, resp.text, latency_ms)


def _call_anthropic(system_prompt: str, user_prompt: str, config: dict) -> dict:
    """Anthropic Messages API を呼び出す。"""
    api_key = config["api_key"]
    model = config.get("model", "claude-opus-4-6")
    max_tokens = config.get("max_tokens", 800)
    temperature = config.get("temperature", 0.3)
    timeout = config.get("timeout_sec", 30)

    for attempt in range(_MAX_RETRIES + 1):
        start = time.time()
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            usage = body.get("usage", {})
            text = body["content"][0]["text"]
            return {
                "text": text[:_MAX_RESPONSE_CHARS],
                "error": None,
                "latency_ms": latency_ms,
                "prompt_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }

        if resp.status_code == 429 and _is_quota_error(resp.text):
            return _make_error(resp.status_code, resp.text, latency_ms)

        if resp.status_code in (429, 500, 502, 503, 529) and attempt < _MAX_RETRIES:
            logger.info(f"LLM [anthropic] HTTP {resp.status_code} → {_RETRY_WAIT_SEC}秒後リトライ ({attempt+1}/{_MAX_RETRIES})")
            time.sleep(_RETRY_WAIT_SEC)
            continue

        return _make_error(resp.status_code, resp.text, latency_ms)

    return _make_error(resp.status_code, resp.text, latency_ms)


def _call_gemini(system_prompt: str, user_prompt: str, config: dict) -> dict:
    """Google Gemini API を呼び出す。"""
    api_key = config["api_key"]
    model = config.get("model", "gemini-2.5-pro")
    max_tokens = config.get("max_tokens", 800)
    temperature = config.get("temperature", 0.3)
    timeout = config.get("timeout_sec", 30)

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={api_key}"
    )

    for attempt in range(_MAX_RETRIES + 1):
        start = time.time()
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            },
            timeout=timeout,
        )
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            usage = body.get("usageMetadata", {})
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return {
                "text": text[:_MAX_RESPONSE_CHARS],
                "error": None,
                "latency_ms": latency_ms,
                "prompt_tokens": usage.get("promptTokenCount"),
                "output_tokens": usage.get("candidatesTokenCount"),
            }

        if resp.status_code == 429 and _is_quota_error(resp.text):
            return _make_error(resp.status_code, resp.text, latency_ms)

        if resp.status_code in (429, 500, 502, 503) and attempt < _MAX_RETRIES:
            logger.info(f"LLM [gemini] HTTP {resp.status_code} → {_RETRY_WAIT_SEC}秒後リトライ ({attempt+1}/{_MAX_RETRIES})")
            time.sleep(_RETRY_WAIT_SEC)
            continue

        return _make_error(resp.status_code, resp.text, latency_ms)

    return _make_error(resp.status_code, resp.text, latency_ms)


# ============================================================
# ユーティリティ
# ============================================================

def _is_enabled(provider: str) -> bool:
    """指定プロバイダーが有効かつ API キーが設定されているか確認する。"""
    cfg = _LLM_CONFIG.get(provider, {})
    if not cfg.get("enabled", False):
        return False
    api_key = cfg.get("api_key", "")
    # プレースホルダーを検出
    if not api_key or "YOUR_" in api_key:
        return False
    return True


def _save_feedback(feedback: dict, batch_type: str, feedback_date: str) -> None:
    """フィードバックを llm_feedback_log テーブルに保存する。"""
    try:
        with get_connection() as conn:
            insert_one(conn, "llm_feedback_log", {
                "feedback_date": feedback_date,
                "batch_type": batch_type,
                "provider": feedback["provider"],
                "model": feedback["model"],
                "prompt_tokens": feedback.get("prompt_tokens"),
                "output_tokens": feedback.get("output_tokens"),
                "response_text": feedback.get("text", "") or feedback.get("error", ""),
                "latency_ms": feedback.get("latency_ms"),
            })
    except Exception as e:
        logger.warning(f"LLM フィードバックの DB 保存に失敗: {e}")
