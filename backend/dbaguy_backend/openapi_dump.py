import json
import sys

from .app import create_app
from .config import Settings


def main() -> None:
    app = create_app(Settings(log_to_file=False))
    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
