#!/usr/bin/env python3
"""Launcher for running Nimbus straight from a source checkout."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nimbus.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))
