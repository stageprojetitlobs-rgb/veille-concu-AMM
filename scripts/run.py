"""Point d'entrée CLI de la veille.

Exemples :
    python -m scripts.run --list-sources
    python -m scripts.run --source anses_anmv --dry-run
    python -m scripts.run                       # toutes les sources, envoi réel
"""
from __future__ import annotations

import argparse
import logging
import sys

from veille.orchestrator import run
from veille.sources.registry import available_sources


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Veille concurrentielle Lobs")
    parser.add_argument(
        "--source", "-s", action="append", dest="sources",
        help="Source à lancer (répétable). Défaut : toutes.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Affiche ce qui SERAIT notifié sans rien envoyer ni écrire l'historique.",
    )
    parser.add_argument("--list-sources", action="store_true", help="Liste les sources et quitte.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    if args.list_sources:
        print("Sources disponibles :")
        for s in available_sources():
            print(f"  - {s}")
        return 0

    results = run(sources=args.sources, dry_run=args.dry_run)

    total = sum(len(r.changements) for r in results)
    print()
    print("=" * 60)
    mode = "DRY-RUN (aucun envoi)" if args.dry_run else "RÉEL"
    print(f"Bilan [{mode}] : {total} changement(s) sur {len(results)} source(s)")
    for r in results:
        print(f"  - {r.source}: {r.collectes} collecté(s), {len(r.changements)} changement(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
