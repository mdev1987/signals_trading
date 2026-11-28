"""Tests for streamed PumpAPI replay reading (no network, no PnL)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import zstandard

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from counterfactual_replay import (  # noqa: E402
    _open_lines_from_file,
    _truncate,
    _try_json,
    hour_url,
    iter_hour,
    schema_check,
)


def write_zst_jsonl(path: Path, objects: list[dict]) -> None:
    payload = "".join(json.dumps(o) + "\n" for o in objects).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))


class UrlTests(unittest.TestCase):
    def test_hour_url(self) -> None:
        self.assertEqual(
            hour_url("2026/11/27/10"), "https://replay.pumpapi.io/2026/11/27/10.jsonl.zst"
        )


class JsonTests(unittest.TestCase):
    def test_try_json(self) -> None:
        self.assertEqual(_try_json('{"a":1}'), {"a": 1})
        self.assertIsNone(_try_json(""))
        self.assertIsNone(_try_json("not json"))
        self.assertIsNone(_try_json("[1,2,3]"))  # non-dict rejected
        self.assertIsNone(_try_json("42"))

    def test_truncate(self) -> None:
        self.assertEqual(_truncate("x" * 500, max_len=10), "x" * 10 + "…")
        self.assertEqual(_truncate({"a": "y" * 500}, max_len=5)["a"], "y" * 5 + "…")


class StreamingTests(unittest.TestCase):
    def test_stream_lines_from_zst_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.jsonl.zst"
            write_zst_jsonl(path, [{"action": "buy", "mint": "M"}, {"action": "sell", "mint": "M"}])
            lines = list(_open_lines_from_file(path))
        self.assertEqual(len(lines), 2)
        self.assertIn('"action": "buy"', lines[0])

    def test_via_file_streams_filters_deletes_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            # stage a source zst and capture a copy of its bytes for re-serving
            src = scratch / "src.zst"
            write_zst_jsonl(src, [
                {"action": "buy", "mint": "M", "n": 1},
                {"action": "sell", "mint": "X", "n": 2},
                {"action": "sell", "mint": "M", "n": 3},
            ])
            payload = src.read_bytes()

            class FakeResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    self.close()
                    return False

            import counterfactual_replay as cr

            original = cr.urllib.request.urlopen

            def fake_urlopen(request, timeout=0):  # noqa: ANN001
                return FakeResp(payload)

            cr.urllib.request.urlopen = fake_urlopen
            try:
                got = list(
                    iter_hour(
                        "2026/11/27/10",
                        line_filter=lambda o: o.get("mint") == "M",
                        via_file=True,
                        scratch_dir=scratch,
                    )
                )
            finally:
                cr.urllib.request.urlopen = original
            # no scratch temp left behind
            self.assertEqual(list(scratch.glob("replay_*")), [])
        self.assertEqual([o["n"] for o in got], [1, 3])

    def test_limit_stops_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            src = scratch / "s.zst"
            write_zst_jsonl(src, [{"n": i} for i in range(100)])
            payload = src.read_bytes()
            import counterfactual_replay as cr

            class FakeResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            original = cr.urllib.request.urlopen
            cr.urllib.request.urlopen = lambda request, timeout=0: FakeResp(payload)
            try:
                got = list(iter_hour("x/y/z/w", via_file=True, scratch_dir=scratch, limit=5))
            finally:
                cr.urllib.request.urlopen = original
        self.assertEqual(len(got), 5)


class SchemaCheckTests(unittest.TestCase):
    def test_schema_check_unions_keys_and_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            src = scratch / "s.zst"
            write_zst_jsonl(src, [
                {"action": "buy", "mint": "M", "price": 1.0},
                {"action": "sell", "mint": "M", "priceSol": 0.5, "extra": True},
            ])
            payload = src.read_bytes()
            import counterfactual_replay as cr

            class FakeResp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            original = cr.urllib.request.urlopen
            cr.urllib.request.urlopen = lambda request, timeout=0: FakeResp(payload)
            try:
                report = schema_check("x/y/z/w", sample=1, scan=100, via_file=True)
            finally:
                cr.urllib.request.urlopen = original
        self.assertEqual(report["scanned"], 2)
        self.assertIn("price", report["keys_union"])
        self.assertIn("priceSol", report["keys_union"])
        self.assertIn("extra", report["keys_union"])
        self.assertEqual(len(report["sample"]), 1)
        self.assertIn("No PnL computed", report["note"])
        self.assertIn("real fee unmeasured", report["note"])


if __name__ == "__main__":
    unittest.main()
