# Python 3.12; explicitly invoked local preparation, never application startup.
import argparse
from pathlib import Path
from shopunow.profiles import prepare


def main():
    parser = argparse.ArgumentParser(description='Prepare approved synthetic roster; no Cloud calls.')
    parser.add_argument('source', type=Path)
    parser.add_argument('--created-at', required=True, help='Explicit deterministic preparation timestamp.')
    options = parser.parse_args()
    prepare(options.source, Path(__file__).with_name('assets'), created_at=options.created_at)


if __name__ == '__main__':
    main()
