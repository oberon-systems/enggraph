"""What the scheduler does with a tree no host agent is bound to."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from enggraph.api import scheduler
from enggraph.core import trees

BOUND = trees.Source("beta", "agent-1", "beta", "/srv/beta", True)


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold alpha, which nothing reads, and beta, which an agent serves."""
    absent = trees.Unserved("alpha")
    remote = trees.RemoteTree(BOUND)
    monkeypatch.setattr(scheduler, "get_db_connection", MagicMock)
    monkeypatch.setattr(
        scheduler,
        "list_readable_projects",
        lambda cursor: [("alpha", "/srv/alpha"), ("beta", "/srv/beta")],
    )
    monkeypatch.setattr(
        trees, "source", lambda project: BOUND if project == "beta" else None
    )
    monkeypatch.setattr(
        trees, "of", lambda project: remote if project == "beta" else absent
    )


@pytest.mark.usefixtures("stack")
def test_the_start_names_only_the_trees_no_agent_is_bound_to(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An agent that has not connected yet is no reason to warn."""
    with caplog.at_level(logging.WARNING, logger=scheduler.LOG.name):
        scheduler.Scheduler().report_unbound()

    assert "alpha (/srv/alpha) has no host agent bound" in caplog.text
    assert "beta" not in caplog.text


@pytest.mark.usefixtures("stack")
def test_a_tree_nothing_reads_is_owed_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is skipped before its schedule is even asked, so nothing is pruned."""
    monkeypatch.setattr(trees.RemoteTree, "available", lambda self: False)

    def asked(*_: object) -> None:
        raise AssertionError("the schedule of a tree nothing reads was asked")

    monkeypatch.setattr(scheduler.features, "resolve", asked)

    targets, owed = scheduler.Scheduler()._plan(MagicMock(), MagicMock())

    assert targets == {}
    assert owed == []
