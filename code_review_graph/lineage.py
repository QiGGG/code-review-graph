"""Function lineage tracing with code-scale statistics.

Given a package (directory) path and a function name, locate the function in
the knowledge graph, expand its full call lineage in both directions
(upstream callers / downstream callees) along ``CALLS`` edges, and report the
code scale covered by that lineage (function-level LOC and file-level LOC).

This module is purely additive: it reads the existing graph database and
never mutates it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .graph import GraphEdge, GraphNode, GraphStore, edge_to_dict, node_to_dict
from .tools._common import _get_store

logger = logging.getLogger(__name__)

_CALLS_KIND = "CALLS"
# Node kinds that count as "functions" for code-scale purposes. Anything
# else reachable via CALLS (rare) still appears in the tree but is excluded
# from the function-level LOC sum when it lacks line information.
_FUNCTION_KINDS = {"Function", "Method", "Test"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LineageNode:
    """One node in the expanded lineage tree."""

    qn: str
    name: str
    kind: str
    file_path: str
    line_start: int
    line_end: int
    depth: int
    external: bool = False
    children: list["LineageNode"] = field(default_factory=list)

    @property
    def loc(self) -> int:
        if self.line_start and self.line_end and self.line_end >= self.line_start:
            return self.line_end - self.line_start + 1
        return 0


@dataclass
class LineageResult:
    """Full lineage expansion for one root function."""

    root: LineageNode
    upstream: list[LineageNode] = field(default_factory=list)
    downstream: list[LineageNode] = field(default_factory=list)
    nodes: dict[str, LineageNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Package / function resolution
# ---------------------------------------------------------------------------


def _normalize_separators(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _path_in_package(file_path: str, package: str) -> bool:
    """Return True when *file_path* sits under the *package* directory.

    Works for both repo-relative and absolute stored paths by comparing path
    segments: the package segments must appear as a contiguous run inside the
    file path segments.
    """
    fp_parts = [p for p in _normalize_separators(file_path).split("/") if p]
    pkg_parts = [p for p in _normalize_separators(package).split("/") if p]
    if not fp_parts or not pkg_parts:
        return False
    if len(pkg_parts) > len(fp_parts):
        return False
    limit = len(fp_parts) - len(pkg_parts)
    for start in range(limit + 1):
        if fp_parts[start : start + len(pkg_parts)] == pkg_parts:
            # The package must cover a directory boundary, not a file name
            # suffix: the match must end before the final (file) segment.
            if start + len(pkg_parts) <= len(fp_parts) - 1:
                return True
    return False


def _path_in_file(file_path: str, file_filter: str) -> bool:
    """Return True when *file_path* matches the user-provided file filter.

    Unlike :func:`_path_in_package`, this matcher is allowed to match the
    final (file-name) segment as well, so that bare filenames like ``utils.py``
    work. The matching rule is still "contiguous run of segments" against the
    file path segments, so all three styles are supported:

    * bare filename       — ``utils.py``
    * repo-relative path  — ``code_review_graph/tools/utils.py``
    * absolute path       — ``D:/repo/code_review_graph/tools/utils.py``

    Path separators (``/`` or ``\\``) are normalised before comparison.
    """
    fp_parts = [p for p in _normalize_separators(file_path).split("/") if p]
    flt_parts = [p for p in _normalize_separators(file_filter).split("/") if p]
    if not fp_parts or not flt_parts:
        return False
    if len(flt_parts) > len(fp_parts):
        return False
    limit = len(fp_parts) - len(flt_parts)
    for start in range(limit + 1):
        if fp_parts[start : start + len(flt_parts)] == flt_parts:
            return True
    return False


def find_function_node(
    store: GraphStore,
    function_name: str,
    package: str = "",
    file_path: str = "",
) -> tuple[Optional[GraphNode], list[GraphNode]]:
    """Locate one function node by package path and/or file path + exact name.

    Filters:

    * ``package`` — when non-empty, only nodes whose ``file_path`` is
      contained under the package directory are kept (see
      :func:`_path_in_package`).
    * ``file_path`` — when non-empty, only nodes whose ``file_path`` matches
      the user-supplied file filter (see :func:`_path_in_file`); matching
      the final file-name segment is allowed so bare filenames work.

    Both filters are AND-combined. Supplying neither is a programming error
    and will be rejected by the CLI; the caller still gets a sane empty
    result in that case.

    Returns ``(node, candidates)``: ``node`` is set when exactly one
    unambiguous match exists; otherwise ``candidates`` carries the matches
    (empty when nothing matched).
    """
    searched = store.search_nodes(function_name, limit=100)
    matches = [
        n
        for n in searched
        if n.name == function_name and n.kind != "File"
    ]
    if package:
        matches = [n for n in matches if _path_in_package(n.file_path, package)]
    if file_path:
        matches = [n for n in matches if _path_in_file(n.file_path, file_path)]
    # Prefer real function-like kinds over containers (Class/Type) when both
    # share the same name.
    fn_matches = [n for n in matches if n.kind in _FUNCTION_KINDS]
    pool = fn_matches or matches
    if len(pool) == 1:
        return pool[0], pool
    return None, pool


# ---------------------------------------------------------------------------
# Lineage expansion
# ---------------------------------------------------------------------------


class _EndpointResolver:
    """Resolve edge endpoint qualified names to graph nodes.

    Edge endpoints are not always stored fully qualified; unresolved bare
    names fall back to an exact-name search, then to a synthetic external
    leaf so the lineage tree stays complete. When several nodes share the
    bare name, the candidate closest to the *hint* file (same file first,
    then same directory) wins, which keeps cross-language name collisions
    (e.g. a Python ``close`` resolving into a TypeScript file) out of the
    lineage.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._cache: dict[str, Optional[GraphNode]] = {}

    def resolve(self, qn: str, hint_file: str = "") -> Optional[GraphNode]:
        cache_key = f"{qn}|{hint_file}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        node = self._store.get_node(qn)
        if node is None:
            bare = qn.split("::")[-1].split(".")[-1]
            candidates = [
                c for c in self._store.search_nodes(bare, limit=20)
                if c.name == bare
            ]
            if candidates and hint_file:
                # Cross-language bare-name hits are almost always wrong:
                # a Python call never targets a .ts node. Restrict the
                # pool to same-extension candidates; if none exist the
                # endpoint stays external instead of being misresolved.
                hint_ext = hint_file.rsplit(".", 1)[-1].lower()
                same_ext = [
                    c for c in candidates
                    if c.file_path.rsplit(".", 1)[-1].lower() == hint_ext
                ]
                candidates = same_ext
            if candidates:
                node = self._pick_best(candidates, hint_file)
        self._cache[cache_key] = node
        return node

    @staticmethod
    def _pick_best(
        candidates: list[GraphNode], hint_file: str,
    ) -> GraphNode:
        if len(candidates) == 1 or not hint_file:
            return candidates[0]
        hint = _normalize_separators(hint_file)
        hint_dir = hint.rsplit("/", 1)[0] if "/" in hint else ""
        for candidate in candidates:
            if _normalize_separators(candidate.file_path) == hint:
                return candidate
        for candidate in candidates:
            cand = _normalize_separators(candidate.file_path)
            if hint_dir and cand.rsplit("/", 1)[0] == hint_dir:
                return candidate
        return candidates[0]


def _make_tree_node(
    qn: str, graph_node: Optional[GraphNode], depth: int,
) -> LineageNode:
    if graph_node is not None:
        return LineageNode(
            qn=graph_node.qualified_name,
            name=graph_node.name,
            kind=graph_node.kind,
            file_path=graph_node.file_path,
            line_start=graph_node.line_start,
            line_end=graph_node.line_end,
            depth=depth,
        )
    return LineageNode(
        qn=qn,
        name=qn.split("::")[-1].split(".")[-1],
        kind="External",
        file_path="",
        line_start=0,
        line_end=0,
        depth=depth,
        external=True,
    )


def _expand(
    store: GraphStore,
    resolver: _EndpointResolver,
    seed_qn: str,
    direction: str,
    max_depth: Optional[int],
    result: LineageResult,
) -> list[LineageNode]:
    """BFS over CALLS edges from *seed_qn* in one direction.

    ``direction="down"`` follows edge targets (callees); ``"up"`` follows
    edge sources (callers). Returns the first-level children; deeper levels
    hang off their parents. Cycle-safe: a node already expanded anywhere in
    the lineage is referenced but not re-expanded.
    """
    first_level: list[LineageNode] = []
    # frontier holds (tree_parent, qn, depth); tree_parent None => first level
    frontier: list[tuple[Optional[LineageNode], str, int]] = [
        (None, seed_qn, 0)
    ]
    expanded: set[str] = set()
    seed_tree_node = result.nodes.get(seed_qn)

    while frontier:
        parent, current_qn, depth = frontier.pop(0)
        if depth > 0 and current_qn in expanded:
            continue
        if current_qn != seed_qn:
            expanded.add(current_qn)
        if max_depth is not None and depth >= max_depth:
            continue

        if direction == "down":
            edges = [
                e for e in store.get_edges_by_source(current_qn)
                if e.kind == _CALLS_KIND
            ]
            next_qns = [e.target_qualified for e in edges]
        else:
            edges = [
                e for e in store.get_edges_by_target(current_qn)
                if e.kind == _CALLS_KIND
            ]
            next_qns = [e.source_qualified for e in edges]

        seen_here: set[str] = set()
        for edge, next_qn in zip(edges, next_qns):
            if next_qn in seen_here:
                continue
            seen_here.add(next_qn)
            result.edges.append(edge)

            graph_node = resolver.resolve(next_qn, hint_file=edge.file_path)
            child = _make_tree_node(next_qn, graph_node, depth + 1)

            if child.qn in result.nodes:
                # Already part of the lineage: attach a reference copy and do
                # not expand again (cycle / converging path).
                ref = LineageNode(
                    qn=child.qn,
                    name=child.name,
                    kind=child.kind,
                    file_path=child.file_path,
                    line_start=child.line_start,
                    line_end=child.line_end,
                    depth=depth + 1,
                    external=child.external,
                )
                _attach(parent, seed_tree_node, first_level, ref, depth)
                continue

            result.nodes[child.qn] = child
            _attach(parent, seed_tree_node, first_level, child, depth)
            frontier.append((child, next_qn, depth + 1))

    return first_level


def _attach(
    parent: Optional[LineageNode],
    seed_tree_node: Optional[LineageNode],
    first_level: list[LineageNode],
    child: LineageNode,
    parent_depth: int,
) -> None:
    if parent_depth == 0 and parent is None:
        first_level.append(child)
    elif parent is not None:
        parent.children.append(child)
    elif seed_tree_node is not None:
        seed_tree_node.children.append(child)


def trace_lineage(
    store: GraphStore,
    root_node: GraphNode,
    max_depth: Optional[int] = None,
) -> LineageResult:
    """Expand the full bidirectional call lineage of *root_node*."""
    root_tree = LineageNode(
        qn=root_node.qualified_name,
        name=root_node.name,
        kind=root_node.kind,
        file_path=root_node.file_path,
        line_start=root_node.line_start,
        line_end=root_node.line_end,
        depth=0,
    )
    result = LineageResult(root=root_tree, nodes={root_tree.qn: root_tree})
    resolver = _EndpointResolver(store)

    # Downstream: functions this function calls. Seed edges attach to the
    # root tree node directly.
    result.downstream = _expand(
        store, resolver, root_tree.qn, "down", max_depth, result,
    )
    for child in result.downstream:
        root_tree.children.append(child)

    # Upstream: callers of this function.
    result.upstream = _expand(
        store, resolver, root_tree.qn, "up", max_depth, result,
    )
    return result


# ---------------------------------------------------------------------------
# Code-scale statistics
# ---------------------------------------------------------------------------


def compute_scale(result: LineageResult) -> dict[str, Any]:
    """Aggregate function-level and file-level code scale for a lineage."""
    fn_nodes = [
        n
        for n in result.nodes.values()
        if not n.external and n.kind in _FUNCTION_KINDS
    ]
    total_loc = sum(n.loc for n in fn_nodes)

    by_file: dict[str, dict[str, Any]] = {}
    for n in fn_nodes:
        entry = by_file.setdefault(n.file_path, {"functions": 0, "loc": 0})
        entry["functions"] += 1
        entry["loc"] += n.loc

    files = [
        {"file_path": fp, "functions": v["functions"], "loc": v["loc"]}
        for fp, v in sorted(by_file.items(), key=lambda kv: -kv[1]["loc"])
    ]
    external = sum(1 for n in result.nodes.values() if n.external)
    return {
        "function_level": {
            "function_count": len(fn_nodes),
            "total_loc": total_loc,
            "external_references": external,
        },
        "file_level": {
            "file_count": len(files),
            "files": files,
        },
    }


# ---------------------------------------------------------------------------
# Tool-style entry point
# ---------------------------------------------------------------------------


def _tree_to_dict(node: LineageNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "qualified_name": node.qn,
        "kind": node.kind,
        "file_path": node.file_path,
        "line_start": node.line_start,
        "line_end": node.line_end,
        "loc": node.loc,
        "external": node.external,
        "children": [_tree_to_dict(c) for c in node.children],
    }


def function_lineage(
    function: str,
    package: str = "",
    file_path: str = "",
    repo_root: str | None = None,
    max_depth: Optional[int] = None,
) -> dict[str, Any]:
    """Trace the call lineage of one function inside one package/file.

    Args:
        function: Exact function name to locate.
        package: Optional directory/package path used to filter candidate
            files. See :func:`_path_in_package` for the matching rule.
        file_path: Optional file path filter (bare filename, repo-relative
            path, or absolute path) — see :func:`_path_in_file`.
        repo_root: Repository root path. Auto-detected if omitted.
        max_depth: Maximum expansion depth per direction. ``None`` expands
            until the lineage is exhausted (cycle-safe).

    Returns:
        A dict with ``status`` ``ok`` / ``ambiguous`` / ``error``, the
        lineage trees (upstream + downstream) and code-scale statistics.

    Note:
        At least one of ``package`` / ``file_path`` must be provided; without
        a filter the global search surface is too broad to be useful and is
        almost always a user mistake.
    """
    if isinstance(max_depth, bool) or (max_depth is not None and max_depth < 1):
        raise ValueError("max_depth must be None or an integer >= 1")
    if not package and not file_path:
        return {
            "status": "error",
            "error": (
                "At least one of --package or --file must be provided to "
                "scope the search."
            ),
            "summary": (
                "Missing scope: provide --package or --file so the function "
                "can be located unambiguously."
            ),
        }

    store, _root = _get_store(repo_root)
    try:
        node, candidates = find_function_node(
            store, function, package=package, file_path=file_path,
        )
        if node is None:
            if not candidates:
                scope = " or ".join(
                    s for s, v in (("package", package), ("file", file_path)) if v
                ) or "the requested scope"
                return {
                    "status": "error",
                    "error": (
                        f"No function named '{function}' found under {scope}."
                    ),
                    "summary": (
                        f"'{function}' not found under {scope}. "
                        "Check the package path, file path, and function name."
                    ),
                }
            return _ambiguous_result(function, package, file_path, candidates)

        result = trace_lineage(store, node, max_depth=max_depth)
        scale = compute_scale(result)
        return {
            "status": "ok",
            "package": package,
            "file": file_path,
            "function": function,
            "max_depth": max_depth,
            "root": node_to_dict(node),
            "upstream": [_tree_to_dict(c) for c in result.upstream],
            "downstream": [_tree_to_dict(c) for c in result.root.children],
            "node_count": len(result.nodes),
            "edge_count": len(result.edges),
            "scale": scale,
        }
    finally:
        store.close()


def _ambiguous_result(
    function: str,
    package: str,
    file_path: str,
    candidates: list[GraphNode],
) -> dict[str, Any]:
    """Build the ``ambiguous`` payload with enriched candidate entries."""
    scope_bits = []
    if package:
        scope_bits.append(f"package='{package}'")
    if file_path:
        scope_bits.append(f"file='{file_path}'")
    scope_desc = " / ".join(scope_bits) or "the requested scope"

    enriched = [
        {
            **node_to_dict(c),
            # Display-only convenience: file:line lets users eyeball the
            # ambiguity in CLI output without having to JSON-dump the whole
            # node.
            "display": (
                f"{c.file_path}:{c.line_start}: {c.qualified_name} "
                f"[{c.kind}]"
            ),
        }
        for c in candidates
    ]
    return {
        "status": "ambiguous",
        "summary": (
            f"'{function}' under {scope_desc} matches {len(candidates)} "
            f"nodes. Re-run with a more specific --package or --file."
        ),
        "candidates": enriched,
        "candidate_count": len(candidates),
    }


# ---------------------------------------------------------------------------
# Scope aggregation (lineage-agg): one file or a whole directory tree
# ---------------------------------------------------------------------------


def _as_abs(path: str, repo_root: Path) -> Path:
    """Resolve a possibly-relative user path against the repo root."""
    p = Path(path.replace("\\", "/"))
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


def _resolve_scope(
    store: GraphStore,
    repo_root: Path,
    scope: str,
) -> dict[str, Any]:
    """Resolve a user ``--scope`` into the graph file paths it covers.

    Decision order:

    * The scope path (resolved against the repo root) is an existing
      directory on disk -> directory mode: every graph file stored anywhere
      under that subtree is in scope (recursive).
    * It is an existing file on disk -> file mode: that single file.
    * It exists on neither -> treat the string as a file filter using the
      same contiguous path-segment rule as :func:`_path_in_file`; a unique
      match wins, multiple matches are ambiguous, none is an error.

    Returns a result dict whose ``status`` is ``ok`` (with ``scope_type`` and
    ``files``), ``ambiguous``, or ``error``.
    """
    scope = scope.strip()
    if not scope:
        return {
            "status": "error",
            "error": "Empty --scope value.",
            "summary": "--scope must name a file or directory.",
        }

    abs_path = _as_abs(scope, repo_root)

    if abs_path.is_dir():
        files = _stored_files_under(store, abs_path, repo_root)
        if not files:
            return {
                "status": "error",
                "error": (
                    f"No graph files found under directory '{scope}'."
                ),
                "summary": (
                    f"'{scope}' contains no files present in the graph. "
                    "Run `code-review-graph build`/`update` first, and for "
                    "new files `git add -N <file>` before updating."
                ),
            }
        return {"status": "ok", "scope_type": "dir", "files": files}

    if abs_path.is_file():
        stored = _stored_file_exact(store, abs_path, repo_root, scope)
        if stored is None:
            return {
                "status": "error",
                "error": (
                    f"File '{scope}' is not present in the graph."
                ),
                "summary": (
                    f"'{scope}' is not indexed. Run `code-review-graph "
                    "build`/`update`, or `git add -N <file>` for a new file."
                ),
            }
        return {"status": "ok", "scope_type": "file", "files": [stored]}

    # Not on disk: fall back to contiguous-segment file matching.
    norm = _normalize_separators(scope)
    matches = [
        fp for fp in store.get_all_files()
        if _path_in_file(fp, norm)
    ]
    if not matches:
        return {
            "status": "error",
            "error": (
                f"No file under scope '{scope}' was found in the graph."
            ),
            "summary": (
                f"'{scope}' matched nothing. Provide a repo-relative or "
                "absolute path, and run build/update first."
            ),
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "summary": (
                f"Scope '{scope}' matches {len(matches)} files. Re-run with "
                "a more specific --scope path."
            ),
            "candidates": [{"file_path": fp} for fp in sorted(matches)],
            "candidate_count": len(matches),
        }
    return {"status": "ok", "scope_type": "file", "files": [matches[0]]}


def _stored_files_under(
    store: GraphStore, abs_dir: Path, repo_root: Path,
) -> list[str]:
    """Return graph-stored file paths located anywhere under *abs_dir*."""
    result: list[str] = []
    for fp in store.get_all_files():
        abs_fp = _as_abs(fp, repo_root)
        try:
            abs_fp.relative_to(abs_dir)
        except ValueError:
            continue
        result.append(fp)
    return sorted(result)


def _stored_file_exact(
    store: GraphStore,
    abs_file: Path,
    repo_root: Path,
    scope: str,
) -> Optional[str]:
    """Return the single stored path spelling for *abs_file*, if indexed."""
    abs_fp = str(abs_file).replace("\\", "/")
    stored = sorted(store.get_all_files())
    exact: list[str] = []
    for fp in stored:
        if _as_abs(fp, repo_root).resolve() == abs_file.resolve():
            exact.append(fp)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact[0]
    # Stored spellings may differ (cwd-relative legacy graphs): fall back to
    # the contiguous-segment matcher so a repo-relative path still works.
    for fp in stored:
        if _path_in_file(fp, _normalize_separators(scope)):
            return fp
    return None


def _expand_scope_union(
    store: GraphStore,
    root_qns: list[str],
    resolver: "_EndpointResolver",
) -> dict[str, LineageNode]:
    """BFS over CALLS in both directions from every root, deduped by node.

    Returns a mapping of canonical qualified name -> ``LineageNode`` for the
    *whole union*: a function reachable from several roots (or via several
    paths) is stored once. External/unresolved endpoints are kept as external
    leaves so the caller can count them.
    """
    nodes: dict[str, LineageNode] = {}
    expanded: set[str] = set()
    frontier: list[str] = []

    for qn in root_qns:
        if qn in nodes:
            continue
        seed = _make_tree_node(qn, store.get_node(qn), 0)
        nodes[seed.qn] = seed
        frontier.append(seed.qn)

    while frontier:
        current = frontier.pop(0)
        if current in expanded:
            continue
        expanded.add(current)

        down_edges = [
            e for e in store.get_edges_by_source(current)
            if e.kind == _CALLS_KIND
        ]
        up_edges = [
            e for e in store.get_edges_by_target(current)
            if e.kind == _CALLS_KIND
        ]

        for edge in down_edges:
            nxt = edge.target_qualified
            graph_node = resolver.resolve(nxt, hint_file=edge.file_path)
            if graph_node is None:
                external = _make_tree_node(nxt, None, 0)
                nodes.setdefault(external.qn, external)
                continue
            qn = graph_node.qualified_name
            if qn not in nodes:
                nodes[qn] = _make_tree_node(qn, graph_node, 0)
                frontier.append(qn)
        for edge in up_edges:
            nxt = edge.source_qualified
            graph_node = resolver.resolve(nxt, hint_file=edge.file_path)
            if graph_node is None:
                external = _make_tree_node(nxt, None, 0)
                nodes.setdefault(external.qn, external)
                continue
            qn = graph_node.qualified_name
            if qn not in nodes:
                nodes[qn] = _make_tree_node(qn, graph_node, 0)
                frontier.append(qn)

    return nodes


def _roots_for_files(
    store: GraphStore,
    files: list[str],
    exclude_tests: bool,
) -> tuple[list[GraphNode], dict[str, Any]]:
    """Collect every function-like root node defined in *files*.

    Returns ``(roots, result)``: when the scope contains no function-like
    nodes, ``roots`` is empty and ``result`` carries the error payload.
    """
    roots: list[GraphNode] = []
    seen_qns: set[str] = set()
    for fp in files:
        for node in store.get_nodes_by_file(fp):
            if node.kind not in _FUNCTION_KINDS:
                continue
            if exclude_tests and node.kind == "Test":
                continue
            if node.qualified_name in seen_qns:
                continue
            seen_qns.add(node.qualified_name)
            roots.append(node)
    return roots, {}


def _scale_over_nodes(
    nodes: dict[str, LineageNode], exclude_tests: bool,
) -> dict[str, Any]:
    """Compute function/file scale over a node union, with test filtering.

    Mirrors :func:`compute_scale` but lets ``lineage-agg`` drop ``Test``
    nodes before aggregating when ``--exclude-tests`` is set.
    """
    fn_nodes = [
        n for n in nodes.values()
        if not n.external
        and n.kind in _FUNCTION_KINDS
        and not (exclude_tests and n.kind == "Test")
    ]
    total_loc = sum(n.loc for n in fn_nodes)
    by_file: dict[str, dict[str, Any]] = {}
    for n in fn_nodes:
        entry = by_file.setdefault(n.file_path, {"functions": 0, "loc": 0})
        entry["functions"] += 1
        entry["loc"] += n.loc
    files = [
        {"file_path": fp, "functions": v["functions"], "loc": v["loc"]}
        for fp, v in sorted(by_file.items(), key=lambda kv: -kv[1]["loc"])
    ]
    external = sum(1 for n in nodes.values() if n.external)
    return {
        "function_level": {
            "function_count": len(fn_nodes),
            "total_loc": total_loc,
            "external_references": external,
        },
        "file_level": {
            "file_count": len(files),
            "files": files,
        },
    }


def scope_lineage(
    scope: str,
    repo_root: str | None = None,
    exclude_tests: bool = False,
) -> dict[str, Any]:
    """Aggregate lineage coverage across every function in a file/folder.

    Every function-like node defined inside the scope is a root. Each root
    expands its full bidirectional CALLS lineage (unlimited depth, cycle
    safe); the union is deduplicated by function identity, so a shared
    callee reached from many roots is counted exactly once, and same-named
    functions in different files stay distinct (their qualified names embed
    the file path).

    Args:
        scope: A file or directory path (absolute or repo-root-relative).
        repo_root: Repository root. Auto-detected if omitted.
        exclude_tests: When True, ``Test`` nodes are excluded from the root
            set and from the reported scale.

    Returns:
        A dict with ``status`` ``ok`` / ``ambiguous`` / ``error``. On
        success it carries the aggregated coverage totals, a file-level
        breakdown and the root-function identity list.
    """
    store, _root = _get_store(repo_root)
    try:
        resolved = _resolve_scope(store, _root, scope)
        if resolved["status"] != "ok":
            return resolved

        roots, err = _roots_for_files(
            store, resolved["files"], exclude_tests,
        )
        if err:
            return err
        if not roots:
            what = "directory" if resolved["scope_type"] == "dir" else "file"
            return {
                "status": "error",
                "error": (
                    f"No function-like nodes found in {what} '{scope}'."
                ),
                "summary": (
                    f"'{scope}' contains no Function/Test nodes in the "
                    "graph. Check the path or run build/update."
                ),
            }

        resolver = _EndpointResolver(store)
        root_qns = [r.qualified_name for r in roots]
        union = _expand_scope_union(store, root_qns, resolver)
        scale = _scale_over_nodes(union, exclude_tests)

        root_payload = [
            {
                "qualified_name": r.qualified_name,
                "name": r.name,
                "kind": r.kind,
                "file_path": r.file_path,
                "line_start": r.line_start,
                "line_end": r.line_end,
                "loc": (r.line_end - r.line_start + 1)
                if r.line_end >= r.line_start else 0,
            }
            for r in sorted(roots, key=lambda n: n.qualified_name)
        ]

        fn = scale["function_level"]
        fl = scale["file_level"]
        return {
            "status": "ok",
            "scope": scope,
            "scope_type": resolved["scope_type"],
            "exclude_tests": exclude_tests,
            "root_count": len(root_payload),
            "roots": root_payload,
            "function_count": fn["function_count"],
            "total_loc": fn["total_loc"],
            "file_count": fl["file_count"],
            "external_references": fn["external_references"],
            "files": fl["files"],
        }
    finally:
        store.close()


def render_lineage_agg_text(result: dict[str, Any]) -> str:
    """Render a ``scope_lineage`` result dict as human-readable text."""
    if result.get("status") != "ok":
        lines = [result.get("summary") or result.get("error", "unknown error")]
        for cand in result.get("candidates", []):
            lines.append(f"  - {cand['file_path']}")
        return "\n".join(lines)

    mode = "文件夹 (递归)" if result["scope_type"] == "dir" else "文件"
    lines = [
        f"作用域聚合: {result['scope']}  ({mode})",
        "=" * 64,
        "",
        f"作用域内根函数: {result['root_count']} 个",
        f"(排除测试: {'是' if result['exclude_tests'] else '否'})",
        "覆盖统计:",
        f"  函数级: {result['function_count']} 个函数 (去重后), "
        f"共 {result['total_loc']} 行",
        f"  文件级: {result['file_count']} 个文件",
        f"  外部未解析引用: {result['external_references']} 个",
    ]
    for entry in result["files"]:
        lines.append(
            f"    {entry['file_path']}: "
            f"{entry['functions']} 个函数, {entry['loc']} 行"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _format_node_line(node: LineageNode) -> str:
    loc = f", {node.loc} 行" if node.loc else ""
    if node.external:
        return f"{node.name} (外部/未解析)"
    return f"{node.name} ({node.file_path}:{node.line_start}-{node.line_end}{loc})"


def _render_tree_lines(
    nodes: Iterable[LineageNode], prefix: str = "",
) -> list[str]:
    lines: list[str] = []
    items = list(nodes)
    for index, node in enumerate(items):
        last = index == len(items) - 1
        connector = "└─ " if last else "├─ "
        lines.append(f"{prefix}{connector}{_format_node_line(node)}")
        child_prefix = prefix + ("   " if last else "│  ")
        lines.extend(_render_tree_lines(node.children, child_prefix))
    return lines


def _dict_to_tree(node_dict: dict[str, Any]) -> LineageNode:
    return LineageNode(
        qn=node_dict["qualified_name"],
        name=node_dict["name"],
        kind=node_dict["kind"],
        file_path=node_dict["file_path"],
        line_start=node_dict["line_start"],
        line_end=node_dict["line_end"],
        depth=0,
        external=node_dict.get("external", False),
        children=[_dict_to_tree(c) for c in node_dict.get("children", [])],
    )


def render_lineage_text(result: dict[str, Any]) -> str:
    """Render a ``function_lineage`` result dict as a human-readable tree."""
    if result.get("status") != "ok":
        lines = [result.get("summary") or result.get("error", "unknown error")]
        for cand in result.get("candidates", []):
            lines.append(
                f"  - {cand['qualified_name']} "
                f"({cand['file_path']}:{cand['line_start']})"
            )
        return "\n".join(lines)

    root = result["root"]
    root_node = LineageNode(
        qn=root["qualified_name"],
        name=root["name"],
        kind=root["kind"],
        file_path=root["file_path"],
        line_start=root["line_start"],
        line_end=root["line_end"],
        depth=0,
    )

    lines: list[str] = []
    lines.append(f"血缘根: {_format_node_line(root_node)}")
    if result.get("max_depth") is not None:
        lines.append(f"(最大展开深度: {result['max_depth']})")
    lines.append("")

    downstream = [_dict_to_tree(c) for c in result.get("downstream", [])]
    lines.append(f"▼ 下游调用链 callees ({len(downstream)} 个直接调用)")
    lines.extend(_render_tree_lines(downstream))
    lines.append("")

    upstream = [_dict_to_tree(c) for c in result.get("upstream", [])]
    lines.append(f"▲ 上游调用方 callers ({len(upstream)} 个直接调用方)")
    lines.extend(_render_tree_lines(upstream))
    lines.append("")

    scale = result["scale"]
    fn = scale["function_level"]
    fl = scale["file_level"]
    lines.append("代码规模统计")
    lines.append(
        f"  函数级: {fn['function_count']} 个函数, 共 {fn['total_loc']} 行"
        f" (外部未解析引用 {fn['external_references']} 个)"
    )
    lines.append(f"  文件级: {fl['file_count']} 个文件")
    for entry in fl["files"]:
        lines.append(
            f"    {entry['file_path']}: "
            f"{entry['functions']} 个函数, {entry['loc']} 行"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering (reuses the visualization full-graph template)
# ---------------------------------------------------------------------------


def render_lineage_html(
    result: dict[str, Any],
    output_path: str | Path,
    repo_root: str | None = None,
) -> Path:
    """Render a ``function_lineage`` result as an interactive HTML graph.

    Reuses the D3 template from :mod:`code_review_graph.visualization` with
    only the lineage subgraph injected.
    """
    if result.get("status") != "ok":
        raise ValueError(
            "Cannot render HTML for a non-ok lineage result: "
            f"{result.get('status')}"
        )

    from . import visualization

    store, _root = _get_store(repo_root)
    try:
        stats = store.get_stats()
    finally:
        store.close()

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_node(nd: dict[str, Any]) -> None:
        qn = nd["qualified_name"]
        if qn in seen:
            return
        seen.add(qn)
        nodes.append({
            "id": len(nodes) + 1,
            "kind": nd["kind"],
            "name": nd["name"],
            "qualified_name": qn,
            "file_path": nd["file_path"],
            "line_start": nd["line_start"],
            "line_end": nd["line_end"],
            "language": "",
            "parent_name": None,
            "is_test": nd["kind"] == "Test",
            "params": None,
            "return_type": None,
            "community_id": None,
        })

    def walk(tree: dict[str, Any]) -> None:
        add_node(tree)
        for child in tree.get("children", []):
            walk(child)

    add_node(result["root"])
    for tree in (*result.get("downstream", []), *result.get("upstream", [])):
        walk(tree)

    # Rebuild edges from the lineage trees: parent -> child for downstream,
    # child -> parent for upstream.
    edges: list[dict[str, Any]] = []
    edge_id = 0

    def emit(parent_qn: str, child: dict[str, Any], direction: str) -> None:
        nonlocal edge_id
        edge_id += 1
        if direction == "down":
            source, target = parent_qn, child["qualified_name"]
        else:
            source, target = child["qualified_name"], parent_qn
        edges.append({
            "id": edge_id,
            "kind": _CALLS_KIND,
            "source": source,
            "target": target,
            "file_path": child["file_path"],
            "line": 0,
            "confidence": 1.0,
            "confidence_tier": "EXTRACTED",
        })
        for grandchild in child.get("children", []):
            emit(child["qualified_name"], grandchild, direction)

    for tree in result.get("downstream", []):
        emit(result["root"]["qualified_name"], tree, "down")
    for tree in result.get("upstream", []):
        emit(result["root"]["qualified_name"], tree, "up")

    data = {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "nodes_by_kind": {},
            "edges_by_kind": {_CALLS_KIND: len(edges)},
            "languages": [],
            "files_count": len({n["file_path"] for n in nodes if n["file_path"]}),
            "last_updated": getattr(stats, "last_updated", None),
        },
        "flows": [],
        "communities": [],
        "lineage_scale": result.get("scale"),
    }
    data_json = json.dumps(data, default=str).replace("</", "<\\/")
    html = visualization._HTML_TEMPLATE.replace(
        "__D3_SCRIPTS__", visualization._d3_script_tags(),
    )
    html = html.replace("__GRAPH_DATA__", data_json)

    output = Path(output_path)
    output.write_text(html, encoding="utf-8")
    visualization._write_d3_asset(output.parent)
    return output
