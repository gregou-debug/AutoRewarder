"""France trending searches (SerpApi) + FR-first query orchestration.

Daily Bing searches used to run in English. This module fetches the day's
popular searches in France via SerpApi (``google_trends_trending_now``,
``geo=FR``, ``hl=fr``) so Edge runs them in priority, and defines how to
fill up when the run needs more than the ~10 trends of the day:

1. FR trends first (shuffled copy of the day's list),
2. then English static fallback queries (``assets/queries.json``),
3. then repeats of FR trends (``random.choices``) when still short.

Every public helper **never raises**: network / key / parsing failures
return ``[]`` (or the best-effort remainder) and log the cause, so a
scheduled run never depends on the network.
"""

import datetime
import json
import os
import random
import urllib.parse
import urllib.request

SERPAPI_ENDPOINT = "https://serpapi.com/search"

# Cache of the day's FR trends, so the PC + mobile phases of one run (and
# two runs of the same day) share a single SerpApi call.
TRENDS_CACHE_FILENAME = "trends_fr_cache.json"

_VALID_HOURS = (4, 24, 48, 168)


def _today_iso():
    return datetime.date.today().isoformat()


def resolve_serpapi_key(explicit=None):
    """Return the SerpApi key to use, or "" when none is configured.

    Priority: explicit argument > ``SERPAPI_API_KEY`` env var. No hardcoded
    key lives here on purpose (the legacy standalone script carried one).
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return str(os.environ.get("SERPAPI_API_KEY") or "").strip()


def fetch_trending_raw(api_key, geo="FR", hl="fr", hours=24, timeout=30):
    """Call SerpApi Trending Now and return the decoded JSON payload.

    Raises on network / HTTP / JSON errors; callers that must not raise
    should use :func:`get_france_trending_queries` instead.
    """
    params = {
        "api_key": api_key,
        "engine": "google_trends_trending_now",
        "geo": geo,
        "hl": hl,
        "hours": str(hours),
    }
    url = f"{SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "AutoRewarder-trends-fr/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise RuntimeError(f"SerpApi HTTP {status}")
        return json.load(resp)


def extract_queries(payload, limit=10):
    """Pull up to ``limit`` query strings from a SerpApi payload.

    Sorted by ``search_volume`` desc so the result is a true "top".
    """
    trending = (payload or {}).get("trending_searches", []) or []
    ordered = sorted(
        trending, key=lambda t: (t.get("search_volume") or 0), reverse=True
    )
    queries = []
    for item in ordered[: max(0, int(limit or 0))]:
        query = (item.get("query") or "").strip() if isinstance(item, dict) else ""
        if query:
            queries.append(query)
    # De-duplicate while preserving order.
    return list(dict.fromkeys(queries))


def _cache_path():
    """Cache file path inside APP_DIR (None when it can't be resolved)."""
    try:
        from ..config import APP_DIR

        if not APP_DIR:
            return None
        return os.path.join(APP_DIR, TRENDS_CACHE_FILENAME)
    except Exception:
        return None


def load_cached_trends(today_only=True, limit=10):
    """Return cached FR queries (today's only by default), else []."""
    path = _cache_path()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if today_only and data.get("date") != _today_iso():
            return []
        queries = data.get("queries") or []
        queries = [q for q in queries if isinstance(q, str) and q.strip()]
        return queries[: max(0, int(limit or 0))] if limit else queries
    except Exception:
        return []


def save_cached_trends(queries):
    """Persist today's FR queries; best-effort, never raises."""
    path = _cache_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"date": _today_iso(), "queries": list(queries)},
                fh,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass


def get_france_trending_queries(
    api_key=None,
    limit=10,
    hours=24,
    logger=None,
    use_cache=True,
):
    """Return up to ``limit`` FR trending query strings. Never raises.

    Order: fresh SerpApi call -> today's cache -> []. A successful fetch
    refreshes the cache. ``hours`` outside {4, 24, 48, 168} falls back to 24.
    """
    try:
        limit = max(0, int(limit or 0))
    except (TypeError, ValueError):
        return []
    if limit <= 0:
        return []
    if hours not in _VALID_HOURS:
        hours = 24

    key = resolve_serpapi_key(api_key)
    if not key:
        if logger:
            logger(
                "[INFO] France trends skipped: no SerpApi key "
                "(set SERPAPI_API_KEY or Settings > Search terms)."
            )
        return load_cached_trends(limit=limit) if use_cache else []

    try:
        payload = fetch_trending_raw(key, hours=hours)
    except Exception as e:
        if logger:
            logger(f"[WARNING] France trends fetch failed ({e}); trying cache.")
        return load_cached_trends(limit=limit) if use_cache else []

    if isinstance(payload, dict) and payload.get("error"):
        if logger:
            logger(f"[WARNING] SerpApi error: {payload['error']}; trying cache.")
        return load_cached_trends(limit=limit) if use_cache else []

    queries = extract_queries(payload, limit=limit)
    if not queries:
        if logger:
            logger("[WARNING] SerpApi returned no FR trends; trying cache.")
        return load_cached_trends(limit=limit) if use_cache else []
    if use_cache:
        save_cached_trends(queries)
    return queries


def orchestrate_queries(count, fr_queries, fallback_queries, logger=None):
    """Build ``count`` queries, FR trends first. Never raises.

    - Take all FR trends first (shuffled copy, order-preserving dedup).
    - If ``count`` <= len(FR): return a random sample of FR only.
    - Else fill the remainder with ``fallback_queries`` (typically the
      English static file), skipping duplicates.
    - If still short, repeat FR trends via ``random.choices``; as a last
      resort repeat whatever is already selected.

    Args:
        count (int): how many queries to produce.
        fr_queries (list): today's France trending queries (priority).
        fallback_queries (list): English/static fallback pool.

    Returns:
        list: up to ``count`` query strings.
    """
    try:
        count = int(count)
    except (TypeError, ValueError):
        return []
    if count <= 0:
        return []

    fr = [q for q in (fr_queries or []) if isinstance(q, str) and q.strip()]
    fr = list(dict.fromkeys(fr))
    fb = [q for q in (fallback_queries or []) if isinstance(q, str) and q.strip()]
    fb = list(dict.fromkeys(fb))

    if not fr and not fb:
        return []

    if fr and count <= len(fr):
        # Random subset of the day's trends: every search stays French,
        # but two runs don't type them in the same order.
        picked = random.sample(fr, count)
        if logger:
            logger(f"Using {len(picked)}/{count} France trending searches.")
        return picked

    selected = list(fr)
    if fr:
        random.shuffle(selected)

    if len(selected) < count:
        seen = set(selected)
        extra = [q for q in fb if q not in seen]
        random.shuffle(extra)
        selected += extra[: count - len(selected)]

    if len(selected) < count and fr:
        # N > 10 case: repeat some French trends before falling back to
        # duplicating English queries.
        need = count - len(selected)
        selected += random.choices(fr, k=need)
        if logger:
            logger(
                f"France trends ({len(fr)}) < {count} searches: "
                f"repeating {need} French trend(s) + "
                f"{len(extra) if 'extra' in dir() else 0} English fallback."
            )
    elif len(selected) < count and selected:
        need = count - len(selected)
        selected += random.choices(selected, k=need)

    result = selected[:count]
    if logger:
        n_fr = sum(1 for q in result if q in set(fr))
        logger(
            f"Query mix: {n_fr}/{len(result)} France trends, "
            f"{len(result) - n_fr}/{len(result)} fallback/repeats."
        )
    return result
