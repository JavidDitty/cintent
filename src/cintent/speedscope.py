from argparse import ArgumentParser, Namespace
import csv
import json
import os
from pathlib import Path

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
        self.sandwich = self.sandwich()
    
    def sandwich(self) -> dict:
        """Aggregate the weights for each frame"""
        frames = self.base['shared']['frames']
        for profile in self.base['profiles']:
            for sample, weight in zip(profile['samples'], profile['weights']):
                for frame in sample:
                    frame_idx = frame - 1
                    if 'weight' not in frames[frame_idx]:
                        frames[frame_idx]['weight'] = 0
                    frames[frame_idx]['weight'] += weight
                    if any(item not in frames[frame_idx] for item in ('fq_name', 'header', 'relpath')):
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
        return frames
    
    def to_csv(self, out_path: str) -> None:
        """Dump sandwich representation to a CSV"""
        sandwich_df = DataFrame(self.sandwich)
        sandwich_df.to_csv(out_path, index=None, quoting=csv.QUOTE_ALL)


def to_csv(speedscope_file: str, out_dir: str, functions_file: str | None = None) -> None:
    # Parse a speedscope file
    speedscope = Speedscope(speedscope_file=speedscope_file, functions_file=functions_file)

    # Dump the speedscope parse to a file
    filename = f'{Path(speedscope_file).stem}.speedscope.csv'
    out_path = os.path.join(out_dir, filename)
    speedscope.to_csv(out_path)


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
