import argparse
import csv
import sys


def load_sales(path: str) -> list[dict]:
    """CSVを読み込んでレコードのリストを返す。amountはfloatに変換する。"""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            records = []
            for i, row in enumerate(reader, start=2):
                try:
                    row["amount"] = float(row["amount"])
                except (KeyError, ValueError, TypeError):
                    raise ValueError(f"{i}行目のamountが不正です: {row.get('amount')!r}")
                records.append(row)
        return records
    except FileNotFoundError:
        raise FileNotFoundError(f"ファイルが見つかりません: {path}")


def summarize(records: list[dict]) -> dict:
    """全体の件数・合計・平均と、カテゴリ別の件数・合計を返す。"""
    count = len(records)
    total = sum((r["amount"] for r in records), 0.0)
    average = total / count if count > 0 else None

    by_category: dict[str, dict] = {}
    for r in records:
        cat = r.get("category", "")
        if cat not in by_category:
            by_category[cat] = {"count": 0, "total": 0.0}
        by_category[cat]["count"] += 1
        by_category[cat]["total"] += r["amount"]

    for stats in by_category.values():
        stats["average"] = stats["total"] / stats["count"]

    return {
        "count": count,
        "total": total,
        "average": average,
        "by_category": by_category,
    }


def format_report(result: dict) -> str:
    """集計結果を文字列にフォーマットする。"""
    lines = ["=== 全体 ==="]
    lines.append(f"件数: {result['count']}")
    lines.append(f"合計: {result['total']:.1f}")
    avg = result["average"]
    lines.append(f"平均: {avg:.1f}" if avg is not None else "平均: N/A")

    lines.append("\n=== カテゴリ別 ===")
    for cat, stats in sorted(result["by_category"].items()):
        lines.append(f"{cat:<10}: 件数={stats['count']}  合計={stats['total']:.1f}  平均={stats['average']:.1f}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="sales.csv を集計するCLIツール")
    parser.add_argument("csv_path", help="集計対象のCSVファイルパス")
    parser.add_argument("--category", help="集計対象のカテゴリ（省略時は全件）")
    parser.add_argument("--top", type=int, help="amount上位N件のみ集計")
    parser.add_argument("--sort", choices=["asc", "desc"], help="amount でソート（asc: 昇順, desc: 降順）")
    args = parser.parse_args()

    try:
        records = load_sales(args.csv_path)
        if args.category:
            records = [r for r in records if r.get("category") == args.category]
            if not records:
                print(f"エラー: カテゴリ '{args.category}' のデータが見つかりません", file=sys.stderr)
                sys.exit(1)
        if args.sort:
            reverse = (args.sort == "desc")
            records = sorted(records, key=lambda r: r["amount"], reverse=reverse)
        if args.top is not None:
            if args.top <= 0:
                print("エラー: --top には1以上の整数を指定してください", file=sys.stderr)
                sys.exit(1)
            if not args.sort:
                records = sorted(records, key=lambda r: r["amount"], reverse=True)
            records = records[:args.top]
        result = summarize(records)
        print(format_report(result))
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
