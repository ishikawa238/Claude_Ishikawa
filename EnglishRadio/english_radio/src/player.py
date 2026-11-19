"""
再生（m3u プレイリスト生成と VLC 起動）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

_DEFAULT_VLC_PATHS = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]


class VlcNotFoundError(RuntimeError):
    pass


def find_vlc(configured: str | None = None) -> str | None:
    """VLC の実行ファイルを探す。見つからなければ None。"""
    candidates = [configured] if configured else []
    candidates += _DEFAULT_VLC_PATHS
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return shutil.which("vlc")


def write_m3u(entries: list[dict], path: Path) -> Path:
    """エントリを UTF-8 の m3u（拡張形式）として書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for e in entries:
        dur = e.get("duration_sec") or -1
        title = f"{e['show']} - {e['title']}".replace("\n", " ")
        lines.append(f"#EXTINF:{dur},{title}")
        lines.append(str(e["path"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_command(vlc_path: str, target: Path) -> list[str]:
    return [vlc_path, "--play-and-exit", "--no-random", str(target)]


def play(target: Path, configured_vlc: str | None = None) -> str:
    """VLC で m3u か mp3 を再生する。完了は待たない。使った VLC のパスを返す。"""
    vlc = find_vlc(configured_vlc)
    if not vlc:
        raise VlcNotFoundError(
            "VLC が見つかりません。https://www.videolan.org/ からインストールするか、"
            "config.json の radio.vlc_path を設定してください"
        )
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        build_command(vlc, target),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    return vlc
