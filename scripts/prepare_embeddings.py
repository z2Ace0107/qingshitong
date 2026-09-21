"""Download the two pinned R0 embedding profiles into the local model cache.

The application remains offline-only at runtime. This preparation command is
the explicit deployment step that obtains public model files before startup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.embedding import EmbeddingConfigurationError, load_embedding_profile  # noqa: E402


PROFILES = ("bge-base-zh-v1.5", "qwen3-embedding-0.6b")


def _download(profile_id: str, cache_dir: Path) -> dict[str, object]:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:  # pragma: no cover - exercised by deployment setup
        raise RuntimeError(
            "huggingface-hub is required; install backend/requirements.txt first"
        ) from error

    profile = load_embedding_profile(
        {
            "QST_EMBEDDING_PROFILE": profile_id,
            "QST_EMBEDDING_CACHE_DIR": str(cache_dir),
        }
    )
    destination = Path(profile.cache_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "profile_id": profile.profile_id,
                "model_id": profile.model_id,
                "revision": profile.model_revision,
                "destination": str(destination),
                "status": "downloading",
            },
            ensure_ascii=False,
        )
    )
    snapshot_download(
        repo_id=profile.model_id,
        revision=profile.model_revision,
        repo_type="model",
        local_dir=str(destination),
        token=os.environ.get("HF_TOKEN") or None,
    )
    if not (destination / "config.json").is_file():
        raise RuntimeError(f"model_download_incomplete:{profile.profile_id}:config.json")
    print(
        json.dumps(
            {
                "profile_id": profile.profile_id,
                "revision": profile.model_revision,
                "destination": str(destination),
                "status": "downloaded",
            },
            ensure_ascii=False,
        )
    )
    return {"profile_id": profile.profile_id, "destination": str(destination)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare pinned R0 local embedding models.")
    parser.add_argument(
        "--profile",
        choices=(*PROFILES, "all"),
        default="all",
        help="Profile to download; defaults to both R0 profiles.",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("QST_EMBEDDING_CACHE_DIR") or str(ROOT / "models" / "embeddings"),
        help="Local model cache root; defaults to models/embeddings.",
    )
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    profile_ids = PROFILES if args.profile == "all" else (args.profile,)
    try:
        for profile_id in profile_ids:
            _download(profile_id, cache_dir)
    except (EmbeddingConfigurationError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
