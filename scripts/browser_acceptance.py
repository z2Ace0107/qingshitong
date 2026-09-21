"""Compatibility entry point for the current R0 browser acceptance flow.

The former script targeted the removed static demo DOM. Keep this stable command
name for local documentation, but delegate to the React/Docker acceptance flow
so it validates the current student surface instead of a historical UI.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from r0_frontend_acceptance import run


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the current R0 browser acceptance flow.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--evidence-dir", default="docs/evidence/browser/r0-current-check")
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), ROOT / args.evidence_dir)


if __name__ == "__main__":
    main()
