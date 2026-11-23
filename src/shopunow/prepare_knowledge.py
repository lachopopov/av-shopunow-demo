# Python 3.12; explicit local preparation only, without Cloud or embedding calls.
import argparse
from pathlib import Path
from shopunow.knowledge import prepare_knowledge


def main():
    parser = argparse.ArgumentParser(description='Package approved Knowledge and source index; offline.')
    parser.add_argument('source', type=Path)
    parser.add_argument('--destination', type=Path, default=Path(__file__).with_name('assets'))
    options = parser.parse_args()
    prepare_knowledge(options.source, options.destination)


if __name__ == '__main__':
    main()


# Flow: explicit source argument -> strict validation -> byte-identical source plus derived digest index.
