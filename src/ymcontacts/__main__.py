"""Запуск пакета как программы: python -m ymcontacts <команда>."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())