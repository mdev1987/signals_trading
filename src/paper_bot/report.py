"""Human-readable summary of a paper run directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .store import summarize


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.run_dir.is_dir():
        raise SystemExit(f"Not a directory: {args.run_dir}")
    summary_path = args.run_dir / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        summary = summarize(args.run_dir)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
