from __future__ import annotations

from argparse import ArgumentParser
import csv
import json
import os
from pathlib import Path
import typing
from typing import Any, Callable

from pandas import DataFrame
from pyinstrument import processors
from pyinstrument.frame import Frame
from pyinstrument.renderers.base import FrameRenderer, ProcessorList
from pyinstrument.session import Session


class CSVRenderer(FrameRenderer):
    """
    Based on pyinstrument/renderers/jsonrenderer.py
    """
    
    output_file_extension = 'csv'
    encode_str = typing.cast(Callable[[str], str], json.encoder.encode_basestring)
    frame_header = [
        'frame_id',
        'function',
        'file_path_short',
        'file_path',
        'line_no',
        'time',
        'await_time',
        'is_application_code',
        'group_id',
        'class_name',
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.frames = []

    def render_frame(self, frame: Frame | None, frame_id: int = 1) -> None:
        # Do not use the json module because it uses 2x the stack frames, crashing on deep but valid call stacks.
        if frame is None:
            return 'null'
        property_decls: list[str] = []
        property_decls.append(frame_id) # frame_id
        property_decls.append(self.encode_str(frame.function).strip('"')) # function
        property_decls.append(self.encode_str(frame.file_path_short or '').strip('"')) # file_path_short
        property_decls.append(self.encode_str(frame.file_path or '').strip('"')) # file_path
        property_decls.append(frame.line_no if frame.line_no is not None else 0) # line_no
        property_decls.append(frame.time) # time
        property_decls.append(frame.await_time()) # await_time
        property_decls.append(self.__encode_bool(frame.is_application_code or False)) # is_application_code
        property_decls.append(self.encode_str(frame.group.id).strip('"') if frame.group else '') # group_id
        property_decls.append(self.encode_str(frame.class_name).strip('"') if frame.class_name else '') # class_name
        for child in frame.children:
            self.render_frame(child, frame_id+1)
        self.frames.append(property_decls)

    def render(self, session: Session) -> tuple[dict, DataFrame]:
        frame = self.preprocess(session.root_frame())
        self.frames = []
        self.render_frame(frame)
        metadata = {
            'start_time': session.start_time,
            'duration': session.duration,
            'sample_count': session.sample_count,
            'target_description': self.encode_str(session.target_description).strip('"'),
            'cpu_time': session.cpu_time,
        }
        return metadata, DataFrame(self.frames, columns=self.frame_header)

    def default_processors(self) -> ProcessorList:
        return [
            processors.remove_importlib,
            processors.remove_tracebackhide,
            processors.merge_consecutive_self_time,
            processors.aggregate_repeated_calls,
            processors.remove_irrelevant_nodes,
            processors.remove_unnecessary_self_time_nodes,
            processors.remove_first_pyinstrument_frames_processor,
            processors.group_library_frames_processor,
        ]
    
    def __encode_bool(self, a_bool: bool) -> str:
        return 'true' if a_bool else 'false'


def parse_session(session_file: str, out_dir: str):
    # Parse the session file
    session = Session.load(session_file)
    renderer = CSVRenderer()
    metadata, render_df = renderer.render(session)
    
    # Dump session parse to file(s)
    filename = Path(session_file).stem
    metadata_path = os.path.join(out_dir, f'{filename}.json')
    render_path = os.path.join(out_dir, f'{filename}.csv')
    with open(metadata_path, 'w') as file:
        metadata_str = [f'"{k}":"{v}"' for k, v in metadata.items()]
        file.write('{' + ','.join(metadata_str) + '}')
    render_df.to_csv(render_path, index=False, quoting=csv.QUOTE_ALL)


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description='Parse a Call Graph from a Pyinstrument Session')
    parser.add_argument('session_file', type=os.path.abspath, help='path to a Pyinstrument session file (.pyisession)')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an output directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isfile(args.session_file), f'The Pyinstrument session file "{args.session_file}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    parse_session(session_file=args.session_file, out_dir=args.out_dir)
