"""
投資委員会のダッシュボード表示用ヘルパー

committee_verdict_log / committee_macro_brief を読み、画面向けに整形する。
LLM 呼び出しは行わない。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.committee_prompts import ROLE_NAMES
from src.data_store import fetch_all, fetch_one, get_connection

_ANALYST_ORDER = ("macro", "fundamental", "technical", "risk")

_SUBJECT_LABELS = {
    "buy_candidate": "新規買い候補",
    "holding": "保有銘柄",
}

_BATCH_LABELS = {
    "evening": "夜バッチ",
    "morning": "朝バッチ",
    "evening_jp": "夜バッチ",
    "morning_all": "朝バッチ",
}

_STANCE_LABELS = {
    "bullish": "強気",
    "neutral": "中立",
    "bearish": "弱気",
}


def list_verdict_dates(limit: int = 30) -> list[str]:
    """判定がある日付を新しい順に返す。"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT verdict_date FROM committee_verdict_log "
            "ORDER BY verdict_date DESC LIMIT ?",
            (limit,),
        )
    return [r["verdict_date"] for r in rows]


def latest_verdict_date() -> Optional[str]:
    dates = list_verdict_dates(1)
    return dates[0] if dates else None


def load_day(verdict_date: str) -> dict:
    """
    指定日のマクロ概況・判定一覧を返す。

    返り値:
        {
          "date": str,
          "macros": [{"batch_type", "label", "jp", "us", "cost_jpy", ...}, ...],
          "verdicts": [詳細付き判定, ...],
          "cost_jpy": float,
        }
    """
    with get_connection() as conn:
        macro_rows = fetch_all(
            conn,
            "SELECT * FROM committee_macro_brief WHERE brief_date = ? ORDER BY id",
            (verdict_date,),
        )
        verdict_rows = fetch_all(
            conn,
            "SELECT * FROM committee_verdict_log WHERE verdict_date = ? ORDER BY id",
            (verdict_date,),
        )

    macros = [_format_macro(r) for r in macro_rows]
    verdicts = [_format_verdict(r) for r in verdict_rows]
    cost = sum(m.get("cost_jpy") or 0 for m in macros) + sum(
        v.get("cost_jpy") or 0 for v in verdicts
    )
    return {
        "date": verdict_date,
        "macros": macros,
        "verdicts": verdicts,
        "cost_jpy": round(cost, 2),
    }


def load_verdict(verdict_id: int) -> Optional[dict]:
    """単一判定の詳細。無ければ None。"""
    with get_connection() as conn:
        row = fetch_one(conn, "SELECT * FROM committee_verdict_log WHERE id = ?", (verdict_id,))
    return _format_verdict(row) if row else None


def _format_macro(row) -> dict:
    brief = _parse_json(row["brief_json"] if "brief_json" in row.keys() else None)
    # brief は {"jp": {...}, "us": {...}} 形式
    markets = {}
    if isinstance(brief, dict):
        for key in ("jp", "us"):
            part = brief.get(key)
            if isinstance(part, dict):
                markets[key] = _format_opinion(part)
    return {
        "batch_type": row["batch_type"],
        "batch_label": _BATCH_LABELS.get(row["batch_type"], row["batch_type"]),
        "jp": markets.get("jp"),
        "us": markets.get("us"),
        "cost_jpy": float(row["estimated_cost_jpy"] or 0),
        "prompt_tokens": row["prompt_tokens"] or 0,
        "output_tokens": row["output_tokens"] or 0,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
    }


def _format_verdict(row) -> dict:
    agents = _parse_json(row["agents_json"] if "agents_json" in row.keys() else None) or {}
    round1_raw = agents.get("round1") or {}
    round1 = []
    for role in _ANALYST_ORDER:
        op = round1_raw.get(role)
        if isinstance(op, dict):
            round1.append({"role": role, "role_name": ROLE_NAMES.get(role, role), **_format_opinion(op)})
        elif role in (agents.get("absent") or []):
            round1.append({
                "role": role,
                "role_name": ROLE_NAMES.get(role, role),
                "absent": True,
                "stance": None,
                "stance_label": "欠席",
                "confidence": None,
                "summary": "",
                "key_points": [],
                "risks": [],
            })

    rebuttal = agents.get("rebuttal")
    rebuttal_items = []
    if isinstance(rebuttal, dict):
        for item in rebuttal.get("rebuttals") or []:
            if not isinstance(item, dict):
                continue
            target = item.get("target") or ""
            rebuttal_items.append({
                "target": target,
                "target_name": ROLE_NAMES.get(target, target),
                "point": item.get("point") or "",
            })

    responses = []
    for role, resp in (agents.get("responses") or {}).items():
        if not isinstance(resp, dict):
            continue
        responses.append({
            "role": role,
            "role_name": ROLE_NAMES.get(role, role),
            "response": resp.get("response") or "",
            "stance_after": resp.get("stance_after"),
            "stance_after_label": _STANCE_LABELS.get(resp.get("stance_after"), resp.get("stance_after")),
        })

    pm = agents.get("pm") if isinstance(agents.get("pm"), dict) else None
    absent = agents.get("absent") or []
    errors = agents.get("errors") or {}

    return {
        "id": row["id"],
        "date": row["verdict_date"],
        "batch_type": row["batch_type"],
        "batch_label": _BATCH_LABELS.get(row["batch_type"], row["batch_type"]),
        "ticker": row["ticker"],
        "market": row["market"] or "",
        "subject_type": row["subject_type"],
        "subject_label": _SUBJECT_LABELS.get(row["subject_type"], row["subject_type"]),
        "verdict": row["verdict"],
        "confidence": row["confidence"],
        "position_size_adj": row["position_size_adj"],
        "rationale": row["pm_rationale"] or "",
        "dissent": row["dissent"] or "",
        "status": row["status"],
        "cost_jpy": float(row["estimated_cost_jpy"] or 0),
        "latency_ms": row["latency_ms"] or 0,
        "prompt_tokens": row["prompt_tokens"] or 0,
        "output_tokens": row["output_tokens"] or 0,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "round1": round1,
        "rebuttal": rebuttal_items,
        "responses": responses,
        "pm": _format_opinion(pm) if pm else None,
        "absent": [{"role": a, "role_name": ROLE_NAMES.get(a, a)} for a in absent],
        "errors": errors,
        "schema_version": agents.get("schema_version"),
    }


def _format_opinion(op: Optional[dict]) -> dict:
    if not op:
        return {
            "absent": False,
            "stance": None,
            "stance_label": "",
            "confidence": None,
            "summary": "",
            "key_points": [],
            "risks": [],
        }
    stance = op.get("stance")
    return {
        "absent": False,
        "stance": stance,
        "stance_label": _STANCE_LABELS.get(stance, stance or ""),
        "confidence": op.get("confidence"),
        "summary": op.get("summary") or op.get("rationale") or "",
        "key_points": list(op.get("key_points") or []),
        "risks": list(op.get("risks") or []),
        # PM 専用フィールドもそのまま載せる
        "verdict": op.get("verdict"),
        "position_size_adj": op.get("position_size_adj"),
        "rationale": op.get("rationale") or "",
        "dissent": op.get("dissent") or "",
    }


def _parse_json(raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
