import logging
import subprocess
import sys
from pathlib import Path

import pytest
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
    logger.remove()  # closes the file sink
    file_text = (tmp_path / "backend.log").read_text(encoding="utf-8")
    for text in (stderr_text, file_text):
        assert "Unhandled error on POST /x" in text
        assert "login failed" in text
        assert SECRET not in text


@pytest.mark.parametrize(("level", "sql_on_disk"), [("INFO", False), ("DEBUG", True)])
def test_file_log_follows_the_configured_level(tmp_path: Path, level: str, sql_on_disk: bool) -> None:
    setup_logging(level, tmp_path)
    logger.debug("execute: SELECT secret_column FROM payroll")  # DEBUG carries SQL text
    logger.info("Session opened")
    logger.remove()
    text = (tmp_path / "backend.log").read_text(encoding="utf-8")
    assert "Session opened" in text
    assert ("SELECT secret_column" in text) is sql_on_disk


def test_file_logging_starts_no_helper_process(tmp_path: Path) -> None:
    # loguru's enqueue=True starts a multiprocessing resource tracker (~37 MB) next to the backend.
    code = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from loguru import logger\n"
        "from dbaguy_backend.logging_setup import setup_logging\n"
        "setup_logging('INFO', Path(sys.argv[1]))\n"
        "logger.info('hello')\n"
        "print(subprocess.run(['pgrep', '-P', str(os.getpid())], capture_output=True, text=True).stdout.split())\n"
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=30,
                            cwd=Path(__file__).resolve().parent.parent)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"helper processes: {result.stdout}"


def test_the_mysql_drivers_connect_chatter_stays_out_of_the_log(capsys) -> None:
    setup_logging("DEBUG", None)
    driver = logging.getLogger("mysql.connector")
    driver.info("plugin_name: caching_sha2_password")  # three of these per connect otherwise
    driver.warning("MFA terminated with a no ok packet")
    stderr_text = capsys.readouterr().err
    logger.remove()
    assert "plugin_name" not in stderr_text and "MFA terminated" in stderr_text
