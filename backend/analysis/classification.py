from __future__ import annotations

import numpy as np

from backend.analysis.dataset import Dataset, split_metadata
from backend.analysis.features import SMOOTHING_LAMBDA, event_features, feature_schema, prediction_features
from backend.analysis.math_tools import binary_metrics, standardize_apply, standardize_fit


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


class DecisionTreeModel:
    # CART 单棵分类树：用 Gini 不纯度贪心分裂，max_depth / min_samples_leaf 约束复杂度，
    # 叶节点输出正类经验频率作为概率。对应作业四“受约束浅树 + 叶节点经验概率”。
    def __init__(self, max_depth: int = 6, min_samples_leaf: int = 5):
        self.max_depth, self.min_samples_leaf = max_depth, min_samples_leaf
        self.root: dict = {}

    @staticmethod
    def _gini(y: np.ndarray) -> float:
        p = float(np.mean(y))
        return 2 * p * (1 - p)

    def _build(self, x: np.ndarray, y: np.ndarray, depth: int) -> dict:
        probability = float(np.mean(y))
        if depth >= self.max_depth or len(y) < 2 * self.min_samples_leaf or len(np.unique(y)) == 1:
            return {"leaf": True, "probability": probability, "size": int(len(y))}
        best = None
        for feature in range(x.shape[1]):
            for threshold in np.unique(np.quantile(x[:, feature], [0.25, 0.5, 0.75])):
                left = x[:, feature] <= threshold
                if left.sum() < self.min_samples_leaf or (~left).sum() < self.min_samples_leaf:
                    continue
                score = (left.sum() * self._gini(y[left]) + (~left).sum() * self._gini(y[~left])) / len(y)
                if best is None or score < best[0]:
                    best = (score, int(feature), float(threshold))
        if best is None:
            return {"leaf": True, "probability": probability, "size": int(len(y))}
        _, feature, threshold = best
        left = x[:, feature] <= threshold
        return {"leaf": False, "feature": feature, "threshold": threshold,
                "left": self._build(x[left], y[left], depth + 1),
                "right": self._build(x[~left], y[~left], depth + 1), "size": int(len(y))}

    def fit(self, x: np.ndarray, y: np.ndarray) -> "DecisionTreeModel":
        self.root = self._build(x, y, 0)
        return self

    def _predict_one(self, row: np.ndarray) -> float:
        node = self.root
        while not node["leaf"]:
            node = node["left"] if row[node["feature"]] <= node["threshold"] else node["right"]
        return float(node["probability"])

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray([self._predict_one(row) for row in x], dtype=float)

    def serialize(self) -> dict:
        def walk(node: dict) -> dict:
            if node["leaf"]:
                return {"leaf": True, "probability": round(float(node["probability"]), 8), "samples": node["size"]}
            return {"leaf": False, "feature": node["feature"], "threshold": round(float(node["threshold"]), 8),
                    "left": walk(node["left"]), "right": walk(node["right"])}
        return walk(self.root)


def run(dataset: Dataset, user_id: int | None, movie_id: int | None, algorithm: str | None) -> dict:
    if user_id is None or movie_id is None:
        raise ValueError("分类任务必须同时提供 user_id 和 movie_id")
    x, y, identities = event_features(dataset)
    if len(x) < 4:
        raise ValueError("可用于无泄漏分类建模的历史评分不足 4 条")
    train_mask, validation_mask, test_mask = identities[:, 2] < dataset.t1, (identities[:, 2] >= dataset.t1) & (identities[:, 2] < dataset.t2), identities[:, 2] >= dataset.t2
    if not train_mask.any() or not validation_mask.any() or not test_mask.any():
        raise ValueError("分类特征经过历史约束后出现空切分，请提供更多评分数据")
    requested = (algorithm or "all").lower().replace(" ", "_")
    aliases = {"logistic": "logistic_regression", "lr": "logistic_regression", "dt": "decision_tree", "tree": "decision_tree", "决策树": "decision_tree"}
    requested = aliases.get(requested, requested)
    names = [requested] if requested in ("logistic_regression", "decision_tree") else ["logistic_regression", "decision_tree"]
    prediction_x = prediction_features(dataset, user_id, movie_id)
    results = []
    for name in names:
        model = LogisticModel().fit(x[train_mask], y[train_mask]) if name == "logistic_regression" else DecisionTreeModel().fit(x[train_mask], y[train_mask])
        probability = float(model.predict(prediction_x)[0])
        model_state = ({"coefficients": [round(float(value), 8) for value in model.weights], "intercept": round(float(model.bias), 8),
                        "scaler_mean": [round(float(value), 8) for value in model.mean], "scaler_scale": [round(float(value), 8) for value in model.scale]}
                       if name == "logistic_regression" else model.serialize())
        results.append({"algorithm": name, "prediction": int(probability >= .5), "prediction_label": "高分" if probability >= .5 else "非高分",
                        "positive_probability": round(probability, 4), "parameters": {"threshold": .5, **({"iterations": 500, "learning_rate": .08} if name == "logistic_regression" else {"criterion": "gini", "max_depth": 6, "min_samples_leaf": 5})},
                        "model_state": model_state,
                        "validation_metrics": binary_metrics(y[validation_mask], model.predict(x[validation_mask])),
                        "test_metrics": binary_metrics(y[test_mask], model.predict(x[test_mask]))})
    return {"task_type": "classification", "user_id": user_id, "movie_id": movie_id, "model_version": "classification-v3", "label_definition": "rating >= 4",
            "feature_schema": feature_schema(dataset), "smoothing_lambda": SMOOTHING_LAMBDA,
            "results": results, "data_split": split_metadata(dataset),
            "limitations": (["movies.dat 不存在，未使用电影类型特征。"] if not dataset.genre_names else []) + (["users.dat 不存在，未使用年龄段特征。"] if not dataset.age_buckets else []) + [f"平滑均分按 λ={SMOOTHING_LAMBDA} 向全局均值收缩，λ 未在验证集上调参。", "冷启动用户或电影无法预测。", "概率用于排序和解释，不代表因果关系。"]}
