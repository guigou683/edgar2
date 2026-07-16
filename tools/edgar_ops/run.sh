#!/usr/bin/env bash
# Lance la console d'exploitation EDGAR (interface Tkinter).
cd "$(dirname "$0")/../.." && exec python3 tools/edgar_ops/ui.py "$@"
