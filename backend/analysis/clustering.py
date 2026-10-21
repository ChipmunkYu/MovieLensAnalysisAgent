from __future__ import annotations

import numpy as np

from backend.analysis.dataset import Dataset, split_metadata
from backend.analysis.features import build_profiles
from backend.analysis.math_tools import cluster_metrics, dbscan, kmeans, standardize_fit


def _summaries(labels: np.ndarray, records: list[dict]) -> list[dict]:
    output = []
    for cluster in sorted(set(int(item) for item in labels)):
        members = [record for record, label in zip(records, labels) if label == cluster]
        genres: dict[str, int] = {}
        for item in members:
            for genre in item["top_genres"]: genres[genre] = genres.get(genre, 0) + 1
        output.append({"cluster_id": cluster, "is_noise": cluster == -1, "size": len(members),
                       "average_rating": round(float(np.mean([item["average_rating"] for item in members])), 4),
                       "average_rating_count": round(float(np.mean([item["rating_count"] for item in members])), 2),
                       "top_genres": sorted(genres, key=genres.get, reverse=True)[:3],
                       "description": "噪声/异常用户" if cluster == -1 else ("高活跃" if np.mean([item["rating_count"] for item in members]) >= np.median([r["rating_count"] for r in records]) else "低活跃") + "、" + ("高评分" if np.mean([item["average_rating"] for item in members]) >= 3.5 else "谨慎评分") + "用户群体"})
    return output


def run(dataset: Dataset, user_id: int | None, algorithm: str | None, n_clusters: int) -> dict:
    users, names, values, records = build_profiles(dataset)
    scaled, _, _ = standardize_fit(values)
    requested = (algorithm or "all").lower().replace("-", "").replace("_", "")
    algorithms = [requested] if requested in ("kmeans", "dbscan") else ["kmeans", "dbscan"]
    results = []
    for name in algorithms:
        if name == "kmeans":
            labels, _ = kmeans(scaled, n_clusters); parameters = {"n_clusters": min(n_clusters, len(users)), "random_state": 42}
        else:
            labels, parameters = dbscan(scaled, eps=1.25, min_samples=min(3, max(2, len(users)))), {"eps": 1.25, "min_samples": min(3, max(2, len(users)))}
        summaries = _summaries(labels, records)
        selected = None
        if user_id is not None:
            if user_id not in users: raise ValueError(f"用户 {user_id} 在训练期不存在，无法判断用户群体")
            record = records[users.index(user_id)]
            if record["rating_count"] < 2: raise ValueError(f"用户 {user_id} 的历史评分数量不足，无法可靠判断其用户群体")
            cluster_id = int(labels[users.index(user_id)])
            selected = {"user_id": user_id, "cluster_id": cluster_id, "is_noise": cluster_id == -1, "profile": record, "cluster_summary": next(item for item in summaries if item["cluster_id"] == cluster_id)}
        results.append({"algorithm": name, "metrics": cluster_metrics(scaled, labels), "parameters": parameters, "selected_user": selected,
                        "assignments": [{"user_id": user, "cluster_id": int(label), "is_noise": int(label) == -1} for user, label in zip(users, labels)],
                        "cluster_summaries": summaries})
    return {"task_type": "clustering", "model_version": "clustering-v2", "profile_version": "user-profile-v2", "feature_schema": names, "profile_count": len(users), "results": results, "data_split": split_metadata(dataset), "limitations": ["聚类描述是基于群体统计的解释，不是用户身份标签。"] + (["movies.dat 不存在，群体摘要不含类型偏好。"] if not dataset.genres else [])}
