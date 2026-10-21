from __future__ import annotations

from collections import defaultdict
import numpy as np

from backend.analysis.dataset import Dataset

BASE_FEATURES = ["rating_count", "average_rating", "rating_std", "high_rating_ratio", "active_days"]

# 作业四“方案 A”的平滑强度：向全局均值收缩的伪计数。λ 最终应由训练/验证确定，这里给固定默认值。
SMOOTHING_LAMBDA = 10.0


def _global_mean(dataset: Dataset) -> float:
    train = dataset.train
    return float(train[:, 2].mean()) if len(train) else 0.0


def _one_hot(value: int | None, buckets: list[int]) -> list[float]:
    return [1.0 if value == bucket else 0.0 for bucket in buckets]


def _multi_hot(genres: list[str], names: list[str]) -> list[float]:
    return [1.0 if name in genres else 0.0 for name in names]


def feature_schema(dataset: Dataset) -> list[str]:
    schema = ["user_smoothed_rating", "user_rating_count", "movie_smoothed_rating", "movie_rating_count"]
    schema += [f"user_age_{bucket}" for bucket in dataset.age_buckets]
    schema += [f"genre_{name}" for name in dataset.genre_names]
    return schema


def build_profiles(dataset: Dataset) -> tuple[list[int], list[str], np.ndarray, list[dict]]:
    rows, feature_names, records = [], list(BASE_FEATURES), []
    feature_names += [f"genre_{name}" for name in dataset.genre_names]
    train = dataset.train
    grouped: dict[int, list[np.ndarray]] = defaultdict(list)
    for event in train:
        grouped[int(event[0])].append(event)
    users = sorted(grouped)
    for user in users:
        events = np.asarray(grouped[user], dtype=float)
        ratings, timestamps = events[:, 2], events[:, 3]
        values = [len(events), ratings.mean(), ratings.std(), (ratings >= 4).mean(), (timestamps.max() - timestamps.min()) / 86400]
        genre_scores: dict[str, list[float]] = defaultdict(list)
        for event in events:
            for genre in dataset.genres.get(int(event[1]), []): genre_scores[genre].append(float(event[2]))
        values += [float(np.mean(genre_scores[name])) if genre_scores[name] else 0.0 for name in dataset.genre_names]
        rows.append(values)
        top = sorted(genre_scores, key=lambda name: np.mean(genre_scores[name]), reverse=True)[:3]
        records.append({"user_id": user, **dict(zip(BASE_FEATURES, [round(float(v), 4) for v in values[:5]])),
                        "feature_values": {name: round(float(value), 4) for name, value in zip(feature_names, values)}, "top_genres": top})
    return users, feature_names, np.asarray(rows, dtype=float), records


def event_features(dataset: Dataset) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # 作业四“方案 A”的点时正确分类特征：用户/电影平滑历史均分（向全局均值收缩）、历史次数、
    # 用户年龄段（One-hot）与电影类型（Multi-hot）。增量维护 [count, sum] 避免 O(n^2)。
    ratings = dataset.ratings
    lam, mu_global = SMOOTHING_LAMBDA, _global_mean(dataset)
    user_stats: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    movie_stats: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    features, labels, identities = [], [], []
    for user_raw, movie_raw, rating, timestamp in ratings:
        user, movie = int(user_raw), int(movie_raw)
        us, ms = user_stats[user], movie_stats[movie]
        if us[0] and ms[0]:
            u_smooth = (us[1] + lam * mu_global) / (us[0] + lam)
            m_smooth = (ms[1] + lam * mu_global) / (ms[0] + lam)
            row = [u_smooth, us[0], m_smooth, ms[0]]
            row += _one_hot(dataset.ages.get(user), dataset.age_buckets)
            row += _multi_hot(dataset.genres.get(movie, []), dataset.genre_names)
            features.append(row)
            labels.append(int(rating >= 4)); identities.append([user, movie, int(timestamp)])
        value = float(rating)
        us[0] += 1; us[1] += value
        ms[0] += 1; ms[1] += value
    return np.asarray(features, dtype=float), np.asarray(labels, dtype=int), np.asarray(identities, dtype=int)


def prediction_features(dataset: Dataset, user_id: int, movie_id: int) -> np.ndarray:
    train = dataset.train
    uh, mh = train[train[:, 0] == user_id, 2], train[train[:, 1] == movie_id, 2]
    if not len(uh): raise ValueError(f"用户 {user_id} 在训练期不存在，无法进行预测")
    if not len(mh): raise ValueError(f"电影 {movie_id} 在训练期不存在，无法进行预测")
    lam, mu_global = SMOOTHING_LAMBDA, _global_mean(dataset)
    u_smooth = (uh.sum() + lam * mu_global) / (len(uh) + lam)
    m_smooth = (mh.sum() + lam * mu_global) / (len(mh) + lam)
    row = [u_smooth, len(uh), m_smooth, len(mh)]
    row += _one_hot(dataset.ages.get(user_id), dataset.age_buckets)
    row += _multi_hot(dataset.genres.get(movie_id, []), dataset.genre_names)
    return np.asarray([row], dtype=float)
