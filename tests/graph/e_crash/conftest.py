"""Fixtures for the crash tests: every process a cluster starts is killed at teardown."""

from __future__ import annotations

import pytest

from ._cluster import Cluster


@pytest.fixture
def make_cluster(tmp_path):
    clusters: list[Cluster] = []

    def make(**kwargs) -> Cluster:
        cluster = Cluster(tmp_path, **kwargs)
        clusters.append(cluster)
        return cluster

    yield make
    for cluster in clusters:
        cluster.close()
