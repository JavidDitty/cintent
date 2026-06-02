#!/usr/bin/env python3
"""
to_dynapyt.py — Convert a cintent graph_df_filtered CSV into a DynaPyt-format JSON.

DynaPyt JSON format:
    {
      ".<workspace_dotted_path>.<caller_fq_name>": ["callee1", "callee2", ...],
      ...
    }

Where the key prefix is derived from the workspace root path baked into
``src_file`` / ``dst_file`` columns of the graph CSV, e.g.
    /home/runner/work/graph-012/graph-012/  ->  .home.runner.work.graph-012.graph-012.

The callees list contains every unique ``dst_fq_name`` that the caller reached
(direct or transitive, across all depths), deduplicated and sorted.

Usage (CLI):
    python -m cintent.to_dynapyt <graph_csv> [-o output.json] [--prefix PATH]
    python to_dynapyt.py       <graph_csv> [-o output.json] [--prefix PATH]

Usage (API):
    from cintent.to_dynapyt import graph_csv_to_dynapyt_json
    result = graph_csv_to_dynapyt_json("graph_df_filtered.csv")
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict


# ─────────────────────────────────────────────────────────────────────────────
# Core conversion
# ─────────────────────────────────────────────────────────────────────────────

def _detect_workspace_prefix(graph_rows: list[dict]) -> str:
    """Derive the dotted workspace prefix from file paths in the graph CSV.

    e.g. ``/home/runner/work/graph-012/graph-012/grab/document.py``
         →  ``.home.runner.work.graph-012.graph-012.``

    Strategy: find the longest common path prefix across all src_file /
    dst_file values, trim to the last ``/``, then convert to dotted form.
    """
    paths = set()
    for row in graph_rows:
        for col in ("src_file", "dst_file"):
            p = row.get(col, "").strip()
            if p:
                paths.add(p)

    if not paths:
        return ""

    # Common path prefix (works on POSIX-style paths stored in the CSV)
    common = os.path.commonprefix(list(paths))
    # Ensure we trim at a directory boundary
    last_slash = max(common.rfind("/"), common.rfind("\\"))
    if last_slash > 0:
        common = common[: last_slash + 1]   # include trailing slash

    # Convert  /home/runner/work/graph-012/graph-012/  ->  .home.runner.work.graph-012.graph-012.
    dotted = common.replace("/", ".").replace("\\", ".").strip(".")
    return "." + dotted + "." if dotted else ""


def graph_csv_to_dynapyt_json(
    graph_csv_path: str,
    workspace_prefix: str | None = None,
) -> dict[str, list[str]]:
    """Load a graph_df_filtered CSV and return a DynaPyt-format dict.

    Parameters
    ----------
    graph_csv_path:
        Path to the ``graph_df_filtered*.csv`` produced by cintent archive.py.
    workspace_prefix:
        Override the auto-detected dotted workspace prefix.
        If omitted the prefix is auto-detected from file paths in the CSV.

    Returns
    -------
    dict mapping  ``".<prefix>.<caller_fq_name>"``  →  ``[callee_fq_name, ...]``
    """
    with open(graph_csv_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return {}

    prefix = workspace_prefix if workspace_prefix is not None else _detect_workspace_prefix(rows)

    # Build caller -> {callees} mapping
    caller_callees: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        src = row.get("src_fq_name", "").strip()
        dst = row.get("dst_fq_name", "").strip()
        if src and dst:
            caller_callees[src].add(dst)

    # Build the output dict: sort callers and callees for determinism
    result: dict[str, list[str]] = {}
    for caller in sorted(caller_callees):
        key = prefix + caller
        result[key] = sorted(caller_callees[caller])

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert a cintent graph CSV to DynaPyt-format JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("graph_csv", help="Path to graph_df_filtered*.csv")
    p.add_argument(
        "-o", "--output",
        default=None,
        help="Output JSON file path. Defaults to <graph_csv_stem>_dynapyt.json.",
    )
    p.add_argument(
        "--prefix",
        default=None,
        help=(
            "Override the dotted workspace prefix, e.g. "
            "'.home.runner.work.graph-012.graph-012.'"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    graph_path = args.graph_csv
    if not os.path.isfile(graph_path):
        print(f"Error: file not found: {graph_path}", file=sys.stderr)
        sys.exit(1)

    result = graph_csv_to_dynapyt_json(graph_path, workspace_prefix=args.prefix)

    if args.output:
        out_path = args.output
    else:
        stem = os.path.splitext(os.path.basename(graph_path))[0]
        out_path = os.path.join(os.path.dirname(graph_path), stem + "_dynapyt.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Summary
    total_edges = sum(len(v) for v in result.values())
    print(f"Wrote {len(result)} callers, {total_edges} edges → {out_path}")
    if result:
        detected_prefix = next(iter(result)).split(".", 2)
        print(f"Workspace prefix: {'.'.join(detected_prefix[:2])!r}")


if __name__ == "__main__":
    main()
