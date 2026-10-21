from __future__ import annotations

import time
import numpy as np

from backend.analysis.dataset import Dataset, split_metadata
from backend.analysis.features import build_profiles
from backend.analysis.math_tools import cluster_metrics, kmeans, standardize_fit


def _reduce(values: np.ndarray, algorithm: str, dimensions: int) -> tuple[np.ndarray, list[float], float]:
    target = min(dimensions, values.shape[0], values.shape[1])
    started = time.perf_counter()
    if algorithm == "pca":
        centered = values - values.mean(axis=0)
        covariance = centered.T @ centered / max(1, len(centered) - 1)
        eigenvalues, vectors = np.linalg.eigh(covariance)
        order = np.argsort(eigenvalues)[::-1]
        eigenvalues, vectors = np.maximum(eigenvalues[order], 0), vectors[:, order]
        coordinates = centered @ vectors[:, :target]
        ratios = eigenvalues[:target] / max(1e-12, eigenvalues.sum())
    else:
        left, singular, _ = np.linalg.svd(values, full_matrices=False)
        coordinates = left[:, :target] * singular[:target]
        ratios = singular[:target] ** 2 / max(1e-12, (singular ** 2).sum())
    return coordinates, [round(float(item), 6) for item in ratios], (time.perf_counter() - started) * 1000


def run(dataset: Dataset, algorithm: str | None, n_clusters: int, dimensions: int) -> dict:
    users, names, values, _ = build_profiles(dataset)
    scaled, _, _ = standardize_fit(values)
    requested = (algorithm or "all").lower().replace("_", "").replace(" ", "")
    algorithms = [requested] if requested in ("pca", "svd", "truncatedsvd") else ["pca", "svd"]
    before_started = time.perf_counter(); before_labels, _ = kmeans(scaled, n_clusters)
    before_ms = (time.perf_counter() - before_started) * 1000
    before_metrics = cluster_metrics(scaled, before_labels)
    results = []
    for name in algorithms:
        canonical = "svd" if name == "truncatedsvd" else name
        coordinates, ratios, fit_ms = _reduce(scaled, canonical, dimensions)
        after_started = time.perf_counter(); after_labels, _ = kmeans(coordinates, n_clusters)
        after_ms = (time.perf_counter() - after_started) * 1000
        results.append({"algorithm": canonical, "original_dimension": int(values.shape[1]), "reduced_dimension": int(coordinates.shape[1]),
                        "explained_variance_ratio": ratios, "total_information_retained": round(sum(ratios), 6), "fit_time_ms": round(fit_ms, 3),
                        "memory_usage_mb": round((scaled.nbytes + coordinates.nbytes) / 1024 / 1024, 4),
                        "coordinates": [{"user_id": user, "x": round(float(row[0]), 5), "y": round(float(row[1]), 5) if len(row) > 1 else 0.0, "cluster_id": int(label)} for user, row, label in zip(users, coordinates, after_labels)],
                        "clustering_comparison": {"before": {"dimensions": int(values.shape[1]), "fit_time_ms": round(before_ms, 3), "memory_mb": round(scaled.nbytes / 1024 / 1024, 4), **before_metrics},
                                                  "after": {"dimensions": int(coordinates.shape[1]), "fit_time_ms": round(after_ms, 3), "memory_mb": round(coordinates.nbytes / 1024 / 1024, 4), **cluster_metrics(coordinates, after_labels)}}})
    return {"task_type": "dimensionality_reduction", "model_version": "reduction-v2", "profile_version": "user-profile-v2", "feature_schema": names, "results": results, "data_split": split_metadata(dataset), "limitations": ["二维投影主要用于可视化；距离和聚类质量应结合原始空间指标解释。"]}
