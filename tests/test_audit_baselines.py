import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _install_radar_infra_stubs():
    radar_infra = types.ModuleType("radar_infra")
    llm = types.ModuleType("radar_infra.llm")

    class DeepSeekProvider:
        pass

    class CachedLLMClient:
        def __init__(self, provider):
            self.provider = provider

    llm.DeepSeekProvider = DeepSeekProvider
    llm.CachedLLMClient = CachedLLMClient
    sys.modules["radar_infra"] = radar_infra
    sys.modules["radar_infra.llm"] = llm


_install_radar_infra_stubs()
audit_baselines = importlib.import_module("audit_baselines")


class AuditCredentialTests(unittest.TestCase):
    def test_blank_deepseek_secret_is_treated_as_missing(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": ""}, clear=True):
            self.assertFalse(audit_baselines._has_llm_credentials())

    def test_deepseek_secret_enables_audit(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
            self.assertTrue(audit_baselines._has_llm_credentials())

    def test_openai_secret_does_not_enable_deepseek_audit(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            self.assertFalse(audit_baselines._has_llm_credentials())


if __name__ == "__main__":
    unittest.main()
