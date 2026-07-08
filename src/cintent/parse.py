from argparse import ArgumentParser, Namespace
import glob
from io import StringIO
import os
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pandas as pd
from pandas import DataFrame
from radon.complexity import cc_rank, cc_visit
from radon.metrics import h_visit, mi_visit
from tqdm import tqdm


def parse_archive(archive_path: str, out_paths: dict[str, str]) -> None:
    """Parse a CIntent archive"""
    assert 'graph' in out_paths
    assert 'metadata' in out_paths
    assert 'sandwich' in out_paths

    files = {
        'execsnoop': [],
        'graph': [],
        'metadata': [],
        'opensnoop': [],
        'sandwich': [],
    }
    
    # Parse Files
    with ZipFile(archive_path, 'r') as archive:
        for filename in tqdm(archive.namelist(), desc=f'Parsing {archive_path}'):
            # Skip the item if it is a directory
            info = archive.getinfo(filename)
            if info.is_dir():
                continue
            
            # Load the file
            try:
                file = archive.read(filename).decode()
            except UnicodeDecodeError:
                continue

            # Parse the file
            if filename.endswith('.execsnoop.txt'):
                pass

            elif filename.endswith('.gethostname.txt'):
                pass

            elif filename.endswith('.metadata.txt'):
                metadata = {}
                for line in file.splitlines():
                    key, value = line.split(' = ', maxsplit=1)
                    metadata[key] = value
                files['metadata'].append(metadata)

            elif filename.endswith('.opensnoop.txt'):
                pass
            
            elif filename.endswith('.setprofile.graph.csv'):
                timestamp_id, step_id = filename.split('.')[:2]
                try:
                    graph_df = pd.read_csv(StringIO(file), dtype='string')
                except pd.errors.EmptyDataError:
                    continue
                graph_df.insert(loc=0, column='timestamp_id', value=timestamp_id)
                graph_df.insert(loc=1, column='step_id', value=step_id)
                graph_df = graph_df.astype('string')
                files['graph'].append(graph_df)

            elif filename.endswith('.setprofile.sandwich.csv'):
                timestamp_id, step_id = filename.split('.')[:2]
                try:
                    sandwich_df = pd.read_csv(StringIO(file), dtype='string')
                except pd.errors.EmptyDataError:
                    continue
                sandwich_df.insert(loc=0, column='timestamp_id', value=timestamp_id)
                sandwich_df.insert(loc=1, column='step_id', value=step_id)
                sandwich_df = sandwich_df.astype('string')
                sandwich_df["is_external"] = sandwich_df["is_external"].map({'True': True, 'False': False}).astype('bool')
                files['sandwich'].append(sandwich_df)

            elif filename.endswith('.tcplife.txt'):
                pass
    
    # Compile Parsed Files
    if not files['metadata']:
        print(f'WARNING: "{archive_path}" does not have metadata file(s) and will be skipped!')
        return
    
    repo_id = files['metadata'][0]['repository']
    job_id = files['metadata'][0]['job_id']
    files['metadata'] = DataFrame(files['metadata'])

    files['execsnoop'] = DataFrame()

    if files['graph']:
        files['graph'] = pd.concat([df for df in files['graph'] if not df.empty], ignore_index=True)
        files['graph'].insert(loc=0, column='job_id', value=job_id)
        files['graph'].insert(loc=0, column='repo_id', value=repo_id)
        files['graph'] = files['graph'][files['graph']['src_id'] != 'src_id']
    else:
        files['graph'] = DataFrame()

    files['opensnoop'] = DataFrame()
    
    if files['sandwich']:
        files['sandwich'] = pd.concat([df for df in files['sandwich'] if not df.empty], ignore_index=True)
        files['sandwich'].insert(loc=0, column='job_id', value=job_id)
        files['sandwich'].insert(loc=0, column='repo_id', value=repo_id)
        files['sandwich'] = files['sandwich'][files['sandwich']['id'] != 'id']
    else:
        files['sandwich'] = DataFrame()

    # Filter and Dump Sandwich and Graph Files
    is_file_empty = lambda path: os.path.isfile(path) and os.path.getsize(path) == 0

    if not files['graph'].empty and not files['sandwich'].empty:
        id_to_external = {}
        for row in files['sandwich'].itertuples():
            key = (str(row.repo_id), str(row.job_id), str(row.step_id), str(row.timestamp_id), str(row.id))
            id_to_external[key] = row.is_external
        
        def filter_graph(row):
            src_key = (str(row.repo_id), str(row.job_id), str(row.step_id), str(row.timestamp_id), str(row.src_id)) 
            return src_key in id_to_external and not id_to_external[src_key]
        
        files['graph'] = files['graph'][files['graph'].apply(filter_graph, axis=1)]

        graph_ids = set()
        for row in files['graph'].itertuples():
            src_key = (str(row.repo_id), str(row.job_id), str(row.step_id), str(row.timestamp_id), str(row.src_id))
            dst_key = (str(row.repo_id), str(row.job_id), str(row.step_id), str(row.timestamp_id), str(row.dst_id))
            graph_ids.add(src_key)
            graph_ids.add(dst_key)

        def filter_sandwich(row):
            key = (str(row.repo_id), str(row.job_id), str(row.step_id), str(row.timestamp_id), str(row.id)) 
            return key in graph_ids

        files['sandwich'] = files['sandwich'][files['sandwich'].apply(filter_sandwich, axis=1)]

        files['graph'].to_csv(out_paths['graph'], mode='a', index=False, header=is_file_empty(out_paths['graph']))
        files['sandwich'].to_csv(out_paths['sandwich'], mode='a', index=False, header=is_file_empty(out_paths['sandwich']))
    
    files['metadata'].to_csv(out_paths['metadata'], mode='a', index=False, header=is_file_empty(out_paths['metadata']))


if __name__ == '__main__':
    # Parse the CLI arguments
    parser = ArgumentParser(description='Parse a CIntent archive')
    parser.add_argument('archive_dir', type=os.path.abspath, help='path to a directory of CIMonitor archive(s)')
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
    for archive_path in archive_paths:
        parse_archive(archive_path=archive_path, out_paths=out_paths)
