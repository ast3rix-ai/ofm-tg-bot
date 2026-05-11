from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.crypto import generate_key  # noqa: E402


def main() -> None:
    key = generate_key()
    sys.stdout.write(key + "\n")
    sys.stderr.write("Paste this value into .env as SESSION_ENCRYPTION_KEY=\n")


if __name__ == "__main__":
    main()
