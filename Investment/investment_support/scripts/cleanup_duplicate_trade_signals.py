"""
trade_signal_log の重複整理 CLI

既定は dry-run。実際に整理する場合だけ --apply を指定する。
"""

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_store import backup_database, get_connection


def find_duplicate_groups(conn) -> list[dict]:
    """同一シグナルキーで複数行あるグループを返す。"""
    rows = conn.execute(
        """
        SELECT signal_date, ticker, market, pattern, COUNT(*) AS row_count
        FROM trade_signal_log
        GROUP BY signal_date, ticker, market, pattern
        HAVING COUNT(*) > 1
        ORDER BY signal_date, market, ticker, pattern
        """
    ).fetchall()
    return [dict(row) for row in rows]


def cleanup_duplicate_trade_signals(conn, *, apply: bool = False) -> dict:
    """代表IDを残し、参照IDを付け替えてから重複行を整理する。"""
    groups = find_duplicate_groups(conn)
    duplicate_rows = 0
    updated_results = 0
    deleted_rows = 0

    for group in groups:
        rows = conn.execute(
            """
            SELECT id
            FROM trade_signal_log
            WHERE signal_date = ? AND ticker = ? AND market = ? AND pattern = ?
            ORDER BY id ASC
            """,
            (
                group["signal_date"],
                group["ticker"],
                group["market"],
                group["pattern"],
            ),
        ).fetchall()
        keep_id = rows[0]["id"]
        remove_ids = [row["id"] for row in rows[1:]]
        duplicate_rows += len(remove_ids)

        if not apply or not remove_ids:
            continue

        placeholders = ",".join("?" * len(remove_ids))
        if _table_exists(conn, "trade_result_log"):
            cursor = conn.execute(
                f"UPDATE trade_result_log SET signal_id = ? "
                f"WHERE signal_id IN ({placeholders})",
                (keep_id, *remove_ids),
            )
            updated_results += cursor.rowcount

        cursor = conn.execute(
            f"DELETE FROM trade_signal_log WHERE id IN ({placeholders})",
            tuple(remove_ids),
        )
        deleted_rows += cursor.rowcount

    return {
        "groups": len(groups),
        "duplicate_rows": duplicate_rows,
        "updated_results": updated_results,
        "deleted_rows": deleted_rows,
    }


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="バックアップ後に重複を整理する（未指定時は dry-run）",
    )
    args = parser.parse_args()

    if args.apply and not backup_database():
        print("DBバックアップに失敗したため、整理を中止しました。")
        return 1

    with get_connection() as conn:
        result = cleanup_duplicate_trade_signals(conn, apply=args.apply)

    mode = "適用" if args.apply else "dry-run"
    print(
        f"[{mode}] 重複グループ={result['groups']} / "
        f"整理対象行={result['duplicate_rows']} / "
        f"参照更新={result['updated_results']} / "
        f"削除={result['deleted_rows']}"
    )
    if not args.apply and result["duplicate_rows"]:
        print("整理する場合は --apply を指定してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
