"""Command-line wrapper for open-loop streaming capacity measurements."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.streaming.capacity import main

if __name__ == "__main__":
    main()
