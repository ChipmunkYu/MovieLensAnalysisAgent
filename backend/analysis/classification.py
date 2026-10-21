from __future__ import annotations

import numpy as np

from backend.analysis.dataset import Dataset, split_metadata
from backend.analysis.features import event_features, prediction_features
from backend.analysis.math_tools import binary_metrics, standardize_apply, standardize_fit

FEATURE_SCHEMA = ["user_rating_count", "user_average_rating", "user_rating_std", "user_high_rating_ratio", "movie_rating_count", "movie_average_rating", "movie_high_rating_ratio"]


class LogisticModel:
    def fit(self, x: np.ndarray, y: np.ndarray) -> "LogisticModel":
        scaled, self.mean, self.scale = standardize_fit(x)
        self.weights, self.bias = np.zeros(x.shape[1]), 0.0
        for _ in range(500):
            probability = 1 / (1 + np.exp(-np.clip(scaled @ self.weights + self.bias, -30, 30)))
            self.weights -= .08 * ((scaled.T @ (probability - y)) / len(y) + .001 * self.weights)
            self.bias -= .08 * float((probability - y).mean())
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        z = standardize_apply(x, self.mean, self.scale) @ self.weights + self.bias
        return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


class ForestModel:
    def fit(self, x: np.ndarray, y: np.ndarray, trees: int = 31) -> "ForestModel":
        rng, self.trees = np.random.default_rng(42), []
        for _ in range(trees):
            sample = rng.choice(len(x), len(x), replace=True); bx, by = x[sample], y[sample]
            best = None
            for feature in rng.choice(x.shape[1], min(4, x.shape[1]), replace=False):
                for threshold in np.unique(np.quantile(bx[:, feature], [.2, .4, .6, .8])):
                    left = bx[:, feature] <= threshold
                    if not left.any() or left.all(): continue
                    score = left.mean() * by[left].var() + (~left).mean() * by[~left].var()
                    if best is None or score < best[0]:
                        best = (score, int(feature), float(threshold), float(by[left].mean()), float(by[~left].mean()))
            self.trees.append(best or (0.0, 0, float("inf"), float(by.mean()), float(by.mean())))
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        outputs = [np.where(x[:, feature] <= threshold, left, right) for _, feature, threshold, left, right in self.trees]
        return np.asarray(outputs).mean(axis=0)


def run(dataset: Dataset, user_id: int | None, movie_id: int | None, algorithm: str | None) -> dict:
    if user_id is None or movie_id is None:
        raise ValueError("分类任务必须同时提供 user_id 和 movie_id")
    x, y, identities = event_features(dataset.ratings)
    if len(x) < 4:
        raise ValueError("可用于无泄漏分类建模的历史评分不足 4 条")
    train_mask, validation_mask, test_mask = identities[:, 2] < dataset.t1, (identities[:, 2] >= dataset.t1) & (identities[:, 2] < dataset.t2), identities[:, 2] >= dataset.t2
    if not train_mask.any() or not validation_mask.any() or not test_mask.any():
        raise ValueError("分类特征经过历史约束后出现空切分，请提供更多评分数据")
    requested = (algorithm or "all").lower().replace(" ", "_")
    aliases = {"logistic": "logistic_regression", "lr": "logistic_regression", "randomforest": "random_forest", "forest": "random_forest"}
    requested = aliases.get(requested, requested)
    names = [requested] if requested in ("logistic_regression", "random_forest") else ["logistic_regression", "random_forest"]
    prediction_x = prediction_features(dataset.train, user_id, movie_id)
    results = []
    for name in names:
        model = LogisticModel().fit(x[train_mask], y[train_mask]) if name == "logistic_regression" else ForestModel().fit(x[train_mask], y[train_mask])
        probability = float(model.predict(prediction_x)[0])
        model_state = ({"coefficients": [round(float(value), 8) for value in model.weights], "intercept": round(float(model.bias), 8),
                        "scaler_mean": [round(float(value), 8) for value in model.mean], "scaler_scale": [round(float(value), 8) for value in model.scale]}
                       if name == "logistic_regression" else {"trees": [[round(float(value), 8) for value in tree] for tree in model.trees]})
        results.append({"algorithm": name, "prediction": int(probability >= .5), "prediction_label": "高分" if probability >= .5 else "非高分",
                        "positive_probability": round(probability, 4), "parameters": {"threshold": .5, **({"iterations": 500, "learning_rate": .08} if name == "logistic_regression" else {"n_estimators": 31, "random_state": 42})},
                        "model_state": model_state,
                        "validation_metrics": binary_metrics(y[validation_mask], model.predict(x[validation_mask])),
                        "test_metrics": binary_metrics(y[test_mask], model.predict(x[test_mask]))})
    return {"task_type": "classification", "user_id": user_id, "movie_id": movie_id, "model_version": "classification-v2", "label_definition": "rating >= 4", "feature_schema": FEATURE_SCHEMA,
            "results": results, "data_split": split_metadata(dataset), "limitations": (["movies.dat 不存在，当前模型未使用电影类型特征。"] if not dataset.genres else []) + ["冷启动用户或电影无法预测。", "概率用于排序和解释，不代表因果关系。"]}
