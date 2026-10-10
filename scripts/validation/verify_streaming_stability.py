"""Command-line wrapper for repeated-session, long-audio and owned-process probes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.streaming.stability import main

if __name__ == "__main__":
    main()
