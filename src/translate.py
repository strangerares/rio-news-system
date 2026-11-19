import json
import sys
import time
from pathlib import Path

from deep_translator import GoogleTranslator

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "output.json"
OUTPUT_FILE = BASE_DIR / "output_en.json"
CACHE_FILE = BASE_DIR / "translation_cache.json"

# Fields to translate: (source_key, target_key)
FIELDS = [
    ("original_title", "title_en"),
    ("original_content", "content_en"),
    ("event_location", "event_location_en"),
]

# Pre-seed cache with proper nouns that must not be touched.
CACHE = {
    "Vivo Rio": "Vivo Rio",
    "Qualistage": "Qualistage",
    "Rio de Janeiro": "Rio de Janeiro",
}

if CACHE_FILE.exists():
    try:
        loaded = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        CACHE.update(loaded)
    except json.JSONDecodeError:
        pass

translator = GoogleTranslator(source="auto", target="en")


def translate_text(text, max_retries=3):
    """
    Translate a single string. Cached. Falls back to the original
    text if translation fails — never loses data.
    """
    if not text or not str(text).strip():
        return text

    text = str(text)

    if text in CACHE:
        return CACHE[text]

    # Google Translate free endpoint has a ~5000-char limit.
    if len(text) > 4500:
        chunks = []
        for para in text.split("\n\n"):
            while len(para) > 4500:
                chunks.append(para[:4500])
                para = para[4500:]
            chunks.append(para)
        translated = "\n\n".join(
            translate_text(chunk) for chunk in chunks
        )
        CACHE[text] = translated
        return translated

    for attempt in range(max_retries):
        try:
            result = translator.translate(text)
            CACHE[text] = result
            time.sleep(0.15)  # polite rate limit
            return result
        except Exception as error:
            if attempt == max_retries - 1:
                print(f"    translation failed ({error}) — keeping original")
                return text
            time.sleep(1.5)

    return text


def translate_article(article):
    """Add _en fields; leave Portuguese originals untouched."""
    out = dict(article)
    for source_key, target_key in FIELDS:
        value = article.get(source_key)
        if value:
            out[target_key] = translate_text(value)
    return out


def main():
    if not INPUT_FILE.exists():
        print(f"Missing {INPUT_FILE}")
        print("Run this first:")
        print("  python src/collector.py > output.json")
        sys.exit(1)

    articles = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    print(f"Translating {len(articles)} articles → English\n")

    translated = []
    for i, article in enumerate(articles, 1):
        title = article.get("original_title", "")[:70]
        print(f"[{i}/{len(articles)}] {title}")
        translated.append(translate_article(article))

        if i % 5 == 0:
            CACHE_FILE.write_text(
                json.dumps(CACHE, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    CACHE_FILE.write_text(
        json.dumps(CACHE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUTPUT_FILE.write_text(
        json.dumps(translated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {len(translated)} translated articles to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
