"""
リファクタリング監査モジュール

全プロジェクトのソースコードと DB スキーマを横断比較し、
統一されていないパターンや共有化すべきモジュールを検出してレポートする。

検出項目:
  1. DB スキーマ比較 (batch_run_log, error_log)
  2. data_store.py の log_error() シグネチャ比較
  3. notifier.py の公開関数シグネチャ比較
  4. 標準ファイルの欠落 (init_db.py, error_log テーブル等)
  5. config.py / logger.py の共有パターン準拠チェック

実行方法（watchdog/ から）:
    python run_refactor_audit.py
"""

import ast
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from shared import slack
from shared.config import get_config
from shared.logger import get_logger

logger = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent  # Ishikawa238/

# ---------------------------------------------------------------------------
# プロジェクト定義
# ---------------------------------------------------------------------------

# 各プロジェクトの src/ と scripts/ のルートを定義
_PROJECTS = {
    "Investment": {
        "src_root": _BASE_DIR / "Investment" / "investment_support" / "src",
        "scripts_root": _BASE_DIR / "Investment" / "investment_support" / "scripts",
        "db_path": _BASE_DIR / "Investment" / "investment_support" / "data" / "investment.db",
    },
    "FreelanceScout": {
        "src_root": _BASE_DIR / "FreelanceScout" / "freelance_scout" / "src",
        "scripts_root": _BASE_DIR / "FreelanceScout" / "freelance_scout" / "scripts",
        "db_path": _BASE_DIR / "FreelanceScout" / "freelance_scout" / "data" / "freelance_scout.db",
    },
    "AiNewsDigest": {
        "src_root": _BASE_DIR / "AiNewsDigest" / "ai_news_digest" / "src",
        "scripts_root": _BASE_DIR / "AiNewsDigest" / "ai_news_digest" / "scripts",
        "db_path": _BASE_DIR / "AiNewsDigest" / "ai_news_digest" / "data" / "ai_news_digest.db",
    },
    "PaperWatch": {
        "src_root": _BASE_DIR / "PaperWatch" / "paper_watch" / "src",
        "scripts_root": _BASE_DIR / "PaperWatch" / "paper_watch" / "scripts",
        "db_path": _BASE_DIR / "PaperWatch" / "paper_watch" / "data" / "paper_watch.db",
    },
}

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def _get_table_columns(db_path: Path, table: str) -> list[str] | None:
    """SQLite DB からテーブルのカラム名リストを取得する。テーブル不在なら None。"""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cursor.fetchall()]
        conn.close()
        return cols if cols else None
    except Exception:
        return None


def _parse_function_signatures(py_path: Path) -> dict[str, dict]:
    """
    Python ファイルをパースし、トップレベル関数のシグネチャ情報を返す。

    返り値: {関数名: {"args": [...], "return_annotation": str | None, "line": int}}
    """
    if not py_path.exists():
        return {}
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return {}

    results = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        # プライベート関数は除外
        if node.name.startswith("_"):
            continue

        args_info = []
        for arg in node.args.args:
            ann = ast.unparse(arg.annotation) if arg.annotation else None
            args_info.append({"name": arg.arg, "annotation": ann})

        ret = ast.unparse(node.returns) if node.returns else None
        results[node.name] = {
            "args": args_info,
            "return_annotation": ret,
            "line": node.lineno,
        }
    return results


def _check_file_contains(py_path: Path, pattern: str) -> bool:
    """ファイルが存在し、指定正規表現にマッチする行を含むか。"""
    if not py_path.exists():
        return False
    try:
        text = py_path.read_text(encoding="utf-8")
        return bool(re.search(pattern, text))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 個別チェック
# ---------------------------------------------------------------------------


def check_db_schemas() -> list[dict]:
    """
    全プロジェクトの batch_run_log / error_log テーブルスキーマを比較する。

    返り値: 検出された問題のリスト
    """
    findings = []
    common_batch_cols = {"id", "run_date", "batch_type", "start_time", "end_time", "status"}
    common_error_cols = {"id", "error_time", "batch_type", "module", "error_type", "error_message", "is_fatal"}

    batch_schemas = {}
    error_schemas = {}

    for name, cfg in _PROJECTS.items():
        db_path = cfg["db_path"]
        if not db_path.exists():
            findings.append({
                "check": "db_schema",
                "project": name,
                "severity": "info",
                "message": f"DB ファイルが存在しません: {db_path.name}",
            })
            continue

        # batch_run_log
        cols = _get_table_columns(db_path, "batch_run_log")
        if cols is None:
            findings.append({
                "check": "db_schema",
                "project": name,
                "severity": "warn",
                "message": "batch_run_log テーブルが存在しません",
            })
        else:
            batch_schemas[name] = set(cols)

        # error_log
        cols = _get_table_columns(db_path, "error_log")
        if cols is None:
            findings.append({
                "check": "db_schema",
                "project": name,
                "severity": "warn",
                "message": "error_log テーブルが存在しません",
            })
        else:
            error_schemas[name] = set(cols)

    # batch_run_log: 共通カラムは統一されているか、固有カラムの差分を報告
    if len(batch_schemas) >= 2:
        for name, cols in batch_schemas.items():
            extra = cols - common_batch_cols
            if extra:
                findings.append({
                    "check": "db_schema",
                    "project": name,
                    "severity": "info",
                    "message": f"batch_run_log 固有カラム: {', '.join(sorted(extra))}",
                })
            missing = common_batch_cols - cols
            if missing:
                findings.append({
                    "check": "db_schema",
                    "project": name,
                    "severity": "warn",
                    "message": f"batch_run_log 共通カラム欠落: {', '.join(sorted(missing))}",
                })

    # error_log: ticker カラムの有無を比較
    if len(error_schemas) >= 2:
        has_ticker = {n for n, c in error_schemas.items() if "ticker" in c}
        no_ticker = {n for n, c in error_schemas.items() if "ticker" not in c}
        if has_ticker and no_ticker:
            findings.append({
                "check": "db_schema",
                "project": "横断",
                "severity": "warn",
                "message": (
                    f"error_log の ticker カラム不統一 — "
                    f"あり: {', '.join(sorted(has_ticker))}, "
                    f"なし: {', '.join(sorted(no_ticker))}"
                ),
            })

    return findings


def check_log_error_signatures() -> list[dict]:
    """各プロジェクトの data_store.py の log_error() シグネチャを比較する。"""
    findings = []
    signatures = {}

    for name, cfg in _PROJECTS.items():
        ds_path = cfg["src_root"] / "data_store.py"
        funcs = _parse_function_signatures(ds_path)
        if "log_error" not in funcs:
            findings.append({
                "check": "log_error_sig",
                "project": name,
                "severity": "warn",
                "message": "data_store.py に log_error() が定義されていません",
            })
            continue
        sig = funcs["log_error"]
        param_names = tuple(a["name"] for a in sig["args"])
        signatures[name] = param_names

    # 比較
    if len(signatures) >= 2:
        unique_sigs = {}
        for name, params in signatures.items():
            unique_sigs.setdefault(params, []).append(name)

        if len(unique_sigs) > 1:
            parts = []
            for params, projects in unique_sigs.items():
                parts.append(f"{', '.join(projects)}: ({', '.join(params)})")
            findings.append({
                "check": "log_error_sig",
                "project": "横断",
                "severity": "warn",
                "message": f"log_error() のシグネチャが不統一 — {' / '.join(parts)}",
            })

    return findings


def check_notifier_signatures() -> list[dict]:
    """各プロジェクトの notifier.py の公開関数シグネチャを比較する。"""
    findings = []
    return_types = {}

    for name, cfg in _PROJECTS.items():
        notifier_path = cfg["src_root"] / "notifier.py"
        funcs = _parse_function_signatures(notifier_path)
        if not funcs:
            findings.append({
                "check": "notifier_sig",
                "project": name,
                "severity": "info",
                "message": "notifier.py に公開関数がありません",
            })
            continue

        for fn_name, sig in funcs.items():
            ret = sig["return_annotation"]
            return_types.setdefault(name, []).append((fn_name, ret))

    # 返り値型の不統一を検出
    all_ret_types = set()
    for name, entries in return_types.items():
        for fn_name, ret in entries:
            all_ret_types.add(ret)

    if len(all_ret_types) > 1:
        parts = []
        for name, entries in return_types.items():
            for fn_name, ret in entries:
                parts.append(f"{name}.{fn_name}() -> {ret or '未指定'}")
        findings.append({
            "check": "notifier_sig",
            "project": "横断",
            "severity": "warn",
            "message": f"notifier 返り値型が不統一 — {', '.join(parts)}",
        })

    return findings


def check_standard_files() -> list[dict]:
    """各プロジェクトに標準ファイルが揃っているか確認する。"""
    findings = []

    expected_files = {
        "init_db.py": "scripts",
        "config.py": "src",
        "logger.py": "src",
        "data_store.py": "src",
        "notifier.py": "src",
    }

    for name, cfg in _PROJECTS.items():
        for filename, location in expected_files.items():
            if location == "scripts":
                path = cfg["scripts_root"] / filename
            else:
                path = cfg["src_root"] / filename
            if not path.exists():
                findings.append({
                    "check": "standard_files",
                    "project": name,
                    "severity": "warn",
                    "message": f"{location}/{filename} が存在しません",
                })

    return findings


def check_shared_pattern_compliance() -> list[dict]:
    """各プロジェクトの config.py / logger.py が shared パターンに準拠しているか確認する。"""
    findings = []

    for name, cfg in _PROJECTS.items():
        # config.py: shared.config.init_config を使っているか
        config_path = cfg["src_root"] / "config.py"
        if config_path.exists():
            if not _check_file_contains(config_path, r"from shared\.config import"):
                findings.append({
                    "check": "shared_pattern",
                    "project": name,
                    "severity": "warn",
                    "message": "config.py が shared.config を使用していません",
                })

        # logger.py: shared.logger を使っているか
        logger_path = cfg["src_root"] / "logger.py"
        if logger_path.exists():
            if not _check_file_contains(logger_path, r"from shared\.logger import"):
                findings.append({
                    "check": "shared_pattern",
                    "project": name,
                    "severity": "warn",
                    "message": "logger.py が shared.logger を使用していません",
                })

        # data_store.py: shared.db を使っているか
        ds_path = cfg["src_root"] / "data_store.py"
        if ds_path.exists():
            if not _check_file_contains(ds_path, r"from shared\.db import"):
                findings.append({
                    "check": "shared_pattern",
                    "project": name,
                    "severity": "warn",
                    "message": "data_store.py が shared.db を使用していません",
                })

    return findings


def check_batch_log_pattern() -> list[dict]:
    """各バッチスクリプトの _start_batch_log / _finish_batch_log パターンを比較する。"""
    findings = []
    patterns_found = {}

    for name, cfg in _PROJECTS.items():
        scripts_dir = cfg["scripts_root"]
        if not scripts_dir.exists():
            continue
        for py_file in scripts_dir.glob("batch_*.py"):
            has_start = _check_file_contains(py_file, r"def _start_batch_log")
            has_finish = _check_file_contains(py_file, r"def _finish_batch_log")
            has_now_str = _check_file_contains(py_file, r"def _now_str")
            script_name = py_file.name
            key = f"{name}/{script_name}"
            patterns_found[key] = {
                "start": has_start,
                "finish": has_finish,
                "now_str": has_now_str,
            }
            if has_start and has_finish and has_now_str:
                # このパターンは共通化候補
                pass
            elif has_start or has_finish:
                findings.append({
                    "check": "batch_log_pattern",
                    "project": name,
                    "severity": "info",
                    "message": (
                        f"{script_name}: バッチログパターンが不完全 "
                        f"(_start={has_start}, _finish={has_finish}, _now_str={has_now_str})"
                    ),
                })

    # 全スクリプトに _now_str が重複定義されていればレポート
    now_str_count = sum(1 for v in patterns_found.values() if v["now_str"])
    if now_str_count >= 3:
        findings.append({
            "check": "batch_log_pattern",
            "project": "横断",
            "severity": "info",
            "message": (
                f"_now_str() が {now_str_count} 個のバッチスクリプトに重複定義されています "
                f"(shared/ への共通化候補)"
            ),
        })

    start_count = sum(1 for v in patterns_found.values() if v["start"])
    if start_count >= 3:
        findings.append({
            "check": "batch_log_pattern",
            "project": "横断",
            "severity": "info",
            "message": (
                f"_start_batch_log() / _finish_batch_log() が "
                f"{start_count} 個のスクリプトに重複定義されています "
                f"(shared/ への共通化候補)"
            ),
        })

    return findings


# ---------------------------------------------------------------------------
# 全チェック実行 & レポート
# ---------------------------------------------------------------------------

_SEVERITY_ICON = {
    "warn": "WARN",
    "info": "INFO",
}


def run_all_checks() -> list[dict]:
    """全チェックを実行し、検出結果のリストを返す。"""
    all_findings = []
    checks = [
        ("DB スキーマ比較", check_db_schemas),
        ("log_error() シグネチャ比較", check_log_error_signatures),
        ("notifier シグネチャ比較", check_notifier_signatures),
        ("標準ファイル存在確認", check_standard_files),
        ("shared パターン準拠", check_shared_pattern_compliance),
        ("バッチログパターン比較", check_batch_log_pattern),
    ]

    for check_name, check_fn in checks:
        logger.info(f"--- {check_name} ---")
        try:
            results = check_fn()
            all_findings.extend(results)
            for r in results:
                icon = _SEVERITY_ICON.get(r["severity"], "?")
                logger.info(f"  [{icon}] [{r['project']}] {r['message']}")
            if not results:
                logger.info("  [OK] 問題なし")
        except Exception as e:
            logger.error(f"  チェック失敗: {e}")
            all_findings.append({
                "check": check_name,
                "project": "watchdog",
                "severity": "warn",
                "message": f"チェック実行エラー: {e}",
            })

    return all_findings


def build_slack_blocks(findings: list[dict], run_time: str) -> list[dict]:
    """監査結果から Slack Block Kit の blocks リストを組み立てる。"""
    warn_count = sum(1 for f in findings if f["severity"] == "warn")
    info_count = sum(1 for f in findings if f["severity"] == "info")

    blocks = [
        slack.section(
            f"*[Refactor Audit] コードベース監査  {run_time}*\n"
            f"検出: WARN {warn_count} 件, INFO {info_count} 件"
        ),
        slack.divider(),
    ]

    # チェック種別ごとにグルーピング
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)

    for check_name, items in by_check.items():
        lines = [f"*{check_name}*"]
        for item in items:
            icon = _SEVERITY_ICON.get(item["severity"], "?")
            lines.append(f"[{icon}] `{item['project']}` {item['message']}")
        blocks.append(slack.section("\n".join(lines)))
        blocks.append(slack.divider())

    return blocks


def run() -> int:
    """
    リファクタリング監査を実行し、結果をレポートする。

    返り値:
        0 = 問題なし
        1 = WARN あり (Slack 通知済み)
        2 = 監査自体のエラー
    """
    cfg = get_config()
    webhook_url = cfg.get("slack", {}).get("webhook_url", "")

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info(f"[Refactor Audit] 監査開始: {run_time}")

    findings = run_all_checks()

    warn_count = sum(1 for f in findings if f["severity"] == "warn")
    info_count = sum(1 for f in findings if f["severity"] == "info")

    logger.info(f"[Refactor Audit] 完了: WARN {warn_count} 件, INFO {info_count} 件")

    if not findings:
        logger.info("[Refactor Audit] 問題なし")
        payload = {"blocks": [
            slack.section(
                f"*[Refactor Audit] コードベース監査  {run_time}*\n"
                "全チェック OK — 問題は検出されませんでした"
            ),
        ]}
    else:
        blocks = build_slack_blocks(findings, run_time)
        payload = {"blocks": blocks}

    slack.send_webhook(webhook_url, payload, logger=logger)
    return 1 if warn_count > 0 else 0
