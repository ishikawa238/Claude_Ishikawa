"""
投資委員会（マルチエージェント判定）オーケストレータ

買い候補・保有銘柄（売り検討 / 損切り接近）を、役割の異なる複数の LLM エージェントに
審議させ、ポートフォリオ・マネージャー（PM）が最終判定を下す。

プロトコル（固定 2 ラウンド）:
    バッチ開始時: マクロエコノミストが市場概況を 1 回だけ生成（全銘柄で共有）
    Round 1: ファンダ / テクニカル / リスクが互いの意見を見ずに並列で評価
    Round 2: リスク・マネージャーが強気意見に反論 → 反論対象のアナリストのみ 1 回応答
    PM: 全材料から JSON で最終判定

設定:
    config.json の "committee" セクション。committee.enabled = false（既定）の場合は何もしない。
    API キーは llm.<provider>.api_key を共用する。
    要件は docs/investment_committee_requirements.md を参照。
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from src import committee_prompts as prompts
from src.config import CONFIG
from src.data_store import fetch_one, get_connection, insert_one, upsert
from src.fundamentals_fetcher import get_fundamentals
from src.llm_advisor import (
    _call_anthropic,
    _call_gemini,
    _call_openai,
    _estimate_tokens,
    _format_market_env_rich,
)
from src.logger import get_logger

logger = get_logger(__name__)

ANALYSTS = ("fundamental", "technical", "risk")
_REBUTTAL_TARGETS = ("fundamental", "technical")
_HOLDING_LABEL_PRIORITY = {"sl_near": 0, "sell_watch": 1}

_CALLERS = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
}

_VALID_VERDICTS = {
    "buy_candidate": {"BUY", "WAIT", "SKIP"},
    "holding": {"HOLD", "REDUCE", "SELL"},
}

_DEFAULTS: dict = {
    "enabled": False,
    "max_subjects_per_batch": 3,
    "daily_budget_jpy": 45,
    "min_analysts": 2,
    "temperature": 0.2,
    "usdjpy_for_cost": 150.0,
    "agents": {
        "macro": {"provider": "openai", "model": "gpt-4.1-mini", "max_tokens": 600},
        "fundamental": {"provider": "openai", "model": "gpt-4.1-mini", "max_tokens": 600},
        "technical": {"provider": "openai", "model": "gpt-4.1-mini", "max_tokens": 600},
        "risk": {"provider": "openai", "model": "gpt-4.1-mini", "max_tokens": 600},
        "pm": {"provider": "anthropic", "model": "claude-sonnet-4-5", "max_tokens": 900},
    },
    "pricing_usd_per_1m": {
        "default": {"input": 3.0, "output": 15.0},
    },
}


# ============================================================
# 設定
# ============================================================

def get_committee_config() -> dict:
    """committee セクションを既定値とマージして返す。"""
    user = CONFIG.get("committee", {}) or {}
    merged = {**_DEFAULTS, **user}
    merged["agents"] = {**_DEFAULTS["agents"], **(user.get("agents") or {})}
    merged["pricing_usd_per_1m"] = {
        **_DEFAULTS["pricing_usd_per_1m"], **(user.get("pricing_usd_per_1m") or {})
    }
    return merged


def _agent_config(role: str, cfg: dict) -> dict | None:
    """エージェントの呼び出し設定を返す。無効・API キー未設定なら None。"""
    agent = cfg["agents"].get(role) or {}
    if agent.get("enabled") is False:
        return None
    provider = agent.get("provider")
    if provider not in _CALLERS:
        return None
    base = (CONFIG.get("llm", {}) or {}).get(provider, {}) or {}
    merged = {**base, **agent}
    merged["temperature"] = agent.get("temperature", cfg["temperature"])
    api_key = merged.get("api_key", "")
    if not api_key or "YOUR_" in api_key:
        return None
    return merged


# ============================================================
# 公開 API
# ============================================================

def run_committee(
    entry_sets: list[dict],
    holdings: list[dict],
    market_env: dict,
    batch_type: str,
    news: list[dict] | None = None,
    today: date | None = None,
) -> dict | None:
    """
    委員会を開催し、判定結果を返す。

    返り値:
        {"date", "batch_type", "macro", "verdicts": [...], "cost_jpy"}
        committee.enabled = false または PM 未設定の場合は None。
    """
    cfg = get_committee_config()
    if not cfg.get("enabled"):
        logger.info("投資委員会: 無効（config.committee.enabled = false）")
        return None
    if _agent_config("pm", cfg) is None:
        logger.warning("投資委員会: PM の provider / API キーが未設定のため開催しません")
        return None

    today_str = (today or date.today()).strftime("%Y-%m-%d")
    subjects = select_subjects(entry_sets or [], holdings or [], int(cfg["max_subjects_per_batch"]))
    result = {"date": today_str, "batch_type": batch_type, "macro": None, "verdicts": [], "cost_jpy": 0.0}
    if not subjects:
        logger.info("投資委員会: 審議対象なし")
        return result

    macro, macro_usage = _get_macro_brief(market_env, news, batch_type, today_str, cfg)
    result["macro"] = macro
    result["cost_jpy"] += macro_usage["cost_jpy"]

    budget = float(cfg["daily_budget_jpy"])
    for subject in subjects:
        item = subject["item"]
        cached = _load_cached_verdict(today_str, batch_type, item["ticker"], subject["subject_type"])
        if cached:
            logger.info(f"投資委員会: {item['ticker']} は同日キャッシュを使用")
            result["verdicts"].append(cached)
            continue

        if _spent_today(today_str) >= budget:
            verdict = _empty_verdict(subject, "skipped_budget")
            logger.info(f"投資委員会: 日次予算 {budget:.0f} 円に到達。{item['ticker']} は未審議")
        else:
            try:
                verdict = deliberate(subject, macro, holdings or [], news, cfg)
            except Exception as e:
                logger.warning(f"投資委員会: {item['ticker']} の審議で予期しないエラー: {e}")
                verdict = _empty_verdict(subject, "failed")
                verdict["agents"]["errors"] = {"committee": str(e)}

        _save_verdict(verdict, today_str, batch_type)
        result["verdicts"].append(verdict)
        result["cost_jpy"] += verdict["cost_jpy"]

    logger.info(f"投資委員会: {len(result['verdicts'])} 件審議（推定コスト {result['cost_jpy']:.2f} 円）")
    return result


def select_subjects(entry_sets: list[dict], holdings: list[dict], max_subjects: int) -> list[dict]:
    """審議対象を優先順（損切り接近 → 売り検討 → 買い候補スコア降順）で返す。"""
    alerts = sorted(
        (h for h in holdings if h.get("label") in _HOLDING_LABEL_PRIORITY),
        key=lambda h: _HOLDING_LABEL_PRIORITY[h["label"]],
    )
    buys = sorted(
        entry_sets,
        key=lambda es: es.get("final_score") or es.get("_final_score") or 0,
        reverse=True,
    )
    subjects = [{"subject_type": "holding", "item": h} for h in alerts]
    subjects += [{"subject_type": "buy_candidate", "item": es} for es in buys]
    return subjects[:max(0, max_subjects)]


def deliberate(
    subject: dict,
    macro: dict | None,
    holdings: list[dict],
    news: list[dict] | None,
    cfg: dict,
) -> dict:
    """
    1 銘柄を 2 ラウンドで審議し、判定結果を返す。

    macro は `{"jp": 意見, "us": 意見}` 形式で、審議対象の市場に対応する側だけを使う。
    """
    started = time.time()
    item = subject["item"]
    subject_type = subject["subject_type"]
    ticker = item["ticker"]
    market = item.get("market", "")

    agent_cfgs = {role: _agent_config(role, cfg) for role in (*ANALYSTS, "pm")}
    usage_total = _new_usage()
    errors: dict[str, str] = {}
    absent: list[str] = []

    subject_text = prompts.format_subject(subject)
    inputs = _build_analyst_inputs(subject_text, item, ticker, market, holdings, news)

    # Round 1: 独立意見（互いの出力は渡さない）
    # マクロは審議対象の市場に対応する意見だけを渡す
    macro_opinion = macro_for_market(macro, market)
    opinions: dict[str, dict] = {"macro": macro_opinion} if macro_opinion else {}
    if not macro_opinion:
        absent.append("macro")
    round1 = _ask_parallel(
        {role: (role, inputs[role]) for role in ANALYSTS if agent_cfgs[role]},
        agent_cfgs, cfg, _valid_analyst,
    )
    for role in ANALYSTS:
        parsed, usage, err = round1.get(role, (None, _new_usage(), "未設定"))
        _merge_usage(usage_total, usage)
        if parsed:
            opinions[role] = _normalize_opinion(parsed)
        else:
            absent.append(role)
            errors[role] = err or "不明なエラー"

    verdict = _empty_verdict(subject, "failed")
    verdict["absent"] = absent

    if len(opinions) < int(cfg["min_analysts"]):
        errors["committee"] = f"出席アナリスト {len(opinions)} 名で定足数未満"
        return _finish(verdict, opinions, None, {}, None, errors, usage_total, started)

    # Round 2: リスク・マネージャーの反論 → 反論対象のみ応答
    rebuttal, responses = None, {}
    targets = [r for r in _REBUTTAL_TARGETS if opinions.get(r, {}).get("stance") == "bullish"]
    if targets and agent_cfgs["risk"]:
        rebuttal, usage, err = _ask(
            "risk", prompts.build_rebuttal_prompt(subject_text, opinions, targets),
            agent_cfgs["risk"], cfg, _valid_rebuttal,
        )
        _merge_usage(usage_total, usage)
        if rebuttal is None:
            errors["rebuttal"] = err or "不明なエラー"
        else:
            points = {
                r["target"]: r["point"] for r in rebuttal["rebuttals"]
                if isinstance(r, dict) and r.get("target") in targets and r.get("point")
            }
            answered = _ask_parallel(
                {
                    role: (role, prompts.build_response_prompt(subject_text, opinions[role], point))
                    for role, point in points.items()
                },
                agent_cfgs, cfg, _valid_response,
            )
            for role, (parsed, usage, err) in answered.items():
                _merge_usage(usage_total, usage)
                if parsed:
                    responses[role] = parsed
                else:
                    errors[f"response_{role}"] = err or "不明なエラー"

    # PM 判定
    pm, usage, err = _ask(
        "pm",
        prompts.build_pm_prompt(subject_type, subject_text, opinions, rebuttal, responses, absent),
        agent_cfgs["pm"], cfg, _pm_validator(subject_type),
    )
    _merge_usage(usage_total, usage)
    if pm is None:
        errors["pm"] = err or "不明なエラー"
    else:
        verdict.update({
            "verdict": str(pm["verdict"]).upper(),
            "confidence": _clamp(pm.get("confidence")),
            "position_size_adj": pm.get("position_size_adj"),
            "rationale": pm.get("rationale", ""),
            "dissent": pm.get("dissent", ""),
            "status": "partial" if absent else "full",
        })
    return _finish(verdict, opinions, rebuttal, responses, pm, errors, usage_total, started)


# ============================================================
# 審議の部品
# ============================================================

def _build_analyst_inputs(
    subject_text: str,
    item: dict,
    ticker: str,
    market: str,
    holdings: list[dict],
    news: list[dict] | None,
) -> dict[str, str]:
    tech_row = _fetch_latest("SELECT * FROM technical_snapshot WHERE ticker = ? ORDER BY date DESC LIMIT 1", ticker)
    risk_row = _fetch_latest("SELECT * FROM risk_snapshot WHERE ticker = ? ORDER BY calc_date DESC LIMIT 1", ticker)
    jq_row = None
    if market == "JP":
        jq_row = _fetch_latest(
            "SELECT * FROM fundamentals_snapshot WHERE ticker = ? "
            "ORDER BY fetched_at DESC, fiscal_year DESC LIMIT 1",
            ticker,
        )
    try:
        fund = get_fundamentals(ticker, market)
    except Exception as e:
        logger.warning(f"財務データ取得スキップ ({ticker}): {e}")
        fund = {"data": {}, "fetched_date": None, "stale": True}

    portfolio = {
        "holdings_count": len(holdings),
        "max_holdings": (CONFIG.get("capital", {}) or {}).get("max_concurrent_holdings"),
    }
    return {
        "fundamental": prompts.build_analyst_prompt(
            subject_text, prompts.format_fundamentals(fund, jq_row), prompts.format_news(news, ticker)
        ),
        "technical": prompts.build_analyst_prompt(subject_text, prompts.format_technical(tech_row, item)),
        "risk": prompts.build_analyst_prompt(
            subject_text, prompts.format_risk(item, tech_row, risk_row, portfolio)
        ),
    }


def _get_macro_brief(
    market_env: dict,
    news: list[dict] | None,
    batch_type: str,
    today_str: str,
    cfg: dict,
) -> tuple[dict | None, dict]:
    """マクロ概況を返す（同日・同バッチはキャッシュ）。"""
    usage = _new_usage()
    cached = _fetch_latest(
        "SELECT brief_json FROM committee_macro_brief WHERE brief_date = ? AND batch_type = ?",
        today_str, batch_type,
    )
    if cached:
        return json.loads(cached["brief_json"]), usage

    agent_cfg = _agent_config("macro", cfg)
    if agent_cfg is None:
        logger.info("投資委員会: マクロエコノミスト未設定（欠席扱い）")
        return None, usage

    prompt = prompts.build_macro_prompt(
        _format_market_env_rich(market_env or {}), _latest_usdjpy(), prompts.format_news(news, None)
    )
    brief, usage, err = _ask("macro", prompt, agent_cfg, cfg, _valid_macro)
    if brief is None:
        logger.warning(f"投資委員会: マクロ概況の生成に失敗（{err}）")
        return None, usage

    brief = _normalize_macro(brief)
    try:
        with get_connection() as conn:
            upsert(conn, "committee_macro_brief", [{
                "brief_date": today_str,
                "batch_type": batch_type,
                "brief_json": json.dumps(brief, ensure_ascii=False),
                "prompt_tokens": usage["prompt_tokens"],
                "output_tokens": usage["output_tokens"],
                "estimated_cost_jpy": usage["cost_jpy"],
            }])
    except Exception as e:
        logger.warning(f"committee_macro_brief 保存失敗: {e}")
    return brief, usage


def _ask_parallel(requests_by_key: dict, agent_cfgs: dict, cfg: dict, validator) -> dict:
    """{key: (role, prompt)} を並列実行し、{key: (parsed, usage, error)} を返す。"""
    if not requests_by_key:
        return {}
    with ThreadPoolExecutor(max_workers=len(requests_by_key)) as pool:
        futures = {
            key: pool.submit(_ask, role, prompt, agent_cfgs[role], cfg, validator)
            for key, (role, prompt) in requests_by_key.items()
        }
        return {key: fut.result() for key, fut in futures.items()}


def _ask(role: str, user_prompt: str, agent_cfg: dict | None, cfg: dict, validator) -> tuple:
    """
    エージェントに問い合わせ、JSON を検証して返す。
    JSON として解釈できない場合は 1 回だけ再要求する。

    返り値: (parsed dict | None, usage dict, error str | None)
    """
    usage = _new_usage()
    if agent_cfg is None:
        return None, usage, "未設定"

    system = prompts.system_prompt(role)
    prompt = user_prompt
    for _ in range(2):
        try:
            res = _invoke(agent_cfg["provider"], system, prompt, agent_cfg)
        except Exception as e:
            return None, usage, f"{type(e).__name__}: {e}"
        _add_call_usage(usage, res, system + prompt, agent_cfg.get("model", ""), cfg)
        if res.get("error"):
            return None, usage, res["error"]
        parsed = parse_json(res.get("text", ""))
        if parsed is not None and validator(parsed):
            return parsed, usage, None
        prompt = (
            user_prompt
            + "\n\n前回の回答は指定の JSON として解釈できませんでした。JSON オブジェクトのみを出力してください。"
        )
    return None, usage, "JSON解析失敗"


def _invoke(provider: str, system: str, prompt: str, agent_cfg: dict) -> dict:
    """プロバイダー API を呼び出す（テストではモックする）。"""
    return _CALLERS[provider](system, prompt, agent_cfg)


# ============================================================
# JSON 解析・検証
# ============================================================

def parse_json(text: str) -> dict | None:
    """LLM 応答から JSON オブジェクトを取り出す（コードブロック等の前後の文字は無視）。"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


_STANCES = {"bullish", "neutral", "bearish"}


def _valid_analyst(obj: dict) -> bool:
    return str(obj.get("stance", "")).lower() in _STANCES


def _valid_macro(obj: dict) -> bool:
    """マクロ概況は日本株・米国株の両方の立場を含むこと。"""
    return all(
        isinstance(obj.get(key), dict) and str(obj[key].get("stance", "")).lower() in _STANCES
        for key in ("jp", "us")
    )


def _valid_rebuttal(obj: dict) -> bool:
    return isinstance(obj.get("rebuttals"), list)


def _valid_response(obj: dict) -> bool:
    return bool(obj.get("response"))


def _pm_validator(subject_type: str):
    allowed = _VALID_VERDICTS[subject_type]
    return lambda obj: str(obj.get("verdict", "")).upper() in allowed


def _normalize_macro(obj: dict) -> dict:
    """マクロ概況を {"jp": 意見, "us": 意見} に正規化する。注目点・リスクは両市場で共有。"""
    shared = {
        "key_points": list(obj.get("key_points") or [])[:3],
        "risks": list(obj.get("risks") or [])[:2],
    }
    return {
        key: {
            "stance": str((obj.get(key) or {}).get("stance", "")).lower(),
            "confidence": _clamp((obj.get(key) or {}).get("confidence")),
            "summary": str((obj.get(key) or {}).get("summary", "")),
            **shared,
        }
        for key in ("jp", "us")
    }


def macro_for_market(macro: dict | None, market: str) -> dict | None:
    """審議対象の市場に対応するマクロ意見を返す。"""
    if not macro:
        return None
    return macro.get("jp" if str(market).upper() == "JP" else "us")


def _normalize_opinion(obj: dict) -> dict:
    return {
        "stance": str(obj.get("stance", "")).lower(),
        "confidence": _clamp(obj.get("confidence")),
        "key_points": list(obj.get("key_points") or [])[:3],
        "risks": list(obj.get("risks") or [])[:2],
        "summary": str(obj.get("summary", "")),
    }


def _clamp(value) -> float | None:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


# ============================================================
# コスト計測
# ============================================================

def _new_usage() -> dict:
    return {"prompt_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0}


def _merge_usage(total: dict, part: dict) -> None:
    for key in total:
        total[key] += part.get(key, 0)


def _add_call_usage(usage: dict, res: dict, prompt_text: str, model: str, cfg: dict) -> None:
    prompt_tokens = res.get("prompt_tokens") or _estimate_tokens(prompt_text)
    output_tokens = res.get("output_tokens") or _estimate_tokens(res.get("text", ""))
    usage["prompt_tokens"] += prompt_tokens
    usage["output_tokens"] += output_tokens
    usage["cost_jpy"] += estimate_cost_jpy(model, prompt_tokens, output_tokens, cfg)


def estimate_cost_jpy(model: str, prompt_tokens: int, output_tokens: int, cfg: dict) -> float:
    """設定単価（USD / 100 万トークン）から推定コスト（円）を返す。未登録モデルは default 単価。"""
    pricing = cfg["pricing_usd_per_1m"]
    price = pricing.get(model) or pricing["default"]
    usd = prompt_tokens / 1_000_000 * price["input"] + output_tokens / 1_000_000 * price["output"]
    return usd * float(cfg["usdjpy_for_cost"])


# ============================================================
# 判定結果の組み立て
# ============================================================

def _empty_verdict(subject: dict, status: str) -> dict:
    item = subject["item"]
    return {
        "ticker": item["ticker"],
        "name": item.get("name") or "",
        "market": item.get("market", ""),
        "subject_type": subject["subject_type"],
        "verdict": None,
        "confidence": None,
        "position_size_adj": None,
        "rationale": "",
        "dissent": "",
        "absent": [],
        "status": status,
        "agents": {"schema_version": prompts.SCHEMA_VERSION},
        "prompt_tokens": 0,
        "output_tokens": 0,
        "cost_jpy": 0.0,
        "latency_ms": 0,
    }


def _finish(verdict, opinions, rebuttal, responses, pm, errors, usage, started) -> dict:
    verdict["agents"] = {
        "schema_version": prompts.SCHEMA_VERSION,
        "round1": opinions,
        "rebuttal": rebuttal,
        "responses": responses,
        "pm": pm,
        "absent": verdict["absent"],
        "errors": errors,
    }
    verdict["prompt_tokens"] = usage["prompt_tokens"]
    verdict["output_tokens"] = usage["output_tokens"]
    verdict["cost_jpy"] = usage["cost_jpy"]
    verdict["latency_ms"] = int((time.time() - started) * 1000)
    return verdict


# ============================================================
# DB
# ============================================================

def _fetch_latest(sql: str, *params) -> dict | None:
    try:
        with get_connection() as conn:
            row = fetch_one(conn, sql, params)
        return dict(row) if row else None
    except Exception as e:
        logger.debug(f"委員会入力の取得スキップ: {e}")
        return None


def _latest_usdjpy() -> float | None:
    row = _fetch_latest("SELECT usdjpy_rate FROM portfolio_snapshot ORDER BY snapshot_date DESC, id DESC LIMIT 1")
    if row and row.get("usdjpy_rate"):
        return float(row["usdjpy_rate"])
    fallback = (CONFIG.get("capital", {}) or {}).get("usdjpy_fallback")
    return float(fallback) if fallback else None


def _spent_today(today_str: str) -> float:
    """当日の委員会推定コスト合計（円）。"""
    row = _fetch_latest(
        "SELECT "
        "(SELECT COALESCE(SUM(estimated_cost_jpy), 0) FROM committee_verdict_log WHERE verdict_date = ?) + "
        "(SELECT COALESCE(SUM(estimated_cost_jpy), 0) FROM committee_macro_brief WHERE brief_date = ?) AS spent",
        today_str, today_str,
    )
    return float(row["spent"]) if row and row.get("spent") is not None else 0.0


def _load_cached_verdict(today_str: str, batch_type: str, ticker: str, subject_type: str) -> dict | None:
    row = _fetch_latest(
        "SELECT * FROM committee_verdict_log "
        "WHERE verdict_date = ? AND batch_type = ? AND ticker = ? AND subject_type = ? "
        "AND status IN ('full', 'partial') ORDER BY id DESC LIMIT 1",
        today_str, batch_type, ticker, subject_type,
    )
    if not row:
        return None
    agents = json.loads(row.get("agents_json") or "{}")
    return {
        "ticker": row["ticker"],
        "name": "",
        "market": row.get("market") or "",
        "subject_type": row["subject_type"],
        "verdict": row.get("verdict"),
        "confidence": row.get("confidence"),
        "position_size_adj": row.get("position_size_adj"),
        "rationale": row.get("pm_rationale") or "",
        "dissent": row.get("dissent") or "",
        "absent": agents.get("absent", []),
        "status": row["status"],
        "agents": agents,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "cost_jpy": 0.0,
        "latency_ms": 0,
        "cached": True,
    }


def _save_verdict(verdict: dict, today_str: str, batch_type: str) -> None:
    try:
        with get_connection() as conn:
            insert_one(conn, "committee_verdict_log", {
                "verdict_date": today_str,
                "batch_type": batch_type,
                "ticker": verdict["ticker"],
                "market": verdict["market"],
                "subject_type": verdict["subject_type"],
                "verdict": verdict["verdict"],
                "confidence": verdict["confidence"],
                "position_size_adj": verdict["position_size_adj"],
                "pm_rationale": verdict["rationale"],
                "dissent": verdict["dissent"],
                "agents_json": json.dumps(verdict["agents"], ensure_ascii=False),
                "prompt_tokens": verdict["prompt_tokens"],
                "output_tokens": verdict["output_tokens"],
                "estimated_cost_jpy": verdict["cost_jpy"],
                "latency_ms": verdict["latency_ms"],
                "status": verdict["status"],
            })
    except Exception as e:
        logger.warning(f"committee_verdict_log 保存失敗 ({verdict['ticker']}): {e}")
