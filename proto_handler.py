#!/usr/bin/env python3
"""
Backward-compatible wrapper for the integrated trial workflow.

Old usage:
  python proto_handler.py --email user@example.com --password 'secret'

Equivalent new usage:
  python windsurf_auth_replay.py --mode trial --email user@example.com --password 'secret'
"""

from __future__ import annotations

import sys

from windsurf_auth_replay import main


if __name__ == "__main__":
    if "--mode" not in sys.argv[1:]:
        sys.argv[1:1] = ["--mode", "trial"]
    sys.exit(main())
