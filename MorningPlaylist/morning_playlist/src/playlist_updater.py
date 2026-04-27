"""
Morning English プレイリスト更新 & 再生のコアロジック

1. 指定ポッドキャスト番組から最新エピソードを取得
2. 合計再生時間が target_minutes 以内に収まるよう選択
3. プレイリストの中身を差し替え
4. Spotify Connect で指定デバイスに再生開始
"""

import time

import spotipy

from shared.config import get_config
from shared.logger import get_logger
from shared import slack


def _logger():
    return get_logger(__name__)


def fetch_latest_episodes(sp: spotipy.Spotify) -> list[dict]:
    """各番組から最新エピソードを取得する。"""
    cfg = get_config()
    shows = cfg["spotify"]["shows"]
    episodes = []

    for show in shows:
        try:
            result = sp.show_episodes(show["id"], limit=5, market="JP")
            items = result.get("items", [])
            for ep in items:
                ep["_show_name"] = show["name"]
            episodes.extend(items)
            _logger().info(f"{show['name']}: {len(items)} エピソード取得")
        except Exception as e:
            _logger().warning(f"{show['name']}: 取得失敗 ({e})")

    return episodes


def select_episodes(episodes: list[dict], target_minutes: int) -> list[dict]:
    """合計再生時間が target_minutes 以内に収まるようエピソードを選択する。

    各番組から均等に選ぶため、番組ごとにラウンドロビンで追加する。
    """
    from collections import OrderedDict

    target_ms = target_minutes * 60 * 1000

    # 番組ごとにグループ化（取得順＝新しい順を維持）
    by_show: OrderedDict[str, list[dict]] = OrderedDict()
    for ep in episodes:
        name = ep.get("_show_name", "unknown")
        by_show.setdefault(name, []).append(ep)

    selected = []
    total_ms = 0

    # ラウンドロビンで各番組から1エピソードずつ追加
    max_rounds = max((len(v) for v in by_show.values()), default=0)
    for round_idx in range(max_rounds):
        for show_name, eps in by_show.items():
            if round_idx >= len(eps):
                continue
            ep = eps[round_idx]
            if total_ms + ep["duration_ms"] <= target_ms:
                selected.append(ep)
                total_ms += ep["duration_ms"]

    return selected


def update_playlist(sp: spotipy.Spotify, episodes: list[dict]) -> None:
    """プレイリストの中身を差し替える。"""
    cfg = get_config()
    playlist_id = cfg["spotify"]["playlist_id"]
    uris = [ep["uri"] for ep in episodes]

    sp.playlist_replace_items(playlist_id, uris)
    _logger().info(f"プレイリスト更新完了: {len(uris)} エピソード")


def start_playback(sp: spotipy.Spotify) -> str | None:
    """Spotify Connect で指定デバイスに再生を開始する。

    Returns:
        再生開始したデバイス名。デバイスが見つからない場合は None。
    """
    cfg = get_config()
    playlist_id = cfg["spotify"]["playlist_id"]
    device_name = cfg["spotify"]["device_name"]

    devices = sp.devices().get("devices", [])
    if not devices:
        _logger().warning("Spotify Connect デバイスが見つかりません")
        return None

    # 指定デバイス名を部分一致で検索
    target = None
    for d in devices:
        if device_name.lower() in d["name"].lower():
            target = d
            break

    # 見つからなければアクティブなデバイス、それもなければ先頭
    if not target:
        _logger().warning(f"'{device_name}' が見つからないためフォールバックします")
        target = next((d for d in devices if d["is_active"]), devices[0])

    sp.start_playback(
        device_id=target["id"],
        context_uri=f"spotify:playlist:{playlist_id}",
    )
    _logger().info(f"再生開始: {target['name']} ({target['type']})")
    return target["name"]


def send_slack_success(episodes: list[dict], device_name: str | None) -> None:
    """成功時の Slack 通知を送信する。"""
    cfg = get_config()
    webhook_url = cfg.get("slack", {}).get("webhook_url", "")
    playlist_id = cfg["spotify"]["playlist_id"]
    playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"

    total_min = sum(ep["duration_ms"] for ep in episodes) / 60000
    device_text = device_name or "デバイス未検出（再生スキップ）"

    lines = []
    for ep in episodes:
        dur = ep["duration_ms"] / 60000
        name = ep["name"][:55]
        show = ep.get("_show_name", "")
        lines.append(f"  *{show}*: {name} ({dur:.0f}分)")

    episode_list = "\n".join(lines)

    blocks = [
        slack.section(
            f":headphones: *<{playlist_url}|Morning English>* 更新完了\n\n"
            f"{episode_list}\n\n"
            f"合計: *{total_min:.0f}分* | 再生先: *{device_text}*"
        ),
    ]

    slack.send_webhook(webhook_url, {"blocks": blocks}, logger=_logger())


def send_slack_failure(error: str) -> None:
    """失敗時の Slack 通知を送信する。"""
    cfg = get_config()
    webhook_url = cfg.get("slack", {}).get("webhook_url", "")

    blocks = [
        slack.section(
            f":warning: *Morning English 更新失敗*\n\n"
            f"エラー: {error}\n"
            f"前日のプレイリストがそのまま残っています"
        ),
    ]

    slack.send_webhook(webhook_url, {"blocks": blocks}, logger=_logger())


def run(sp: spotipy.Spotify) -> None:
    """メイン処理: エピソード取得 → プレイリスト更新 → 再生開始 → 通知"""
    cfg = get_config()
    target_minutes = cfg["spotify"].get("target_minutes", 90)

    try:
        # エピソード取得
        episodes = fetch_latest_episodes(sp)
        if not episodes:
            raise RuntimeError("エピソードが1件も取得できませんでした")

        # 選択
        selected = select_episodes(episodes, target_minutes)
        total_min = sum(ep["duration_ms"] for ep in selected) / 60000
        _logger().info(f"選択: {len(selected)} エピソード / {total_min:.0f}分")

        # プレイリスト更新
        update_playlist(sp, selected)

        # 少し待ってから再生（Spotify 側の反映待ち）
        time.sleep(2)

        # 再生開始
        device_name = start_playback(sp)

        # 成功通知
        send_slack_success(selected, device_name)

    except Exception as e:
        _logger().error(f"処理失敗: {e}")
        send_slack_failure(str(e))
        raise
