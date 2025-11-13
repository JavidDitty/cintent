from argparse import ArgumentParser, Namespace
from csv import DictWriter, QUOTE_ALL
import json
import os
from pathlib import Path


class Speedscope:
    def __init__(self, speedscope_file: str) -> None:
        self.speedscope_file = speedscope_file
        self.base = self.load()
        self.sandwich = self.sandwich()

    def load(self) -> dict:
        """Load the Speedscope file"""
        with open(self.speedscope_file, 'r') as file:
            base = json.load(file)
        return base
    
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
        return frames
    
    def to_csv(self, out_path: str) -> None:
        """Dump sandwich representation to a CSV"""
        with open(out_path, 'w', newline='') as file:
            if len(self.sandwich):
                fieldnames = self.sandwich[0].keys()
                writer = DictWriter(file, fieldnames=fieldnames, quoting=QUOTE_ALL)
                writer.writeheader()
                writer.writerows(self.sandwich)


def parse_speedscope(speedscope_file: str, out_dir: str) -> None:
    filename = f'{Path(speedscope_file).stem}.speedscope.csv'
    out_path = os.path.join(out_dir, filename)
    speedscope = Speedscope(speedscope_file)
    speedscope.to_csv(out_path)


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse a Speedscope file')
    parser.add_argument('speedscope_file', type=os.path.abspath, help='path to a speedscope file')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isfile(args.speedscope_file), f'The speedscope file "{args.speedscope_file}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    parse_speedscope(speedscope_file=args.speedscope_file, out_dir=args.out_dir)
