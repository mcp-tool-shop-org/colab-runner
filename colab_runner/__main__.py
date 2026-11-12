"""Entry point: `python -m colab_runner --version`."""

import sys

from colab_runner import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in ([], ["--help"], ["-h"]):
        print("usage: python -m colab_runner --version\n\nScaffold: no commands yet.")
        return 0
    if args == ["--version"]:
        print(__version__)
        return 0
    print(f"colab-runner: unknown arguments: {' '.join(args)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
