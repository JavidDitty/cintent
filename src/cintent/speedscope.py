from argparse import ArgumentParser, Namespace
from copy import deepcopy
import csv
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from pandas import DataFrame
from tqdm import tqdm


class Speedscope:
    def __init__(self, speedscope_file: str | dict, functions_file: str | DataFrame | None = None) -> None:
        # Load the Speedscope file
        if isinstance(speedscope_file, str) and os.path.isfile(speedscope_file):
            self.speedscope_file = speedscope_file
            with open(self.speedscope_file, 'r') as file:
                self.base = json.load(file)
        elif isinstance(speedscope_file, dict):
            self.base = speedscope_file
        else:
            raise TypeError()

        # Load the CIntent functions file
        if isinstance(functions_file, str) and os.path.isfile(functions_file):
            self.functions_file = functions_file
            if self.functions_file:
                self.functions = pd.read_csv(self.functions_file)
        elif isinstance(functions_file, DataFrame):
            self.functions = functions_file

        # Parse Speedscope representations
        self.sandwich = self.__sandwich()
        self.graph = self.__graph()
    
    def __sandwich(self) -> DataFrame:
        """Aggregate the weights for each frame"""
        frames = self.base['shared']['frames']
        for profile in self.base['profiles']:
            for sample, weight in zip(profile['samples'], profile['weights']):
                for frame in sample:
                    frame_idx = frame
                    if 'weight' not in frames[frame_idx]:
                        frames[frame_idx]['weight'] = 0
                    frames[frame_idx]['weight'] += weight
                    if self.functions is not None and any(item not in frames[frame_idx] for item in ('fq_name', 'header', 'relpath')):
                        functions = self.functions
                        functions = functions[functions['name'] == frames[frame_idx]['name']]
                        functions = functions[functions['line'] == frames[frame_idx]['line']]
                        path_mask = functions['path'].apply(lambda path: frames[frame_idx]['file'].endswith(path))
                        functions = functions[path_mask]
                        if len(functions):
                            functions = functions.iloc[0].to_dict()
                            frames[frame_idx]['fq_name'] = functions['fq_name']
                            frames[frame_idx]['header'] = functions['header']
                            frames[frame_idx]['relpath'] = functions['path']
                        else:
                            frames[frame_idx]['fq_name'] = None
                            frames[frame_idx]['header'] = None
                            frames[frame_idx]['relpath'] = None
                        frames[frame_idx]['frame_idx'] = frame_idx
        columns = ['frame_idx','name','fq_name','header','file','relpath','line','col','weight']
        try:
            frames_df = DataFrame(frames)[columns]
        except KeyError:
            frames_df = DataFrame([], columns=columns)
        # Keep only frames whose source file lives inside the GitHub Actions
        # workspace (/home/runner/work/<repo>/<repo>/).  This filters out
        # stdlib, installed packages and other non-repo code.
        mask = frames_df['file'].str.contains(r'/home/runner/work/[^/]+/[^/]+/', regex=True)
        frames_df = frames_df[mask]
        return frames_df
    
    def __graph(self) -> DataFrame:
        """Create a transition graph that includes all the frames"""
        graph = {}
        profiles = deepcopy(self.base['profiles'])

        # Remove library frames
        frames = set(self.sandwich['frame_idx'].to_list())
        for i, profile in enumerate(profiles):
            for j, sample in enumerate(profile['samples']):
                profiles[i]['samples'][j] = [frame for frame in sample if frame in frames]
        
        # Count the direct frame transitions (adjacent pairs in each sample)
        # depth=1 for all pairs: every adjacent pair in a stack sample is a
        # direct call, consistent with depth semantics in trace.py.
        for profile in profiles:
            for sample in profile['samples']:
                length = len(sample)
                for i in range(length - 1):
                    src = sample[i]
                    dst = sample[i + 1]
                    if src not in graph:
                        graph[src] = {}
                    if dst not in graph[src]:
                        graph[src][dst] = {'depth': 1, 'count': 0}
                    graph[src][dst]['count'] += 1

        # Convert the graph to a dataframe
        graph_df = DataFrame([], columns=['src_idx', 'dst_idx', 'depth', 'count'])
        for src_idx, transitions in graph.items():
            for dst_idx, value in transitions.items():
                graph_df.loc[len(graph_df)] = [src_idx, dst_idx, value['depth'], value['count']]

        return graph_df


def to_csv(speedscope_file: str, out_dir: str | None = None, functions_file: str | None = None) -> Speedscope | None:
    # Parse a speedscope file
    speedscope = Speedscope(speedscope_file=speedscope_file, functions_file=functions_file)

    # Dump the speedscope representation(s)
    if out_dir:
        filename = Path(speedscope_file).stem
        sandwich_path = os.path.join(out_dir, f'{filename}.sandwich.csv')
        graph_path = os.path.join(out_dir, f'{filename}.graph.csv')
        # speedscope.sandwich[~speedscope.sandwich['fq_name'].isna()].to_csv(sandwich_path, index=None, quoting=csv.QUOTE_ALL)
        speedscope.sandwich.to_csv(sandwich_path, index=None, quoting=csv.QUOTE_ALL)
        speedscope.graph.to_csv(graph_path, index=None, quoting=csv.QUOTE_ALL)
    return speedscope


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse a Speedscope file')
    parser.add_argument('speedscope_file', type=os.path.abspath, help='path to a speedscope file')
    parser.add_argument('-f', '--functions_file', default=None, type=os.path.abspath, help='path to a CIntent functions file')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isfile(args.speedscope_file), f'The speedscope file "{args.speedscope_file}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    to_csv(speedscope_file=args.speedscope_file, out_dir=args.out_dir, functions_file=args.functions_file)
