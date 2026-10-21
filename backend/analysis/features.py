from __future__ import annotations

from collections import defaultdict
import numpy as np

from backend.analysis.dataset import Dataset

BASE_FEATURES = ["rating_count", "average_rating", "rating_std", "high_rating_ratio", "active_days"]


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


def event_features(ratings: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # 增量维护每个用户/电影的统计量，避免对每条评分都重算整个历史列表的 mean/std（原实现是 O(n^2)）。
    # 每个用户/电影存 [count, sum, sum_of_squares, high_count]，均值和标准差按公式在线计算。
    user_stats: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    movie_stats: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    features, labels, identities = [], [], []
    for user_raw, movie_raw, rating, timestamp in ratings:
        user, movie = int(user_raw), int(movie_raw)
        us, ms = user_stats[user], movie_stats[movie]
        if us[0] and ms[0]:
            u_mean = us[1] / us[0]
            u_std = max(0.0, us[2] / us[0] - u_mean * u_mean) ** 0.5
            features.append([us[0], u_mean, u_std, us[3] / us[0], ms[0], ms[1] / ms[0], ms[3] / ms[0]])
            labels.append(int(rating >= 4)); identities.append([user, movie, int(timestamp)])
        value = float(rating)
        us[0] += 1; us[1] += value; us[2] += value * value; us[3] += int(rating >= 4)
        ms[0] += 1; ms[1] += value; ms[2] += value * value; ms[3] += int(rating >= 4)
    return np.asarray(features, dtype=float), np.asarray(labels, dtype=int), np.asarray(identities, dtype=int)


def prediction_features(train: np.ndarray, user_id: int, movie_id: int) -> np.ndarray:
    uh, mh = train[train[:, 0] == user_id, 2], train[train[:, 1] == movie_id, 2]
    if not len(uh): raise ValueError(f"用户 {user_id} 在训练期不存在，无法进行预测")
    if not len(mh): raise ValueError(f"电影 {movie_id} 在训练期不存在，无法进行预测")
    return np.asarray([[len(uh), uh.mean(), uh.std(), (uh >= 4).mean(), len(mh), mh.mean(), (mh >= 4).mean()]])
