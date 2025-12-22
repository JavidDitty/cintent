from argparse import ArgumentParser, Namespace
import csv
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from pandas import DataFrame


class Speedscope:
    def __init__(self, speedscope_file: str, functions_file: str | None = None) -> None:
        # Load the Speedscope file
        self.speedscope_file = speedscope_file
        with open(self.speedscope_file, 'r') as file:
            self.base = json.load(file)

        # Load the CIntent functions file
        self.functions_file = functions_file
        if self.functions_file:
            self.functions = pd.read_csv(self.functions_file)

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
                    if self.functions_file and any(item not in frames[frame_idx] for item in ('fq_name', 'header', 'relpath')):
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
        frames_df = DataFrame(frames)[['frame_idx','name','fq_name','header','file','relpath','line','col','weight']]
        # frames_df = frames_df[~frames_df['fq_name'].isna()]
        return frames_df
    
    def __graph(self) -> DataFrame:
        """Create a transition graph that includes all the frames"""
        # Get the frame stacks and the unique frames
        samples, frames = [], set()
        for profile in self.base['profiles']:
            for sample in profile['samples']:
                frames.update(sample)
            samples += profile['samples']
        frames = sorted(frames)
        num_of_frames = len(frames)
        frame_to_index = {frame: i for i, frame in enumerate(frames)}
        index_to_name = {row.frame_idx: row.fq_name for row in self.sandwich.itertuples()}

        # Create the transition count matrix
        transition_counts = np.zeros((num_of_frames, num_of_frames), dtype=int)
        for sample in samples:
            for i in range(len(sample)-1):
                curr = frame_to_index[sample[i]]
                next = frame_to_index[sample[i+1]]
                transition_counts[curr, next] += 1

        # Normalize the counts to probabilities
        transition_matrix = np.zeros((num_of_frames, num_of_frames), dtype=float)
        for i in range(num_of_frames):
            row_sum = transition_counts[i, :].sum()
            if row_sum > 0:
                transition_matrix[i, :] = transition_counts[i, :] / row_sum
        
        # Convert transition counts and matrix to a dataframe
        transition_df = []
        indicies = list(range(len(transition_counts)))
        for i, c_row, p_row in zip(indicies, transition_counts, transition_matrix):
            for j, c_cell, p_cell in zip(indicies, c_row, p_row):
                if index_to_name[i] and index_to_name[j] and c_cell > 0:
                    transition_df.append({
                        'src': index_to_name[i], 
                        'dest': index_to_name[j], 
                        'count': c_cell, 
                        'probability': p_cell,
                    })
        transition_df = DataFrame(transition_df).sort_values(by='count', ascending=False)
        return transition_df


def to_csv(speedscope_file: str, out_dir: str, functions_file: str | None = None) -> None:
    # Parse a speedscope file
    speedscope = Speedscope(speedscope_file=speedscope_file, functions_file=functions_file)

    # Dump the speedscope representation(s)
    filename = Path(speedscope_file).stem
    sandwich_path = os.path.join(out_dir, f'{filename}.sandwich.csv')
    graph_path = os.path.join(out_dir, f'{filename}.graph.csv')
    speedscope.sandwich[~speedscope.sandwich['fq_name'].isna()].to_csv(sandwich_path, index=None, quoting=csv.QUOTE_ALL)
    speedscope.graph.to_csv(graph_path, index=None, quoting=csv.QUOTE_ALL)


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
