from importlib.metadata import PackageNotFoundError, version


def app_version() -> str:
    try:
        return version("dbaguy-backend")
    except PackageNotFoundError:
        return "0.0.0"
