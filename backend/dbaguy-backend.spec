# -*- mode: python ; coding: utf-8 -*-
# Build with: .venv/bin/pyinstaller --noconfirm --clean dbaguy-backend.spec
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    *collect_submodules("uvicorn"),
    *collect_submodules("dbaguy_backend"),
    "keyring.backends.macOS",
    "psycopg_binary",
    *collect_submodules("psycopg_binary"),
    # sqlglot looks dialects up by name with importlib, which the analysis cannot follow. The classifier imports the
    # ones it uses by name (dbaguy_backend/sql/sqlglot_rules.py); these keep them, and what they load, bundled anyway.
    "sqlglot.dialects.postgres",
    "sqlglot.dialects.sqlite",
    "sqlglot.parsers.postgres",
    "sqlglot.parsers.sqlite",
    "sqlglot.generators.postgres",
    "sqlglot.generators.sqlite",
    "sqlglot.typing.postgres",
]

a = Analysis(
    ["entry.py"],
    pathex=["."],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dbaguy-backend",
    console=True,
    target_arch="arm64",
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="dbaguy-backend")
