from src.config import LinkCatalog, Planning
from src.services.knowledge import Passage, load_passages, search, terms


def test_terms_ignore_accents_case_and_common_words():
    assert terms("Quelle est l'ÉCHÉANCE du rendu ?") == {"echea", "rendu"}


def test_markdown_files_are_split_by_heading(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "sujet.md").write_text("# Sujet\nUn jeu de plateforme.\n\n## Rendu final\nLa soutenance a lieu en janvier.\n", encoding="utf-8")
    passages = load_passages(knowledge_dir=knowledge, docs=(), root=tmp_path)
    assert [p.source for p in passages] == ["knowledge/sujet.md › Sujet", "knowledge/sujet.md › Rendu final"]


def test_planning_and_links_become_passages(tmp_path):
    catalog = LinkCatalog(urls={"sujet_sae": "https://exemple.fr/sujet", "readme": ""})
    passages = load_passages(planning=Planning(), catalog=catalog, knowledge_dir=tmp_path / "absent", docs=(), root=tmp_path)
    assert len(passages) == 1 and "https://exemple.fr/sujet" in passages[0].text


def test_search_ranks_by_shared_words():
    passages = [Passage("a", "Les tests unitaires utilisent pytest."), Passage("b", "La soutenance a lieu en janvier."),
                Passage("soutenance", "Préparer les diapositives.")]
    found = search(passages, "Quand a lieu la soutenance ?")
    assert [p.source for p in found] == ["b", "soutenance"]  # 2 mots communs chacun : ordre des fichiers
    assert search(passages, "le la les") == []


def test_real_project_files_load():
    passages = load_passages()
    assert any(p.source.startswith("README.md") for p in passages)
