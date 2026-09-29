#!/usr/bin/env python3
"""
Récupère le Top 10 des recherches Google en France via SerpApi.

Engine: google_trends_trending_now
Geo: FR / hl: fr

Thin CLI over :mod:`src.search.trends` — toute la logique réseau / tri /
cache vit dans le module partagé avec l'app (orchestration FR-first),
ce script ne fait que l'affichage.

Usage:
    export SERPAPI_API_KEY="votre_cle"
    python3 top10_france.py
    python3 top10_france.py --limit 10 --hours 24
    python3 top10_france.py --api-key VOTRE_CLE --json-out trends_fr.json

Sans --api-key, la clé est lue depuis la variable d'environnement SERPAPI_API_KEY.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.search.trends import (  # noqa: E402
    extract_queries,
    fetch_trending_raw,
    resolve_serpapi_key,
)

# Clé historique embarquée : conservée en repli pour ne pas casser les usages
# existants, mais la variable d'environnement reste prioritaire.
LEGACY_DEFAULT_API_KEY = "325f0a35e03001980f801a7d657e5fb531b60c577cd7d77bdebeb36552e5fdcd"


def format_date(ts: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Top recherches Google France via SerpApi (Trending Now)"
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Clé SerpApi (défaut: env SERPAPI_API_KEY, sinon clé historique)",
    )
    parser.add_argument("--geo", default="FR", help="Géo (défaut: FR)")
    parser.add_argument("--hl", default="fr", help="Langue (défaut: fr)")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        choices=[4, 24, 48, 168],
        help="Fenêtre en heures (4, 24, 48, 168. Défaut: 24)",
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Nombre de résultats (défaut: 10)"
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Chemin optionnel pour sauvegarder le JSON brut",
    )
    args = parser.parse_args()

    api_key = resolve_serpapi_key(args.api_key) or LEGACY_DEFAULT_API_KEY

    if not api_key:
        print(
            "Erreur: clé API manquante. Exportez SERPAPI_API_KEY ou passez --api-key.",
            file=sys.stderr,
        )
        return 1

    try:
        data = fetch_trending_raw(api_key, args.geo, args.hl, args.hours)
    except Exception as e:
        print(f"Erreur SerpApi: {e}", file=sys.stderr)
        return 1

    # SerpApi peut renvoyer une erreur JSON : {"error": "..."}
    if "error" in data:
        print(f"Erreur SerpApi: {data['error']}", file=sys.stderr)
        return 1

    trending = data.get("trending_searches", [])
    # Tri par volume décroissant pour garantir un vrai "top"
    queries_top = extract_queries(data, limit=args.limit)
    by_query = {t.get("query"): t for t in trending if isinstance(t, dict)}
    trending_sorted = [by_query[q] for q in queries_top if q in by_query]

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(trending_sorted, f, ensure_ascii=False, indent=2)
        print(f"JSON sauvegardé: {args.json_out}")

    print(f"\nTop {len(trending_sorted)} recherches Google - {args.geo} (dernières {args.hours}h)")
    print("=" * 70)
    for i, t in enumerate(trending_sorted, 1):
        query = t.get("query", "?")
        vol = t.get("search_volume", "?")
        inc = t.get("increase_percentage", "?")
        active = t.get("active")
        cats = ", ".join(c.get("name", "?") for c in t.get("categories", []) or [])
        breakdown = t.get("trend_breakdown") or []
        print(f"\n{i}. {query}")
        print(f"   Volume: {vol} | +{inc}% | Actif: {active} | Catégorie: {cats or '-'}")
        print(f"   Début: {format_date(t.get('start_timestamp', 0))}")
        if breakdown:
            print(f"   Associées: {', '.join(breakdown[:8])}")

    print(f"\nTotal tendances reçues: {len(trending)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
