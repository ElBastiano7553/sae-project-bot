from src.services.text_limits import chunk_lines, truncate


def test_truncate_short_text_unchanged():
    assert truncate("bonjour", 10) == "bonjour"


def test_truncate_adds_ellipsis_within_limit():
    result = truncate("a" * 50, 10)
    assert len(result) == 10
    assert result.endswith("…")


def test_truncate_edge_cases():
    assert truncate("abc", 0) == ""
    assert truncate("abcdef", 1) == "a"


def test_chunk_lines_never_exceeds_limit():
    lines = [f"ligne {i} " + "x" * 30 for i in range(100)]
    chunks = chunk_lines(lines, 200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert sum(chunk.count("ligne") for chunk in chunks) == 100


def test_chunk_lines_truncates_a_single_huge_line():
    chunks = chunk_lines(["y" * 5000], 1024)
    assert chunks == ["y" * 1023 + "…"]


def test_chunk_lines_empty():
    assert chunk_lines([], 100) == []
