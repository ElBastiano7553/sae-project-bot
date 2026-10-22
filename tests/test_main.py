from src.main import check_content_files


def test_content_files_are_valid_without_gitlab():
    check_content_files()


def test_content_files_are_valid_with_gitlab():
    check_content_files(gitlab_enabled=True)
