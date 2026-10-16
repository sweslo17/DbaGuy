from pathlib import Path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_file(data_dir: Path) -> Path:
    return data_dir / "profiles.json"


def settings_file(data_dir: Path) -> Path:
    return data_dir / "settings.json"


def workspace_file(data_dir: Path) -> Path:
    return data_dir / "workspace.json"


def history_file(data_dir: Path) -> Path:
    return data_dir / "history.db"


def default_scripts_dir(data_dir: Path) -> Path:
    return data_dir / "scripts"
