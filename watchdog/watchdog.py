"""
Watchdog — 全自動化システム横断監視スクリプト

各プロジェクトの SQLite DB (batch_run_log / error_log) を読み取り、
異常を検知した場合のみ Slack に通知する。

検知条件:
  (a) 期待バッチが直近 N 時間以内に success で記録されていない
  (b) error_log に直近 N 時間以内の is_fatal=1 レコードがある
  (c) 本日分のログファイルが存在しない

実行方法（watchdog/ から）:
    python run_watchdog.py
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from shared import db, slack
from shared.config import get_config
from shared.logger import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 検知ロジック
# ---------------------------------------------------------------------------


def check_batch(
    conn: sqlite3.Connection,
    batch_type: str,
    schedule: str,
    window_hours: int,
    *,
    single_batch: bool = False,
) -> dict:
    """
    batch_run_log を確認し、バッチの状態を返す。

    返り値:
        {"batch_type": str, "status": str, "detail": str, "ok": bool}

    status は以下のいずれか:
        "success", "partial_failure", "failed", "running", "missing"

    single_batch=True の場合、batch_type カラムが無いテーブル向けに
    WHERE batch_type 条件を省略する。
    """
    if schedule == "monthly":
        return _check_monthly_batch(conn, batch_type)

    # daily バッチ: 直近 window_hours 以内の最新レコードを取得
    if single_batch:
        row = db.fetch_one(
            conn,
            """
            SELECT status, end_time, start_time
            FROM batch_run_log
            WHERE start_time >= datetime('now', 'localtime', ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"-{window_hours} hours",),
        )
    else:
        row = db.fetch_one(
            conn,
            """
            SELECT status, end_time, start_time
            FROM batch_run_log
            WHERE batch_type = ?
              AND start_time >= datetime('now', 'localtime', ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (batch_type, f"-{window_hours} hours"),
        )

    if row is None:
        return {
            "batch_type": batch_type,
            "status": "missing",
            "detail": f"直近 {window_hours}h 以内に実行記録なし",
            "ok": False,
        }

    status = row["status"]
    ok = status == "success"
    end_time = row["end_time"] or ""
    detail = f"status={status}"
    if end_time:
        detail += f", 終了={end_time}"

    return {
        "batch_type": batch_type,
        "status": status,
        "detail": detail,
        "ok": ok,
    }


def _check_monthly_batch(conn: sqlite3.Connection, batch_type: str) -> dict:
    """月次バッチ: 当月に success レコードがあれば OK。"""
    first_of_month = date.today().replace(day=1).strftime("%Y-%m-%d")
    row = db.fetch_one(
        conn,
        """
        SELECT status, end_time
        FROM batch_run_log
        WHERE batch_type = ?
          AND run_date >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (batch_type, first_of_month),
    )

    if row is None:
        # 月初3日以内はまだ猶予がある
        if date.today().day <= 3:
            return {
                "batch_type": batch_type,
                "status": "pending",
                "detail": "月初 — まだ実行猶予期間",
                "ok": True,
            }
        return {
            "batch_type": batch_type,
            "status": "missing",
            "detail": f"当月 ({first_of_month}〜) に実行記録なし",
            "ok": False,
        }

    status = row["status"]
    ok = status == "success"
    return {
        "batch_type": batch_type,
        "status": status,
        "detail": f"status={status}, 終了={row['end_time'] or '?'}",
        "ok": ok,
    }


def check_fatal_errors(
    conn: sqlite3.Connection,
    lookback_hours: int,
) -> dict:
    """
    error_log に直近 N 時間以内の is_fatal=1 レコードがあるか確認する。

    返り値:
        {"count": int, "ok": bool, "samples": list[str]}
    """
    # error_log テーブルが存在しないシステムはスキップ
    tbl = db.fetch_one(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='error_log'",
    )
    if tbl is None:
        return {"count": 0, "ok": True, "samples": []}

    row = db.fetch_one(
        conn,
        """
        SELECT COUNT(*) as cnt
        FROM error_log
        WHERE is_fatal = 1
          AND error_time >= datetime('now', 'localtime', ?)
        """,
        (f"-{lookback_hours} hours",),
    )
    count = row["cnt"] if row else 0

    samples = []
    if count > 0:
        rows = db.fetch_all(
            conn,
            """
            SELECT error_time, module, error_type, error_message
            FROM error_log
            WHERE is_fatal = 1
              AND error_time >= datetime('now', 'localtime', ?)
            ORDER BY id DESC
            LIMIT 3
            """,
            (f"-{lookback_hours} hours",),
        )
        samples = [
            f"{r['error_time']} [{r['module']}] {r['error_type']}: "
            f"{(r['error_message'] or '')[:80]}"
            for r in rows
        ]

    return {"count": count, "ok": count == 0, "samples": samples}


def check_log_file(log_dir: Path) -> dict:
    """
    本日分のログファイル (batch_YYYYMMDD.log) が存在するか確認する。

    返り値:
        {"exists": bool, "ok": bool, "path": str}
    """
    today_str = date.today().strftime("%Y%m%d")
    log_file = log_dir / f"batch_{today_str}.log"
    exists = log_file.exists()
    return {
        "exists": exists,
        "ok": exists,
        "path": str(log_file),
    }


# ---------------------------------------------------------------------------
# プロジェクト単位の監視
# ---------------------------------------------------------------------------


def check_project(project_key: str, project_cfg: dict, watchdog_cfg: dict) -> dict:
    """
    1プロジェクトの全チェックを実行し、結果を返す。

    返り値:
        {
            "label": str,
            "ok": bool,
            "batches": list[dict],
            "fatal_errors": dict,
            "log_file": dict,
            "db_available": bool,
        }
    """
    base_dir = Path(__file__).resolve().parent
    db_path = (base_dir / project_cfg["db_path"]).resolve()
    log_dir = (base_dir / project_cfg["log_dir"]).resolve()
    label = project_cfg.get("label", project_key)
    window = watchdog_cfg.get("check_window_hours", 26)
    error_lookback = watchdog_cfg.get("fatal_error_lookback_hours", 26)

    result = {
        "label": label,
        "ok": True,
        "batches": [],
        "fatal_errors": {"count": 0, "ok": True, "samples": []},
        "log_file": check_log_file(log_dir),
        "db_available": True,
    }

    if not db_path.exists():
        logger.warning(f"[{label}] DB が見つかりません: {db_path}")
        result["ok"] = False
        result["db_available"] = False
        return result

    try:
        with db.get_connection(db_path) as conn:
            # バッチ状態チェック
            for batch_cfg in project_cfg.get("batches", []):
                batch_result = check_batch(
                    conn,
                    batch_cfg["batch_type"],
                    batch_cfg.get("schedule", "daily"),
                    window,
                    single_batch=batch_cfg.get("single_batch", False),
                )
                result["batches"].append(batch_result)

            # Fatal エラーチェック
            result["fatal_errors"] = check_fatal_errors(conn, error_lookback)

    except Exception as e:
        logger.error(f"[{label}] DB 読み取りエラー: {e}")
        result["ok"] = False
        result["db_available"] = False
        return result

    # 総合判定
    if not all(b["ok"] for b in result["batches"]):
        result["ok"] = False
    if not result["fatal_errors"]["ok"]:
        result["ok"] = False
    if not result["log_file"]["ok"]:
        result["ok"] = False

    return result


# ---------------------------------------------------------------------------
# 新規システム自動発見
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDE_DIRS = {"shared", "shared.egg-info", "watchdog", "claude-study"}


def _find_db_file(system_dir: Path) -> Path | None:
    """システムディレクトリ配下の data/*.db を探す。"""
    for sub in system_dir.iterdir():
        if not sub.is_dir():
            continue
        data_dir = sub / "data"
        if data_dir.is_dir():
            db_files = list(data_dir.glob("*.db"))
            if db_files:
                return db_files[0]
    return None


def _has_batch_run_log(db_path: Path) -> bool:
    """DB に batch_run_log テーブルが存在するか確認する。"""
    try:
        with db.get_connection(db_path) as conn:
            row = db.fetch_one(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='batch_run_log'",
            )
            return row is not None
    except Exception:
        return False


def _detect_batch_types(db_path: Path) -> list[str]:
    """batch_run_log から直近のバッチ種別を取得する。"""
    try:
        with db.get_connection(db_path) as conn:
            rows = db.fetch_all(
                conn,
                "SELECT DISTINCT batch_type FROM batch_run_log ORDER BY batch_type",
            )
            return [r["batch_type"] for r in rows]
    except Exception:
        return []


def _guess_log_dir(db_path: Path) -> Path:
    """DB パスからログディレクトリを推定する。"""
    # data/ と同階層に logs/ があるパターン
    return db_path.parent.parent / "logs"


def discover_new_projects(known_keys: set[str], cfg: dict) -> list[dict]:
    """
    リポジトリルートを走査し、config 未登録のシステムを発見する。

    返り値: check_project() に渡せる (project_key, project_cfg) 相当の辞書リスト
        [{"key": str, "cfg": dict}, ...]
    """
    discovery_cfg = cfg.get("discovery", {})
    if not discovery_cfg.get("enabled", True):
        return []

    exclude = set(discovery_cfg.get("exclude_dirs", _DEFAULT_EXCLUDE_DIRS))

    # config のキーとラベルの両方を正規化して比較対象にする
    known_names = set(known_keys)
    projects_cfg = cfg.get("projects", {})
    for proj_cfg in projects_cfg.values():
        label = proj_cfg.get("label", "")
        known_names.add(label.lower().replace("-", "_").replace(" ", "_"))

    discovered = []

    for entry in sorted(_REPO_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in exclude:
            continue

        # config に既に登録されているか判定
        dir_lower = entry.name.lower().replace("-", "_")
        if dir_lower in known_names:
            continue

        db_path = _find_db_file(entry)
        if db_path is None:
            continue

        if not _has_batch_run_log(db_path):
            logger.info(f"[Discovery] {entry.name}: DB あり・batch_run_log なし → スキップ")
            continue

        batch_types = _detect_batch_types(db_path)
        if not batch_types:
            logger.info(f"[Discovery] {entry.name}: batch_run_log 空 → スキップ")
            continue

        log_dir = _guess_log_dir(db_path)
        batches = [{"batch_type": bt, "schedule": "daily"} for bt in batch_types]

        project_cfg = {
            "label": entry.name,
            "db_path": str(db_path),
            "log_dir": str(log_dir),
            "batches": batches,
        }
        discovered.append({"key": dir_lower, "cfg": project_cfg})
        logger.info(
            f"[Discovery] 新規発見: {entry.name} "
            f"(batches: {', '.join(batch_types)})"
        )

    return discovered


# ---------------------------------------------------------------------------
# Slack 通知
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "success": "OK",
    "partial_failure": "WARN",
    "failed": "NG",
    "running": "RUN",
    "missing": "NG",
    "pending": "---",
}


def _build_project_lines(proj: dict) -> list[str]:
    """1プロジェクト分の表示行を組み立てる。"""
    lines = [f"*--- {proj['label']} ---*"]

    for b in proj["batches"]:
        icon = _STATUS_ICON.get(b["status"], "?")
        lines.append(f"`{b['batch_type']:16s}` [{icon}] {b['detail']}")

    fe = proj["fatal_errors"]
    if not fe["ok"]:
        lines.append(f"`fatal errors   ` [NG] {fe['count']} 件")
        for s in fe["samples"]:
            lines.append(f"    {s}")
    else:
        lines.append("`fatal errors   ` [OK] 0 件")

    lf = proj["log_file"]
    if not lf["ok"]:
        lines.append("`log file       ` [NG] 本日分なし")

    if not proj["db_available"]:
        lines.append("`database       ` [NG] DB ファイルが見つかりません")

    return lines


def build_slack_blocks(
    results: list[dict],
    run_time: str,
    *,
    all_ok: bool,
    discovered_results: list[dict] | None = None,
) -> list[dict]:
    """監視結果から Slack Block Kit の blocks リストを組み立てる。"""
    if all_ok and not discovered_results:
        header = f"*[Watchdog] 全プロジェクト正常  {run_time}*"
    elif all_ok and discovered_results:
        header = f"*[Watchdog] 正常 + 新規発見あり  {run_time}*"
    else:
        header = f"*[Watchdog] バッチ異常検知  {run_time}*"

    blocks = [
        slack.section(header),
        slack.divider(),
    ]

    for proj in results:
        lines = _build_project_lines(proj)
        blocks.append(slack.section("\n".join(lines)))
        blocks.append(slack.divider())

    # 新規発見セクション
    if discovered_results:
        blocks.append(slack.section(
            "*--- 新規発見（config 未登録） ---*\n"
            "_以下のシステムが自動検出されました。"
            "config.json への登録を検討してください。_"
        ))
        blocks.append(slack.divider())
        for proj in discovered_results:
            lines = _build_project_lines(proj)
            blocks.append(slack.section("\n".join(lines)))
            blocks.append(slack.divider())

    return blocks


# ---------------------------------------------------------------------------
# メインエントリーポイント
# ---------------------------------------------------------------------------


def run() -> int:
    """
    全プロジェクトを監視し、異常があれば Slack に通知する。

    返り値:
        0 = 全て正常 (通知なし)
        1 = 異常検知あり (Slack 通知済み)
        2 = watchdog 自体のエラー
    """
    cfg = get_config()
    projects_cfg = cfg.get("projects", {})
    watchdog_cfg = cfg.get("watchdog", {})
    webhook_url = cfg.get("slack", {}).get("webhook_url", "")

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info(f"[Watchdog] 監視開始: {run_time}")
    logger.info(f"対象プロジェクト: {', '.join(projects_cfg.keys())}")

    # --- 登録済みプロジェクトのチェック ---
    results = []
    for key, proj_cfg in projects_cfg.items():
        logger.info(f"--- {proj_cfg.get('label', key)} ---")
        result = check_project(key, proj_cfg, watchdog_cfg)
        results.append(result)
        _log_project_result(result)

    # --- 新規システムの自動発見 ---
    known_keys = set(projects_cfg.keys())
    discovered = discover_new_projects(known_keys, cfg)
    discovered_results = []
    for item in discovered:
        logger.info(f"--- [Discovery] {item['cfg']['label']} ---")
        result = check_project(item["key"], item["cfg"], watchdog_cfg)
        discovered_results.append(result)
        _log_project_result(result)

    if discovered_results:
        logger.info(f"[Discovery] 新規 {len(discovered_results)} システムを検出")

    # --- 総合判定 & Slack 通知 ---
    all_ok = all(r["ok"] for r in results)

    if all_ok:
        logger.info("[Watchdog] 全プロジェクト正常")
        display_results = results
    else:
        failed_results = [r for r in results if not r["ok"]]
        logger.info(f"[Watchdog] 異常検知 {len(failed_results)} プロジェクト")
        display_results = failed_results

    blocks = build_slack_blocks(
        display_results,
        run_time,
        all_ok=all_ok,
        discovered_results=discovered_results or None,
    )
    payload = {"blocks": blocks}
    slack.send_webhook(webhook_url, payload, logger=logger)
    return 0 if all_ok else 1


def _log_project_result(result: dict) -> None:
    """1プロジェクトの監視結果をコンソールに出力する。"""
    for b in result["batches"]:
        icon = _STATUS_ICON.get(b["status"], "?")
        logger.info(f"  {b['batch_type']:16s} [{icon}] {b['detail']}")
    fe = result["fatal_errors"]
    logger.info(f"  {'fatal errors':16s} {'[OK]' if fe['ok'] else '[NG]'} {fe['count']} 件")
    lf = result["log_file"]
    logger.info(f"  {'log file':16s} {'[OK]' if lf['ok'] else '[NG]'}")
