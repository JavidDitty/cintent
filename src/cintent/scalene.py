from argparse import ArgumentParser, Namespace
from collections import Counter
import csv
import json
import os
from pathlib import Path
import re

import pandas as pd
from pandas import DataFrame


class Scalene:
    def __init__(self, scalene_file: str, functions_file: str | None) -> None:
        # Load the Scalene file
        self.scalene_file = scalene_file
        with open(self.scalene_file, 'r') as file:
            self.base = json.load(file)

        # Load the CIntent functions file
        self.functions_file = functions_file
        self.functions = None
        if self.functions_file:
            self.functions = pd.read_csv(self.functions_file)

        # Parse Scalene representations
        self.sandwich = self.__sandwich()
        self.graph = self.__graph()
    
    def __sandwich(self) -> DataFrame:
        """Aggregate the time for each function"""
        functions = []
        execution_time = self.base['elapsed_time_sec']
        for file_path, file_profile in self.base['files'].items():
            match = re.search(r'/home/runner/work/.+?/.+?/', file_path)
            if match and 'site-packages' not in file_path:
                relpath = file_path.removeprefix(match.group())
                for function in file_profile['functions']:
                    if self.functions is not None:
                        df = self.functions.copy()
                        df = df[df['path'] == relpath]
                        df = df[df['line'] == function['lineno']]
                        if len(df) > 0:
                            assert len(df) <= 1
                            info = df.iloc[0].to_dict()
                            info['execution_time'] = execution_time * (function['n_cpu_percent_c'] + function['n_sys_percent'] + function['n_cpu_percent_python'])
                            functions.append(info)
        columns = ['name','fq_name','header','docstring','class_name','class_docstring','path','line','execution_time']
        functions_df = DataFrame(functions, columns=columns)
        return functions_df
    
    def __graph(self) -> DataFrame:
        """Create a transition graph that includes all the frames"""
        def fq_name(function: str) -> str | None:
            repo_match = re.search(r'/home/runner/work/.+?/.+?/', function)
            if repo_match and 'site-packages' not in function:
                repo_path = repo_match.group()
                rel_path = function.removeprefix(repo_path)
                script_match = re.search(r'.+\.py', rel_path)
                if script_match:
                    script_path = script_match.group()
                    function_name = script_match.string.removeprefix(script_path).split(':', maxsplit=1)[0].strip()
                    module_name = script_path.removesuffix('.py').replace('/', '.')
                    return f'{module_name}.{function_name}'
            return None
        
        transition_counter = Counter()
        for stack, info in self.base['stacks']:
            for curr_idx in range(len(stack)):
                next_idx = curr_idx + 1
                if next_idx < len(stack):
                    curr, next = stack[curr_idx], stack[next_idx]
                    curr_str, next_str = fq_name(curr), fq_name(next)
                    if curr_str is not None and next_str is not None:
                        transition_counter.update([(curr_str, next_str)] * info['count'])

        transition_records = [(row, col, count) for (row, col), count in transition_counter.items()]
        count_df = DataFrame(transition_records, columns=['from', 'to', 'count'])
        graph_df = count_df.pivot(index='from', columns='to', values='count').fillna(0)
        return graph_df


def to_csv(scalene_file: str, out_dir: str, functions_file: str | None) -> None:
    # Parse a scalene file
    scalene = Scalene(scalene_file=scalene_file, functions_file=functions_file)

    # Dump the scalene representation(s)
    filename = Path(scalene_file).stem
    sandwich_path = os.path.join(out_dir, f'{filename}.sandwich.csv')
    graph_path = os.path.join(out_dir, f'{filename}.graph.csv')
    scalene.sandwich[~scalene.sandwich['fq_name'].isna()].to_csv(sandwich_path, index=None, quoting=csv.QUOTE_ALL)
    scalene.graph.astype('Int64').to_csv(graph_path, quoting=csv.QUOTE_ALL)


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse a Scalene file')
    parser.add_argument('scalene_file', type=os.path.abspath, help='path to a scalene file')
    parser.add_argument('-f', '--functions_file', default=None, type=os.path.abspath, help='path to a CIntent functions file')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isfile(args.scalene_file), f'The scalene file "{args.scalene_file}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    to_csv(scalene_file=args.scalene_file, out_dir=args.out_dir, functions_file=args.functions_file)
