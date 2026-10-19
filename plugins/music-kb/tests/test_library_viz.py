from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "runners/library-viz/build_viz.py"


@pytest.fixture()
def viz(monkeypatch):
    original_import = builtins.__import__
    forbidden = {"torch", "torchaudio", "transformers", "muq", "merit", "mlx", "umap", "hdbscan"}

    def model_free_import(name, *args, **kwargs):
        if name.split(".")[0] in forbidden:
            raise AssertionError(f"pure-logic test attempted a heavy import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", model_free_import)
    spec = importlib.util.spec_from_file_location("library_viz_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_top_neighbors_orders_by_cosine_distance(viz):
    vectors = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [-1.0, 0.0]])
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    neighbors = viz.top_neighbors(vectors, 2)
    assert [index for index, _ in neighbors[0]] == [1, 2]
    assert neighbors[0][0][1] < neighbors[0][1][1]
    assert all(index != 0 for index, _ in neighbors[0])


def test_clip_start_keeps_window_inside_track(viz):
    assert viz.clip_start(10) == 0.0
    assert viz.clip_start(100) == 25.0
    assert viz.clip_start(20) == 0.0
    assert viz.clip_start(21) == 1.0


def test_normalize_2d_centers_and_scales(viz):
    points = np.array([[10.0, 20.0], [30.0, 40.0], [20.0, 30.0]])
    normalized = viz.normalize_2d(points)
    assert np.allclose(normalized.mean(axis=0), 0.0)
    assert np.isclose(np.abs(normalized).max(), 1.0)


def test_cluster_payload_orders_by_size_and_colors(viz):
    labels = np.array([5, 5, 5, 2, 2, -1])
    clusters = viz.cluster_payload(labels)
    assert clusters[0]["id"] == 5 and clusters[0]["size"] == 3
    assert clusters[1]["id"] == 2 and clusters[1]["size"] == 2
    assert clusters[2]["id"] == -1 and clusters[2]["name"] == "散点"
    assert len({cluster["color"] for cluster in clusters}) == 3
