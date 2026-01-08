from argparse import ArgumentParser, Namespace
from io import StringIO
import json
import os
import re
from typing import Any
from zipfile import ZipFile

import pandas as pd
from pandas import DataFrame
from tqdm import tqdm

from cintent.speedscope import to_csv


def parse_archive(archive_path: str) -> dict[str, Any]:
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
        assert function_filename is not None
        function_str = archive.read(function_filename).decode()
        files['functions'] = pd.read_csv(StringIO(function_str))

        for filename in tqdm(archive.namelist(), desc='Processing Archive'):
            file_info = archive.getinfo(filename)
            if not file_info.is_dir():
                file_str = archive.read(filename).decode()
                if '.functions.' not in filename:
                    timestamp, step_id, file_type, extension = filename.split('.')
                    match file_type:
                        case 'metadata':
                            metadata = {}
                            for line in file_str.splitlines():
                                key, value = line.split(' = ', maxsplit=1)
                                metadata[key] = value
                            if 'step_id' not in metadata:
                                metadata['step_id'] = step_id
                            files['metadata'].append(metadata)
                        case 'execsnoop':
                            pattern = r'^(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)$'
                            for line in file_str.splitlines()[2:]:
                                time_s, pcomm, pid, ppid, ret, args = re.match(pattern, line).groups()
                                files['execsnoop'].append({
                                    'step_id': step_id,
                                    'time_s': time_s,
                                    'pcomm': pcomm,
                                    'pid': pid,
                                    'ppid': ppid,
                                    'ret': ret,
                                    'args': args,
                                })
                        case 'opensnoop':
                            pattern = r'^(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)\s+(.+?)$'
                            for line in file_str.splitlines()[2:]:
                                time_s, pid, comm, fd, err, flags, path = re.match(pattern, line).groups()
                                files['opensnoop'].append({
                                    'step_id': step_id,
                                    'time_s': time_s,
                                    'pid': pid,
                                    'comm': comm,
                                    'fd': fd,
                                    'err': err,
                                    'flags': flags,
                                    'path': path,
                                })
                        case 'speedscope':
                            speedscope = to_csv(speedscope_file=json.loads(file_str), functions_file=files['functions'])
                            speedscope.sandwich.insert(loc=0, column='step_id', value=step_id)
                            speedscope.graph.insert(loc=0, column='step_id', value=step_id)
                            files['sandwich'].append(speedscope.sandwich)
                            files['graph'].append(speedscope.graph)
                        case _:
                            raise TypeError(f'Unexpected file type for "{filename}"')
                else:
                    pass

    repo_id = files['metadata'][0]['repository'].split('/')[1]
    job_id = files['metadata'][0]['job_id']
    files['sandwich'] = pd.concat(files['sandwich'])
    files['graph'] = pd.concat(files['graph'])
    files['sandwich'].insert(loc=0, column='job_id', value=job_id)
    files['sandwich'].insert(loc=0, column='repo_id', value=repo_id)
    files['graph'].insert(loc=0, column='job_id', value=job_id)
    files['graph'].insert(loc=0, column='repo_id', value=repo_id)


    files['metadata'] = DataFrame(files['metadata'])
    files['execsnoop'] = DataFrame(files['execsnoop'])
    files['execsnoop'].insert(loc=0, column='job_id', value=job_id)
    files['execsnoop'].insert(loc=0, column='repo_id', value=repo_id)
    files['opensnoop'] = DataFrame(files['opensnoop'])
    files['opensnoop'].insert(loc=0, column='job_id', value=job_id)
    files['opensnoop'].insert(loc=0, column='repo_id', value=repo_id)

    return files


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse a CIntent archive')
    parser.add_argument('archive_path', type=os.path.abspath, help='path to a CIntent archive')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isfile(args.archive_path), f'The CIntent archive "{args.archive_path}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    archive = parse_archive(args.archive_path)
