"""
Parse trace-based profiler output (uprobe, setprofile) into sandwich and graph DataFrames.

Uprobe CSV format:   timestamp,pid,event,function,file,line,duration_ns
Setprofile CSV format: timestamp_ns,event,function,filename,line
"""

from io import StringIO

import pandas as pd
from pandas import DataFrame


class TraceProfile:
    """Parse trace-based profiler output into sandwich and graph DataFrames.

    Provides the same .sandwich and .graph interface as the Speedscope class
    so that archive.py can consume them identically.
    """

    SANDWICH_COLUMNS = [
        'frame_idx', 'name', 'fq_name', 'header',
        'file', 'relpath', 'line', 'col', 'weight',
    ]
    GRAPH_COLUMNS = ['src_idx', 'dst_idx', 'depth', 'count']

    def __init__(
        self,
        trace_data: str,
        trace_format: str,
        functions_file: DataFrame | None = None,
    ) -> None:
        self.trace_format = trace_format
        self.functions = functions_file
        self.frame_map: dict[tuple, int] = {}     # (function, file, line) -> frame_idx
        self.events = self._parse_events(trace_data)
        self.sandwich = self._build_sandwich()
        self.graph = self._build_graph()

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def _parse_events(self, trace_data: str) -> DataFrame:
        """Parse raw CSV trace data into a normalised DataFrame of events.

        Normalised columns: timestamp, pid, event, function, file, line, duration_ns
        Event values are normalised to 'call' / 'return'.
        """
        if not trace_data or not trace_data.strip():
            return DataFrame()

        try:
            df = pd.read_csv(StringIO(trace_data))
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            return DataFrame()

        if df.empty:
            return df

        if self.trace_format == 'uprobe':
            # Expected: timestamp,pid,event,function,file,line,duration_ns
            required = {'event', 'function', 'file', 'line'}
            if not required.issubset(df.columns):
                return DataFrame()
            # Normalise event names: enter -> call, exit -> return
            df['event'] = df['event'].map(
                {'enter': 'call', 'exit': 'return'}
            ).fillna(df['event'])
            if 'duration_ns' not in df.columns:
                df['duration_ns'] = 0
            if 'pid' not in df.columns:
                df['pid'] = 0
            if 'timestamp' not in df.columns:
                df['timestamp'] = 0

        elif self.trace_format == 'setprofile':
            # Expected: timestamp_ns,event,function,filename,line
            df = df.rename(columns={'timestamp_ns': 'timestamp', 'filename': 'file'})
            required = {'event', 'function', 'file', 'line'}
            if not required.issubset(df.columns):
                return DataFrame()
            if 'pid' not in df.columns:
                df['pid'] = 0
            if 'duration_ns' not in df.columns:
                df['duration_ns'] = 0
            if 'timestamp' not in df.columns:
                df['timestamp'] = 0

        return df

    # ------------------------------------------------------------------
    # Function matching
    # ------------------------------------------------------------------

    def _match_function(self, name: str, file_path: str, line: int) -> dict:
        """Match a traced function against the parsed functions file."""
        result = {'fq_name': None, 'header': None, 'relpath': None}
        if self.functions is None or self.functions.empty:
            return result

        funcs = self.functions
        funcs = funcs[funcs['name'] == name]
        funcs = funcs[funcs['line'] == line]
        if not funcs.empty:
            path_mask = funcs['path'].apply(lambda p: file_path.endswith(str(p)))
            funcs = funcs[path_mask]

        if len(funcs):
            match = funcs.iloc[0].to_dict()
            result['fq_name'] = match.get('fq_name')
            result['header'] = match.get('header')
            result['relpath'] = match.get('path')

        return result

    # ------------------------------------------------------------------
    # Sandwich (per-function weight aggregation)
    # ------------------------------------------------------------------

    def _build_sandwich(self) -> DataFrame:
        """Aggregate weight (execution time) per unique function frame."""
        if self.events.empty:
            return DataFrame([], columns=self.SANDWICH_COLUMNS)

        if self.trace_format == 'uprobe':
            grouped = self._aggregate_uprobe()
        elif self.trace_format == 'setprofile':
            grouped = self._aggregate_setprofile()
        else:
            return DataFrame([], columns=self.SANDWICH_COLUMNS)

        if grouped.empty:
            return DataFrame([], columns=self.SANDWICH_COLUMNS)

        frames = []
        for _, row in grouped.iterrows():
            func_name = str(row['function'])
            file_path = str(row['file'])
            line_num = int(row['line'])
            weight = int(row['duration_ns'])

            match = self._match_function(func_name, file_path, line_num)

            frame_idx = len(frames)
            self.frame_map[(func_name, file_path, line_num)] = frame_idx
            frames.append({
                'frame_idx': frame_idx,
                'name': func_name,
                'fq_name': match['fq_name'],
                'header': match['header'],
                'file': file_path,
                'relpath': match['relpath'],
                'line': line_num,
                'col': None,
                'weight': weight,
            })

        frames_df = DataFrame(frames, columns=self.SANDWICH_COLUMNS)

        # Keep only functions that live inside the GitHub Actions workspace
        # (/home/runner/work/<repo>/<repo>/).  This filters out stdlib,
        # installed packages and other non-repo code.
        if not frames_df.empty:
            mask = frames_df['file'].str.contains(
                r'/home/runner/work/[^/]+/[^/]+/', regex=True,
            )
            frames_df = frames_df[mask]

        return frames_df

    def _aggregate_uprobe(self) -> DataFrame:
        """Uprobe: sum duration_ns from exit/return events per function."""
        returns = self.events[self.events['event'] == 'return'].copy()
        if returns.empty:
            # Fall back to counting call events if no returns were recorded
            calls = self.events[self.events['event'] == 'call'].copy()
            if calls.empty:
                return DataFrame(columns=['function', 'file', 'line', 'duration_ns'])
            grouped = calls.groupby(['function', 'file', 'line']).size().reset_index(name='duration_ns')
            return grouped
        return returns.groupby(['function', 'file', 'line'])['duration_ns'].sum().reset_index()

    def _aggregate_setprofile(self) -> DataFrame:
        """Setprofile: compute duration by matching call/return timestamp pairs."""
        if self.events.empty:
            return DataFrame(columns=['function', 'file', 'line', 'duration_ns'])

        durations: list[dict] = []
        call_stacks: dict[tuple, list[int]] = {}

        for _, event in self.events.iterrows():
            key = (event['function'], str(event['file']), int(event['line']))

            if event['event'] == 'call':
                call_stacks.setdefault(key, []).append(int(event['timestamp']))
            elif event['event'] == 'return':
                if key in call_stacks and call_stacks[key]:
                    call_ts = call_stacks[key].pop()
                    duration = int(event['timestamp']) - call_ts
                    durations.append({
                        'function': key[0],
                        'file': key[1],
                        'line': key[2],
                        'duration_ns': max(0, duration),
                    })

        if not durations:
            return DataFrame(columns=['function', 'file', 'line', 'duration_ns'])

        df = DataFrame(durations)
        return df.groupby(['function', 'file', 'line'])['duration_ns'].sum().reset_index()

    # ------------------------------------------------------------------
    # Graph (caller → callee transitions)
    # ------------------------------------------------------------------

    def _build_graph(self) -> DataFrame:
        """Build a call-graph DataFrame from enter/exit trace events."""
        if self.events.empty or self.sandwich.empty:
            return DataFrame([], columns=self.GRAPH_COLUMNS)

        valid_frames = set(self.sandwich['frame_idx'].tolist())
        graph: dict[tuple, dict] = {}
        call_stacks: dict[int, list[int | None]] = {}   # pid -> stack of frame_idx

        for _, event in self.events.iterrows():
            pid = int(event.get('pid', 0))
            key = (str(event['function']), str(event['file']), int(event['line']))

            if pid not in call_stacks:
                call_stacks[pid] = []
            stack = call_stacks[pid]

            if event['event'] == 'call':
                frame_idx = self.frame_map.get(key)
                if frame_idx is not None and frame_idx in valid_frames:
                    # Walk stack backwards to find nearest valid parent
                    parent_idx = None
                    for s in reversed(stack):
                        if s is not None and s in valid_frames:
                            parent_idx = s
                            break

                    if parent_idx is not None:
                        edge = (parent_idx, frame_idx)
                        if edge not in graph:
                            depth = sum(1 for s in stack if s is not None and s in valid_frames)
                            graph[edge] = {'depth': depth, 'count': 0}
                        graph[edge]['count'] += 1

                    stack.append(frame_idx)
                else:
                    stack.append(None)          # placeholder for non-repo frames

            elif event['event'] == 'return':
                if stack:
                    stack.pop()

        rows = [
            {'src_idx': src, 'dst_idx': dst, 'depth': val['depth'], 'count': val['count']}
            for (src, dst), val in graph.items()
        ]
        return DataFrame(rows, columns=self.GRAPH_COLUMNS) if rows else DataFrame([], columns=self.GRAPH_COLUMNS)
