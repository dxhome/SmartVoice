"""Run a real-time simultaneous Chinese→English / English→Chinese soak."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from benchmarks.streaming.duplex_soak import cli

if __name__=='__main__':cli()
