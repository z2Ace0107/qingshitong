from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_RENDERERS = {"svg", "raster", "sprite-sheet", "frame-sequence"}
ALLOWED_STATES = {
    "idle",
    "clarifying",
    "retrieving",
    "evidence_ready",
    "warning",
    "awaiting_confirmation",
    "simulated_success",
    "needs_reconfirmation",
    "degraded",
}


def fail(message: str) -> None:
    raise ValueError(message)


def asset_path(root: Path, source: str) -> Path:
    if not isinstance(source, str) or not source or urlparse(source).scheme or source.startswith(("//", "/")) or any(char in source for char in "?#"):
        fail(f"asset path must be a local relative path: {source!r}")
    path = (root / source).resolve()
    if root.resolve() not in path.parents:
        fail(f"asset escapes mascot directory: {source!r}")
    if not path.is_file():
        fail(f"asset does not exist: {source!r}")
    return path


def validate(path: Path) -> dict:
    root = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "id", "display_name", "renderer", "source", "canvas", "asset", "fallback", "states"}
    missing = required - manifest.keys()
    if missing:
        fail(f"missing manifest fields: {', '.join(sorted(missing))}")
    if manifest["schema_version"] != "mascot-asset-v1":
        fail("unsupported schema_version")
    if manifest["renderer"] not in ALLOWED_RENDERERS:
        fail(f"renderer is not enabled: {manifest['renderer']!r}")
    if manifest["source"].get("kind") not in {"original", "licensed", "user_provided"}:
        fail("source.kind must declare an asset provenance")
    if not manifest["source"].get("license") or not manifest["source"].get("attribution"):
        fail("source.license and source.attribution are required")
    if not isinstance(manifest["states"], dict) or not manifest["states"]:
        fail("states must be a non-empty object")
    unknown_states = set(manifest["states"]) - ALLOWED_STATES
    if unknown_states:
        fail(f"unknown states: {', '.join(sorted(unknown_states))}")

    checked: set[Path] = set()
    for key in ("asset", "fallback"):
        spec = manifest[key]
        if not spec.get("alt") or not isinstance(spec.get("sha256"), str) or len(spec["sha256"]) != 64:
            fail(f"{key} must declare alt and sha256")
        checked.add(asset_path(root, spec["src"]))

    if manifest["renderer"] == "sprite-sheet":
        atlas = manifest.get("assets", {}).get("atlas") or manifest.get("atlas", {}).get("src")
        if not atlas:
            fail("sprite-sheet requires assets.atlas or atlas.src")
        checked.add(asset_path(root, atlas))
        frames = manifest.get("frames")
        if not isinstance(frames, dict) or not frames:
            fail("sprite-sheet requires named frames")
        for frame in frames.values():
            if not all(isinstance(frame.get(key), (int, float)) and frame[key] >= 0 for key in ("x", "y")):
                fail("sprite frame needs non-negative x/y")
            if not all(isinstance(frame.get(key), (int, float)) and frame[key] > 0 for key in ("width", "height")):
                fail("sprite frame needs positive width/height")
    if manifest["renderer"] == "frame-sequence":
        for state, spec in manifest["states"].items():
            for frame in spec.get("frames", []):
                source = frame if isinstance(frame, str) else frame.get("src")
                checked.add(asset_path(root, source))

    for asset in checked:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        declared = next((manifest[key].get("sha256") for key in ("asset", "fallback") if manifest[key].get("src") == str(asset.relative_to(root)).replace("\\", "/")), None)
        if declared and digest != declared.lower():
            fail(f"sha256 mismatch: {asset.relative_to(root)}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a qingshitong mascot manifest and local asset paths")
    parser.add_argument("path", nargs="?", type=Path, default=Path(__file__).resolve().parents[1] / "web" / "mascot" / "manifest.json")
    args = parser.parse_args()
    manifest = validate(args.path.resolve())
    print(f"mascot manifest valid: {args.path} ({manifest['renderer']})")


if __name__ == "__main__":
    main()
