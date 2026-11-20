import os
import unittest
from unittest.mock import patch


with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
    from src import gemini_processor


class GeminiProcessorTests(unittest.TestCase):
    def test_client_uses_v1beta_api_for_generation(self):
        self.assertEqual(
            gemini_processor.client._api_client._http_options.api_version,
            "v1beta",
        )

    def test_default_model_is_supported_flash_lite(self):
        self.assertEqual(
            gemini_processor.DEFAULT_MODEL,
            "gemini-3.5-flash-lite",
        )

    @patch.object(gemini_processor, "MODEL", "")
    @patch.object(gemini_processor, "API_KEY", "")
    def test_resolver_uses_default_model_without_api_key(self):
        self.assertEqual(
            gemini_processor.resolve_gemini_model(),
            "gemini-3.5-flash-lite",
        )


if __name__ == "__main__":
    unittest.main()
