"""Allow python -m sts2_auto."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
