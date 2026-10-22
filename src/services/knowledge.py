"""Recherche documentaire simple pour /ask : quels extraits envoyer à l'IA.

Sources, toutes locales et relues à chaque question :

* ``knowledge/*.md`` et ``*.txt`` : notes de l'équipe sur le projet (sujet, conventions,
  décisions…), à compléter librement ;
* ``config/planning.toml`` : échéances et prochaines étapes ;
* ``config/links.toml`` : liens renseignés ;
* ``content/messages/*.toml`` : règles et informations des messages permanents ;
* ``README.md`` et ``docs/*.md`` : documentation du bot lui-même.

La recherche compte les mots communs entre la question et chaque extrait (sans
accents ni majuscules, mots courants ignorés). Pas d'index ni de base vectorielle :
le volume d'une équipe de cinq reste petit, et le résultat est explicable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT, LinkCatalog, Planning
from src.services.content import MessageTemplate

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
DOCS = (PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").glob("*.md")))
CHUNK_CHARS = 1500
MAX_FILE_BYTES = 200_000
MAX_PASSAGES = 6
STOPWORDS = set(
    """
    les des une est pour que qui dans par sur avec pas plus son ses aux du de la le un et ou en au ce cette ces
    sont mais comme tout tous elle ils nous vous leur leurs quoi quel quelle quels quelles comment quand
    est-ce faire fait peut doit avoir etre été être via the and for are what how
    """.split()
)


@dataclass(frozen=True)
class Passage:
    source: str  # ex. « knowledge/sujet.md › Rendu final »
    text: str


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_]{3,}", normalize(text))
    # Racine grossière (5 lettres) : « deadline » / « deadlines », « tester » / « tests ».
    return {w[:5] for w in words if w not in STOPWORDS}


def load_passages(
    *,
    planning: Planning | None = None,
    catalog: LinkCatalog | None = None,
    templates: Iterable[MessageTemplate] = (),
    knowledge_dir: Path = KNOWLEDGE_DIR,
    docs: Iterable[Path] = DOCS,
    root: Path = PROJECT_ROOT,
) -> list[Passage]:
    passages: list[Passage] = []
    if knowledge_dir.is_dir():
        for path in sorted([*knowledge_dir.glob("*.md"), *knowledge_dir.glob("*.txt")]):
            if path.name.upper() != "README.MD":  # le mode d'emploi du dossier n'est pas une source
                passages.extend(_file_passages(path, root))
    if planning is not None:
        lines = [f"- {d.date:%d/%m/%Y} : {d.title}" + (f" ({d.details})" if d.details else "") for d in planning.deadlines]
        lines += [f"- étape : {s.title}" + (f" (responsable : {s.owner})" if s.owner else "") for s in planning.next_steps]
        if lines:
            passages.append(Passage("config/planning.toml › échéances et étapes", "\n".join(lines)))
    if catalog is not None:
        lines = [f"- {catalog.label(key)} : {url}" for key, url in catalog.urls.items() if url]
        missing = [catalog.label(key) for key in catalog.missing]
        if missing:
            lines.append("Liens pas encore renseignés : " + ", ".join(missing))
        if lines:
            passages.append(Passage("config/links.toml › liens du projet", "\n".join(lines)))
    for template in templates:
        body = "\n".join([template.description, *[f"{f.name}\n{f.value}" for f in template.fields]])
        passages.append(Passage(f"message permanent « {template.title} »", body.strip()))
    for path in docs:
        if path.is_file():
            passages.extend(_file_passages(path, root))
    return [p for p in passages if p.text.strip()]


def search(passages: list[Passage], question: str, limit: int = MAX_PASSAGES) -> list[Passage]:
    wanted = terms(question)
    if not wanted:
        return []
    scored = []
    for index, passage in enumerate(passages):
        score = len(wanted & terms(passage.text)) + 2 * len(wanted & terms(passage.source))
        if score:
            scored.append((-score, index, passage))
    return [p for _, _, p in sorted(scored)[:limit]]


def _file_passages(path: Path, root: Path) -> list[Passage]:
    try:
        raw = path.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return []
    try:
        name = path.relative_to(root).as_posix()
    except ValueError:
        name = path.name
    passages = []
    for heading, body in _sections(raw):
        for i, chunk in enumerate(_chunks(body)):
            label = f"{name} › {heading}" if heading else name
            passages.append(Passage(label if i == 0 else f"{label} (suite)", chunk))
    return passages


def _sections(text: str) -> list[tuple[str, str]]:
    sections, heading, lines = [], "", []
    for line in text.splitlines():
        match = re.match(r"^#{1,4}\s+(.*)", line)
        if match:
            if any(l.strip() for l in lines):
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = match.group(1).strip(), []
        else:
            lines.append(line)
    if any(l.strip() for l in lines):
        sections.append((heading, "\n".join(lines).strip()))
    return sections


def _chunks(text: str) -> list[str]:
    chunks, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph[: CHUNK_CHARS * 2]
    if current:
        chunks.append(current)
    return chunks
