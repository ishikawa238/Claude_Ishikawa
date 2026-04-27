"""
Slack 通知モジュール（FreelanceScout 用）

新着案件を Block Kit 形式で Slack に送信する。
新着0件の場合は通知をスキップしログのみ出力する。
"""

from datetime import date

from shared.slack import (
    divider as _divider,
    section as _section,
    send_webhook,
)

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_WEBHOOK_URL = CONFIG.get("slack", {}).get("webhook_url", "")
_MAX_PER_SITE = int(CONFIG.get("filter", {}).get("max_notify_per_site", 10))


def send_job_report(
    target_date: date,
    new_jobs: list[dict],
    stats: dict,
) -> bool:
    """
    新着案件を Slack に通知する。

    Args:
        target_date: 対象日
        new_jobs: フィルタ済み新着案件リスト（matched_keywords 付き）
        stats: バッチ統計情報
            - crowdworks_found: CW 取得件数
            - coconala_found: CN 取得件数

    Returns:
        送信成功なら True（新着0件でスキップした場合も True）
    """
    if not new_jobs:
        logger.info("新着案件 0 件 — Slack 通知をスキップします。")
        return True

    cw_jobs = [j for j in new_jobs if j["source"] == "crowdworks"]
    cn_jobs = [j for j in new_jobs if j["source"] == "coconala"]

    date_str = target_date.strftime("%Y-%m-%d")
    blocks = []

    # ヘッダー
    blocks.append(_section(
        f"*フリーランス案件レポート | {date_str}*"
    ))
    blocks.append(_divider())

    # サマリー
    cw_found = stats.get("crowdworks_found", 0)
    cn_found = stats.get("coconala_found", 0)
    blocks.append(_section(
        f"新着: *{len(new_jobs)}件*（CW: {len(cw_jobs)} / CN: {len(cn_jobs)}）\n"
        f"取得総数: CW {cw_found}件 / CN {cn_found}件"
    ))
    blocks.append(_divider())

    # CrowdWorks セクション
    if cw_jobs:
        blocks.append(_section(f"*CrowdWorks（{len(cw_jobs)}件）*"))
        _append_job_blocks(blocks, cw_jobs)
        blocks.append(_divider())

    # Coconala セクション
    if cn_jobs:
        blocks.append(_section(f"*Coconala（{len(cn_jobs)}件）*"))
        _append_job_blocks(blocks, cn_jobs)
        blocks.append(_divider())

    payload = {"blocks": blocks}
    success = send_webhook(_WEBHOOK_URL, payload, logger=logger)
    return success


def _append_job_blocks(blocks: list[dict], jobs: list[dict]) -> None:
    """案件ブロックを追加する。max_notify_per_site を超えた分は省略表示。"""
    show = jobs[:_MAX_PER_SITE]
    rest = len(jobs) - len(show)

    for job in show:
        blocks.append(_job_block(job))

    if rest > 0:
        blocks.append(_section(f"_…他 {rest} 件_"))


def _job_block(job: dict) -> dict:
    """1案件分の Block Kit セクションを生成する。"""
    title = job.get("title", "（タイトルなし）")
    url = job.get("url", "")
    budget = job.get("budget", "")
    keywords = job.get("matched_keywords", "")

    parts = [f"*{title}*"]

    if budget:
        parts.append(f"  {budget}")

    if keywords:
        parts.append(f"  マッチ: {keywords}")

    if url:
        parts.append(f"  <{url}|詳細を見る>")

    return _section("\n".join(parts))
