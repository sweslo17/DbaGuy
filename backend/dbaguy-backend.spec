# -*- mode: python ; coding: utf-8 -*-
# Build with: .venv/bin/pyinstaller --noconfirm --clean dbaguy-backend.spec
from PyInstaller.utils.hooks import collect_submodules

# mysql-connector runs pure Python in DbaGuy (use_pure=True): leave its C extension and the parts that need other
# packages (Django, asyncio API, OpenTelemetry, Kerberos/LDAP/OCI/OpenID/WebAuthn plugins) out of the bundle.
_MYSQL_LEFT_OUT = (
    "mysql.connector.connection_cext", "mysql.connector.cursor_cext", "mysql.connector.aio", "mysql.connector.django",
    "mysql.connector.opentelemetry.instrumentation", "mysql.connector.plugins.authentication_kerberos_client",
    "mysql.connector.plugins.authentication_ldap_sasl_client", "mysql.connector.plugins.authentication_oci_client",
    "mysql.connector.plugins.authentication_openid_connect_client",
    "mysql.connector.plugins.authentication_webauthn_client",
)

# The MCP SDK (spec ⑧): agent/server.py imports it only once the MCP switch is on, which the analysis still follows;
# what it loads by name (mcp_types' protocol versions, the server's transports) is collected whole. Not its CLI or the
# v1 FastMCP stub, which raises ModuleNotFoundError on import by design.
_MCP_LEFT_OUT = ("mcp.cli", "mcp.server.fastmcp")

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
    "sqlglot.dialects.mysql",
    "sqlglot.parsers.mysql",
    "sqlglot.generators.mysql",
    "sqlglot.typing.mysql",
    "sqlglot.dialects.tsql",
    "sqlglot.parsers.tsql",
    "sqlglot.generators.tsql",
    "sqlglot.typing.tsql",
    # pymssql has FreeTDS linked into _mssql*.so; the OpenSSL it loads comes from pymssql/.dylibs (@loader_path) and is
    # collected as a binary dependency. pyinstaller-hooks-contrib's hook only names _mssql: naming both extensions keeps
    # a hook change from dropping one.
    "pymssql._mssql",
    "pymssql._pymssql",
    # Keeps the locales (loaded with importlib) and the native/caching_sha2/sha256/clear-password auth plugins.
    *collect_submodules("mysql.connector", filter=lambda name: not name.startswith(_MYSQL_LEFT_OUT)),
    *collect_submodules("mcp", filter=lambda name: not name.startswith(_MCP_LEFT_OUT)),
    *collect_submodules("mcp_types"),
]

a = Analysis(
    ["entry.py"],
    pathex=["."],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller", "_mysql_connector", *_MYSQL_LEFT_OUT, *_MCP_LEFT_OUT],
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
