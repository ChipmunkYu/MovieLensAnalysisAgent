from __future__ import annotations

import numpy as np


def standardize_fit(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean, scale = values.mean(axis=0), values.std(axis=0)
    scale[scale < 1e-9] = 1.0
    return (values - mean) / scale, mean, scale


def standardize_apply(values: np.ndarray, mean: np.ndarray, scale: np.ndarray) -> np.ndarray:
    return (values - mean) / scale


def binary_metrics(y: np.ndarray, probability: np.ndarray) -> dict[str, float]:
    predicted, y = (probability >= .5).astype(int), y.astype(int)
    tp, tn = int(((predicted == 1) & (y == 1)).sum()), int(((predicted == 0) & (y == 0)).sum())
    fp, fn = int(((predicted == 1) & (y == 0)).sum()), int(((predicted == 0) & (y == 1)).sum())
    precision, recall = tp / max(1, tp + fp), tp / max(1, tp + fn)
    positives, negatives = probability[y == 1], probability[y == 0]
    if not len(positives) or not len(negatives):
        auc = .5
    else:
        order = np.argsort(probability, kind="stable")
        ranks = np.empty(len(probability), dtype=float)
        sorted_values = probability[order]
        start = 0
        while start < len(sorted_values):
            end = start + 1
            while end < len(sorted_values) and sorted_values[end] == sorted_values[start]:
                end += 1
            ranks[order[start:end]] = (start + end - 1) / 2 + 1
            start = end
        positive_count = len(positives)
        negative_count = len(negatives)
        auc = float((ranks[y == 1].sum() - positive_count * (positive_count + 1) / 2) / (positive_count * negative_count))
    return {"accuracy": round((tp + tn) / max(1, len(y)), 4), "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(2 * precision * recall / max(1e-12, precision + recall), 4), "roc_auc": round(auc, 4)}


def kmeans(values: np.ndarray, n_clusters: int, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    if len(values) < 2:
        raise ValueError("至少需要 2 个用户才能聚类")
    k, rng = min(n_clusters, len(values)), np.random.default_rng(seed)
    centers, labels = values[rng.choice(len(values), k, replace=False)].copy(), np.zeros(len(values), dtype=int)
    for _ in range(100):
        next_labels = ((values[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        next_centers = np.asarray([values[next_labels == i].mean(axis=0) if np.any(next_labels == i) else centers[i] for i in range(k)])
        if np.array_equal(labels, next_labels) and np.allclose(centers, next_centers):
            break
        labels, centers = next_labels, next_centers
    return labels, centers


def dbscan(values: np.ndarray, eps: float = 1.25, min_samples: int = 3, block_size: int = 512) -> np.ndarray:
    labels = np.full(len(values), -99, dtype=int)

    def neighbours(point: int) -> np.ndarray:
        found = []
        for start in range(0, len(values), block_size):
            block = values[start:start + block_size]
            distance = np.sqrt(((block - values[point]) ** 2).sum(axis=1))
            found.extend((start + np.flatnonzero(distance <= eps)).tolist())
        return np.asarray(found, dtype=int)

    cluster = 0
    for point in range(len(values)):
        if labels[point] != -99:
            continue
        point_neighbours = neighbours(point)
        if len(point_neighbours) < min_samples:
            labels[point] = -1; continue
        labels[point], seeds = cluster, list(point_neighbours[point_neighbours != point])
        while seeds:
            candidate = seeds.pop()
            if labels[candidate] == -1: labels[candidate] = cluster
            if labels[candidate] != -99: continue
            labels[candidate] = cluster
            more = neighbours(candidate)
            if len(more) >= min_samples:
                seeds.extend(int(item) for item in more if labels[item] in (-99, -1))
        cluster += 1
    labels[labels == -99] = -1
    return labels


def cluster_metrics(values: np.ndarray, labels: np.ndarray) -> dict:
    valid, unique = labels >= 0, np.unique(labels[labels >= 0])
    base = {"cluster_count": int(len(unique)), "noise_count": int((labels == -1).sum())}
    if valid.sum() < 2 or len(unique) < 2 or len(unique) >= valid.sum():
        return {"silhouette_score": None, "calinski_harabasz_score": None, "davies_bouldin_score": None, **base}
    x, lab = values[valid], labels[valid]
    distance = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(axis=2))
    silhouettes, centers, scatters, within = [], [], [], 0.0
    for index in range(len(x)):
        same = lab == lab[index]
        a = float(distance[index, same].sum() / max(1, same.sum() - 1))
        b = min(float(distance[index, lab == other].mean()) for other in unique if other != lab[index])
        silhouettes.append((b - a) / max(a, b, 1e-12))
    overall = x.mean(axis=0)
    for value in unique:
        points, center = x[lab == value], x[lab == value].mean(axis=0)
        centers.append(center); scatters.append(float(np.linalg.norm(points - center, axis=1).mean()))
        within += float(((points - center) ** 2).sum())
    between = sum(int((lab == value).sum()) * float(((center - overall) ** 2).sum()) for value, center in zip(unique, centers))
    ch = (between / (len(unique) - 1)) / max(1e-12, within / (len(x) - len(unique)))
    db = [max((scatters[i] + scatters[j]) / max(1e-12, float(np.linalg.norm(centers[i] - centers[j]))) for j in range(len(unique)) if j != i) for i in range(len(unique))]
    return {"silhouette_score": round(float(np.mean(silhouettes)), 4), "calinski_harabasz_score": round(float(ch), 4), "davies_bouldin_score": round(float(np.mean(db)), 4), **base}
