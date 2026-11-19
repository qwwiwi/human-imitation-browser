#!/usr/bin/env python3
"""List, show and delete browser profiles.

Examples:
    .venv/bin/python scripts/profile.py list
    .venv/bin/python scripts/profile.py show default
    .venv/bin/python scripts/profile.py delete my-shop
"""

import sys

from hib.profile_cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
