"""Check update_size against the ranking in version-jump-order.txt.

Run with: uv run --with pytest pytest
"""

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).parent

_spec = importlib.util.spec_from_file_location("bog", HERE / "brew-outdated-greedy.py")
bog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bog)


def load_groups():
    """Return [(header, [(running, latest), ...]), ...], biggest jump first."""
    blocks = (HERE / "version-jump-order.txt").read_text().strip().split("\n\n")
    groups = []
    for block in blocks[1:]:  # first block is the explanation
        header, *lines = block.splitlines()
        groups.append((header, [tuple(line.split(" -> ")) for line in lines]))
    return groups


GROUPS = load_groups()


@pytest.mark.parametrize("header,pairs", GROUPS, ids=[h for h, _ in GROUPS])
def test_group_scores_equal(header, pairs):
    scores = {f"{r} -> {l}": bog.update_size(r, l) for r, l in pairs}
    assert len(set(scores.values())) == 1, scores


@pytest.mark.parametrize(
    "bigger,smaller",
    list(zip(GROUPS, GROUPS[1:])),
    ids=[f"{a[0]} > {b[0]}" for a, b in zip(GROUPS, GROUPS[1:])],
)
def test_groups_strictly_ordered(bigger, smaller):
    assert bog.update_size(*bigger[1][0]) > bog.update_size(*smaller[1][0])
