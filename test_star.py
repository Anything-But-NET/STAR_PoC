from collections.abc import Generator

import numpy as np
import pytest

from db import VECTOR_SIZE, DbService
from main import DEFAULT_MD_PATH, run_pipeline, search_query
from spliter import parse_markdown_to_tree
from transformer import merge_vectors


def _unit_vec(direction: list[float]) -> np.ndarray:
    """Create an L2-normalized vector of dimension VECTOR_SIZE (1024)."""
    vec = np.zeros(VECTOR_SIZE, dtype=float)
    vec[: len(direction)] = direction
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


@pytest.fixture
def pg_db() -> Generator[DbService, None, None]:
    """Provide a clean PostgreSQL + pgvector DbService instance for unit tests."""
    service = DbService()
    service.reset_db()
    yield service
    service.engine.dispose()


@pytest.fixture(scope="module")
def indexed_test_md() -> None:
    """Index the full TEST.md into PostgreSQL once for end-to-end showcase tests."""
    run_pipeline(DEFAULT_MD_PATH)


# ---------------------------------------------------------------------------
# 1. Markdown AST Splitter (spliter.py)
# ---------------------------------------------------------------------------


def test_markdown_hierarchy_and_preamble() -> None:
    md = (
        "Intro text before any headers.\n\n"
        "# Main Title\n"
        "Main content.\n"
        "## Section A\n"
        "Content A.\n"
        "### SubSection A1\n"
        "Content A1.\n"
        "## Section B\n"
        "Content B.\n"
    )
    tree = parse_markdown_to_tree(md)

    assert tree.level == 0
    assert len(tree.childrens) == 2

    preamble, h1 = tree.childrens
    assert preamble.name == "Preamble"
    assert preamble.content == "Intro text before any headers."

    assert h1.name == "Main Title"
    assert h1.level == 1
    assert [c.name for c in h1.childrens] == ["Section A", "Section B"]
    assert h1.childrens[0].childrens[0].name == "SubSection A1"


def test_code_block_headers_are_shielded() -> None:
    md = (
        "## Running it\n"
        "```bash\n"
        "# This comment inside a bash block must NOT become an H1 header\n"
        "docker compose up -d\n"
        "```\n"
    )
    tree = parse_markdown_to_tree(md)

    assert len(tree.childrens) == 1
    section = tree.childrens[0]
    assert section.name == "Running it"
    assert len(section.childrens) == 0
    assert "# This comment inside a bash block" in section.content


# ---------------------------------------------------------------------------
# 2. Self-Attention Vector Centrality Pooling (transformer.py)
# ---------------------------------------------------------------------------


def test_self_attention_merge_vectors_favors_consensus() -> None:
    # Three semantically aligned vectors (along X) and one noisy outlier (along Y)
    v1 = _unit_vec([1.0, 0.05, 0.0])
    v2 = _unit_vec([1.0, -0.05, 0.0])
    v3 = _unit_vec([1.0, 0.0, 0.0])
    outlier = _unit_vec([0.0, 1.0, 0.0])

    merged = merge_vectors([v1, v2, v3, outlier], temperature=0.1)

    assert np.isclose(np.linalg.norm(merged), 1.0)
    simple_mean = _unit_vec(np.mean([v1, v2, v3, outlier], axis=0).tolist())
    assert float(np.dot(merged, v3)) > float(np.dot(simple_mean, v3))


# ---------------------------------------------------------------------------
# 3. Dual-Trigger Auto-Merge in PostgreSQL + pgvector (db.py)
# ---------------------------------------------------------------------------


def test_quorum_trigger_merges_into_parent(pg_db: DbService) -> None:
    """When >50% of a section's children match and parent is not diluted, return the parent section."""
    q_vec = _unit_vec([1.0, 0.0])
    parent_vec = _unit_vec([0.99, 0.14])  # cosine similarity ~ 0.99

    c1 = pg_db.add_chunk("Step 1: Install dependencies", "doc.md")
    c2 = pg_db.add_chunk("Step 2: Start container", "doc.md")
    c3 = pg_db.add_chunk("Step 3: Check port 8080", "doc.md")

    parent_id = pg_db.add_point(parent_vec, [c1, c2, c3], children_count=3)
    pg_db.add_point(q_vec, [c1], parent_id=parent_id, children_count=0)
    pg_db.add_point(_unit_vec([0.995, 0.10]), [c2], parent_id=parent_id, children_count=0)

    results = pg_db.query_with_auto_merge(q_vec, limit=2, threshold=0.5)

    assert len(results) == 1
    assert results[0]["point_id"] == str(parent_id)
    assert len(results[0]["chunks"]) == 3


def test_anti_dilution_guard_keeps_sharp_leaf(pg_db: DbService) -> None:
    """If a leaf chunk is much sharper than its diluted parent (diff > 0.06), block merging."""
    q_vec = _unit_vec([1.0, 0.0])
    diluted_parent_vec = _unit_vec([0.75, 0.66])  # cosine similarity ~ 0.75 (diff > 0.06)

    c1 = pg_db.add_chunk("Server listens on http://localhost:5066", "doc.md")
    c2 = pg_db.add_chunk("Extra Docker and fixture details", "doc.md")

    parent_id = pg_db.add_point(diluted_parent_vec, [c1, c2], children_count=2)
    p1 = pg_db.add_point(q_vec, [c1], parent_id=parent_id, children_count=0)
    pg_db.add_point(diluted_parent_vec, [c2], parent_id=parent_id, children_count=0)

    results = pg_db.query_with_auto_merge(q_vec, limit=2, threshold=0.5)

    returned_ids = [r["point_id"] for r in results]
    assert str(parent_id) not in returned_ids
    assert returned_ids[0] == str(p1)


def test_semantic_dominance_merges_without_quorum(pg_db: DbService) -> None:
    """If only 1 of 4 children matches (no quorum), but parent_score > best_child_score, merge upward."""
    q_vec = _unit_vec([1.0, 0.0])
    parent_vec = _unit_vec([0.98, 0.20])  # cosine similarity ~ 0.98

    chunks = [pg_db.add_chunk(f"Table row #{i}", "doc.md") for i in range(4)]
    parent_id = pg_db.add_point(parent_vec, chunks, children_count=4)
    pg_db.add_point(_unit_vec([0.94, 0.34]), [chunks[0]], parent_id=parent_id, children_count=0)

    results = pg_db.query_with_auto_merge(q_vec, limit=1, threshold=0.5)

    assert len(results) == 1
    assert results[0]["point_id"] == str(parent_id)
    assert len(results[0]["chunks"]) == 4


# ---------------------------------------------------------------------------
# 4. End-to-End Showcase on Real TEST.md (main.py)
# ---------------------------------------------------------------------------


def test_e2e_pinpoint_query_keeps_sharp_leaf_chunk(indexed_test_md: None) -> None:
    """Pinpoint query ('What port does dotnet run listen on?') returns the exact 1-chunk leaf."""
    results = search_query("What port does dotnet run listen on?", limit=20, threshold=0.5)
    assert len(results) > 0

    top = results[0]
    assert top["children_count"] == 0
    assert len(top["chunk_ids"]) == 1
    assert "http://localhost:5066" in top["text"]


def test_e2e_broad_query_auto_merges_full_section(indexed_test_md: None) -> None:
    """Broad query ('What it covers') merges all 18 table chunks into the parent section node."""
    results = search_query("What it covers", limit=20, threshold=0.5)
    assert len(results) > 0

    top = results[0]
    assert top["children_count"] == 18
    assert len(top["chunk_ids"]) == 18
    assert "GET /users" in top["text"]
    assert "GET /drives/{driveId}/root" in top["text"]
