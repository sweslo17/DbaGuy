from pathlib import Path

from loguru import logger

from dbaguy_backend.logging_setup import setup_logging

SECRET = "leak-me-123"


def _open_connection(password: str) -> None:
    raise RuntimeError("login failed")


def _connect_with_saved_password() -> None:
    password = "leak-me-123"
    _open_connection(password)  # loguru's diagnose mode prints `password = ...` for this caller line


def test_error_logs_do_not_print_local_variable_values(tmp_path: Path, capsys) -> None:
    setup_logging("INFO", tmp_path)
    try:
        _connect_with_saved_password()
    except RuntimeError:
        logger.opt(exception=True).error("Unhandled error on POST /x")
    stderr_text = capsys.readouterr().err
    logger.remove()  # stops the enqueued file sink and flushes it to disk
    file_text = (tmp_path / "backend.log").read_text(encoding="utf-8")
    for text in (stderr_text, file_text):
        assert "Unhandled error on POST /x" in text
        assert "login failed" in text
        assert SECRET not in text
