import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from scripts import list_gemini_models


class GeminiModelListTests(unittest.TestCase):
    @staticmethod
    def _response(payload):
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda: response
        response.__exit__ = lambda *_: None
        return response

    @patch("scripts.list_gemini_models.urllib.request.urlopen")
    def test_lists_only_generation_models_across_pages(self, urlopen):
        urlopen.side_effect = [
            self._response({
                "models": [
                    {
                        "name": "models/gemini-alpha",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ],
                "nextPageToken": "next-page",
            }),
            self._response({
                "models": [
                    {
                        "name": "models/gemini-beta",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ],
            }),
        ]

        models = list_gemini_models.list_generate_content_models("secret")

        self.assertEqual(
            models,
            ["models/gemini-alpha", "models/gemini-beta"],
        )
        first_request = urlopen.call_args_list[0].args[0]
        self.assertNotIn("secret", first_request.full_url)
        self.assertEqual(first_request.get_header("X-goog-api-key"), "secret")
        second_url = urlopen.call_args_list[1].args[0].full_url
        self.assertIn("pageToken=next-page", second_url)

    @patch("scripts.list_gemini_models.urllib.request.urlopen")
    def test_reports_authentication_errors_without_exposing_key(self, urlopen):
        api_key = "private-api-key"
        body = json.dumps({
            "error": {"message": f"Invalid key: {api_key}"}
        }).encode("utf-8")
        urlopen.side_effect = urllib.error.HTTPError(
            list_gemini_models.MODELS_ENDPOINT,
            403,
            "Forbidden",
            {},
            io.BytesIO(body),
        )

        with self.assertRaises(list_gemini_models.AuthenticationError) as error:
            list_gemini_models.list_generate_content_models(api_key)

        self.assertNotIn(api_key, str(error.exception))
        self.assertIn("HTTP 403", str(error.exception))

    @patch("scripts.list_gemini_models.urllib.request.urlopen")
    def test_reports_quota_and_network_errors(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            list_gemini_models.MODELS_ENDPOINT,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        )
        with self.assertRaises(list_gemini_models.QuotaError):
            list_gemini_models.list_generate_content_models("secret")

        urlopen.side_effect = urllib.error.URLError("connection refused")
        with self.assertRaises(list_gemini_models.NetworkError):
            list_gemini_models.list_generate_content_models("secret")


if __name__ == "__main__":
    unittest.main()
