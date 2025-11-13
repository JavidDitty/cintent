from argparse import ArgumentParser, Namespace
import csv
import json
import glob
import os
import re

from pandas import DataFrame
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


def parse_functions_in_repo(root_dir: str) -> Generator[dict, None, None]:
    PY_LANGUAGE = Language(tree_sitter_python.language())
    parser = Parser(PY_LANGUAGE)

    pattern = os.path.join(root_dir, '**', '*.py')
    paths = glob.glob(pattern, recursive=True)
    for path in paths:
        with open(path, 'rb') as file:
            source = file.read()
        tree = parser.parse(source)
        for node in traverse_tree(tree):
            if node.type == 'function_definition':
                # Get the fully qualified name of the function
                function_name = node.child_by_field_name('name').text.decode()
                qualified_name = [function_name]
                parent = node.parent
                while parent is not None:
                    if parent.type in ('class_definition', 'function_definition'):
                        parent_name = parent.child_by_field_name('name').text.decode()
                        qualified_name.append(parent_name)
                    parent = parent.parent
                
                relative_path = os.path.dirname(os.path.relpath(path, root_dir))
                module_path = re.split(r'\\|/', relative_path)
                module_path.reverse()
                qualified_name.extend(module_path)
                
                qualified_name.reverse()
                qualified_name = re.subn(r'\.+', '.', '.'.join(qualified_name))[0].strip('.')

                # Get the line number of the function
                line_number = node.start_point.row + 1

                # Get the header of the function
                function_parameters = node.child_by_field_name('parameters').text.decode()
                function_return_type = node.child_by_field_name('return_type')
                if function_return_type:
                    function_return_type = function_return_type.text.decode()
                    function_header = f'def {function_name}{function_parameters} -> {function_return_type}'
                else:
                    function_header = f'def {function_name}{function_parameters}'
                yield {'name': function_name, 'fq_name': qualified_name, 'header': json.dumps(function_header), 'line': line_number}


def parse_functions(root_dir: str, out_path: str) -> None:
    functions = list(parse_functions_in_repo(root_dir=root_dir))
    functions_df = DataFrame(functions)
    functions_df.to_csv(out_path, index=False, quoting=csv.QUOTE_ALL)


def parse_args() -> Namespace:
    parser = ArgumentParser(description='Parse Python functions/methods from Repositories')
    parser.add_argument('root_dir', type=os.path.abspath, help='path to the root directory of a repository')
    parser.add_argument('-o', '--out_path', type=os.path.abspath, help='path to an output csv file')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert os.path.isdir(args.root_dir), f'The root directory "{args.root_dir}" does not exist.'
    parse_functions(root_dir=args.root_dir, out_path=args.out_path)
