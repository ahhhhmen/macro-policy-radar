import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


def _install_radar_infra_stubs():
    radar_infra = types.ModuleType("radar_infra")
    llm = types.ModuleType("radar_infra.llm")
    guard = types.ModuleType("radar_infra.guard")
    sink = types.ModuleType("radar_infra.sink")
    support = types.ModuleType("radar_infra.support")

    class DeepSeekProvider:
        pass

    class CachedLLMClient:
        def __init__(self, provider):
            self.provider = provider

    def create_llm_retry_decorator(max_attempts=3):
        def decorator(func):
            return func
        return decorator

    class CircuitBreaker:
        def __init__(self, *args, **kwargs):
            pass

    def sanitize_numbers(text, fetched_text):
        return text, 0

    def send_dingtalk(*args, **kwargs):
        return None

    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    def setup_logging(name):
        return _Logger()

    llm.DeepSeekProvider = DeepSeekProvider
    llm.CachedLLMClient = CachedLLMClient
    llm.create_llm_retry_decorator = create_llm_retry_decorator
    guard.CircuitBreaker = CircuitBreaker
    guard.sanitize_numbers = sanitize_numbers
    sink.send_dingtalk = send_dingtalk
    support.setup_logging = setup_logging

    sys.modules["radar_infra"] = radar_infra
    sys.modules["radar_infra.llm"] = llm
    sys.modules["radar_infra.guard"] = guard
    sys.modules["radar_infra.sink"] = sink
    sys.modules["radar_infra.support"] = support


_install_radar_infra_stubs()
main = importlib.import_module("main")


def _policy(**overrides):
    data = {
        "policy_dynamics": {
            "current_stage": "Fully_Effective",
            "policy_name_zh": "测试政策",
        },
        "strategic_implications": {
            "supply_chain_impact_level": "High_Disruption",
            "analytic_confidence": "High",
        },
        "event_update": {
            "event_classification": "Milestone_Amendment",
            "event_summary": "测试事件",
        },
        "news_recency_verification": {
            "article_type": "News_Report",
        },
        "notion_integration": {
            "dingtalk_alert_required": True,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data


class PushGateTests(unittest.TestCase):
    def test_routine_commentary_mutes_even_when_impact_is_high(self):
        data = _policy(event_update={
            "event_classification": "Routine_Commentary",
            "event_summary": "重复报道",
        })

        should_push, reason = main._should_push(data, is_new_document=False)

        self.assertFalse(should_push)
        self.assertIn("绝对静默", reason)

    def test_semantic_diff_mute_wins_over_high_impact(self):
        data = _policy(_material_change={
            "has_material_change": False,
            "change_summary": "",
        })

        should_push, reason = main._should_push(data, is_new_document=False)

        self.assertFalse(should_push)
        self.assertIn("语义Diff", reason)

    def test_number_sanitizer_hit_blocks_push(self):
        data = _policy(_numbers_flagged=2)

        should_push, reason = main._should_push(data, is_new_document=True)

        self.assertFalse(should_push)
        self.assertIn("数字净化", reason)

    def test_explicit_notion_gate_false_blocks_push(self):
        data = _policy(notion_integration={"dingtalk_alert_required": False})

        should_push, reason = main._should_push(data, is_new_document=True)

        self.assertFalse(should_push)
        self.assertIn("不推送", reason)


class NotionSignatureDedupeTests(unittest.TestCase):
    def test_search_by_rich_text_queries_file_signature(self):
        os.environ["NOTION_TOKEN"] = "test-token"
        os.environ["NOTION_DATABASE_ID"] = "test-db"
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"results": [{"id": "page-1"}]},
        )

        with patch.object(main.requests, "post", return_value=response) as post:
            exists, page_id = main._notion_search_by_rich_text("文件签名", "CN-rare-earth-doc")

        self.assertTrue(exists)
        self.assertEqual(page_id, "page-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["filter"]["property"], "文件签名")
        self.assertEqual(payload["filter"]["rich_text"]["equals"], "CN-rare-earth-doc")


if __name__ == "__main__":
    unittest.main()
