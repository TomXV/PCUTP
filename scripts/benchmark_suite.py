"""Run the representative compression/fault matrix with audible UART feedback.

Prepare immutable fixtures first, then pass their paths. Files on PicoCalc are
written only as PCBENCH.BIN by hardware_benchmark.py. Stop on any failed case.
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basic", required=True)
    parser.add_argument("--mod", required=True)
    parser.add_argument("--gzip", required=True)
    parser.add_argument("--random", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    for name in ("basic", "mod", "gzip", "random"):
        for mode in ("off", "auto"):
            cases.append((f"{name}-{mode}", ["--file", getattr(args, name),
                          "--compression", mode, "--blocks", "auto", "--repeat", "3"]))
    cases.extend([
        ("raw-crc", ["--compression", "off", "--corrupt-seq", "1"]),
        ("lz4-crc", ["--corrupt-seq", "1"]),
        ("lost-last-ack", ["--compression", "off", "--drop-ack", "7"]),
    ])
    for name, extra in cases:
        print(f"Testing {name}", flush=True)
        subprocess.run([
            sys.executable, str(Path(__file__).with_name("hardware_benchmark.py")),
            "--program", "B:/PCFAST.BAS", "--window", "2", "--blocks", "4096",
            "--repeat", "1", "--output", str(args.output_dir / f"{name}.json"), *extra,
        ], check=True)


if __name__ == "__main__":
    main()
