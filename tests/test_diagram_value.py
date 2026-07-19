import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "news_bot"))
sys.path.insert(0, str(ROOT / "src" / "common"))

from diagram_post import assess_diagram_value  # noqa: E402


class FakeClient:
    def __init__(self, payload):
        message = SimpleNamespace(content=__import__("json").dumps(payload))
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: response)
        )


ITEM = SimpleNamespace(title="test headline", source="test source")


def test_clear_high_value_comparison_is_diagrammed():
    result = assess_diagram_value(ITEM, FakeClient({
        "score": 8, "has_clear_structure": True,
        "structure_type": "compare", "fact_count": 4,
        "numeric_fact_count": 0, "reason": "比較材料が十分",
    }), "test-model")
    assert result["should_diagram"] is True


def test_high_score_without_structure_falls_back_to_text():
    result = assess_diagram_value(ITEM, FakeClient({
        "score": 9, "has_clear_structure": False,
        "structure_type": "none", "fact_count": 1,
        "numeric_fact_count": 1,
    }), "test-model")
    assert result["should_diagram"] is False


def test_multi_metric_requires_two_numeric_facts():
    result = assess_diagram_value(ITEM, FakeClient({
        "score": 8, "has_clear_structure": True,
        "structure_type": "multi_metric", "fact_count": 3,
        "numeric_fact_count": 1,
    }), "test-model")
    assert result["should_diagram"] is False


def test_api_failure_falls_back_to_text():
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    ))
    assert assess_diagram_value(ITEM, client, "test-model")["should_diagram"] is False
