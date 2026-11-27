"""Analyse en lecture seule d'un classeur Excel : ``python -m src.tracker.inspect "GAME DESIGN DATABASE — LUNASTEH.xlsx"``.

Le fichier n'est jamais modifié (il est lu en mémoire). Options :

* ``--json rapport.json`` : écrit aussi l'analyse complète au format JSON ;
* ``--markdown rapport.md`` : écrit le rapport dans un fichier au lieu de l'afficher.

Le rapport liste, pour chaque onglet, la ligne d'en-tête, les colonnes, les colonnes à formule (avec un
exemple), les identifiants (motif, doublons), les statuts observés et les listes déroulantes, les
références entre onglets, ainsi que les graphiques et images (que openpyxl ne recopie pas).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.tracker.workbook import inspect_workbook, report_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse (lecture seule) du classeur de suivi du projet.")
    parser.add_argument("classeur", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.classeur.is_file():
        print(f"Fichier introuvable : {args.classeur}", file=sys.stderr)
        return 2
    info = inspect_workbook(args.classeur.read_bytes(), args.classeur.name)
    text = report_markdown(info)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
        print(f"Rapport écrit dans {args.markdown}")
    else:
        sys.stdout.write(text)
    if args.json:
        args.json.write_text(json.dumps(info.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Analyse JSON écrite dans {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
