"""Allow running the app with ``python3 -m nimbus``."""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
