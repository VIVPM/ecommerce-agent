"""Image -> search query for multimodal (shop-by-photo) search.

Gemini 2.5 Flash is already multimodal, so one vision call turns an uploaded shoe
photo into structured attributes, from which we build a query for the EXISTING
text-to-SQL search. No image embeddings / vector index — the catalogue is text.

Deliberate: the query is built from brand + product type + gender ONLY. Colour is
dropped even though vision reads it, because the catalogue can't be searched by
colour (titles don't carry it and the app refuses colour filters) — including it
returns nothing. Returns None when the image isn't a recognisable shoe, so the
caller can say so instead of blind-searching the catalogue.
"""
import hashlib
import json
import logging
import os
import re

from google import genai
from google.genai import types

from app.cache import cache_get, cache_set

# cache_set drops empty values, so "not a shoe" needs a non-empty sentinel to cache.
_NOT_A_SHOE = "__not_a_shoe__"

logger = logging.getLogger(__name__)

_VISION_PROMPT = """Look at this image. Your FIRST job is to decide whether its main
subject is a shoe / footwear. Do not assume it is — many images are not.

Return ONLY a JSON object, no prose:
- is_shoe: true ONLY if the main subject is a shoe or footwear (sneaker, boot, sandal,
  heel, formal/casual/sports shoe, etc.). For ANYTHING else — a person, animal, food,
  scenery, gadget, clothing that isn't footwear, a random object — return false.
- product_type: the shoe category ("running shoes", "sneakers", "walking shoes",
  "casual shoes", "formal shoes", "boots", "sports shoes"). null if is_shoe is false.
- brand: the brand name ONLY if a logo or brand text is clearly legible in the image.
  If no brand marking is visible, return null. NEVER guess a brand from style, shape,
  or colour — a guess is worse than null.
- gender: "men" or "women" if clearly inferable, else null.

If is_shoe is false, set product_type, brand and gender ALL to null and invent nothing.
Be consistent: the same photo must always give the same answer."""


def extract_shoe_query(image_bytes: bytes, mime: str = "image/jpeg") -> str | None:
    """Return a catalogue search phrase (brand + type + gender) for the shoe in the
    image, or None if it isn't a shoe / can't be read.

    Cached on a hash of the image BYTES (the vision call is temperature-0 but not
    bit-deterministic on shared hardware — a borderline logo can flip the brand read
    between calls). Caching makes the same photo return the same phrase EVERY time,
    and a re-uploaded photo free. Purge with cache_purge('vision') after editing the
    prompt below. An empty cached value is the "not a shoe" sentinel."""
    key = hashlib.sha256(image_bytes).hexdigest()
    cached = cache_get("vision", key)
    if cached is not None:
        return None if cached == _NOT_A_SHOE else cached

    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), _VISION_PROMPT],
            config=types.GenerateContentConfig(temperature=0.0),
        )
        m = re.search(r"\{.*\}", resp.text or "", re.DOTALL)
        attrs = json.loads(m.group(0)) if m else {}
    except Exception as e:
        # Transient failure — do NOT cache, so a retry can still succeed.
        logger.error("Vision extraction failed: %s", e)
        return None

    if attrs.get("is_shoe"):
        query = " ".join(filter(None, [
            attrs.get("brand"),
            attrs.get("product_type") or "shoes",
            f"for {attrs['gender']}" if attrs.get("gender") else None,
        ])).strip() or None
    else:
        query = None
    # Cache the successful read (a phrase, or the sentinel meaning "not a shoe").
    cache_set("vision", key, query or _NOT_A_SHOE)
    return query
