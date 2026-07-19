import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from PIL import Image

from market_map.build_treemap import GREEN, NEUTRAL, RED, _color_for, build_treemap


class MarketMapTreemapTests(unittest.TestCase):
    def test_directional_colors_are_distinct(self):
        down = _color_for(-0.02, 0.04)
        flat = _color_for(0.0, 0.04)
        up = _color_for(0.02, 0.04)
        self.assertEqual(flat, NEUTRAL)
        self.assertGreater(down[0], down[1])
        self.assertGreater(up[1], up[0])
        self.assertNotEqual(down, up)
        self.assertNotEqual(RED, GREEN)

    def test_renders_logo_badges(self):
        frame = pd.DataFrame([
            {"ticker": "AAPL", "market_cap": 60, "percent_change": 0.02, "logo_url": ""},
            {"ticker": "MSFT", "market_cap": 40, "percent_change": -0.02, "logo_url": ""},
        ])
        logo = Image.new("RGBA", (64, 64), (10, 20, 30, 255))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "map.png"
            with patch("market_map.build_treemap._fetch_logo", return_value=logo):
                result = build_treemap(frame, "Market map", str(output), width=800, height=450)
            self.assertEqual(Path(result), output)
            self.assertTrue(output.exists())
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (800, 450))


if __name__ == "__main__":
    unittest.main()
