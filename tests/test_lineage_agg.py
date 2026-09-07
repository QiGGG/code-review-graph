"""Tests for ``lineage-agg`` (scope aggregation of lineage coverage).

Covers the two correctness rules the feature exists for:

* **Rule 1 (dedup)** — a callee shared by several roots is counted once.
* **Rule 2 (identity)** — same-named functions in different files stay
  distinct.

plus file/directory scope resolution, ``--exclude-tests`` and the counting
metric (function row-span sum, external references not counted).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.lineage import (
    _EndpointResolver,
    _expand_scope_union,
    _resolve_scope,
    _roots_for_files,
    _scale_over_nodes,
    scope_lineage,
)
from code_review_graph.parser import EdgeInfo, NodeInfo


def _store_for(tmp_path: Path) -> GraphStore:
    return GraphStore(tmp_path / "test.db")


def _func_node(
    file_path: str,
    name: str,
    line_start: int,
    line_end: int,
    *,
    parent: str | None = None,
    is_test: bool = False,
) -> NodeInfo:
    return NodeInfo(
        kind="Test" if is_test else "Function",
        name=name,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        language="python",
        parent_name=parent,
        is_test=is_test,
    )


def _calls(
    store: GraphStore, source: str, target: str, file_path: str,
) -> None:
    store.upsert_edge(
        EdgeInfo(
            kind="CALLS",
            source=source,
            target=target,
            file_path=file_path,
            line=1,
        )
    )


def _qn(file_path: str, name: str, parent: str | None = None) -> str:
    if parent:
        return f"{file_path}::{parent}.{name}"
    return f"{file_path}::{name}"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Rule 1: shared callee dedup
# ---------------------------------------------------------------------------


class TestSharedCalleeDedup:
    def test_shared_callee_counted_once_across_roots(self, repo):
        a = str((repo / "a.py").resolve()).replace("\\", "/")
        b = str((repo / "b.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            # a.py defines two roots that both call the same helper in b.py.
            store.upsert_node(_func_node(a, "f1", 10, 20))
            store.upsert_node(_func_node(a, "f2", 30, 40))
            store.upsert_node(_func_node(b, "helper", 50, 80))
            store.commit()

            qn_f1 = _qn(a, "f1")
            qn_f2 = _qn(a, "f2")
            qn_helper = _qn(b, "helper")
            _calls(store, qn_f1, qn_helper, a)
            _calls(store, qn_f2, qn_helper, a)
            store.commit()

            # Scope is only a.py: f1 and f2 are the roots; helper in b.py is
            # reached from both but is not itself a root.
            roots, err = _roots_for_files(store, [a], exclude_tests=False)
            assert err == {}
            assert {r.qualified_name for r in roots} == {qn_f1, qn_f2}

            resolver = _EndpointResolver(store)
            union = _expand_scope_union(
                store, [r.qualified_name for r in roots], resolver,
            )
            # helper present exactly once despite two inbound CALLS
            assert qn_helper in union
            scale = _scale_over_nodes(union, exclude_tests=False)
            fn = scale["function_level"]
            # f1(11) + f2(11) + helper(31) — helper counted once
            assert fn["function_count"] == 3
            assert fn["total_loc"] == 11 + 11 + 31
        finally:
            store.close()

    def test_root_reached_as_callee_not_double_counted(self, repo):
        a = str((repo / "a.py").resolve()).replace("\\", "/")
        b = str((repo / "b.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(a, "a_func", 10, 20))
            store.upsert_node(_func_node(b, "b_func", 30, 40))
            store.commit()
            qn_a = _qn(a, "a_func")
            qn_b = _qn(b, "b_func")
            _calls(store, qn_a, qn_b, a)
            store.commit()

            roots, _ = _roots_for_files(store, [a, b], exclude_tests=False)
            resolver = _EndpointResolver(store)
            union = _expand_scope_union(
                store, [r.qualified_name for r in roots], resolver,
            )
            scale = _scale_over_nodes(union, exclude_tests=False)
            assert scale["function_level"]["function_count"] == 2
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Rule 2: cross-file same-name functions keep distinct identities
# ---------------------------------------------------------------------------


class TestCrossFileSameName:
    def test_same_name_in_different_files_stays_distinct(self, repo):
        x = str((repo / "x.py").resolve()).replace("\\", "/")
        y = str((repo / "y.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(x, "run", 10, 20))
            store.upsert_node(_func_node(y, "run", 30, 40))
            store.commit()

            roots, _ = _roots_for_files(store, [x, y], exclude_tests=False)
            qns = [r.qualified_name for r in roots]
            assert _qn(x, "run") in qns
            assert _qn(y, "run") in qns
            assert len(roots) == 2  # never merged into one

            resolver = _EndpointResolver(store)
            union = _expand_scope_union(
                store, [r.qualified_name for r in roots], resolver,
            )
            assert _qn(x, "run") in union
            assert _qn(y, "run") in union
            scale = _scale_over_nodes(union, exclude_tests=False)
            # both same-named functions counted independently
            assert scale["function_level"]["function_count"] == 2
        finally:
            store.close()

    def test_same_name_functions_with_distinct_callees(self, repo):
        x = str((repo / "x.py").resolve()).replace("\\", "/")
        y = str((repo / "y.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(x, "run", 10, 20))
            store.upsert_node(_func_node(y, "run", 30, 40))
            store.upsert_node(_func_node(x, "x_helper", 50, 60))
            store.upsert_node(_func_node(y, "y_helper", 70, 80))
            store.commit()
            qn_x_run = _qn(x, "run")
            qn_y_run = _qn(y, "run")
            _calls(store, qn_x_run, _qn(x, "x_helper"), x)
            _calls(store, qn_y_run, _qn(y, "y_helper"), y)
            store.commit()

            roots, _ = _roots_for_files(store, [x, y], exclude_tests=False)
            resolver = _EndpointResolver(store)
            union = _expand_scope_union(
                store, [r.qualified_name for r in roots], resolver,
            )
            scale = _scale_over_nodes(union, exclude_tests=False)
            # run(x) + x_helper + run(y) + y_helper = 4, no mixing
            assert scale["function_level"]["function_count"] == 4
        finally:
            store.close()


# ---------------------------------------------------------------------------
# exclude-tests
# ---------------------------------------------------------------------------


class TestExcludeTests:
    def test_exclude_tests_drops_test_roots(self, repo):
        a = str((repo / "a.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(a, "prod", 10, 20))
            store.upsert_node(_func_node(a, "test_prod", 30, 40, is_test=True))
            store.commit()

            roots_incl, _ = _roots_for_files(store, [a], exclude_tests=False)
            roots_excl, _ = _roots_for_files(store, [a], exclude_tests=True)
            assert len(roots_incl) == 2
            assert len(roots_excl) == 1
            assert roots_excl[0].qualified_name == _qn(a, "prod")
        finally:
            store.close()

    def test_scale_respects_exclude_tests(self, repo):
        a = str((repo / "a.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(a, "prod", 10, 20))
            store.upsert_node(_func_node(a, "test_prod", 30, 40, is_test=True))
            store.commit()
            roots_incl, _ = _roots_for_files(store, [a], exclude_tests=False)
            roots_excl, _ = _roots_for_files(store, [a], exclude_tests=True)
            resolver = _EndpointResolver(store)
            union_incl = _expand_scope_union(
                store, [r.qualified_name for r in roots_incl], resolver,
            )
            union_excl = _expand_scope_union(
                store, [r.qualified_name for r in roots_excl], resolver,
            )
            # union_excl must not contain the Test node; scale must reflect that
            assert _qn(a, "test_prod") not in union_excl
            assert _scale_over_nodes(union_excl, True)["function_level"]["total_loc"] == 11
            incl_scale = _scale_over_nodes(union_incl, True)
            # Even when traversed (include-roots), passing exclude_tests=True
            # drops the Test node from the count.
            assert incl_scale["function_level"]["total_loc"] == 11
        finally:
            store.close()


# ---------------------------------------------------------------------------
# External / unresolved references
# ---------------------------------------------------------------------------


class TestExternalReferences:
    def test_external_callee_is_not_loc(self, repo):
        a = str((repo / "a.py").resolve()).replace("\\", "/")
        store = _store_for(repo)
        try:
            store.upsert_node(_func_node(a, "f", 10, 20))
            store.commit()
            _calls(store, _qn(a, "f"), "os.path.join", a)
            store.commit()

            roots, _ = _roots_for_files(store, [a], exclude_tests=False)
            resolver = _EndpointResolver(store)
            union = _expand_scope_union(
                store, [r.qualified_name for r in roots], resolver,
            )
            scale = _scale_over_nodes(union, exclude_tests=False)
            fn = scale["function_level"]
            assert fn["function_count"] == 1
            assert fn["total_loc"] == 11
            assert fn["external_references"] >= 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Scope resolution: file vs directory, recursive, ambiguous
# ---------------------------------------------------------------------------


class TestScopeResolution:
    def test_directory_scope_is_recursive(self, repo):
        pkg = repo / "pkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        a = pkg / "a.py"
        b = sub / "b.py"
        a.write_text("def fa():\n    pass\n")
        b.write_text("def fb():\n    pass\n")
        store = _store_for(repo)
        try:
            fa = str(a.resolve()).replace("\\", "/")
            fb = str(b.resolve()).replace("\\", "/")
            store.upsert_node(_func_node(fa, "fa", 1, 2))
            store.upsert_node(_func_node(fb, "fb", 1, 2))
            store.commit()

            result = _resolve_scope(store, repo, str(pkg))
            assert result["status"] == "ok"
            assert result["scope_type"] == "dir"
            assert set(result["files"]) == {fa, fb}
        finally:
            store.close()

    def test_file_scope_is_single_file(self, repo):
        a = repo / "a.py"
        a.write_text("def fa():\n    pass\n")
        store = _store_for(repo)
        try:
            fa = str(a.resolve()).replace("\\", "/")
            store.upsert_node(_func_node(fa, "fa", 1, 2))
            store.commit()

            result = _resolve_scope(store, repo, str(a))
            assert result["status"] == "ok"
            assert result["scope_type"] == "file"
            assert result["files"] == [fa]
        finally:
            store.close()

    def test_bare_filename_unique_match(self, repo):
        util = repo / "util.py"
        util.write_text("def u():\n    pass\n")
        store = _store_for(repo)
        try:
            fu = str(util.resolve()).replace("\\", "/")
            store.upsert_node(_func_node(fu, "u", 1, 2))
            store.commit()

            result = _resolve_scope(store, repo, "util.py")
            assert result["status"] == "ok"
            assert result["files"] == [fu]
        finally:
            store.close()

    def test_missing_scope_errors(self, repo):
        store = _store_for(repo)
        try:
            result = _resolve_scope(store, repo, "nope.py")
            assert result["status"] == "error"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# End-to-end: scope_lineage JSON payload
# ---------------------------------------------------------------------------


class TestScopeLineageEndToEnd:
    def test_scope_lineage_directory(self, repo, monkeypatch):
        pkg = repo / "pkg"
        pkg.mkdir()
        a = pkg / "a.py"
        a.write_text("def f1():\n    pass\n")
        fa = str(a.resolve()).replace("\\", "/")
        store = _store_for(repo)

        def fake_get_store(repo_root=None):
            return store, Path(repo_root) if repo_root else repo

        monkeypatch.setattr(
            "code_review_graph.lineage._get_store", fake_get_store,
        )
        try:
            store.upsert_node(_func_node(fa, "f1", 1, 2))
            store.upsert_node(_func_node(fa, "f2", 5, 6))
            store.commit()

            result = scope_lineage(str(pkg), repo_root=str(repo))
            assert result["status"] == "ok"
            assert result["scope_type"] == "dir"
            assert result["root_count"] == 2
            assert result["function_count"] == 2
            assert result["total_loc"] == 2 + 2
            assert result["file_count"] == 1
            assert result["files"][0]["functions"] == 2
        finally:
            store.close()

    def test_scope_lineage_no_functions_errors(self, repo, monkeypatch):
        store = _store_for(repo)

        def fake_get_store(repo_root=None):
            return store, Path(repo_root) if repo_root else repo

        monkeypatch.setattr(
            "code_review_graph.lineage._get_store", fake_get_store,
        )
        try:
            result = scope_lineage(str(repo), repo_root=str(repo))
            assert result["status"] == "error"
        finally:
            store.close()
