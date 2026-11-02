#!/usr/bin/env python3
"""Optional offline review audit; run --help for explicit publication options."""

if __package__:
    from .review_support.audit import main
else:
    from review_support.audit import main

if __name__ == "__main__":
    raise SystemExit(main())
