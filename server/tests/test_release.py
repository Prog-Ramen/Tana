"""The example config loads, and the metrics render as Prometheus text."""
import re
import unittest
from pathlib import Path

import yaml

from tana import config as config_mod
from tana import metrics

ROOT = Path(__file__).resolve().parents[2]


class ExampleConfigs(unittest.TestCase):
    def test_the_web_apps_example_config_loads_over_the_defaults(self):
        cfg = config_mod.load(str(ROOT / "server" / "config.example.yaml"))
        self.assertEqual(cfg["assistant"]["backend"], "rameness")
        self.assertEqual(cfg["web"]["local_port"], 8005)
        self.assertIn("192.168.0.0/16", cfg["web"]["claude_code"]["allowed_networks"])
        unknown = set(yaml.safe_load((ROOT / "server" / "config.example.yaml").read_text())) - set(config_mod.DEFAULTS)
        self.assertEqual(unknown, set())                    # every section of the example is a real setting



class Metrics(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_counters_histograms_and_gauges_render_as_prometheus_text(self):
        metrics.inc("tana_chat_messages_total", brain="tana")
        metrics.inc("tana_chat_messages_total", brain="tana")
        metrics.observe("tana_reply_seconds", 0.7, brain="tana")
        text = metrics.render([("tana_web_sessions", "Signed-in sessions.", [({}, 3)])])
        self.assertIn('tana_chat_messages_total{brain="tana"} 2', text)
        self.assertIn('tana_reply_seconds_bucket{brain="tana",le="1"} 1', text)
        self.assertIn('tana_reply_seconds_bucket{brain="tana",le="0.5"} 0', text)
        self.assertIn('tana_reply_seconds_count{brain="tana"} 1', text)
        self.assertIn("# TYPE tana_web_sessions gauge\ntana_web_sessions 3", text)
        sample = re.compile(r'^[a-z_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? -?[0-9.e+]+$')
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, sample)

    def test_label_values_are_escaped(self):
        metrics.inc("tana_chat_messages_total", brain='say "hi"\nnow')
        self.assertIn('brain="say \\"hi\\" now"', metrics.render())


if __name__ == "__main__":
    unittest.main()
