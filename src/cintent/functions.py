from argparse import ArgumentParser, Namespace
from csv import DictWriter, QUOTE_ALL
import json
import glob
import os
from pathlib import Path
import re

from typing import Generator
from tree_sitter import Language, Parser, Tree, Node
import tree_sitter_python


def traverse_tree(tree: Tree) -> Generator[Node, None, None]:
    """From examples/walk_tree.py in tree-sitter/py-tree-sitter at commit 61e6657"""
    cursor = tree.walk()
    visited_children = False
    while True:
        if not visited_children:
            yield cursor.node
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break


def parse_functions(root_dir: str) -> Generator[dict, None, None]:
    # Initialize the Python tree-sitter parser
    PY_LANGUAGE = tree_sitter_python.language()
    parser = Parser(Language(PY_LANGUAGE))

    # Extract the Python files in the repository
    pattern = os.path.join(root_dir, '**', '*.py')
    paths = glob.glob(pattern, recursive=True)

    for path in paths:
        # Read a Python file and parse it into a tree
        with open(path, 'rb') as file:
            source = file.read()
        tree = parser.parse(source)
        module_name = Path(path).stem

        for node in traverse_tree(tree):
            # Skip nodes that are not function definitions
            if node.type != 'function_definition':
                continue

            # Get the fully qualified name of the function
            fq_name = []

            ## Get the name of the function
            name = node.child_by_field_name('name').text.decode()
            fq_name.append(name)
            
            ## Get the name(s) of the class(es)/function(s) that the function is in
            class_name = None
            class_docstring = None
            parent = node.parent
            while parent is not None:
                if parent.type in ('class_definition', 'function_definition'):
                    parent_name = parent.child_by_field_name('name').text.decode()
                    if class_name is not None and parent.type == 'class_definition':
                        class_name = parent_name
                        candidate = node.child_by_field_name('body').child(0)
                        if candidate.type == 'expression_statement' and candidate.child(0).type == 'string':
                            class_docstring = candidate.text.decode()
                    fq_name.append(parent_name)
                parent = parent.parent
            
            ## Get the name(s) of the module(s) that the function is in
            relpath = os.path.relpath(path, root_dir)
            modules = os.path.dirname(relpath)
            modules = re.split(r'\\|/', modules)
            modules.reverse()
            fq_name.append(module_name)
            fq_name.extend(modules)
            
            ## Combine the name of the function, class(es)/function(s), and module(s)
            fq_name.reverse()
            fq_name = '.'.join(fq_name)
            fq_name = re.subn(r'\.+', '.', fq_name)[0].strip('.')

            # Get the docstring of the function (if any)
            docstring = None
            candidate = node.child_by_field_name('body').child(0)
            if candidate.type == 'expression_statement' and candidate.child(0).type == 'string':
                docstring = candidate.text.decode()

            # Get the line number of the function
            line = node.start_point.row + 1
            if node.parent is not None and node.parent.type == 'decorated_definition':
                line = node.parent.start_point.row + 1

            # Get the header of the function
            parameters = node.child_by_field_name('parameters').text.decode()
            return_type = node.child_by_field_name('return_type')
            header = f'def {name}{parameters}'
            if return_type:
                return_type = return_type.text.decode()
                header = f'{header} -> {return_type}'
            if node.parent is not None and node.parent.type == 'decorated_definition':
                decorator = node.parent.children[0].text.decode()
                header = f'{decorator}\n{header}'
            
            # Return the function's metadata
            yield \
            {
                'name': name, 
                'fq_name': fq_name, 
                'header': json.dumps(header), 
                'docstring': json.dumps(docstring) if docstring else None,
                'class_name': class_name,
                'class_docstring': json.dumps(class_docstring) if class_docstring else None,
                'path': relpath, 
                'line': line,
            }


def to_csv(root_dir: str, out_dir: str) -> None:
    # Parse the function metadata from the repository
    functions = list(parse_functions(root_dir=root_dir))

    # Dump the function metadata to a file
    filename = f'{Path(root_dir).stem}.functions.csv'
    out_path = os.path.join(out_dir, filename)
    with open(out_path, mode='w', encoding='utf-8', newline='') as file:
        fieldnames = ['name', 'fq_name', 'header', 'docstring', 'class_name', 'class_docstring', 'path', 'line']
        writer = DictWriter(file, fieldnames=fieldnames, quoting=QUOTE_ALL)
        writer.writeheader()
        writer.writerows(functions)


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse Python functions/methods from repositories')
    parser.add_argument('root_dir', type=os.path.abspath, help='path to the root directory of a repository')
    parser.add_argument('-o', '--out_dir', default='out', type=os.path.abspath, help='path to an out directory')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isdir(args.root_dir), f'The root directory "{args.root_dir}" does not exist.'
    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)
    to_csv(root_dir=args.root_dir, out_dir=args.out_dir)
