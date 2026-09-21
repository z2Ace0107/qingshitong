from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence


class EmbeddingConfigurationError(ValueError):
    """Raised when deployment selects an embedding profile outside the R0 contract."""


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the selected local embedding profile cannot be loaded."""


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    model_id: str
    model_revision: str
    dimension: int
    normalized: bool
    query_instruction: str | None
    document_instruction: str | None
    tokenizer_version: str | None
    device_policy: str
    input_limit: int
    cache_path: str
    local_files_only: bool = True

    def to_health_payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "normalized": self.normalized,
            "device_policy": self.device_policy,
            "input_limit": self.input_limit,
            "local_files_only": self.local_files_only,
        }


DEFAULT_EMBEDDING_PROFILE_ID = "bge-base-zh-v1.5"

_PROFILE_SPECS: dict[str, EmbeddingProfile] = {
    "bge-base-zh-v1.5": EmbeddingProfile(
        profile_id="bge-base-zh-v1.5",
        model_id="BAAI/bge-base-zh-v1.5",
        model_revision="f03589ceff5aac7111bd60cfc7d497ca17ecac65",
        dimension=768,
        normalized=True,
        query_instruction=None,
        document_instruction=None,
        tokenizer_version=None,
        device_policy="cpu",
        input_limit=512,
        cache_path="",
    ),
    "qwen3-embedding-0.6b": EmbeddingProfile(
        profile_id="qwen3-embedding-0.6b",
        model_id="Qwen/Qwen3-Embedding-0.6B",
        model_revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        dimension=1024,
        normalized=True,
        query_instruction=None,
        document_instruction=None,
        tokenizer_version=None,
        device_policy="cpu",
        input_limit=32768,
        cache_path="",
    ),
}


def load_embedding_profile(environ: Mapping[str, str] | None = None) -> EmbeddingProfile:
    env = os.environ if environ is None else environ
    profile_id = (env.get("QST_EMBEDDING_PROFILE") or DEFAULT_EMBEDDING_PROFILE_ID).strip().lower()
    try:
        profile = _PROFILE_SPECS[profile_id]
    except KeyError as error:
        raise EmbeddingConfigurationError("embedding_profile_not_supported") from error

    cache_root = (env.get("QST_EMBEDDING_CACHE_DIR") or "models/embeddings").strip()
    cache_path = Path(cache_root) / profile.profile_id / profile.model_revision
    return replace(profile, cache_path=str(cache_path))


def _as_embedding_matrix(value: Any, profile: EmbeddingProfile):
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - dependency is declared for runtime use
        raise EmbeddingUnavailableError("runtime_dependency_missing:numpy") from error

    matrix = np.asarray(value, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or matrix.shape[1] != profile.dimension:
        raise EmbeddingUnavailableError("embedding_dimension_mismatch")
    if not np.isfinite(matrix).all():
        raise EmbeddingUnavailableError("embedding_non_finite")
    if profile.normalized:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if (norms == 0).any():
            raise EmbeddingUnavailableError("embedding_zero_vector")
        matrix = matrix / norms
    return matrix.astype("<f4", copy=False)


class LocalEmbeddingEncoder:
    """Small adapter around the selected local Sentence Transformers model."""

    def __init__(self, profile: EmbeddingProfile, model: Any):
        self.profile = profile
        self.model = model

    def _encode(self, method_name: str, texts: Sequence[str]):
        method = getattr(self.model, method_name, None) or getattr(self.model, "encode", None)
        if method is None:
            raise EmbeddingUnavailableError("model_encode_not_supported")
        try:
            value = method(
                list(texts),
                normalize_embeddings=self.profile.normalized,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except TypeError:
            value = method(list(texts), normalize_embeddings=self.profile.normalized)
        return _as_embedding_matrix(value, self.profile)

    def encode_documents(self, texts: Sequence[str]):
        return self._encode("encode_document", texts)

    def encode_query(self, text: str):
        return self._encode("encode_query", [text])


def _load_sentence_transformer(profile: EmbeddingProfile) -> Any:
    model_path = Path(profile.cache_path)
    if not model_path.is_dir():
        raise EmbeddingUnavailableError("model_not_prepared")
    if not importlib.util.find_spec("sentence_transformers") or not importlib.util.find_spec("torch"):
        raise EmbeddingUnavailableError("runtime_dependency_missing")
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            str(model_path),
            device=profile.device_policy,
            revision=profile.model_revision,
            local_files_only=profile.local_files_only,
        )
        dimension_getter = getattr(model, "get_embedding_dimension", None)
        if dimension_getter is None:
            dimension_getter = model.get_sentence_embedding_dimension
        dimension = dimension_getter()
    except EmbeddingUnavailableError:
        raise
    except Exception as error:
        raise EmbeddingUnavailableError("model_load_failed") from error
    if dimension != profile.dimension:
        raise EmbeddingUnavailableError("embedding_dimension_mismatch")
    return model


@lru_cache(maxsize=2)
def _cached_embedding_encoder(profile: EmbeddingProfile) -> LocalEmbeddingEncoder:
    return LocalEmbeddingEncoder(profile, _load_sentence_transformer(profile))


def load_embedding_encoder(environ: Mapping[str, str] | None = None) -> LocalEmbeddingEncoder:
    profile = load_embedding_profile(environ)
    return _cached_embedding_encoder(profile)


def embedding_profile_health(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    profile = load_embedding_profile(environ)
    model_path = Path(profile.cache_path)
    if not model_path.is_dir():
        status = "unavailable"
        reason = "model_not_prepared"
    elif not importlib.util.find_spec("sentence_transformers") or not importlib.util.find_spec("torch"):
        status = "unavailable"
        reason = "runtime_dependency_missing"
    else:
        try:
            load_embedding_encoder(environ)
        except EmbeddingUnavailableError as error:
            status = "unavailable"
            reason = str(error)
        else:
            status = "ready"
            reason = None
    return {
        **profile.to_health_payload(),
        "status": status,
        "reason": reason,
        "fallback_profile": None,
    }
