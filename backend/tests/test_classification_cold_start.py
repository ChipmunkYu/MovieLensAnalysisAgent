from pathlib import Path

import numpy as np
import pytest

from backend.analysis import classification
from backend.analysis.dataset import Dataset
from backend.analysis.features import prediction_features


@pytest.fixture
def dataset():
    rows = [[user, user * 10, 4, index + 1] for index, user in enumerate(range(7, 0, -1))]
    rows += [[1, 10, 2 + index % 4, 8 + index] for index in range(6)]
    rows += [[99, 990, 5, 20], [1, 10, 2, 21], [98, 980, 3, 30], [1, 10, 5, 31]]
    return Dataset(Path("unused.dat"), np.asarray(rows, dtype=float), {999: ["Drama"]}, ["Drama"],
                   {999: 25}, [25], 20, 30, "test")


@pytest.fixture
def forbid_training(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid target reached event features or model training")
    monkeypatch.setattr(classification, "event_features", unexpected)
    monkeypatch.setattr(classification.LogisticModel, "fit", unexpected)
    monkeypatch.setattr(classification.DecisionTreeModel, "fit", unexpected)


@pytest.mark.parametrize("user,movie", [(99, 10), (1, 990), (99, 990), (98, 980), (999, 999)])
def test_cold_start_rejected_before_features_and_training(dataset, forbid_training, user, movie):
    with pytest.raises(ValueError) as error:
        classification.run(dataset, user, movie, None)
    message = str(error.value)
    assert "训练期（TRAIN）不存在" in message and "不支持冷启动" in message and "尚未开始模型训练" in message
    if user != 1:
        assert f"用户 {user}" in message
        assert "训练期用户 ID 示例：[1, 2, 3, 4, 5]" in message
    else:
        assert "可保留用户 1" in message and "训练期用户 ID 示例" not in message
    if movie != 10:
        assert f"电影 {movie}" in message
        assert "训练期电影 ID 示例：[10, 20, 30, 40, 50]" in message
    else:
        assert "可保留电影 10" in message and "训练期电影 ID 示例" not in message
    with pytest.raises(ValueError) as direct_error:
        prediction_features(dataset, user, movie)
    assert str(direct_error.value) == message


def test_empty_train_has_no_usable_examples(dataset, forbid_training):
    dataset.ratings = dataset.ratings[dataset.ratings[:, 3] >= dataset.t1]
    with pytest.raises(ValueError) as error:
        classification.run(dataset, 99, 990, None)
    assert "训练期无可用用户示例" in str(error.value)
    assert "训练期无可用电影示例" in str(error.value)
    assert "尚未开始模型训练" in str(error.value)
    assert "ID 示例：[" not in str(error.value)


@pytest.mark.parametrize("user,movie", [(None, 10), (1, None), (None, None)])
def test_missing_arguments_remain_clear(dataset, forbid_training, user, movie):
    with pytest.raises(ValueError, match="分类任务必须同时提供 user_id 和 movie_id"):
        classification.run(dataset, user, movie, None)


def test_valid_prediction_validated_before_event_features(dataset, monkeypatch):
    calls = []
    original_prediction = classification.prediction_features
    original_events = classification.event_features

    def prediction(*args):
        calls.append("prediction")
        return original_prediction(*args)

    def events(*args):
        assert calls == ["prediction"]
        calls.append("events")
        return original_events(*args)

    monkeypatch.setattr(classification, "prediction_features", prediction)
    monkeypatch.setattr(classification, "event_features", events)
    result = classification.run(dataset, 1, 10, None)
    assert calls == ["prediction", "events"]
    assert result["data_split"]["strategy"] == "chronological"
    assert dataset.t1 == 20 and dataset.t2 == 30
    assert {item["algorithm"] for item in result["results"]} == {"logistic_regression", "decision_tree"}
    assert all(0 <= item["positive_probability"] <= 1 for item in result["results"])
