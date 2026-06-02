from argparse import ArgumentParser, Namespace
import csv
import glob
from io import StringIO
import json
import os
from pathlib import Path
import re
from typing import Any
from zipfile import ZipFile

import pandas as pd
from pandas import DataFrame
from tqdm import tqdm

from cintent.speedscope import to_csv
from cintent.trace import TraceProfile


def parse_archive(archive_path: str, out_dir: str) -> dict[str, Any]:
    """Parse a CIntent archive"""
    files = {
        'metadata': [],
        'execsnoop': [],
        'opensnoop': [],
        'functions': DataFrame({
            'name': [], 
            'fq_name': [], 
            'header': [], 
            'docstring': [], 
            'class_name': [], 
            'class_docstring': [], 
            'path': [], 
            'line': [],
        }),
        'sandwich': [],
        'graph': [],
    }
    with ZipFile(archive_path, 'r') as archive:
        function_filename = None
        for filename in archive.namelist():
            if '.functions.' in filename:
                function_filename = filename
                break
        if function_filename is None:
            raise ValueError(f'No .functions. file found in archive: {archive_path}')
        function_str = archive.read(function_filename).decode()
        try:
            files['functions'] = pd.read_csv(StringIO(function_str))
        except pd.errors.EmptyDataError:
            return files

        for filename in tqdm(archive.namelist(), desc=f'Processing {archive_path}'):
            file_info = archive.getinfo(filename)
            if not file_info.is_dir():
                try:
                    file_str = archive.read(filename).decode()
                except UnicodeDecodeError:
                    continue   # skip binary files (e.g. .pyc bytecode)
                if '.functions.' not in filename:
                    parts = filename.split('.')
                    # Expect exactly 4 parts: timestamp.step_id.file_type.extension
                    # Skip files that don't match (e.g. uprobe_2887.pid)
                    if len(parts) != 4:
                        continue
                    timestamp, step_id, file_type, extension = parts
                    match file_type:
                        case 'metadata':
                            metadata = {}
                            for line in file_str.splitlines():
                                key, value = line.split(' = ', maxsplit=1)
                                metadata[key] = value
                            if 'step_id' not in metadata:
                                metadata['step_id'] = step_id
                            files['metadata'].append(metadata)
                        # case 'execsnoop':
                        #     pattern = r'^(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)$'
                        #     for line in file_str.splitlines()[2:]:
                        #         time_s, pcomm, pid, ppid, ret, args = re.match(pattern, line).groups()
                        #         files['execsnoop'].append({
                        #             'step_id': step_id,
                        #             'time_s': time_s,
                        #             'pcomm': pcomm,
                        #             'pid': pid,
                        #             'ppid': ppid,
                        #             'ret': ret,
                        #             'args': args,
                        #         })
                        # case 'opensnoop':
                        #     pattern = r'^(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)$'
                        #     for line in file_str.splitlines()[2:]:
                        #         time_s, pid, comm, fd, err, flags, path = re.match(pattern, line).groups()
                        #         files['opensnoop'].append({
                        #             'step_id': step_id,
                        #             'time_s': time_s,
                        #             'pid': pid,
                        #             'comm': comm,
                        #             'fd': fd,
                        #             'err': err,
                        #             'flags': flags,
                        #             'path': path,
                        #         })
                        case 'speedscope':
                            speedscope = to_csv(speedscope_file=json.loads(file_str), functions_file=files['functions'])
                            speedscope.sandwich.insert(loc=0, column='step_id', value=step_id)
                            speedscope.graph.insert(loc=0, column='step_id', value=step_id)
                            speedscope.sandwich.insert(loc=0, column='timestamp_id', value=timestamp)
                            speedscope.graph.insert(loc=0, column='timestamp_id', value=timestamp)
                            files['sandwich'].append(speedscope.sandwich)
                            files['graph'].append(speedscope.graph)
                        case 'uprobe':
                            trace = TraceProfile(trace_data=file_str, trace_format='uprobe', functions_file=files['functions'])
                            if not trace.sandwich.empty:
                                trace.sandwich.insert(loc=0, column='step_id', value=step_id)
                                trace.graph.insert(loc=0, column='step_id', value=step_id)
                                trace.sandwich.insert(loc=0, column='timestamp_id', value=timestamp)
                                trace.graph.insert(loc=0, column='timestamp_id', value=timestamp)
                                files['sandwich'].append(trace.sandwich)
                                files['graph'].append(trace.graph)
                        case 'setprofile' | 'sysmonitor':
                            trace = TraceProfile(trace_data=file_str, trace_format='setprofile', functions_file=files['functions'])
                            if not trace.sandwich.empty:
                                trace.sandwich.insert(loc=0, column='step_id', value=step_id)
                                trace.graph.insert(loc=0, column='step_id', value=step_id)
                                trace.sandwich.insert(loc=0, column='timestamp_id', value=timestamp)
                                trace.graph.insert(loc=0, column='timestamp_id', value=timestamp)
                                files['sandwich'].append(trace.sandwich)
                                files['graph'].append(trace.graph)
                        case 'perf':
                            # perf.data is a binary format that cannot be parsed directly.
                            # Convert upstream on Linux CI with:
                            #   perf script -i file.perf.data > file.speedscope.json
                            print(f'[cintent] Skipping binary perf.data file: {filename} '
                                  '(convert to speedscope JSON upstream)')
                        case _:
                            pass
                else:
                    pass

    if files['metadata']:
        repo_id = files['metadata'][0]['repository']
        job_id = files['metadata'][0]['job_id']
        files['sandwich'] = pd.concat(files['sandwich']) if files['sandwich'] else DataFrame([], columns=['frame_idx','name','fq_name','header','file','relpath','line','col','weight'])
        files['graph'] = pd.concat(files['graph']) if files['graph'] else DataFrame([], columns=['src_idx','dst_idx','depth','count'])
        files['sandwich'].insert(loc=0, column='job_id', value=job_id)
        files['sandwich'].insert(loc=0, column='repo_id', value=repo_id)
        files['graph'].insert(loc=0, column='job_id', value=job_id)
        files['graph'].insert(loc=0, column='repo_id', value=repo_id)

        files['metadata'] = DataFrame(files['metadata'])
        # files['execsnoop'] = DataFrame(files['execsnoop'])
        # files['execsnoop'].insert(loc=0, column='job_id', value=job_id)
        # files['execsnoop'].insert(loc=0, column='repo_id', value=repo_id)
        # files['opensnoop'] = DataFrame(files['opensnoop'])
        # files['opensnoop'].insert(loc=0, column='job_id', value=job_id)
        # files['opensnoop'].insert(loc=0, column='repo_id', value=repo_id)
        
        archive_name = Path(archive_path).stem
        parse_dir = os.path.join(out_dir, 'parse')
        if not os.path.isdir(parse_dir):
            os.mkdir(parse_dir)
        files['metadata'].to_csv(os.path.join(parse_dir, f'{archive_name}_metadata.csv'), index=False, quoting=csv.QUOTE_ALL)
        # files['execsnoop'].to_csv(os.path.join(parse_dir, 'execsnoop.csv'), index=False, quoting=csv.QUOTE_ALL)
        # files['opensnoop'].to_csv(os.path.join(parse_dir, 'opensnoop.csv'), index=False, quoting=csv.QUOTE_ALL)
        files['sandwich'].to_csv(os.path.join(parse_dir, f'{archive_name}_sandwich.csv'), index=False, quoting=csv.QUOTE_ALL)
        files['graph'].to_csv(os.path.join(parse_dir, f'{archive_name}_graph.csv'), index=False, quoting=csv.QUOTE_ALL)

    return files


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse a CIntent archive')
    parser.add_argument('archive_path', type=os.path.abspath, help='path to a CIntent archive or directory of archives')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    parser.add_argument('--overwrite', action='store_true', help='whether to overwrite the existing parses')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.exists(args.archive_path), f'The CIntent archive "{args.archive_path}" does not exist.'

    # Create the output directory
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    
    if os.path.isfile(args.archive_path):
        archive = parse_archive(archive_path=args.archive_path, out_dir=args.out_dir)
    else:
        paths = sorted(glob.glob(os.path.join(args.archive_path, 'cintent-*')))
        for path in tqdm(paths, desc='Parsing Logs'):
            if args.overwrite or not os.path.isdir(os.path.join(path, 'parse')):
                subpaths = sorted(glob.glob(os.path.join(path, '*-cintent_logs.zip')))
                for subpath in subpaths:
                    parse_archive(archive_path=subpath, out_dir=path)

        graph_header = ['repo_id','job_id','step_id','timestamp_id','src_idx','dst_idx','depth','count']
        metadata_header = ['repository','branch','commit','workflow','run_number','run_attempt','workspace','job_id','matrix','step_id','start_time','end_time']
        sandwich_header = ['repo_id','job_id','step_id','timestamp_id','frame_idx','name','fq_name','header','file','relpath','line','col','weight']

        graph = DataFrame([], columns=graph_header)
        metadata = DataFrame([], columns=metadata_header)
        sandwich = DataFrame([], columns=sandwich_header)

        paths = sorted(glob.glob(os.path.join(args.archive_path, 'cintent-*', 'parse'), recursive=True))
        for path in tqdm(paths, desc='Combining Parses'):
            graph_paths = glob.glob(os.path.join(path, '*-cintent_logs_graph.csv'))
            metadata_paths = glob.glob(os.path.join(path, '*-cintent_logs_metadata.csv'))
            sandwich_paths = glob.glob(os.path.join(path, '*-cintent_logs_sandwich.csv'))

            for graph_path in graph_paths:
                graph = pd.concat([graph, pd.read_csv(graph_path)])

            for metadata_path in metadata_paths:
                metadata = pd.concat([metadata, pd.read_csv(metadata_path)])

            for sandwich_path in sandwich_paths:
                sandwich = pd.concat([sandwich, pd.read_csv(sandwich_path)])
        
        metadata.to_csv(os.path.join(args.archive_path, 'metadata.csv'), index=False, quoting=csv.QUOTE_ALL)
        sandwich.to_csv(os.path.join(args.archive_path, 'sandwich.csv'), index=False, quoting=csv.QUOTE_ALL)
        graph.to_csv(os.path.join(args.archive_path, 'graph.csv'), index=False, quoting=csv.QUOTE_ALL)
