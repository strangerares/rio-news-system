"""List Gemini models available to the configured API key."""

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request


MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SECONDS = 20


class ModelListError(Exception):
    """Base error for Gemini model-list requests."""


class AuthenticationError(ModelListError):
    """The API key is invalid or lacks permission."""


class QuotaError(ModelListError):
    """The API key has exhausted its available quota."""


class NetworkError(ModelListError):
    """The Gemini API could not be reached."""


def _api_error_message(body, api_key):
    try:
        payload = json.loads(body)
        message = payload.get("error", {}).get("message", "")
    except (json.JSONDecodeError, AttributeError):
        message = ""

    return message.replace(api_key, "[REDACTED]") if message else ""


def list_generate_content_models(api_key):
    """Return exact API model resource names supporting generateContent."""
    models = []
    page_token = ""

    while True:
        params = {"pageSize": "1000"}
        if page_token:
            params["pageToken"] = page_token
        url = f"{MODELS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "x-goog-api-key": api_key,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = _api_error_message(body, api_key)
            detail = f": {message}" if message else ""
            normalized_message = message.lower()
            error_status = ""
            try:
                error_status = json.loads(body).get("error", {}).get(
                    "status", ""
                )
            except (json.JSONDecodeError, AttributeError):
                pass

            if (
                error.code == 429
                or error_status == "RESOURCE_EXHAUSTED"
                or "quota" in normalized_message
            ):
                raise QuotaError(
                    f"Gemini API quota exceeded (HTTP {error.code}){detail}"
                ) from None
            if error.code in (401, 403):
                raise AuthenticationError(
                    "Gemini API authentication/permission failed "
                    f"(HTTP {error.code}){detail}"
                ) from None
            raise ModelListError(
                f"Gemini API returned HTTP {error.code}{detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
            reason = getattr(error, "reason", error)
            raise NetworkError(
                f"Could not reach the Gemini API: {reason}"
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ModelListError(
                f"Gemini API returned invalid JSON: {error}"
            ) from None

        if not isinstance(payload, dict) or not isinstance(
            payload.get("models", []), list
        ):
            raise ModelListError(
                "Gemini API returned an unexpected model-list response."
            )

        models.extend(
            model["name"]
            for model in payload.get("models", [])
            if isinstance(model, dict)
            and isinstance(model.get("name"), str)
            and "generateContent"
            in model.get("supportedGenerationMethods", [])
        )

        page_token = payload.get("nextPageToken", "")
        if not page_token:
            return sorted(set(models))


def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print(
            "GEMINI_API_KEY is not set. Provide it through the environment "
            "(for GitHub Actions, map the repository secret to this variable).",
            file=sys.stderr,
        )
        return 2

    try:
        models = list_generate_content_models(api_key)
    except ModelListError as error:
        print(error, file=sys.stderr)
        return 1

    print("Models available to this API key supporting generateContent:")
    if not models:
        print("  No matching models were returned.")
        return 0

    for model_name in models:
        print(model_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
