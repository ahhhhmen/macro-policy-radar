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
    fetch = types.ModuleType("radar_infra.fetch")

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

    import json
    def safe_json_parse(raw):
        try:
            return json.loads(raw)
        except Exception:
            return None

    def send_dingtalk(*args, **kwargs):
        return None

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    def setup_logging(name):
        return _Logger()

    def fetch_html(url, selector=None, **kwargs):
        return "Mocked HTML Content"

    def extract_article_body(url, **kwargs):
        return "Mocked Article Body Content"

    def clean_chinese_title_noise(s):
        return s

    def clean_title_noise(s):
        return s

    def get_tokens(s):
        return set(s.split())

    def calculate_title_similarity(s1, s2):
        return 1.0

    class NotionSink:
        def __init__(self, token=None, database_id=None):
            self._token = token or "mock_token"
            self._db_id = database_id or "mock_db"
        def api_request(self, method, url, **kwargs):
            import requests
            return requests.request(method, url, **kwargs)

    class NotionAPIError(Exception):
        def __init__(self, status_code, message, response_text=""):
            self.status_code = status_code
            self.message = message
            self.response_text = response_text

    llm.DeepSeekProvider = DeepSeekProvider
    llm.CachedLLMClient = CachedLLMClient
    llm.create_llm_retry_decorator = create_llm_retry_decorator
    guard.CircuitBreaker = CircuitBreaker
    guard.sanitize_numbers = sanitize_numbers
    guard.safe_json_parse = safe_json_parse
    guard.clean_chinese_title_noise = clean_chinese_title_noise
    guard.clean_title_noise = clean_title_noise
    guard.get_tokens = get_tokens
    guard.calculate_title_similarity = calculate_title_similarity
    sink.send_dingtalk = send_dingtalk
    sink.NotionSink = NotionSink
    sink.NotionAPIError = NotionAPIError
    support.setup_logging = setup_logging
    fetch.fetch_html = fetch_html
    fetch.extract_article_body = extract_article_body

    sys.modules["radar_infra"] = radar_infra
    sys.modules["radar_infra.llm"] = llm
    sys.modules["radar_infra.guard"] = guard
    sys.modules["radar_infra.sink"] = sink
    sys.modules["radar_infra.support"] = support
    sys.modules["radar_infra.fetch"] = fetch


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

        with patch("requests.request", return_value=response) as req:
            exists, page_id = main._notion_search_by_rich_text("文件签名", "CN-rare-earth-doc")

        self.assertTrue(exists)
        self.assertEqual(page_id, "page-1")
        payload = req.call_args.kwargs["json"]
        self.assertEqual(payload["filter"]["property"], "文件签名")
        self.assertEqual(payload["filter"]["rich_text"]["equals"], "CN-rare-earth-doc")


class GoogleNewsResolutionTests(unittest.TestCase):
    def test_resolve_valid_news_url(self):
        # Non-google URLs should be returned unchanged
        url = "https://example.com/article"
        self.assertEqual(main.resolve_google_news_url(url), url)

    def test_resolve_google_news_locally(self):
        # Verify local base64 decode works
        target_url = "https://www.business-humanrights.org/en/latest-news/global-mineral-tracker-2026"
        # Create a mock base64 protobuf encoded payload
        import base64
        fake_protobuf = b"\x08\x01\x12\x4b" + target_url.encode('utf-8') + b"\x1a\x05stuff"
        encoded = base64.urlsafe_b64encode(fake_protobuf).decode('utf-8').rstrip('=')
        google_url = f"https://news.google.com/rss/articles/{encoded}"

        # We mock googlenewsdecoder importing/failing to force fallbacks
        with patch("googlenewsdecoder.GoogleDecoder.decode_google_news_url", side_effect=Exception("mock fail")):
            resolved = main.resolve_google_news_url(google_url)
            self.assertEqual(resolved, target_url)

    def test_resolve_google_news_query_params(self):
        # Verify query parameters fallback
        google_url = "https://news.google.com/rss/articles/foo?url=https%3A%2F%2Fexample.com%2Farticle"
        with patch("googlenewsdecoder.GoogleDecoder.decode_google_news_url", side_effect=Exception("mock fail")):
            resolved = main.resolve_google_news_url(google_url)
            self.assertEqual(resolved, "https://example.com/article")

    @patch("main.requests.head")
    def test_resolve_google_news_via_head_redirect(self, mock_head):
        # Verify HTTP HEAD redirection works
        google_url = "https://news.google.com/rss/articles/example"
        redirected_url = "https://publisher.example.com/story"
        
        mock_resp = types.SimpleNamespace(url=redirected_url)
        mock_head.return_value = mock_resp

        with patch("googlenewsdecoder.GoogleDecoder.decode_google_news_url", side_effect=Exception("mock fail")):
            resolved = main.resolve_google_news_url(google_url)
            self.assertEqual(resolved, redirected_url)

    @patch("main.requests.get")
    @patch("main.requests.head")
    def test_resolve_google_news_via_html_canonical(self, mock_head, mock_get):
        # Verify HTML canonical resolution fallback
        google_url = "https://news.google.com/rss/articles/example"
        original = "https://publisher.example.com/story"

        # Mock HEAD not redirecting (returning same URL)
        mock_head.return_value = types.SimpleNamespace(url=google_url)
        
        # Mock GET returning HTML containing canonical link
        html_content = f'<html><head><link rel="canonical" href="{original}"></head></html>'
        mock_get.return_value = types.SimpleNamespace(url=google_url, text=html_content)

        with patch("googlenewsdecoder.GoogleDecoder.decode_google_news_url", side_effect=Exception("mock fail")):
            resolved = main.resolve_google_news_url(google_url)
            self.assertEqual(resolved, original)

    def test_filter_invalid_news_urls(self):
        # Test that _is_valid_news_url correctly flags trackers and styles
        self.assertFalse(main._is_valid_news_url("https://fonts.googleapis.com/css?family=Google+Sans"))
        self.assertFalse(main._is_valid_news_url("https://www.google-analytics.com/collect"))
        self.assertFalse(main._is_valid_news_url("https://example.com/style.css"))
        self.assertTrue(main._is_valid_news_url("https://reuters.com/news-story-1"))


if __name__ == "__main__":
    unittest.main()
