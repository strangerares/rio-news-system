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


if __name__ == "__main__":
    unittest.main()
