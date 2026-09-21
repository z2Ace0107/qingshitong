"""Report whether the selected local R0 embedding Profile is ready.

This command is intentionally read-only: it never downloads a model or changes
the selected Profile. Model preparation belongs to the deployment environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.embedding import EmbeddingConfigurationError, embedding_profile_health  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check one R0 local embedding Profile without downloading it.")
    parser.add_argument(
        "--profile",
        choices=("bge-base-zh-v1.5", "qwen3-embedding-0.6b"),
        help="Profile to check; defaults to QST_EMBEDDING_PROFILE or the BGE-base default.",
    )
    parser.add_argument("--cache-dir", help="Override QST_EMBEDDING_CACHE_DIR for this read-only check.")
    args = parser.parse_args()

    environ = dict(os.environ)
    if args.profile:
        environ["QST_EMBEDDING_PROFILE"] = args.profile
    if args.cache_dir:
        environ["QST_EMBEDDING_CACHE_DIR"] = args.cache_dir

    try:
        health = embedding_profile_health(environ)
    except EmbeddingConfigurationError as error:
        print(json.dumps({"status": "invalid", "reason": str(error)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(health, ensure_ascii=False, indent=2))
    return 0 if health["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
