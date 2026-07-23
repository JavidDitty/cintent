from argparse import ArgumentParser, Namespace
import csv
from csv import DictReader
import glob
from io import StringIO, TextIOWrapper
import os
from pathlib import Path
import re
import sys
from typing import Any
from zipfile import ZipFile

import pandas as pd
from pandas import DataFrame
from radon.complexity import cc_rank, cc_visit
from radon.metrics import h_visit, mi_visit
from tqdm import tqdm


def parse_archive(archive_path: str, out_paths: dict[str, str], functions_path: str | None = None) -> None:
    """Parse a CIntent archive"""
    assert 'graph' in out_paths
    assert 'metadata' in out_paths
    assert 'sandwich' in out_paths

    csv.field_size_limit(sys.maxsize)

    functions_df = None
    if functions_path:
        functions_df = pd.read_csv(functions_path)
        functions_df = functions_df[["path", "line", "name", "code"]]
    
    with ZipFile(archive_path, 'r') as archive:
        metadata_paths = sorted(name for name in archive.namelist() if name.endswith(".metadata.txt"))
        metadata_dfs = []
        for metadata_path in metadata_paths:
            metadata_df = []
            with archive.open(metadata_path, "r") as file:
                wrapper = TextIOWrapper(file, encoding="utf-8", newline="")
                metadata = {line.split(' = ', maxsplit=1)[0]: line.split(' = ', maxsplit=1)[1] for line in wrapper.read().splitlines()}
                metadata_df.append(metadata)
            metadata_df = DataFrame(metadata_df)
            metadata_dfs.append(metadata_df)

        sandwich_paths = sorted(name for name in archive.namelist() if name.endswith(".setprofile.sandwich.csv"))
        graph_paths = sorted(name for name in archive.namelist() if name.endswith(".setprofile.graph.csv"))
        sandwich_dfs, graph_dfs = [], []
        for sandwich_path, graph_path in zip(sandwich_paths, graph_paths):
            assert sandwich_path.split('.')[:2] == graph_path.split('.')[:2], "Sandwich/Graph Mismatch!"
            timestamp_id, step_id = sandwich_path.split('.')[:2]

            EXTERNAL_PATHS = [
                "/site-packages/",
                "/opt/hostedtoolcache/",
                "/miniconda/envs/",
                ".pixi/",
            ]
            is_external = {}
            to_bool = {"True": True, "False": False}
            with archive.open(sandwich_path, "r") as file:
                wrapper = TextIOWrapper(file, encoding="utf-8", newline="")
                reader = DictReader(wrapper)
                for row in reader:
                    if row["is_external"] not in to_bool:
                        continue
                    if any(path in row["path"] for path in EXTERNAL_PATHS): # catch false-negatives in CIMonitor
                        row["is_external"] = "True"
                    is_external[row["id"]] = to_bool[row["is_external"]]
            
            graph_df = []
            with archive.open(graph_path, "r") as file:
                wrapper = TextIOWrapper(file, encoding="utf-8", newline="")
                reader = DictReader(wrapper)
                for row in reader:
                    if row["src_id"] == "src_id" or row["dst_id"] == "dst_id":
                        continue
                    if not is_external[row["src_id"]] or not is_external[row["dst_id"]]:
                        graph_df.append(row)
            graph_df = DataFrame(graph_df, columns=["src_id", "dst_id", "count", "duration_ns"])
            graph_df.insert(loc=0, column='timestamp_id', value=timestamp_id)
            graph_df.insert(loc=1, column='step_id', value=step_id)
            graph_dfs.append(graph_df)
            
            sandwich_df = []
            if not graph_df.empty:
                graph_ids = pd.concat([graph_df["src_id"], graph_df["dst_id"]]).unique()
                with archive.open(sandwich_path, "r") as file:
                    wrapper = TextIOWrapper(file, encoding="utf-8", newline="")
                    reader = DictReader(wrapper)
                    for row in reader:
                        if row["id"] in graph_ids:
                            if functions_df is not None: 
                                match = re.match(r"(/home/runner/work/.+?/.+?/)(.+)", row["path"])
                                if match:
                                    relpath = match.group(2)
                                    row["path"] = relpath
                                entry = functions_df[
                                    (functions_df["path"] == row["path"]) & 
                                    (functions_df["line"].astype(str) == str(row["line"])) & 
                                    (functions_df["name"] == row["name"])
                                ]
                                row["code"] = entry.iloc[0]["code"] if not entry.empty else None
                            sandwich_df.append(row)
            sandwich_df = DataFrame(sandwich_df, columns=["id", "name", "path", "line", "count", "duration_ns", "is_external", "code"])
            sandwich_df.insert(loc=0, column='timestamp_id', value=timestamp_id)
            sandwich_df.insert(loc=1, column='step_id', value=step_id)
            sandwich_dfs.append(sandwich_df)
            
            del is_external
            del graph_df
            del sandwich_df

    is_file_empty = lambda path: os.path.isfile(path) and os.path.getsize(path) == 0

    if metadata_dfs:
        metadata_dfs = pd.concat(metadata_dfs, ignore_index=True).drop_duplicates()
        repo_id = metadata_dfs['repository'].iloc[0]
        job_id = metadata_dfs['job_id'].iloc[0]
        metadata_dfs.to_csv(out_paths['metadata'], mode='a', index=False, header=is_file_empty(out_paths['metadata']))
        
    if graph_dfs:
        graph_dfs = pd.concat(graph_dfs, ignore_index=True).drop_duplicates()
        graph_dfs.insert(loc=0, column='repo_id', value=repo_id)
        graph_dfs.insert(loc=1, column='job_id', value=job_id)
        graph_dfs.to_csv(out_paths['graph'], mode='a', index=False, header=is_file_empty(out_paths['graph']))
    
    if sandwich_dfs:
        sandwich_dfs = pd.concat(sandwich_dfs, ignore_index=True).drop_duplicates()
        sandwich_dfs.insert(loc=0, column='repo_id', value=repo_id)
        sandwich_dfs.insert(loc=1, column='job_id', value=job_id)
        sandwich_dfs.to_csv(out_paths['sandwich'], mode='a', index=False, header=is_file_empty(out_paths['sandwich']))


if __name__ == '__main__':
    # Parse the CLI arguments
    parser = ArgumentParser(description='Parse a CIntent archive')
    parser.add_argument('archive_dir', type=os.path.abspath, help='path to a directory of CIMonitor archive(s)')
    parser.add_argument('--functions_dir', type=os.path.abspath, default=None, help='path to a directory of pyfunctions logs')
    args = parser.parse_args()
    
    # Find any archives in the archive directory
    pattern = os.path.join(args.archive_dir, '**', '*.zip')
    archive_paths = sorted(glob.glob(pattern, recursive=True))

    # Define Out Paths
    out_paths = {
        'graph': os.path.join(args.archive_dir, 'graph.csv'),
        'metadata': os.path.join(args.archive_dir, 'metadata.csv'),
        'sandwich': os.path.join(args.archive_dir, 'sandwich.csv'),
    }

    with open(out_paths['graph'], 'w') as file:
        pass
    with open(out_paths['metadata'], 'w') as file:
        pass
    with open(out_paths['sandwich'], 'w') as file:
        pass

    # Parse the archives
    progress_bar = tqdm(archive_paths)
    for archive_path in progress_bar:
        progress_bar.set_description(f'Parsing "{Path(archive_path).parent.stem}"')

        functions_path = None
        if args.functions_dir:
            repository = Path(archive_path).parent.stem
            functions_path = os.path.join(args.functions_dir, f"{repository}.functions.csv") 
        
        parse_archive(archive_path=archive_path, out_paths=out_paths, functions_path=functions_path)
