from argparse import ArgumentParser, Namespace
import os


def parse_args() -> Namespace:
    """Parse CLI arguments"""
    parser = ArgumentParser(description='Summarize the intent of GitHub Actions workflows')
    subparsers = parser.add_subparsers(dest='command', required=True, help='Available commands')

    workflow_parser = subparsers.add_parser('workflow', help='Manage and analyze GitHub Actions workflows')
    workflow_subparsers = workflow_parser.add_subparsers(dest='workflow_command', required=True, help='Available workflow commands')

    workflow_subparsers.add_parser('clone', help='Clone repositories from ')
    workflow_parser.add_argument('workflow_dir', type=os.path.abspath, help='path to a directory containing GitHub Actions workflows')

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    match args.command:
        case 'workflow':
            pass


if __name__ == '__main__':
    main()
