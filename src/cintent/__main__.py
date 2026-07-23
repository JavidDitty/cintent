from argparse import ArgumentParser, Namespace
import os


def parse_args() -> Namespace:
    """Parse CLI arguments"""
    parser = ArgumentParser(description='Summarize the intent of GitHub Actions workflows')
    subparsers = parser.add_subparsers(dest='command', required=True, help='Available commands')

    preprocess_parser = subparsers.add_parser('preprocess', help='Preprocess a CIMonitor Archive')
    preprocess_parser.add_argument()

    analyze_parser = subparsers.add_parser('analyze', help='Analyze a Preprocessed CIMonitor Archive')
    analyze_parser.add_argument()

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    match args.command:
        case 'preprocess':
            pass
        case 'analyze':
            pass
        case _:
            pass


if __name__ == '__main__':
    main()
