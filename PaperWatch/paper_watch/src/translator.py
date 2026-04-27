"""
英語→日本語 翻訳モジュール

Google Translate を利用してキーワード・Abstract を日本語に変換する。
翻訳に失敗した場合は原文をそのまま返す。
"""

from deep_translator import GoogleTranslator

from src.logger import get_logger

logger = get_logger(__name__)

_translator = GoogleTranslator(source="en", target="ja")
_cache: dict[str, str] = {}


def translate_to_ja(text: str) -> str:
    """テキストを日本語に翻訳する。失敗時は原文を返す。"""
    if not text:
        return text
    if text in _cache:
        return _cache[text]
    try:
        result = _translator.translate(text)
        _cache[text] = result
        return result
    except Exception as e:
        logger.warning(f"翻訳失敗（原文を使用）: {e}")
        return text


def translate_keywords(keywords_str: str) -> str:
    """カンマ区切りのキーワード文字列を日本語に翻訳する。"""
    if not keywords_str:
        return keywords_str
    keywords = [kw.strip() for kw in keywords_str.split(",") if kw.strip()]
    translated = [translate_to_ja(kw) for kw in keywords]
    return ", ".join(translated)
