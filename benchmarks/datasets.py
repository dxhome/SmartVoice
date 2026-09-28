"""On-demand public benchmark dataset download and local audio caching."""

from __future__ import annotations

import hashlib
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpeechSample:
    sample_id: str
    language: str
    text: str
    audio_path: Path
    duration_seconds: float
    dataset: str


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100] or "sample"


def _write_wav(path: Path, audio: dict[str, Any]) -> float:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Install benchmark dependencies with: python -m pip install -r benchmarks/requirements.txt") from exc
    values = np.asarray(audio["array"])
    if values.ndim > 1:
        values = values.mean(axis=0 if values.shape[0] <= 8 else 1)
    values = values.astype(np.float32, copy=False)
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    if peak > 1.0:
        values = values / max(peak, 1.0)
    pcm = (np.clip(values, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    sample_rate = int(audio["sampling_rate"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return len(values) / sample_rate


def load_fleurs_samples(
    *, repo_id: str, revision: str, split: str, dataset_config: str,
    language: str, cache_dir: Path, limit: int | None = None, seed: int = 20260928,
) -> tuple[list[SpeechSample], dict[str, Any]]:
    """Load one pinned FLEURS split and materialize its audio outside the repo."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("Install benchmark dependencies with: python -m pip install -r benchmarks/requirements.txt") from exc
    # Load the pinned, data-only Parquet shards instead of executing the
    # dataset repository's custom Python builder. FLEURS Parquet row groups
    # are too large for reliable remote range streaming, so cache the source
    # shards locally outside the repository before reading them.
    from huggingface_hub import HfApi, hf_hub_download

    prefix = f"{dataset_config}/{split}-"
    paths = sorted(
        path for path in HfApi().list_repo_files(repo_id, repo_type="dataset", revision=revision)
        if path.startswith(prefix) and path.endswith(".parquet")
    )
    if not paths:
        raise RuntimeError(f"No Parquet files found for {repo_id}@{revision}:{dataset_config}/{split}")
    data_files = [
        hf_hub_download(
            repo_id=repo_id, filename=path, revision=revision,
            repo_type="dataset", cache_dir=str(cache_dir / "huggingface"),
        )
        for path in paths
    ]
    dataset = load_dataset(
        "parquet", data_files={split: data_files}, split=split,
        cache_dir=str(cache_dir / "huggingface"),
    )
    if limit is not None and hasattr(dataset, "shuffle"):
        dataset = dataset.shuffle(seed=seed).select(range(min(limit, len(dataset))))
    audio_dir = cache_dir / "datasets" / _safe_component(repo_id) / revision / dataset_config / split
    samples = []
    for index, row in enumerate(dataset):
        if limit is not None and index >= limit:
            break
        audio = row["audio"]
        # FLEURS `id` identifies a transcript and can repeat for different
        # recordings. Its top-level `path` identifies the actual audio file.
        upstream_path = str(row.get("path") or row.get("id", index))
        sample_id = _safe_component(Path(upstream_path).stem or upstream_path)
        audio_path = audio_dir / f"{sample_id}.wav"
        if audio_path.is_file():
            with wave.open(str(audio_path), "rb") as cached:
                duration = cached.getnframes() / cached.getframerate()
        else:
            duration = _write_wav(audio_path, audio)
        samples.append(SpeechSample(
            sample_id=sample_id,
            language=language,
            text=str(row.get("transcription", row.get("raw_transcription", ""))),
            audio_path=audio_path,
            duration_seconds=duration,
            dataset=f"{repo_id}@{revision}:{dataset_config}/{split}",
        ))
    if not samples:
        raise RuntimeError(f"The selected dataset split is empty: {repo_id}:{dataset_config}/{split}")
    metadata = {
        "repository": repo_id,
        "revision": revision,
        "config": dataset_config,
        "split": split,
        "license": "cc-by-4.0",
        "sample_count": len(samples),
        "sample_ids_sha256": hashlib.sha256("\n".join(sample.sample_id for sample in samples).encode()).hexdigest(),
    }
    return samples, metadata
