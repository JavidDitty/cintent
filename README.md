# CIntent

`CIntent` analyzes runtime information from your GitHub Actions workflows to summarize their intent.

This information can be captured with [CIMonitor](https://github.com/JavidDitty/cintent).

## Usage

A workflow run with `CIMonitor` will output an archive containing runtime information (e.g., file operations, network traffic, call graphs, etc.)

To preprocess an archive so that it can be used:

```bash
uv run cintent preprocess PATH_TO_ARCHIVE
```

This will output the following files:

- `metadata.csv`: Information about each workflow execution context that was monitored.
- `sandwich.csv`: List of all called functions and their runtime information (e.g., duration)
- `graph.csv`: List of <caller, callee> function pairs and their runtime information (e.g., count)

To analyze a preprocessed archive:

```bash
uv run cintent analyze -s PATH_TO_SANDWICH -g PATH_TO_GRAPH
```

This will output the following files:

- `intents.csv`: An annotated version of `graph.csv` that associates each function with their intent.
- `summary.md`: A summary of the workflow, including its intent, its bottlenecks, etc.


## Acknowledgement

![University of Michigan-Dearborn Logo](./docs/umd_logo.png)

`CIntent` was developed for research in the Software Evolution and Maintenance (SEM) Lab at the University of Michigan-Dearborn.
