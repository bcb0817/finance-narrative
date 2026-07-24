import unittest
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from common.json_utils import make_json_safe


class Label(Enum):
    READY = "ready"


@dataclass
class Artifact:
    path: Path
    created_at: datetime


class JsonUtilsTests(unittest.TestCase):
    def test_nested_path_and_windows_compatible_path(self):
        value={"generated_files":[Path("C:/Program Files/report.json")],
               "nested":{"output_path":Path("D:/SNS Bot/output.md")}}
        result=make_json_safe(value)
        self.assertIsInstance(result["generated_files"][0],str)
        self.assertIsInstance(result["nested"]["output_path"],str)

    def test_datetime_enum_decimal_set_tuple_and_dataclass(self):
        now=datetime(2026,7,25,3,45,tzinfo=timezone.utc)
        result=make_json_safe({
            "when":now,"day":date(2026,7,25),"status":Label.READY,
            "cost":Decimal("0.125"),"tags":{"a","b"},"pair":(1,2),
            "artifact":Artifact(Path("report.json"),now),
        })
        self.assertEqual(result["when"],now.isoformat())
        self.assertEqual(result["day"],"2026-07-25")
        self.assertEqual(result["status"],"ready")
        self.assertEqual(result["cost"],0.125)
        self.assertEqual(set(result["tags"]),{"a","b"})
        self.assertEqual(result["pair"],[1,2])
        self.assertEqual(result["artifact"]["path"],"report.json")

    def test_arbitrary_objects_are_not_silently_stringified(self):
        with self.assertRaises(TypeError):
            make_json_safe(object())


if __name__ == "__main__":
    unittest.main()
