import json
import tempfile
import unittest
from pathlib import Path

from engine.cache import AnalysisCache, CacheKey
from engine.config import AppConfig
from engine.protocol import parse_engine_line
from engine.state import AnalysisState, EngineController


class ProtocolTests(unittest.TestCase):
    def test_info_and_jq_are_parsed(self):
        info = parse_engine_line("info depth 12 multipv 2 score cp -34 pv a0a1 b0b1")
        self.assertEqual(info.kind, "info")
        self.assertEqual(info.info.depth, 12)
        self.assertEqual(info.info.multipv, 2)
        self.assertEqual(info.info.pv, ("a0a1", "b0b1"))
        payload = {"move": "a0a1", "mode": "Worst", "identities": {"R": -20, "B": 4}}
        jq = parse_engine_line("info string JQ " + json.dumps(payload))
        self.assertEqual(jq.kind, "jq")

    def test_malformed_jq_does_not_break_uci(self):
        self.assertEqual(parse_engine_line("info string JQ {").kind, "jq")
        self.assertEqual(parse_engine_line("bestmove a0a1").value, "a0a1")


class ControllerTests(unittest.TestCase):
    def test_pending_search_starts_only_after_bestmove(self):
        sent = []
        snapshots = []
        controller = EngineController(sent.append, snapshots.append)
        controller.request_search("position fen one", "go infinite")
        controller.request_search("position fen two", "go infinite")
        self.assertEqual(sent, ["position fen one", "go infinite", "stop"])
        self.assertEqual(controller.state, AnalysisState.STOPPING)
        controller.on_line("info depth 99 score cp 900 pv a0a1")
        self.assertEqual(len(snapshots), 0)
        controller.on_line("bestmove a0a1")
        self.assertEqual(sent[-2:], ["position fen two", "go infinite"])
        self.assertEqual(controller.state, AnalysisState.SEARCHING)

    def test_jq_snapshot_is_attached_to_current_move(self):
        sent = []
        snapshots = []
        controller = EngineController(sent.append, snapshots.append)
        controller.request_search("position fen one", "go infinite")
        controller.on_line("info depth 2 score cp 10 pv a0a1")
        controller.on_line('info string JQ {"move":"a0a1","mode":"Worst","identities":{"R":{"score":-3,"count":2}}}')
        self.assertEqual(snapshots[-1].recommend, "a0a1")
        self.assertEqual(snapshots[-1].identities[0].identities["R"]["score"], -3)


class CacheAndConfigTests(unittest.TestCase):
    def test_cache_is_lru_and_bounded(self):
        cache = AnalysisCache(1)
        first = CacheKey("one", (), 1)
        second = CacheKey("two", (), 1)
        from engine.state import AnalysisSnapshot
        cache.put(first, AnalysisSnapshot(1))
        cache.put(second, AnalysisSnapshot(2))
        self.assertIsNone(cache.get(first))
        self.assertEqual(cache.get(second).generation, 2)

    def test_config_round_trip_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig(engine_path="x", threads=0, dark_mode="bad")
            config.save(path)
            loaded = AppConfig.load(path)
            self.assertEqual(loaded.engine_path, "x")
            self.assertEqual(loaded.threads, 1)
            self.assertEqual(loaded.dark_mode, "Expected")


if __name__ == "__main__":
    unittest.main()