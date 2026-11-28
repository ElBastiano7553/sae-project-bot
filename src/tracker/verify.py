"""Vérification du classeur depuis un terminal, en lecture seule.

    python -m src.tracker.verify "GAME DESIGN DATABASE — LUNASTEH.xlsx"
    python -m src.tracker.verify --google https://docs.google.com/spreadsheets/d/<identifiant>/edit

Options : ``--markdown rapport.md`` écrit le rapport dans un fichier, ``--json rapport.json`` écrit
les constats au format JSON. Code de sortie : 0 sans erreur, 1 s'il y a des erreurs, 2 si le
classeur est illisible. Rien n'est jamais modifié (ni le fichier, ni le Google Sheet).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from src.tracker.checks import report_markdown, sheets_from_xlsx, verify_workbook
from src.tracker.google import GoogleSheetError, download_xlsx, spreadsheet_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vérifie la cohérence du classeur de suivi (lecture seule).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("classeur", nargs="?", type=Path, help="fichier .xlsx")
    source.add_argument("--google", metavar="ADRESSE", help="adresse ou identifiant du Google Sheet (partagé par lien)")
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.google:
            data = asyncio.run(download_xlsx(args.google))
            label = f"Google Sheet {spreadsheet_id(args.google)}"
        else:
            if not args.classeur.is_file():
                print(f"Fichier introuvable : {args.classeur}", file=sys.stderr)
                return 2
            data, label = args.classeur.read_bytes(), args.classeur.name
        report = verify_workbook(sheets_from_xlsx(data), source=label)
    except (GoogleSheetError, ValueError, OSError) as exc:
        print(f"Classeur illisible : {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # fichier corrompu, format inattendu
        print(f"Classeur illisible ({type(exc).__name__}) : est-ce bien un fichier .xlsx ?", file=sys.stderr)
        return 2
    text = report_markdown(report)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
        print(f"{report.summary()}\nRapport écrit dans {args.markdown}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text + "\n")
    if args.json:
        payload = {"source": report.source, "checked_at": report.checked_at, "counts": report.counts,
                   "findings": [asdict(f) for f in report.findings]}
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
