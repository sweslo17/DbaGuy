from pathlib import Path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_file(data_dir: Path) -> Path:
    return data_dir / "profiles.json"
