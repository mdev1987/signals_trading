from __future__ import annotations

import csv
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "output" / "backtest"
REPORT = OUT / "preflight_report.json"
BUNDLE = OUT / "backtest_source_bundle.zip"

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
}

CSV_TERMS = (
    "ohlcv_1m",
    "pool_selection_timeline",
    "candidate_execution_audit",
    "candidate_orders",
    "candidate_fills",
    "pool_compatibility",
    "signals",
    "orders",
    "trades",
)

SOURCE_EXTENSIONS = {
    ".py", ".md", ".toml", ".lock", ".yaml", ".yml", ".ini", ".cfg"
}

SOURCE_TERMS = (
    "backtest", "strategy", "signal", "portfolio", "simulator",
    "execution", "risk", "position", "order", "ohlcv",
)

KEY_FIELDS = (
    "timestamp", "mint", "pool", "pool_id", "side", "signal",
    "price", "amount", "volume", "execution", "available",
    "fee", "slippage", "return", "equity", "position",
)

STRATEGY_MARKERS = (
    "initial_capital",
    "position_size",
    "position_sizing",
    "max_positions",
    "take_profit",
    "stop_loss",
    "trailing_stop",
    "portfolio",
    "realized_pnl",
    "unrealized_pnl",
    "drawdown",
    "available_at_timestamp_ms",
    "execution_supported",
    "pool_id",
    "priorityfee",
    "slippage",
)


def run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except OSError:
        return ""


def discover_files() -> list[Path]:
    found = []

    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for name in names:
            path = Path(base) / name
            if path.is_file():
                found.append(path)

    return found


def inspect_csv(path: Path) -> dict:
    result = {
        "path": str(path.relative_to(ROOT)),
        "size_bytes": path.stat().st_size,
        "columns": [],
        "rows_scanned": 0,
        "row_count_complete": False,
        "timestamp_ranges": {},
        "sample_rows": [],
    }

    time_values: dict[str, list[int]] = {}

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            result["columns"] = reader.fieldnames or []

            time_columns = [
                c for c in result["columns"]
                if "timestamp" in c.lower() or c.lower() == "time"
            ]
            time_values = {c: [] for c in time_columns}

            for row in reader:
                result["rows_scanned"] += 1

                for col in time_columns:
                    value = (row.get(col) or "").strip()
                    try:
                        time_values[col].append(int(float(value)))
                    except (TypeError, ValueError):
                        pass

                if len(result["sample_rows"]) < 2:
                    result["sample_rows"].append({
                        key: (value or "")[:120]
                        for key, value in row.items()
                        if key and any(term in key.lower() for term in KEY_FIELDS)
                    })

                if result["rows_scanned"] >= 500_000:
                    break

            result["row_count_complete"] = result["rows_scanned"] < 500_000

        for col, values in time_values.items():
            if values:
                result["timestamp_ranges"][col] = {
                    "min": min(values),
                    "max": max(values),
                }

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def inspect_json(path: Path) -> dict:
    item = {
        "path": str(path.relative_to(ROOT)),
        "size_bytes": path.stat().st_size,
    }

    try:
        if path.stat().st_size > 5_000_000:
            item["note"] = "Skipped: JSON larger than 5 MB"
            return item

        data = json.loads(path.read_text(encoding="utf-8"))
        item["top_level_type"] = type(data).__name__

        if isinstance(data, dict):
            item["top_level_keys"] = list(data.keys())[:100]
            for key in (
                "checks_passed", "buy", "sell", "validation",
                "pool_selection_validation", "mints_total",
                "mints_included", "real_bars_included",
                "execution_supported_rows", "reserve_model_counts",
            ):
                if key in data:
                    item[key] = data[key]

    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"

    return item


all_files = discover_files()

git = {
    "root": run_git("rev-parse", "--show-toplevel"),
    "branch": run_git("branch", "--show-current"),
    "commit": run_git("rev-parse", "HEAD"),
    "status": run_git("status", "--short"),
    "recent_commits": run_git("log", "-5", "--oneline"),
}

expected = [
    "raw_to_clean_ohlcv.py",
    "build_vbt_dataset.py",
    "execution_simulator.py",
    "candidate_execution_audit.py",
    "output/ohlcv_clean_v7_1_1/ohlcv_1m.csv",
    "output/vbt_dataset_pit_v7_2_1m/pool_selection_timeline.csv",
    "output/vbt_dataset_pit_v7_2_1m/ohlcv_clean_1m.csv",
    "output/execution_audit/candidate_execution_audit.csv",
    "output/execution_audit/candidate_execution_audit_summary.json",
]

expected_files = []
for rel in expected:
    path = ROOT / rel
    expected_files.append({
        "path": rel,
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else None,
    })

csv_files = []
json_files = []
source_files = []
marker_counts = {marker: 0 for marker in STRATEGY_MARKERS}

for path in all_files:
    rel = path.relative_to(ROOT)
    rel_parts = set(rel.parts)

    if rel_parts.intersection({"output", "data", "datasets"}):
        pass

    if path.suffix.lower() == ".csv":
        lower = path.name.lower()
        if any(term in lower for term in CSV_TERMS):
            csv_files.append(inspect_csv(path))

    if path.suffix.lower() == ".json" and (
        "report" in path.name.lower()
        or "summary" in path.name.lower()
        or "audit" in path.name.lower()
    ):
        json_files.append(inspect_json(path))

    if (
        path.suffix.lower() in SOURCE_EXTENSIONS
        and not rel_parts.intersection({"output", "data", "datasets"})
        and path.stat().st_size <= 2_000_000
    ):
        if (
            path.suffix.lower() in {".py", ".md", ".toml", ".lock", ".yaml", ".yml"}
            and (
                path.suffix.lower() != ".py"
                or any(term in path.name.lower() for term in SOURCE_TERMS)
                or path.name.startswith("test_")
            )
        ):
            source_files.append(path)

            if path.suffix.lower() in {".py", ".md"}:
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore").lower()
                    for marker in STRATEGY_MARKERS:
                        marker_counts[marker] += content.count(marker)
                except OSError:
                    pass

packages = {}
for package in ("numpy", "pandas", "vectorbt", "numba", "orjson", "pytest"):
    try:
        packages[package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        packages[package] = None

raw_path = Path(
    os.environ.get(
        "RAW_REPLAY",
        "/mnt/HDD/PumpAPI_Replay_Zst/2026-11-08/20261108_05.jsonl.zst",
    )
)

report = {
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "project": str(ROOT),
    "git": git,
    "runtime": {
        "python": sys.version,
        "platform": platform.platform(),
        "packages": packages,
    },
    "raw_replay": {
        "path": str(raw_path),
        "exists": raw_path.is_file(),
        "size_bytes": raw_path.stat().st_size if raw_path.is_file() else None,
    },
    "expected_files": expected_files,
    "csv_inputs": csv_files,
    "json_reports": json_files,
    "strategy_code_markers": marker_counts,
    "source_files_in_bundle": [
        str(p.relative_to(ROOT)) for p in sorted(source_files)
    ],
}

OUT.mkdir(parents=True, exist_ok=True)
REPORT.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

with zipfile.ZipFile(BUNDLE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(source_files):
        zf.write(path, path.relative_to(ROOT))

    zf.write(REPORT, REPORT.relative_to(ROOT))

print(json.dumps({
    "git": git,
    "raw_replay": report["raw_replay"],
    "expected_files": expected_files,
    "csv_files": [
        {
            "path": x["path"],
            "rows_scanned": x["rows_scanned"],
            "columns": x["columns"],
            "timestamp_ranges": x["timestamp_ranges"],
        }
        for x in csv_files
    ],
    "strategy_code_markers": marker_counts,
    "source_file_count": len(source_files),
    "report": str(REPORT.relative_to(ROOT)),
    "source_bundle": str(BUNDLE.relative_to(ROOT)),
    "source_bundle_bytes": BUNDLE.stat().st_size,
}, ensure_ascii=False, indent=2))
