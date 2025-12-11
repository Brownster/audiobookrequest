import json
import time
from collections import Counter
from typing import Any, Dict, List, Optional, TypedDict, cast

from aiohttp import ClientSession, ClientTimeout
from sqlmodel import Session, select

from app.internal.ai.config import ai_config
from app.internal.models import User
from app.util.log import logger


class AICategory(TypedDict, total=False):
    title: str
    description: str
    search_terms: List[str]
    reasoning: str


# Simple in-memory cache for per-user AI category generation
_AI_CATEGORY_CACHE: Dict[str, tuple[float, List[AICategory]]] = {}


def _cache_key_for_user(user: Optional[User]) -> str:
    if user is None:
        return "anon"
    return f"user:{user.username}"


def clear_ai_cache_for_user(user: Optional[User]):
    key = _cache_key_for_user(user)
    if key in _AI_CATEGORY_CACHE:
        del _AI_CATEGORY_CACHE[key]


async def fetch_ai_categories(
    session: Session,
    client_session: ClientSession,
    user: Optional[User] = None,
    desired_count: int = 3,
    use_cache: bool = True,
) -> Optional[List[AICategory]]:
    """
    Ask the configured Ollama model for up to N recommended categories.

    Returns a list of AICategory dicts or None if not configured or failed.
    Caches results per-user for a short TTL.
    """
    endpoint = ai_config.get_endpoint(session)
    model = ai_config.get_model(session)
    provider = ai_config.get_provider(session)
    api_key = ai_config.get_api_key(session)
    if not endpoint or not model or (provider == "openai" and not api_key):
        logger.info("AI not configured; skipping category generation")
        return None

    cache_key = _cache_key_for_user(user)
    now = time.time()
    ttl = ai_config.get_cache_ttl_seconds(session)
    if use_cache:
        hit = _AI_CATEGORY_CACHE.get(cache_key)
        if hit and (hit[0] + ttl) > now and len(hit[1]) >= 1:
            logger.info("Using cached AI categories", count=len(hit[1]))
            return hit[1][:desired_count]

    # Build light-weight profile from both ABS library and request history
    from app.internal.models import BookRequest

    top_authors: list[str] = []
    top_narrators: list[str] = []
    recent_titles: list[str] = []
    if user is not None:
        author_counts: Counter[str] = Counter()
        narrator_counts: Counter[str] = Counter()

        # First, include books from Audiobookshelf library
        try:
            from app.internal.audiobookshelf.config import abs_config
            from app.internal.audiobookshelf.client import abs_list_library_items

            if abs_config.is_valid(session):
                abs_books = await abs_list_library_items(session, client_session, limit=30)
                for book in abs_books:
                    for au in book.authors or []:
                        author_counts[au] += 1
                    for na in book.narrators or []:
                        narrator_counts[na] += 1
                    if len(recent_titles) < 15 and book.title:
                        recent_titles.append(book.title)
                logger.info("Added ABS library books to AI category profile", count=len(abs_books))
        except Exception as e:
            logger.debug("Could not fetch ABS library books for AI category profile", error=str(e))

        # Then add books from request history
        updated_at_column = cast(Any, BookRequest.updated_at)
        reqs = session.exec(
            select(BookRequest)
            .where(BookRequest.user_username == user.username)
            .order_by(updated_at_column.desc())
            .limit(50)
        ).all()
        for r in reqs:
            for au in r.authors or []:
                author_counts[au] += 1
            for na in r.narrators or []:
                narrator_counts[na] += 1
            if len(recent_titles) < 20 and r.title:
                recent_titles.append(r.title)

        top_authors = [k for k, _ in author_counts.most_common(10)]  # Increased from 8 to 10
        top_narrators = [k for k, _ in narrator_counts.most_common(10)]  # Increased from 8 to 10

    system_instructions = (
        "You are an assistant that suggests discovery categories for audiobooks. "
        "The user profile includes books from their library and listening history. "
        "Suggest categories that match their tastes and help them discover similar content. "
        "Respond strictly in compact JSON matching the schema. Avoid any prose."
    )

    user_prompt = {
        "task": "propose_multiple_categories",
        "count": max(1, min(desired_count, 4)),
        "requirements": {
            "title": "Short category title (<= 32 chars)",
            "description": "One-liner (<= 120 chars)",
            "search_terms": "3-8 concise queries",
            "reasoning": "Short sentence why this fits the user",
        },
        "audience": {
            "authors": top_authors,
            "narrators": top_narrators,
            "recent_titles": recent_titles,
        },
        "constraints": {
            "language": "English",
            "json_only": True,
        },
        "output_schema": [
            {
                "title": "string",
                "description": "string",
                "search_terms": ["string"],
                "reasoning": "string",
            }
        ],
        "example": [
            {
                "title": "Focus & Productivity",
                "description": "Actionable guides to build habits and get more done.",
                "search_terms": ["productivity", "habit building", "time management", "deep work"],
                "reasoning": "User enjoys practical self-improvement and habit books.",
            },
            {
                "title": "Big Ideas in Science",
                "description": "Accessible tours of modern science and how it shapes the world.",
                "search_terms": ["popular science", "innovation", "psychology", "neuroscience"],
                "reasoning": "User shows interest in psychology and science-forward titles.",
            }
        ],
    }

    logger.info(
        "Requesting AI categories",
        endpoint=endpoint,
        model=model,
        desired_count=desired_count,
        provider=provider,
    )
    headers: dict[str, str] = {}
    url: str
    body: dict[str, Any]
    if provider == "openai":
        url = f"{endpoint}/v1/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
    else:
        url = f"{endpoint}/api/generate"
        body = {
            "model": model,
            "prompt": (
                f"SYSTEM: {system_instructions}\n\n"
                + "USER: "
                + json.dumps(user_prompt, ensure_ascii=False)
            ),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }

    try:
        timeout = ClientTimeout(total=30)
        async with client_session.post(url, json=body, timeout=timeout, headers=headers) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                try:
                    err_preview = await resp.text()
                except Exception:
                    err_preview = "<unable to read body>"
                logger.info(
                    "AI generate returned non-200",
                    status=resp.status,
                    content_type=ctype,
                    body=err_preview[:500],
                )
                return None

            # Be robust to wrong content-type: try JSON first without content-type guard
            parsed_envelope: Dict[str, Any] | List[Any] | None = None
            raw_text: str | None = None
            raw_dump: str | None = None
            try:
                parsed_envelope = await resp.json(content_type=None)
                try:
                    raw_dump = json.dumps(parsed_envelope)
                except Exception:
                    raw_dump = None
            except Exception as je:
                try:
                    raw_text = await resp.text()
                except Exception:
                    raw_text = None
                logger.info("AI response not JSON envelope; reading text", error=str(je), content_type=ctype, raw_preview=(raw_text or "")[:500])

            # If we got a JSON envelope, extract the model text based on provider
            parsed_obj: list[dict[str, Any]] | dict[str, Any] | None = None
            model_text: str | None = None
            if isinstance(parsed_envelope, dict):
                if provider == "openai" and "choices" in parsed_envelope:
                    try:
                        choice = parsed_envelope["choices"][0]
                        message = choice.get("message", {})
                        content = message.get("content", "")
                        if isinstance(content, str):
                            model_text = content
                    except Exception as e:
                        logger.info("AI response parse (choices) failed", error=str(e))
                        model_text = None
                elif "response" in parsed_envelope:
                    raw_response: object | None = parsed_envelope.get("response")
                    if isinstance(raw_response, str):
                        model_text = raw_response
                    elif raw_response is None:
                        model_text = ""
                    else:
                        model_text = str(raw_response)
                else:
                    parsed_obj = [parsed_envelope]
            elif isinstance(parsed_envelope, list):
                parsed_obj = [p for p in parsed_envelope if isinstance(p, dict)]
            else:
                # Fallback to text and parse JSON from it
                if raw_text is None:
                    try:
                        raw_text = await resp.text()
                    except Exception:
                        raw_text = None
                model_text = raw_text

            if model_text is not None:
                stripped = model_text.strip()
                if not stripped:
                    logger.info("AI generate returned empty response body")
                    return None
                try:
                    parsed_obj = json.loads(stripped)
                except json.JSONDecodeError:
                    # Attempt to extract array or object from raw text
                    start_arr = stripped.find("[")
                    end_arr = stripped.rfind("]")
                    start_obj = stripped.find("{")
                    end_obj = stripped.rfind("}")
                    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
                        parsed_obj = json.loads(stripped[start_arr : end_arr + 1])
                    elif start_obj != -1 and end_obj != -1 and end_obj > start_obj:
                        parsed_obj = json.loads(stripped[start_obj : end_obj + 1])
                    else:
                        logger.info("AI response did not contain JSON payload", raw=stripped[:500])
                        return None

            items_raw: list[dict[str, Any]]
            if isinstance(parsed_obj, dict):
                # Handle wrapped payloads like {"output_schema": [...]}
                if isinstance(parsed_obj.get("output_schema"), list):
                    items_raw = [
                        p for p in parsed_obj.get("output_schema", []) if isinstance(p, dict)
                    ]
                else:
                    items_raw = [parsed_obj]
            elif isinstance(parsed_obj, list):
                items_raw = list(parsed_obj)
            else:
                logger.info("AI response JSON not a list; ignoring")
                return None
            categories: List[AICategory] = []
            for item in items_raw:
                title = item.get("title")
                if not isinstance(title, str) or not title:
                    continue
                terms_raw = item.get("search_terms")
                if not isinstance(terms_raw, list):
                    continue
                terms_list = cast(List[Any], terms_raw)
                terms = [t.strip() for t in terms_list if isinstance(t, str)]
                terms = [t for t in terms if t]
                if not terms:
                    continue
                desc_value = item.get("description")
                reasoning_value = item.get("reasoning")
                desc_str = str(desc_value) if isinstance(desc_value, str) else ""
                reasoning_str = str(reasoning_value) if isinstance(reasoning_value, str) else ""
                categories.append(
                    {
                        "title": str(title)[:64],
                        "description": desc_str[:200],
                        "search_terms": terms[:8],
                        "reasoning": reasoning_str[:200],
                    }
                )
            if not categories:
                preview = raw_dump or raw_text
                logger.info("AI returned zero valid categories after parsing", raw_preview=(preview or "")[:500])
                return None
            _AI_CATEGORY_CACHE[cache_key] = (now, categories)
            logger.info("AI categories generated", count=len(categories))
            return categories[:desired_count]
    except Exception as e:
        logger.info("AI category request failed", error=str(e))
        return None


async def fetch_ai_category(
    session: Session,
    client_session: ClientSession,
    user: Optional[User] = None,
) -> Optional[AICategory]:
    """
    Ask the configured Ollama model for a single recommended category
    tailored to the current user and return a JSON dict:

    {
      "title": str,                   # category title to display
      "description": str | None,      # optional one-liner
      "search_terms": list[str],      # 3-8 terms to drive searches
    }

    Returns None if AI is not configured or request fails.
    """
    # Backwards-compatible: return the first of multiple categories
    cats = await fetch_ai_categories(session, client_session, user, desired_count=1)
    if not cats:
        return None
    cat = cats[0]
    return {
        "title": str(cat.get("title") or "AI Picks"),
        "description": str(cat.get("description") or ""),
        "search_terms": cat.get("search_terms", []),
        "reasoning": str(cat.get("reasoning") or ""),
    }


class AIBookRec(TypedDict, total=False):
    seed_title: str
    seed_author: str
    title: str
    author: str
    reasoning: str
    search_terms: List[str]


# Cache for AI book-level recommendations
_AI_BOOKREC_CACHE: Dict[str, tuple[float, List[AIBookRec]]] = {}


async def fetch_ai_book_recommendations(
    session: Session,
    client_session: ClientSession,
    user: Optional[User] = None,
    desired_count: int = 12,
    use_cache: bool = True,
) -> Optional[List[AIBookRec]]:
    """
    Ask the AI to produce concrete book-level recommendations with short reasons,
    based on the user's recent requests. Returns a list of items with fields:
      - seed_title, seed_author (the input it matched from)
      - title, author (the proposed recommendation)
      - reasoning (short justification)
      - search_terms (optional hints to search)
    """
    endpoint = ai_config.get_endpoint(session)
    model = ai_config.get_model(session)
    if not endpoint or not model:
        return None

    cache_key = _cache_key_for_user(user)
    now = time.time()
    ttl = ai_config.get_cache_ttl_seconds(session)
    if use_cache:
        hit = _AI_BOOKREC_CACHE.get(cache_key)
        if hit and (hit[0] + ttl) > now and len(hit[1]) >= 1:
            logger.info("Using cached AI book recs", count=len(hit[1]))
            return hit[1][:desired_count]

    # Build small seed list of recent user requests + ABS library books
    from app.internal.models import BookRequest
    seeds: list[dict[str, str]] = []
    seen: set[str] = set()

    if user is not None:
        # First, try to get books from Audiobookshelf library (recently added/listened)
        try:
            from app.internal.audiobookshelf.config import abs_config
            from app.internal.audiobookshelf.client import abs_list_library_items

            if abs_config.is_valid(session):
                abs_books = await abs_list_library_items(session, client_session, limit=15)
                for book in abs_books:
                    if not book.title:
                        continue
                    key = (book.title or "") + "|" + (book.authors[0] if book.authors else "")
                    if key in seen:
                        continue
                    seen.add(key)
                    seeds.append({
                        "title": book.title,
                        "author": (book.authors[0] if book.authors else "")
                    })
                    if len(seeds) >= 10:
                        break
                logger.info("Added ABS library books as AI recommendation seeds", count=len(seeds))
        except Exception as e:
            logger.debug("Could not fetch ABS library books for AI seeds", error=str(e))

        # Then add books from request history
        updated_at_column = cast(Any, BookRequest.updated_at)
        reqs = session.exec(
            select(BookRequest)
            .where(BookRequest.user_username == user.username)
            .order_by(updated_at_column.desc())
            .limit(20)
        ).all()
        for r in reqs:
            key = (r.title or "") + "|" + (r.authors[0] if r.authors else "")
            if key in seen:
                continue
            seen.add(key)
            if r.title:
                seeds.append({"title": r.title, "author": (r.authors[0] if r.authors else "")})
            if len(seeds) >= 15:  # Increased from 8 to 15 for richer context
                break

    system = (
        "You recommend specific audiobook titles that match a user's tastes. "
        "The user has provided their listening history including books they own in their library. "
        "Recommend NEW books they don't already have. "
        "Return only compact JSON; no extra text."
    )
    user_prompt = {
        "task": "title_recommendations_with_reasons",
        "count": max(4, min(desired_count, 16)),
        "user_listening_history": seeds,
        "requirements": {
            "seed_title": "one of the user's recent titles you matched against",
            "seed_author": "best-effort main author of that seed",
            "title": "recommended title (must be DIFFERENT from user's history)",
            "author": "main author",
            "reasoning": "short phrase e.g. 'similar theme and narration style'",
            "search_terms": "optional concise queries to help locate the book",
        },
        "constraints": {
            "json_only": True,
            "language": "English",
            "exclude_user_books": "Do not recommend any books from the user's listening history"
        },
        "output_schema": [
            {
                "seed_title": "string",
                "seed_author": "string",
                "title": "string",
                "author": "string",
                "reasoning": "string",
                "search_terms": ["string"],
            }
        ],
        "example": [
            {
                "seed_title": "Atomic Habits",
                "seed_author": "James Clear",
                "title": "Deep Work",
                "author": "Cal Newport",
                "reasoning": "practical focus and habit-building themes",
                "search_terms": ["Deep Work Cal Newport audiobook"],
            }
        ],
    }

    body = {
        "model": model,
        "prompt": f"SYSTEM: {system}\n\nUSER: " + json.dumps(user_prompt, ensure_ascii=False),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3},
    }

    url = f"{endpoint}/api/generate"
    try:
        timeout = ClientTimeout(total=40)
        async with client_session.post(url, json=body, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status != 200:
                logger.info("AI book recs returned non-200", status=resp.status, content_type=ctype)
                return None
            envelope: Dict[str, Any] | List[Any] | None = None
            try:
                envelope = await resp.json(content_type=None)
            except Exception:
                envelope = None
            parsed_obj: list[dict[str, Any]] | dict[str, Any] | None = None
            text: str | None = None
            if isinstance(envelope, dict):
                if "response" in envelope:
                    raw_resp: object | None = envelope.get("response")
                    if isinstance(raw_resp, str):
                        text = raw_resp
                    elif raw_resp is None:
                        text = ""
                    else:
                        text = str(raw_resp)
                else:
                    parsed_obj = [envelope]
            elif isinstance(envelope, list):
                parsed_obj = [p for p in envelope if isinstance(p, dict)]
            else:
                text = await resp.text()

            if text is not None:
                stripped = text.strip()
                if not stripped:
                    return None
                try:
                    parsed_obj = json.loads(stripped)
                except json.JSONDecodeError:
                    s1, e1 = stripped.find("["), stripped.rfind("]")
                    s2, e2 = stripped.find("{"), stripped.rfind("}")
                    if s1 != -1 and e1 != -1 and e1 > s1:
                        parsed_obj = json.loads(stripped[s1 : e1 + 1])
                    elif s2 != -1 and e2 != -1 and e2 > s2:
                        parsed_obj = json.loads(stripped[s2 : e2 + 1])
                    else:
                        return None

            parsed_list: list[dict[str, Any]]
            if isinstance(parsed_obj, dict):
                parsed_list = [parsed_obj]
            elif isinstance(parsed_obj, list):
                parsed_list = list(parsed_obj)
            else:
                return None
            items: List[AIBookRec] = []
            for it in parsed_list:
                title = it.get("title")
                author = it.get("author")
                if not isinstance(title, str) or not isinstance(author, str):
                    continue
                seed_title_raw = it.get("seed_title")
                seed_author_raw = it.get("seed_author")
                reasoning_raw = it.get("reasoning")
                terms_raw = it.get("search_terms")
                terms_clean: list[str] = []
                if isinstance(terms_raw, list):
                    terms_list = cast(List[Any], terms_raw)
                    terms_clean = [str(t)[:100] for t in terms_list if isinstance(t, str)]
                items.append(
                    {
                        "seed_title": str(seed_title_raw or "")[:128],
                        "seed_author": str(seed_author_raw or "")[:128],
                        "title": str(title)[:128],
                        "author": str(author)[:128],
                        "reasoning": str(reasoning_raw or "")[:200],
                        "search_terms": terms_clean[:5],
                    }
                )
            if not items:
                return None
            _AI_BOOKREC_CACHE[cache_key] = (now, items)
            return items[:desired_count]
    except Exception as e:
        logger.info("AI book recs request failed", error=str(e))
        return None
