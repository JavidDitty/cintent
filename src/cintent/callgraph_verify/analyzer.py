#!/usr/bin/env python3
"""
Verify DynaPyt-style call-graph edges against a local repository for manual review.

Input JSON format:
    { "<caller_fqn>": ["<callee_fqn>", ...], ... }

Typical DynaPyt callers include a workspace prefix:
    .home.runner.work.repo.repo.pkg.module.Func
This module auto-detects and strips that prefix before matching symbols in a local repo.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "build",
    "dist",
    "node_modules",
}


@dataclass
class Definition:
    canonical_fqn: str
    file_path: str
    line: int
    end_line: int
    kind: str


@dataclass
class CallSite:
    line: int
    expr: str
    leaf: str
    chain: str | None


@dataclass
class MatchResult:
    score: int
    label: str
    callsites: list[CallSite]


@dataclass
class EdgeVerification:
    caller: str
    callee: str
    status: str
    evidence_score: int
    evidence_label: str
    caller_found: bool
    caller_file: str
    caller_line: int
    caller_kind: str
    caller_candidates: int
    callee_found: bool
    callee_file: str
    callee_line: int
    callee_kind: str
    callee_candidates: int
    caller_total_callsites: int
    matched_callsites: list[dict[str, str | int]]


def group_verifications_by_caller(verifications: list[EdgeVerification]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}

    for v in verifications:
        if v.caller not in grouped:
            grouped[v.caller] = {
                "caller": v.caller,
                "caller_found": v.caller_found,
                "caller_file": v.caller_file,
                "caller_line": v.caller_line,
                "caller_kind": v.caller_kind,
                "caller_candidates": v.caller_candidates,
                "caller_total_callsites": v.caller_total_callsites,
                "status_counts": Counter(),
                "callees": [],
            }

        grouped[v.caller]["status_counts"][v.status] += 1
        grouped[v.caller]["callees"].append(
            {
                "callee": v.callee,
                "status": v.status,
                "evidence_score": v.evidence_score,
                "evidence_label": v.evidence_label,
                "callee_found": v.callee_found,
                "callee_file": v.callee_file,
                "callee_line": v.callee_line,
                "callee_kind": v.callee_kind,
                "callee_candidates": v.callee_candidates,
                "matched_callsites": v.matched_callsites,
            }
        )

    callers = []
    for _, caller_obj in sorted(grouped.items(), key=lambda x: x[0]):
        caller_obj["callees"] = sorted(caller_obj["callees"], key=lambda c: c["callee"])
        caller_obj["status_counts"] = dict(caller_obj["status_counts"])
        caller_obj["total_callees"] = len(caller_obj["callees"])
        callers.append(caller_obj)

    return callers


def normalize_fqn(name: str) -> str:
    return name.lstrip(".").replace(".<locals>.", ".")


def split_callee_parts(callee: str) -> tuple[str, str, str]:
    parts = normalize_fqn(callee).split(".")
    leaf = parts[-1] if parts else ""
    constructor_name = parts[-2] if len(parts) >= 2 and leaf == "__init__" else ""
    return normalize_fqn(callee), leaf, constructor_name


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _dotted_name(node.value)
        if left:
            return f"{left}.{node.attr}"
        return node.attr
    return None


class _CallCollector(ast.NodeVisitor):
    def __init__(self, source: str):
        self.source = source
        self.callsites: list[CallSite] = []

    def visit_Call(self, node: ast.Call) -> None:
        chain = _dotted_name(node.func)
        expr = ast.get_source_segment(self.source, node.func) or "<unknown>"
        leaf = ""
        if chain:
            leaf = chain.split(".")[-1]
        elif isinstance(node.func, ast.Attribute):
            leaf = node.func.attr
        elif isinstance(node.func, ast.Name):
            leaf = node.func.id
        self.callsites.append(
            CallSite(
                line=getattr(node, "lineno", -1),
                expr=expr.strip(),
                leaf=leaf,
                chain=chain,
            )
        )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None


class RepoSymbolIndex:
    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        self.modules: set[str] = set()
        self.definitions_by_key: dict[str, list[Definition]] = defaultdict(list)
        self.callsites_by_canonical: dict[str, list[CallSite]] = defaultdict(list)

    def build(self) -> None:
        for file_path in self._iter_python_files():
            module = self._module_from_path(file_path)
            if not module:
                continue
            self.modules.add(module)
            self._index_file(file_path, module)

    @property
    def top_modules(self) -> set[str]:
        return {m.split(".", 1)[0] for m in self.modules if m}

    def resolve_definition(self, fqn: str) -> tuple[Definition | None, int]:
        keys = self._candidate_lookup_keys(fqn)
        for key in keys:
            candidates = self.definitions_by_key.get(key)
            if candidates:
                # Stable preference: earliest line in shortest path.
                ordered = sorted(candidates, key=lambda d: (len(d.file_path), d.line))
                return ordered[0], len(ordered)
        return None, 0

    def get_callsites(self, canonical_fqn: str) -> list[CallSite]:
        return self.callsites_by_canonical.get(canonical_fqn, [])

    def _candidate_lookup_keys(self, fqn: str) -> list[str]:
        clean = normalize_fqn(fqn)
        keys = [fqn, clean]
        if fqn.endswith(".__init__"):
            bare = fqn[: -len(".__init__")]
            keys.append(bare)
            keys.append(normalize_fqn(bare))
        else:
            maybe_parts = clean.split(".")
            if maybe_parts and maybe_parts[-1] and maybe_parts[-1][0].isupper():
                keys.append(clean + ".__init__")
        # Deduplicate while preserving order.
        return list(dict.fromkeys(k for k in keys if k))

    def _iter_python_files(self) -> Iterable[str]:
        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(root, name)

    def _module_from_path(self, file_path: str) -> str | None:
        rel = os.path.relpath(file_path, self.repo_root)
        if rel.startswith(".."):
            return None
        parts = Path(rel).parts
        if not parts:
            return None

        path_parts = list(parts)
        if path_parts[0] in {"src", "lib"} and len(path_parts) > 1:
            path_parts = path_parts[1:]

        if path_parts[-1] == "__init__.py":
            mod_parts = path_parts[:-1]
        else:
            mod_parts = path_parts[:-1] + [os.path.splitext(path_parts[-1])[0]]

        if not mod_parts:
            return None
        return ".".join(mod_parts)

    def _index_file(self, file_path: str, module: str) -> None:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=file_path)
        except (UnicodeDecodeError, SyntaxError):
            return

        self._walk_definitions(tree.body, module, file_path, source, scopes=[])

    def _walk_definitions(
        self,
        nodes: list[ast.stmt],
        module: str,
        file_path: str,
        source: str,
        scopes: list[tuple[str, str]],
    ) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                chain = scopes + [(node.name, "class")]
                canonical_qual = ".".join(name for name, _ in chain)
                runtime_qual = self._runtime_qual(chain)
                canonical_fqn = f"{module}.{canonical_qual}"

                kind = "class" if not scopes else "nested_class"
                definition = Definition(
                    canonical_fqn=canonical_fqn,
                    file_path=file_path,
                    line=getattr(node, "lineno", -1),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", -1)),
                    kind=kind,
                )
                self._register_definition(definition, module, canonical_qual, runtime_qual)
                self._walk_definitions(node.body, module, file_path, source, chain)
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chain = scopes + [(node.name, "function")]
                canonical_qual = ".".join(name for name, _ in chain)
                runtime_qual = self._runtime_qual(chain)
                canonical_fqn = f"{module}.{canonical_qual}"

                parent_kind = scopes[-1][1] if scopes else ""
                if parent_kind == "class":
                    kind = "method"
                elif not scopes:
                    kind = "function"
                else:
                    kind = "nested_function"

                definition = Definition(
                    canonical_fqn=canonical_fqn,
                    file_path=file_path,
                    line=getattr(node, "lineno", -1),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", -1)),
                    kind=kind,
                )
                self._register_definition(definition, module, canonical_qual, runtime_qual)
                self.callsites_by_canonical[canonical_fqn].extend(self._extract_calls(node, source))
                self._walk_definitions(node.body, module, file_path, source, chain)

    def _register_definition(
        self,
        definition: Definition,
        module: str,
        canonical_qual: str,
        runtime_qual: str,
    ) -> None:
        keys = {
            definition.canonical_fqn,
            normalize_fqn(definition.canonical_fqn),
            f"{module}.{canonical_qual}",
            f"{module}.{runtime_qual}",
            normalize_fqn(f"{module}.{runtime_qual}"),
        }
        for key in keys:
            if key:
                self.definitions_by_key[key].append(definition)

    @staticmethod
    def _runtime_qual(chain: list[tuple[str, str]]) -> str:
        out: list[str] = []
        prev_kind = ""
        for name, kind in chain:
            if out and prev_kind == "function":
                out.append("<locals>")
            out.append(name)
            prev_kind = kind
        return ".".join(out)

    @staticmethod
    def _extract_calls(node: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> list[CallSite]:
        collector = _CallCollector(source)
        for stmt in node.body:
            collector.visit(stmt)
        return collector.callsites


def detect_dynapyt_prefix(keys: Iterable[str], repo_top_modules: set[str]) -> str:
    if not repo_top_modules:
        return ""

    prefix_counter: Counter[str] = Counter()
    for raw_key in keys:
        parts = raw_key.lstrip(".").split(".")
        for idx, part in enumerate(parts):
            if part in repo_top_modules:
                if idx == 0:
                    prefix = ""
                else:
                    prefix = "." + ".".join(parts[:idx]) + "."
                prefix_counter[prefix] += 1
                break

    if not prefix_counter:
        return ""
    best, _ = prefix_counter.most_common(1)[0]
    return best


def normalize_dynapyt_caller(raw_caller: str, prefix: str) -> str:
    caller = raw_caller.lstrip(".")
    clean_prefix = prefix.strip(".")
    if clean_prefix and caller.startswith(clean_prefix + "."):
        caller = caller[len(clean_prefix) + 1 :]
    elif clean_prefix and caller.startswith(clean_prefix):
        caller = caller[len(clean_prefix) :].lstrip(".")
    return caller.lstrip(".")


def assess_match(callsites: list[CallSite], callee: str, max_callsites: int) -> MatchResult:
    normalized_callee, callee_leaf, constructor_name = split_callee_parts(callee)
    best_score = 0
    best_label = "none"
    scored: list[tuple[int, str, CallSite]] = []

    for cs in callsites:
        score = 0
        label = "none"
        cs_chain = normalize_fqn(cs.chain) if cs.chain else ""

        if cs_chain and cs_chain == normalized_callee:
            score, label = 4, "exact_fqn"
        elif cs_chain and (
            normalized_callee.endswith("." + cs_chain) or cs_chain.endswith("." + normalized_callee)
        ):
            score, label = 3, "suffix_fqn"
        elif constructor_name and cs_chain and (
            cs_chain == constructor_name or cs_chain.endswith("." + constructor_name)
        ):
            score, label = 2, "constructor_name"
        elif cs.leaf and (
            cs.leaf == callee_leaf or (constructor_name and cs.leaf == constructor_name)
        ):
            score, label = 1, "leaf_name"

        if score > 0:
            scored.append((score, label, cs))
            if score > best_score:
                best_score = score
                best_label = label

    best_callsites: list[CallSite] = []
    if best_score > 0:
        for score, _, cs in scored:
            if score == best_score:
                best_callsites.append(cs)
        best_callsites = sorted(best_callsites, key=lambda c: c.line)[:max_callsites]

    return MatchResult(score=best_score, label=best_label, callsites=best_callsites)


def load_dynapyt_json(path: str) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object mapping caller->callees.")
    cleaned: dict[str, list[str]] = {}
    for caller, callees in data.items():
        if not isinstance(caller, str):
            continue
        if isinstance(callees, list):
            cleaned[caller] = [c for c in callees if isinstance(c, str) and c.strip()]
    return cleaned


def build_edge_list(dynapyt: dict[str, list[str]], prefix: str) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for raw_caller, callee_list in dynapyt.items():
        caller = normalize_dynapyt_caller(raw_caller, prefix)
        if not caller:
            continue
        for callee in callee_list:
            callee = callee.strip()
            if not callee:
                continue
            edges.add((caller, callee))
    return sorted(edges)


def analyze_callgraph(
    dynapyt_json_path: str,
    repo_root: str,
    output_dir: str | None = None,
    max_callsites: int = 5,
) -> dict[str, object]:
    dynapyt_path = os.path.abspath(dynapyt_json_path)
    repo_root = os.path.abspath(repo_root)
    if not os.path.isfile(dynapyt_path):
        raise FileNotFoundError(f"DynaPyt JSON not found: {dynapyt_path}")
    if not os.path.isdir(repo_root):
        raise FileNotFoundError(f"Repository path not found: {repo_root}")

    dynapyt = load_dynapyt_json(dynapyt_path)

    index = RepoSymbolIndex(repo_root)
    index.build()

    prefix = detect_dynapyt_prefix(dynapyt.keys(), index.top_modules)
    edges = build_edge_list(dynapyt, prefix)

    verifications: list[EdgeVerification] = []
    status_counter: Counter[str] = Counter()

    for caller, callee in edges:
        caller_def, caller_candidates = index.resolve_definition(caller)
        callee_def, callee_candidates = index.resolve_definition(callee)

        callsites = index.get_callsites(caller_def.canonical_fqn) if caller_def else []
        match = assess_match(callsites, callee, max_callsites=max_callsites) if callsites else MatchResult(0, "none", [])

        if not caller_def:
            status = "missing_caller"
        elif match.score >= 3:
            status = "verified_static"
        elif match.score == 2:
            status = "likely_static"
        else:
            status = "needs_manual"

        status_counter[status] += 1

        matched_callsites = [
            {"line": cs.line, "expr": cs.expr, "leaf": cs.leaf, "chain": cs.chain or ""}
            for cs in match.callsites
        ]

        verifications.append(
            EdgeVerification(
                caller=caller,
                callee=callee,
                status=status,
                evidence_score=match.score,
                evidence_label=match.label,
                caller_found=caller_def is not None,
                caller_file=caller_def.file_path if caller_def else "",
                caller_line=caller_def.line if caller_def else -1,
                caller_kind=caller_def.kind if caller_def else "",
                caller_candidates=caller_candidates,
                callee_found=callee_def is not None,
                callee_file=callee_def.file_path if callee_def else "",
                callee_line=callee_def.line if callee_def else -1,
                callee_kind=callee_def.kind if callee_def else "",
                callee_candidates=callee_candidates,
                caller_total_callsites=len(callsites),
                matched_callsites=matched_callsites,
            )
        )

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(dynapyt_path), "callgraph_verify")
    os.makedirs(output_dir, exist_ok=True)

    details_json_path = os.path.join(output_dir, "edge_verification.json")
    details_flat_json_path = os.path.join(output_dir, "edge_verification_flat.json")
    details_csv_path = os.path.join(output_dir, "edge_verification.csv")
    summary_json_path = os.path.join(output_dir, "verification_summary.json")
    report_md_path = os.path.join(output_dir, "MANUAL_REVIEW.md")

    grouped_verifications = group_verifications_by_caller(verifications)
    with open(details_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total_callers": len(grouped_verifications),
                "total_edges": len(verifications),
                "callers": grouped_verifications,
            },
            f,
            indent=2,
        )

    with open(details_flat_json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(v) for v in verifications], f, indent=2)

    with open(details_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "caller",
                "callee",
                "status",
                "evidence_score",
                "evidence_label",
                "caller_found",
                "caller_file",
                "caller_line",
                "caller_kind",
                "callee_found",
                "callee_file",
                "callee_line",
                "callee_kind",
                "caller_total_callsites",
                "matched_callsites",
            ]
        )
        for v in verifications:
            callsites = " | ".join(f"{c['line']}:{c['expr']}" for c in v.matched_callsites)
            writer.writerow(
                [
                    v.caller,
                    v.callee,
                    v.status,
                    v.evidence_score,
                    v.evidence_label,
                    v.caller_found,
                    v.caller_file,
                    v.caller_line,
                    v.caller_kind,
                    v.callee_found,
                    v.callee_file,
                    v.callee_line,
                    v.callee_kind,
                    v.caller_total_callsites,
                    callsites,
                ]
            )

    callers_total = len({v.caller for v in verifications})
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dynapyt_json": dynapyt_path,
        "repo_root": repo_root,
        "detected_dynapyt_prefix": prefix,
        "repo_modules_indexed": len(index.modules),
        "repo_top_modules": sorted(index.top_modules),
        "total_callers": callers_total,
        "total_edges": len(verifications),
        "status_counts": dict(status_counter),
        "edges_with_callee_found_in_repo": sum(1 for v in verifications if v.callee_found),
    }
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write("# Call Graph Manual Review\n\n")
        f.write(f"- DynaPyt JSON: `{dynapyt_path}`\n")
        f.write(f"- Repository root: `{repo_root}`\n")
        f.write(f"- Detected prefix: `{prefix}`\n")
        f.write(f"- Total edges: **{len(verifications)}**\n")
        f.write(f"- Verified static: **{status_counter.get('verified_static', 0)}**\n")
        f.write(f"- Likely static: **{status_counter.get('likely_static', 0)}**\n")
        f.write(f"- Needs manual: **{status_counter.get('needs_manual', 0)}**\n")
        f.write(f"- Missing caller: **{status_counter.get('missing_caller', 0)}**\n\n")
        f.write("## Needs Manual Review\n\n")
        manual_rows = [v for v in verifications if v.status in {"needs_manual", "missing_caller"}]
        if not manual_rows:
            f.write("No manual-review edges.\n")
        else:
            for v in manual_rows[:500]:
                f.write(f"- `{v.caller}` -> `{v.callee}`\n")
                if v.caller_found:
                    f.write(f"  - caller: `{v.caller_file}:{v.caller_line}`\n")
                else:
                    f.write("  - caller: not found in repository\n")
                if v.callee_found:
                    f.write(f"  - callee: `{v.callee_file}:{v.callee_line}`\n")
                else:
                    f.write("  - callee: not found in repository\n")
                if v.matched_callsites:
                    hints = ", ".join(f"{c['line']}:{c['expr']}" for c in v.matched_callsites)
                    f.write(f"  - call hints: `{hints}`\n")
                f.write("\n")

    return {
        "summary": summary,
        "output_dir": os.path.abspath(output_dir),
        "files": {
            "summary_json": summary_json_path,
            "details_json": details_json_path,
            "details_flat_json": details_flat_json_path,
            "details_csv": details_csv_path,
            "manual_report_md": report_md_path,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate DynaPyt call-graph edges against a local repository and "
            "generate a manual-verification report."
        )
    )
    parser.add_argument("dynapyt_json", help="Path to graph_df_filtered_*_dynapyt.json")
    parser.add_argument("repo_root", help="Path to local repository root")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for generated reports (default: sibling callgraph_verify/)",
    )
    parser.add_argument(
        "--max-callsites",
        type=int,
        default=5,
        help="Maximum matched callsites to keep per edge (default: 5)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = analyze_callgraph(
        dynapyt_json_path=args.dynapyt_json,
        repo_root=args.repo_root,
        output_dir=args.output_dir,
        max_callsites=max(1, int(args.max_callsites)),
    )
    summary = result["summary"]
    print("Call graph verification complete.")
    print(f"Output directory: {result['output_dir']}")
    print(f"Total edges: {summary['total_edges']}")
    print(f"Verified static: {summary['status_counts'].get('verified_static', 0)}")
    print(f"Likely static: {summary['status_counts'].get('likely_static', 0)}")
    print(f"Needs manual: {summary['status_counts'].get('needs_manual', 0)}")
    print(f"Missing caller: {summary['status_counts'].get('missing_caller', 0)}")


if __name__ == "__main__":
    main()
