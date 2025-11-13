from copy import deepcopy
from io import StringIO
import json
import re
import textwrap

import ruamel.yaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString
from tqdm import tqdm


def workflow_to_dict(workflow: str) -> dict:
    """Convert a string workflow to a dictionary representation"""
    return get_yaml_parser().load(workflow)


def workflow_to_str(workflow: dict) -> str:
    """Convert a dictionary workflow to a string representation"""
    workflow_stream = StringIO()
    get_yaml_parser().dump(workflow, workflow_stream)
    workflow_str = workflow_stream.getvalue()
    workflow_stream.close()
    return workflow_str


def load_workflow(workflow_path: str, as_str: bool = False) -> str | dict | None:
    """Load a workflow and represent it as a string or dictionary"""
    workflow = None
    yaml_parser = get_yaml_parser()
    with open(workflow_path, 'r') as workflow_file:
        workflow = workflow_file.read() if as_str else yaml_parser.load(workflow_file)
    return workflow


def dump_workflow(workflow: dict, output_path: str):
    """Dump a workflow to a file"""
    copied = deepcopy(workflow)
    for job_id in copied['jobs']:
        for i, step in enumerate(copied['jobs'][job_id]['steps']):
            if 'run' in step:
                copied['jobs'][job_id]['steps'][i]['run'] = to_multiline_str(copied['jobs'][job_id]['steps'][i]['run'].splitlines())

    yaml_parser = get_yaml_parser()
    with open(output_path, 'w') as file:
        yaml_parser.dump(copied, file)


def get_yaml_parser() -> YAML:
    """Get pre-configured yaml parser"""
    ruamel.yaml.representer.RoundTripRepresenter.ignore_aliases = lambda x, y: True
    yaml_parser = YAML(pure=True)
    yaml_parser.indent(sequence=4, offset=2)
    yaml_parser.sort_base_mapping_type_on_output = False
    yaml_parser.default_style = None
    yaml_parser.width = 100
    yaml_parser.ignore_aliases = lambda *args : True
    return yaml_parser


def to_multiline_str(strings: list) -> str:
    """Retrieve multiline string that will be rendered properly"""
    newline_strings = '\n'.join(strings) + '\n'
    return LiteralScalarString(textwrap.dedent(f"""{newline_strings}"""))


def get_context_dependencies(paths: list[str], quiet: bool = False) -> list[dict[str, str]]:
    """Get jobs and steps in a list of workflows whose behaviors are dependent on workflow contexts"""
    dependencies = []
    
    for path in tqdm(paths, disable=quiet, desc='Checking for trigger dependencies'):
        workflow = load_workflow(path)
        if not workflow:
            continue
        
        contexts = ('github', 'inputs', 'vars', 'secrets')
        for job_id, job in workflow['jobs'].items():
            # Check for trigger dependencies in `jobs.<job_id>.if`
            for context in contexts:
                if 'if' in job:
                    for expression in re.findall(r'\$\{\{.*?\}\}', str(job['if'])):
                        for reference in re.findall(fr'{context}(?:\.|\[)', expression):
                            dependencies.append({'path': path, 'source': 'job', 'name': job_id, 'context': context, 'expression': expression, 'reference': reference})
            
            if 'steps' not in job:
                continue
            for step_id, step in enumerate(job['steps']):
                # Check for trigger dependencies in `jobs.<job_id>.steps[*].<if|run>`
                for context in contexts:
                    for syntax in ('if', 'run'):
                        if syntax in step:
                            for expression in re.findall(r'\$\{\{.*?\}\}', str(step[syntax])):
                                for reference in re.findall(fr'{context}(?:\.|\[)', expression):
                                    dependencies.append({'path': path, 'source': syntax, 'name': f'{job_id}.steps[{step_id}]', 'context': context, 'expression': expression, 'reference': reference})

    return json.loads(json.dumps(dependencies))
