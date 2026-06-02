#!/usr/bin/env python3
"""
evaluate_coverage.py — Evaluate profiler call-graph coverage against DynaPyt ground truth.

Compares edges (caller → callee) from a profiler's graph CSV against
DynaPyt's ground-truth JSON to compute recall, precision, and F1 metrics.

DynaPyt JSON format:
    { "<dotted_filesystem_path>.<fq_name>": ["callee1", "callee2", ...], ... }

Graph CSV format (from cintent archive.py):
    repo_id, job_id, ..., src_fq_name, ..., dst_fq_name, ...

Metrics computed:
    1. Caller coverage   — % of ground-truth callers detected by the tool
    2. Total edge recall — % of ALL ground-truth edges detected
    3. Workspace edge recall — % of workspace-only edges detected (fair comparison,
       since the profiler by design only captures workspace function calls)
    4. Per-caller breakdown with missed edges

Usage:
    python -m cintent.evaluate <dynapyt_json> <graph_csv> [-o output_dir]
    python evaluate.py <dynapyt_json> <graph_csv> [-o output_dir]
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict


# ──────────────────────────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────────────────────────

def load_dynapyt(path):
    """Load DynaPyt ground-truth JSON."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_graph_csv(path):
    """Load the tool's graph CSV and return list of row dicts."""
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def detect_workspace_prefix(dynapyt_data, tool_callers):
    """Auto-detect the filesystem-path prefix in DynaPyt keys.

    DynaPyt keys look like:
        .home.runner.work.graph-012.graph-012.tests.util
    The workspace root prefix is:
        .home.runner.work.graph-012.graph-012.

    Strategy: try every possible prefix length on the first key and pick
    the one that, when stripped from ALL keys, maximises the overlap with
    the tool's caller set.  Falls back to callee cross-referencing and
    finally to common-prefix heuristic.
    """
    keys = list(dynapyt_data.keys())
    if not keys:
        return ""

    first_key = keys[0].lstrip(".")
    parts = first_key.split(".")

    best_prefix = ""
    best_score = -1

    # Method 1: maximize overlap with tool_callers
    for prefix_len in range(1, len(parts)):
        candidate = "." + ".".join(parts[:prefix_len]) + "."
        score = sum(1 for k in keys if k.startswith(candidate) and k[len(candidate):] in tool_callers)
        if score > best_score:
            best_score = score
            best_prefix = candidate

    if best_score > 0:
        return best_prefix

    # Method 2: find prefix where stripped keys appear as DynaPyt callees
    all_callees = set()
    for callees in dynapyt_data.values():
        for c in callees:
            if "(" not in c:
                all_callees.add(c)

    for prefix_len in range(1, len(parts)):
        candidate = "." + ".".join(parts[:prefix_len]) + "."
        score = sum(1 for k in keys if k.startswith(candidate) and k[len(candidate):] in all_callees)
        if score > best_score:
            best_score = score
            best_prefix = candidate

    if best_score > 0:
        return best_prefix

    # Method 3: fallback to longest common prefix trimmed at dot
    prefix = os.path.commonprefix(keys)
    last_dot = prefix.rfind(".")
    if last_dot > 0:
        prefix = prefix[: last_dot + 1]
    return prefix


def detect_workspace_packages(graph_rows):
    """Extract workspace top-level package names from graph CSV relpaths.

    For example, if relpaths include ``tests/util.py`` and ``grab/document.py``,
    the workspace packages are ``{'tests', 'grab'}``.
    """
    packages = set()
    for row in graph_rows:
        for col in ("src_relpath", "dst_relpath"):
            relpath = row.get(col, "").strip()
            if relpath:
                top = relpath.split("/")[0].split("\\")[0]  # handle both separators
                top = top.split(".")[0]  # strip .py if top-level file
                if top:
                    packages.add(top)
    return packages


def is_workspace_callee(callee, workspace_packages):
    """Return True if the callee's top-level module is a workspace package."""
    if not callee:
        return False
    top = callee.split(".")[0]
    return top in workspace_packages


def is_constructor(callee):
    """Heuristic: a callee like ``grab.document.Document`` (last part CamelCase)
    is likely a constructor call.  Returns the __init__ variant if so."""
    if not callee or "(" in callee:
        return None
    parts = callee.split(".")
    last = parts[-1]
    if last and last[0].isupper() and not last.startswith("_"):
        return callee + ".__init__"
    return None


def normalize_dynapyt_caller(raw_key, prefix):
    """Strip the filesystem prefix from a DynaPyt key to get a module-relative FQ name."""
    caller = raw_key
    if caller.startswith(prefix):
        caller = caller[len(prefix):]
    else:
        # Fallback: strip leading dots then try prefix without leading dot
        caller = caller.lstrip(".")
        clean_prefix = prefix.lstrip(".")
        if caller.startswith(clean_prefix):
            caller = caller[len(clean_prefix):]
    return caller


def edge_matches(caller, callee, tool_edges, tool_edges_by_caller=None):
    """Check if a ground-truth edge (caller, callee) matches any tool edge.

    Tries (in order):
        1. Direct match: (caller, callee) in tool_edges
        2. Constructor -> __init__: (caller, callee.__init__) in tool_edges
        3. __init__ -> bare class: (caller, callee[:-9]) if callee ends with .__init__
        4. <locals> stripping: caller.<locals>.ClassName -> caller.ClassName
           or caller.ClassName.__init__
        5. Parent constructor heuristic (<locals>): if callee is
           caller.<locals>.ClassName (a local-class constructor), and the tool
           recorded (caller, SomeParent.__init__), the local class likely
           inherits from SomeParent without its own __init__.
        6. Parent constructor heuristic (non-<locals>): if callee is a
           CamelCase class like grab.client.HttpClient and neither bare nor
           .__init__ matched, the tool may have (caller, ParentClass.__init__)
           because HttpClient inherits from BaseClient which has __init__.
    """
    if (caller, callee) in tool_edges:
        return True

    # Constructor match
    init_var = is_constructor(callee)
    if init_var and (caller, init_var) in tool_edges:
        return True

    # Reverse: callee is __init__ variant, tool has bare class
    if callee.endswith(".__init__"):
        bare = callee[: -len(".__init__")]
        if (caller, bare) in tool_edges:
            return True

    # ── <locals> constructor resolution ──
    if ".<locals>." in callee:
        # Pattern 4: strip <locals>, try bare and __init__
        # DynaPyt: caller.<locals>.ClassName  ->  tool: caller.ClassName or caller.ClassName.__init__
        stripped = callee.replace(".<locals>.", ".")
        if (caller, stripped) in tool_edges:
            return True
        stripped_init = stripped + ".__init__"
        if (caller, stripped_init) in tool_edges:
            return True

        # Pattern 5: parent constructor heuristic for <locals> classes
        # DynaPyt: test_method -> test_method.<locals>.TestSpider  (constructor call)
        # Tool:    test_method -> grab.spider.base.Spider.__init__ (parent's __init__)
        # The local class inherits from a parent and has no own __init__.
        if tool_edges_by_caller is not None:
            parts = callee.split(".<locals>.")
            if len(parts) == 2:
                class_name = parts[1].split(".")[0]
                # Only apply heuristic for CamelCase names (constructor calls)
                if class_name and class_name[0].isupper():
                    caller_callees = tool_edges_by_caller.get(caller, set())
                    for tool_callee in caller_callees:
                        if tool_callee.endswith(".__init__"):
                            return True

    # ── Pattern 6: parent constructor heuristic for non-<locals> constructors ──
    # DynaPyt: test_method -> grab.client.HttpClient           (constructor)
    # Tool:    test_method -> grab.base.BaseClient.__init__    (parent's __init__)
    # HttpClient inherits from BaseClient and has no own __init__.
    if tool_edges_by_caller is not None and ".<locals>." not in callee:
        if init_var is not None:
            # init_var is callee.__init__ — we already checked direct match above.
            # Now check if the caller has ANY __init__ callee (likely the parent).
            caller_callees = tool_edges_by_caller.get(caller, set())
            for tool_callee in caller_callees:
                if tool_callee.endswith(".__init__"):
                    return True

    return False


# ──────────────────────────────────────────────────────────────────
# Main evaluation
# ──────────────────────────────────────────────────────────────────

def evaluate(dynapyt_path, graph_csv_path, output_dir=None):
    """Run the full coverage evaluation.

    Returns a dict with aggregate metrics for programmatic use.
    """
    # ── Load data ──
    dynapyt = load_dynapyt(dynapyt_path)
    graph_rows = load_graph_csv(graph_csv_path)

    # ── Build tool edge set (needed for prefix detection) ──
    tool_edges = set()
    tool_edges_by_caller = defaultdict(set)   # caller -> {callee, ...}
    tool_callers = set()
    tool_all_fqnames = set()

    for row in graph_rows:
        src = row.get("src_fq_name", "").strip()
        dst = row.get("dst_fq_name", "").strip()
        if src and dst:
            tool_edges.add((src, dst))
            tool_edges_by_caller[src].add(dst)
            tool_callers.add(src)
            tool_all_fqnames.add(src)
            tool_all_fqnames.add(dst)

    # ── Detect workspace context ──
    prefix = detect_workspace_prefix(dynapyt, tool_callers)
    ws_packages = detect_workspace_packages(graph_rows)

    print(f"DynaPyt workspace prefix : {prefix!r}")
    print(f"Workspace packages (CSV) : {sorted(ws_packages)}")

    print(f"\nTool stats:")
    print(f"  Unique edges    : {len(tool_edges)}")
    print(f"  Unique callers  : {len(tool_callers)}")
    print(f"  Unique functions: {len(tool_all_fqnames)}")

    # ── Build ground-truth edges ──
    gt_edges_all = []   # (caller, callee) — all edges
    gt_edges_ws = []    # (caller, callee) — workspace callee only
    gt_callers = set()
    skipped = 0

    per_caller_data = {}

    for raw_key, callees in dynapyt.items():
        caller = normalize_dynapyt_caller(raw_key, prefix)
        gt_callers.add(caller)

        all_callees = []
        ws_callees = []

        for raw_callee in callees:
            # Skip malformed entries (e.g., decorator expressions with parens)
            if "(" in raw_callee or ")" in raw_callee:
                skipped += 1
                continue

            all_callees.append(raw_callee)
            gt_edges_all.append((caller, raw_callee))

            if is_workspace_callee(raw_callee, ws_packages):
                ws_callees.append(raw_callee)
                gt_edges_ws.append((caller, raw_callee))

        per_caller_data[caller] = {
            "all_callees": all_callees,
            "ws_callees": ws_callees,
        }

    # Deduplicate (shouldn't happen but just in case)
    gt_edges_all_set = set(gt_edges_all)
    gt_edges_ws_set = set(gt_edges_ws)

    print(f"\nGround truth stats:")
    print(f"  Unique callers       : {len(gt_callers)}")
    print(f"  Total edges          : {len(gt_edges_all_set)}")
    print(f"  Workspace-only edges : {len(gt_edges_ws_set)}")
    if skipped:
        print(f"  Skipped (malformed)  : {skipped}")

    # ── Compute matches ──
    matched_all = set()
    matched_ws = set()

    for edge in gt_edges_all_set:
        if edge_matches(edge[0], edge[1], tool_edges, tool_edges_by_caller):
            matched_all.add(edge)

    for edge in gt_edges_ws_set:
        if edge_matches(edge[0], edge[1], tool_edges, tool_edges_by_caller):
            matched_ws.add(edge)

    matched_callers = gt_callers & tool_callers

    # Tool-only edges (not in ground truth — extra edges the tool found)
    tool_only_edges = set()
    for edge in tool_edges:
        found = False
        if edge in gt_edges_all_set:
            found = True
        if not found:
            # Check reverse constructor
            src, dst = edge
            if dst.endswith(".__init__"):
                bare = dst[: -len(".__init__")]
                if (src, bare) in gt_edges_all_set:
                    found = True
            else:
                init_var = is_constructor(dst)
                if init_var and (src, init_var) in gt_edges_all_set:
                    found = True
        if not found:
            # Check reverse <locals> match: tool has caller.ClassName.__init__
            # but GT has caller.<locals>.ClassName
            src, dst = edge
            if dst.endswith(".__init__"):
                bare = dst[: -len(".__init__")]
                # Try inserting <locals> before the class name
                # e.g. tool: test_method.TestSpider.__init__
                #  ->  GT:   test_method.<locals>.TestSpider
                last_dot = bare.rfind(".")
                if last_dot > 0:
                    parent = bare[:last_dot]
                    class_name = bare[last_dot + 1:]
                    if class_name and class_name[0].isupper():
                        locals_var = parent + ".<locals>." + class_name
                        if (src, locals_var) in gt_edges_all_set:
                            found = True
        if not found:
            tool_only_edges.add(edge)

    # ── Per-caller analysis ──
    per_caller_results = []
    for caller, data in per_caller_data.items():
        all_c = data["all_callees"]
        ws_c = data["ws_callees"]

        matched_c = [c for c in all_c if edge_matches(caller, c, tool_edges, tool_edges_by_caller)]
        matched_ws_c = [c for c in ws_c if edge_matches(caller, c, tool_edges, tool_edges_by_caller)]

        missed_all = [c for c in all_c if not edge_matches(caller, c, tool_edges, tool_edges_by_caller)]
        missed_ws = [c for c in ws_c if not edge_matches(caller, c, tool_edges, tool_edges_by_caller)]

        recall_all = len(matched_c) / len(all_c) * 100 if all_c else 100.0
        recall_ws = len(matched_ws_c) / len(ws_c) * 100 if ws_c else 100.0

        per_caller_results.append({
            "caller": caller,
            "total_callees": len(all_c),
            "ws_callees": len(ws_c),
            "matched_total": len(matched_c),
            "matched_ws": len(matched_ws_c),
            "recall_total_pct": recall_all,
            "recall_ws_pct": recall_ws,
            "missed_ws": sorted(missed_ws),
            "missed_all": sorted(missed_all),
        })

    per_caller_results.sort(key=lambda x: x["caller"])

    # ── Aggregate metrics ──
    caller_recall = len(matched_callers) / len(gt_callers) * 100 if gt_callers else 0
    total_recall = len(matched_all) / len(gt_edges_all_set) * 100 if gt_edges_all_set else 0
    ws_recall = len(matched_ws) / len(gt_edges_ws_set) * 100 if gt_edges_ws_set else 0

    # Precision: among workspace tool edges, how many are in ground truth?
    tool_ws_edges = {e for e in tool_edges if is_workspace_callee(e[1], ws_packages)}

    def tool_edge_in_gt(src, dst):
        """Check if a tool edge matches any ground-truth edge (reverse direction)."""
        if (src, dst) in gt_edges_ws_set:
            return True
        # Constructor: tool has __init__, GT has bare class
        if dst.endswith(".__init__"):
            bare = dst[:-len(".__init__")]
            if (src, bare) in gt_edges_ws_set:
                return True
            # <locals> reverse: tool has caller.ClassName.__init__, GT has caller.<locals>.ClassName
            last_dot = bare.rfind(".")
            if last_dot > 0:
                parent = bare[:last_dot]
                class_name = bare[last_dot + 1:]
                if class_name and class_name[0].isupper():
                    locals_var = parent + ".<locals>." + class_name
                    if (src, locals_var) in gt_edges_ws_set:
                        return True
        else:
            init_var = is_constructor(dst)
            if init_var and (src, init_var) in gt_edges_ws_set:
                return True
        return False

    ws_precision_matched = sum(1 for e in tool_ws_edges if tool_edge_in_gt(e[0], e[1]))
    ws_precision = ws_precision_matched / len(tool_ws_edges) * 100 if tool_ws_edges else 0

    # F1
    if ws_recall + ws_precision > 0:
        ws_f1 = 2 * (ws_recall * ws_precision) / (ws_recall + ws_precision)
    else:
        ws_f1 = 0

    # ── Print results ──
    print("\n" + "=" * 80)
    print("  COVERAGE EVALUATION RESULTS")
    print("=" * 80)

    print(f"\n  {'Metric':<50} {'Value':>10}")
    print("  " + "-" * 62)

    # Caller metrics
    print(f"  {'Ground truth callers':<50} {len(gt_callers):>10}")
    print(f"  {'Tool callers':<50} {len(tool_callers):>10}")
    print(f"  {'Matched callers':<50} {len(matched_callers):>10}")
    print(f"  {'Caller recall':<50} {caller_recall:>9.1f}%")

    # Edge metrics
    print()
    print(f"  {'Ground truth edges (all)':<50} {len(gt_edges_all_set):>10}")
    print(f"  {'Ground truth edges (workspace-only)':<50} {len(gt_edges_ws_set):>10}")
    print(f"  {'Tool edges (total)':<50} {len(tool_edges):>10}")
    print(f"  {'Tool edges (workspace callees)':<50} {len(tool_ws_edges):>10}")

    print()
    print(f"  {'Matched edges (all)':<50} {len(matched_all):>10}")
    print(f"  {'Total edge recall':<50} {total_recall:>9.1f}%")
    print(f"  {'Matched edges (workspace-only)':<50} {len(matched_ws):>10}")
    print(f"  {'Workspace edge recall  (MAIN METRIC)':<50} {ws_recall:>9.1f}%")
    print(f"  {'Workspace edge precision':<50} {ws_precision:>9.1f}%")
    print(f"  {'Workspace edge F1':<50} {ws_f1:>9.1f}%")

    print(f"\n  {'Tool-only edges (extra, not in ground truth)':<50} {len(tool_only_edges):>10}")

    # Per-caller table (workspace recall)
    callers_with_ws = [r for r in per_caller_results if r["ws_callees"] > 0]
    callers_100 = sum(1 for r in callers_with_ws if r["recall_ws_pct"] == 100.0)
    callers_0 = sum(1 for r in callers_with_ws if r["recall_ws_pct"] == 0.0)
    callers_partial = len(callers_with_ws) - callers_100 - callers_0

    print(f"\n  {'Callers with WS callees':<50} {len(callers_with_ws):>10}")
    print(f"  {'  100% recall':<50} {callers_100:>10}")
    print(f"  {'  Partial recall (0% < R < 100%)':<50} {callers_partial:>10}")
    print(f"  {'  0% recall (completely missed)':<50} {callers_0:>10}")

    # Per-caller breakdown table
    print("\n" + "=" * 80)
    print("  PER-CALLER BREAKDOWN (workspace edges)")
    print("=" * 80)
    print(f"  {'Caller':<62} {'WS':>4} {'Hit':>4} {'Recall':>7}")
    print("  " + "-" * 79)

    for r in callers_with_ws:
        marker = " " if r["recall_ws_pct"] == 100.0 else "*"
        print(
            f" {marker}{r['caller']:<62} {r['ws_callees']:>4} "
            f"{r['matched_ws']:>4} {r['recall_ws_pct']:>6.1f}%"
        )

    # Missed workspace edges
    total_missed_ws = sum(len(r["missed_ws"]) for r in per_caller_results)
    if total_missed_ws > 0:
        print("\n" + "=" * 80)
        print(f"  MISSED WORKSPACE EDGES ({total_missed_ws} edges)")
        print("=" * 80)
        for r in per_caller_results:
            for callee in r["missed_ws"]:
                print(f"    {r['caller']}")
                print(f"      -> {callee}")
    else:
        print("\n  [OK] No workspace edges were missed!")

    # ── Save detailed output ──
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

        # Per-caller CSV
        detail_path = os.path.join(output_dir, "coverage_per_caller.csv")
        with open(detail_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "caller", "total_callees", "ws_callees",
                "matched_total", "matched_ws",
                "recall_total_pct", "recall_ws_pct",
            ])
            for r in per_caller_results:
                writer.writerow([
                    r["caller"], r["total_callees"], r["ws_callees"],
                    r["matched_total"], r["matched_ws"],
                    f"{r['recall_total_pct']:.1f}",
                    f"{r['recall_ws_pct']:.1f}",
                ])

        # Missed edges CSV
        missed_path = os.path.join(output_dir, "coverage_missed_edges.csv")
        with open(missed_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["caller", "missed_callee", "is_workspace"])
            for r in per_caller_results:
                for callee in r["missed_ws"]:
                    writer.writerow([r["caller"], callee, "yes"])
                for callee in r["missed_all"]:
                    if callee not in r["missed_ws"]:
                        writer.writerow([r["caller"], callee, "no"])

        # Summary JSON (machine-readable)
        summary_path = os.path.join(output_dir, "coverage_summary.json")
        summary = {
            "dynapyt_file": os.path.basename(dynapyt_path),
            "graph_csv_file": os.path.basename(graph_csv_path),
            "workspace_prefix": prefix,
            "workspace_packages": sorted(ws_packages),
            "gt_callers": len(gt_callers),
            "tool_callers": len(tool_callers),
            "matched_callers": len(matched_callers),
            "caller_recall_pct": round(caller_recall, 2),
            "gt_edges_all": len(gt_edges_all_set),
            "gt_edges_ws": len(gt_edges_ws_set),
            "tool_edges": len(tool_edges),
            "tool_ws_edges": len(tool_ws_edges),
            "matched_edges_all": len(matched_all),
            "matched_edges_ws": len(matched_ws),
            "total_edge_recall_pct": round(total_recall, 2),
            "ws_edge_recall_pct": round(ws_recall, 2),
            "ws_edge_precision_pct": round(ws_precision, 2),
            "ws_edge_f1_pct": round(ws_f1, 2),
            "tool_only_edges": len(tool_only_edges),
            "callers_100pct_ws_recall": callers_100,
            "callers_partial_ws_recall": callers_partial,
            "callers_0pct_ws_recall": callers_0,
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        print(f"\n  Detailed results saved to: {output_dir}/")
        print(f"    - coverage_per_caller.csv")
        print(f"    - coverage_missed_edges.csv")
        print(f"    - coverage_summary.json")

    return {
        "caller_recall": caller_recall,
        "total_edge_recall": total_recall,
        "workspace_edge_recall": ws_recall,
        "workspace_edge_precision": ws_precision,
        "workspace_edge_f1": ws_f1,
    }


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Evaluate profiler call-graph coverage against DynaPyt ground truth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    python evaluate.py dynapyt.json graph.csv
    python evaluate.py dynapyt.json graph.csv -o results/
    python -m cintent.evaluate dynapyt.json graph.csv -o results/
""",
    )
    parser.add_argument("dynapyt_json", help="Path to DynaPyt ground-truth JSON")
    parser.add_argument(
        "graph_csv",
        help="Path to tool's graph CSV (must have src_fq_name, dst_fq_name columns)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="Directory to save detailed CSV/JSON results",
        default=None,
    )
    args = parser.parse_args()
    evaluate(args.dynapyt_json, args.graph_csv, args.output_dir)


if __name__ == "__main__":
    main()
