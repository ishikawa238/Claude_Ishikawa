"""
キーワードマッチスコアリングモジュール

論文のタイトル・Abstract と設定キーワードの一致度でスコアを計算する。
"""

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)


def _calc_score(title: str, abstract: str, scoring_cfg: dict) -> tuple[int, list[str]]:
    """
    1件の論文に対してスコアとマッチキーワードを計算する。

    Returns:
        (score, matched_keywords_list)
    """
    title_lower = title.lower()
    abstract_lower = abstract.lower()
    title_multiplier = scoring_cfg.get("title_multiplier", 3)

    total = 0
    matched = []

    for keyword in scoring_cfg.get("primary_keywords", []):
        kw_lower = keyword.lower()
        weight = scoring_cfg.get("primary_weight", 15)
        hit = False
        if kw_lower in title_lower:
            total += weight * title_multiplier
            hit = True
        if kw_lower in abstract_lower:
            total += weight
            hit = True
        if hit:
            matched.append(keyword)

    for keyword in scoring_cfg.get("secondary_keywords", []):
        kw_lower = keyword.lower()
        weight = scoring_cfg.get("secondary_weight", 5)
        hit = False
        if kw_lower in title_lower:
            total += weight * title_multiplier
            hit = True
        if kw_lower in abstract_lower:
            total += weight
            hit = True
        if hit:
            matched.append(keyword)

    return min(100, total), matched


def score_papers(papers: list[dict]) -> list[dict]:
    """
    論文リストにスコアと matched_keywords を付与して返す。
    入力 dict は変更せず、新しい dict を返す。
    """
    scoring_cfg = CONFIG.get("scoring", {})
    scored = []

    for paper in papers:
        score, matched = _calc_score(
            paper.get("title", ""),
            paper.get("abstract", ""),
            scoring_cfg,
        )
        scored.append({
            **paper,
            "score": score,
            "matched_keywords": ", ".join(matched),
        })

    scored.sort(key=lambda p: p["score"], reverse=True)
    return scored
