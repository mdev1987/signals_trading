#!/usr/bin/env python3
"""Streamed PumpAPI replay reader (one hour at a time) for counterfactuals.

This is the **mechanics**, not a PnL engine, and it is not a shipping test. It
enforces the hard replay rules from ``docs/ENTRY2S_LANE.md``:

* **stream**, never fully load; line-buffered decompression;
* **one hour at a time**; a file/hour is finished before the next is opened;
* **old files deleted before downloading new ones**; a scratch temp is removed
  first and always cleaned up;
* raw replay is **never persisted**; only a caller's filtered subset, if asked,
  is written and then removed;
* **no PnL is computed here** — PnL needs the measured real all-in fee (still
  unmeasured) and the verified schema; recall `execution_simulator` is the
  reference fill model and `sold_share_15s` was *not* a robust filter.

Source: ``https://replay.pumpapi.io/YYYY/MM/DD/HH.jsonl.zst`` (zstd JSONL).

Examples::

    env PYTHONPATH=src .venv/bin/python src/counterfactual_replay.py \
        --schema-check 2026/11/27/10 --sample 3 --scan 500
    env PYTHONPATH=src .venv/bin/python src/counterfactual_replay.py \
        --schema-check 2026/11/27/10 --mints <mint1>,<mint2>
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import zstandard

REPLAY_BASE = "https://replay.pumpapi.io"
TEMP_NAME = "pumpapi_replay_hour.jsonl.zst"
DEFAULT_SCRATCH = Path("/tmp/opencode")

LineFilter = Callable[[dict[str, Any]], bool]


def hour_url(yyyy_mm_dd_hh: str) -> str:
    """``YYYY/MM/DD/HH`` -> full replay URL."""
    return f"{REPLAY_BASE}/{yyyy_mm_dd_hh.strip('/')}.jsonl.zst"


def _clean(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


@contextmanager
def _scratch(src_dir: Path | None = None) -> Iterator[Path]:
    base = src_dir or DEFAULT_SCRATCH
    base.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="replay_", dir=str(base)))
    try:
        yield tmpdir / TEMP_NAME
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _open_lines_from_file(path: Path) -> Iterator[str]:
    dctx = zstandard.ZstdDecompressor()
    with path.open("rb") as raw:
        with dctx.stream_reader(raw) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
            for line in text:
                yield line


def _open_lines_from_url(url: str, timeout: float) -> Iterator[str]:
    """Stream HTTP -> zstd -> lines; never buffer the whole payload."""
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "signals-trading-research/1.0"})
    response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - fixed https host
    try:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(response) as reader:
            text = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
            for line in text:
                yield line
    finally:
        response.close()


def iter_hour(
    yyyy_mm_dd_hh: str,
    *,
    line_filter: LineFilter | None = None,
    limit: int | None = None,
    timeout: float = 120.0,
    via_file: bool = False,
    scratch_dir: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from one replay hour, streamed.

    ``via_file`` downloads to a scratch temp, streams it, then **deletes the
    old file before returning**; the scratch dir is removed on exit. By default
    (``via_file=False``) HTTP is streamed straight into the decompressor and
    nothing is persisted. A pre-existing temp is removed before downloading.
    """
    url = hour_url(yyyy_mm_dd_hh)
    emitted = 0

    def accept(obj: dict[str, Any]) -> bool:
        return line_filter is None or line_filter(obj)

    if via_file:
        with _scratch(scratch_dir) as tmp:
            _clean(tmp)  # delete any old file before downloading new ones
            with urllib.request.urlopen(  # noqa: S310 - fixed https host
                urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "signals-trading-research/1.0"}),
                timeout=timeout,
            ) as response, tmp.open("wb") as out:
                shutil.copyfileobj(response, out, length=1 << 20)  # stream copy
            try:
                for line in _open_lines_from_file(tmp):
                    obj = _try_json(line)
                    if obj is None or not accept(obj):
                        continue
                    yield obj
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        break
            finally:
                _clean(tmp)  # delete after filtering, before any next hour
        return

    for line in _open_lines_from_url(url, timeout):
        obj = _try_json(line)
        if obj is None or not accept(obj):
            continue
        yield obj
        emitted += 1
        if limit is not None and emitted >= limit:
            break


def _try_json(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def schema_check(
    yyyy_mm_dd_hh: str,
    *,
    sample: int = 3,
    scan: int = 500,
    mints: set[str] | None = None,
    timeout: float = 120.0,
    via_file: bool = False,
) -> dict[str, Any]:
    """Scan up to ``scan`` streamed lines; return schema + a small sample.

    No PnL. Honest: schema varies; keys are the union seen; samples truncated.
    """
    keys: set[str] = set()
    types: dict[str, set[str]] = {}
    sample_rows: list[dict[str, Any]] = []
    total = 0
    matched = 0

    def keep(obj: dict[str, Any]) -> bool:
        if not mints:
            return True
        # don't hardcode the field name; check common spots
        for field in ("mint", "token", "mintAddress", "baseMint", "tokenMint"):
            if obj.get(field) in mints:
                return True
        return False

    for obj in iter_hour(
        yyyy_mm_dd_hh, line_filter=keep, timeout=timeout, via_file=via_file
    ):
        total += 1
        for key, value in obj.items():
            keys.add(key)
            types.setdefault(key, set()).add(type(value).__name__)
        if mints:
            matched += 1
        if len(sample_rows) < sample:
            sample_rows.append({k: _truncate(v) for k, v in obj.items()})
        if total >= scan:
            break

    return {
        "url": hour_url(yyyy_mm_dd_hh),
        "scanned": total,
        "mint_filter_matched": matched if mints else None,
        "keys_union": sorted(keys),
        "types": {k: sorted(v) for k, v in types.items()},
        "sample": sample_rows,
        "note": "schema varies; verify before any PnL. No PnL computed; real fee unmeasured.",
    }


def _truncate(value: Any, max_len: int = 200) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, max_len) for v in value[:5]]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-check", metavar="YYYY/MM/DD/HH", required=True)
    parser.add_argument("--sample", type=int, default=3)
    parser.add_argument("--scan", type=int, default=500)
    parser.add_argument("--mints", default="", help="comma-separated mint filter (schema field auto-detected)")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--via-file", action="store_true", help="download to a scratch temp, stream it, delete")
    args = parser.parse_args(argv)

    mints = {m.strip() for m in args.mints.split(",") if m.strip()} or None
    report = schema_check(
        args.schema_check, sample=args.sample, scan=args.scan, mints=mints,
        timeout=args.timeout, via_file=args.via_file,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
