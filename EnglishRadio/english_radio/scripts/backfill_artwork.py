"""
既存エピソードにアートワークと紹介文を埋める（english_radio/ から `python scripts/backfill_artwork.py`）。

アートワーク対応前にダウンロードした mp3 向け。各番組の RSS を多めに読み直し、
guid が一致するエピソードの画像 URL・紹介文を登録してから、画像をキャッシュする。
何度実行しても安全（取得済みの画像は再取得しない）。
"""

import _bootstrap

_bootstrap.setup()

from shared.config import get_config  # noqa: E402
from shared.db import get_connection  # noqa: E402
from shared.logger import get_logger  # noqa: E402
from src import feeds, paths, store  # noqa: E402
from src.radio import ensure_db, refresh_artwork  # noqa: E402

LOOKBACK_EPISODES = 50


def main() -> None:
    logger = get_logger(__name__)
    ensure_db()
    episodes, show_images = feeds.fetch_all(get_config().get("radio", {}).get("feeds", []), LOOKBACK_EPISODES)
    by_guid = {e.guid: e for e in episodes}
    updated = 0
    with get_connection(paths.db_path()) as conn:
        for entry in store.list_episodes(conn):
            ep = by_guid.get(entry["guid"])
            if ep and ep.image_url and not entry.get("image_url"):
                store.set_episode_art(conn, ep.guid, ep.image_url, None, ep.summary)
                updated += 1
    logger.info(f"画像URL・紹介文を {updated} 件登録しました")
    refresh_artwork(show_images, limit=1000)
    with get_connection(paths.db_path()) as conn:
        missing = len(store.episodes_missing_art(conn, 1000))
    logger.info(f"アートワークのキャッシュ完了（未取得 {missing} 件）")


if __name__ == "__main__":
    main()
