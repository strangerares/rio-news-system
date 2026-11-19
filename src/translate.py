import json
import logging
import time
from pathlib import Path
from deep_translator import GoogleTranslator

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).resolve().parent.parent / "translation_cache.json"

# Pre-seed cache with proper nouns that must not be touched.
CACHE = {
    "Vivo Rio": "Vivo Rio",
    "Qualistage": "Qualistage",
    "Rio de Janeiro": "Rio de Janeiro",
}

if CACHE_FILE.exists():
    try:
        CACHE.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass

translator = GoogleTranslator(source="auto", target="en")

def translate_text(text, max_retries=3):
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
        translated = "\n\n".join(translate_text(chunk) for chunk in chunks)
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
                logger.warning(f"Translation failed ({error}) — keeping original")
                return text
            time.sleep(1.5)

    return text

def translate_articles(articles):
    """Processes a list of article dicts and adds English fields."""
    logger.info(f"Translating {len(articles)} articles to English...")
    
    translated_articles = []
    for article in articles:
        out = dict(article)
        
        # Only translate if the field has content
        if article.get("original_title"):
            out["title_en"] = translate_text(article["original_title"])
        
        if article.get("original_content"):
            out["content_en"] = translate_text(article["original_content"])
            
        if article.get("event_location"):
            out["event_location_en"] = translate_text(article["event_location"])
            
        translated_articles.append(out)
        
    # Save cache for next time the GitHub Action runs
    try:
        CACHE_FILE.write_text(json.dumps(CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save translation cache: {e}")

    return translated_articles
