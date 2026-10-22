from src.gitlab.security import clean_text, first_line, plain_text, safe_url, token_matches


def test_token_matches_only_exact_secret():
    assert token_matches("un-secret-de-test-assez-long", "un-secret-de-test-assez-long")
    assert not token_matches("un-secret-de-test-assez-long", "un-secret-de-test-assez-lonG")
    assert not token_matches("un-secret-de-test-assez-long", "")
    assert not token_matches("un-secret-de-test-assez-long", None)


def test_empty_expected_secret_never_matches():
    assert not token_matches("", "")


def test_non_ascii_token_does_not_crash():
    assert not token_matches("un-secret-de-test-assez-long", "sécrét")


def test_clean_text_neutralizes_mentions():
    text = clean_text("ping @everyone et @here et <@123> et <@&456>", 200)
    assert "@everyone" not in text
    assert "@here" not in text
    assert "<@123>" not in text
    assert "<@&456>" not in text


def test_clean_text_escapes_markdown_and_masked_links():
    text = clean_text("**gras** [clique](https://evil.example) `code`", 200)
    assert "**" not in text.replace("\\*", "")
    assert "\\[clique\\]" in text
    assert "\\`code\\`" in text


def test_clean_text_escapes_line_start_markdown():
    assert clean_text("# Titre\n> citation\n- liste", 200) == "\\# Titre\n\\> citation\n\\- liste"


def test_clean_text_removes_control_chars_and_truncates():
    text = clean_text("a\x00b‮c" + "x" * 300, 50)
    assert "\x00" not in text and "‮" not in text
    assert len(text) == 50 and text.endswith("…")


def test_clean_text_single_line_and_non_string():
    assert clean_text("une\nligne   seule", 50, single_line=True) == "une ligne seule"
    assert clean_text(None, 50) == ""
    assert clean_text(42, 50) == ""


def test_plain_text_keeps_markdown_but_not_mentions():
    assert plain_text("fix: *menu*\n@everyone", 100) == "fix: *menu* @​everyone"


def test_first_line():
    assert first_line("\n\n  Titre  \ncorps") == "Titre"
    assert first_line(None) == ""


def test_safe_url():
    assert safe_url("https://gitlab.com/a/b") == "https://gitlab.com/a/b"
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("https://gitlab.com/a b") is None
    assert safe_url("https://x.example/)[y](https://evil)") is None
    assert safe_url(None) is None
