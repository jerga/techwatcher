import datetime
import importlib
import json
import os

try:
    functions_framework = importlib.import_module("functions_framework")
except ModuleNotFoundError:  # pragma: no cover - local runner/tests can run without this dependency.
    class _FunctionsFrameworkShim:
        @staticmethod
        def http(func):
            return func

    functions_framework = _FunctionsFrameworkShim()

from google.cloud import storage


def _get_runtime_config():
    return {
        "bucket_name": os.getenv("BUCKET_NAME", ""),
        "secret_api_key": os.getenv("SECRET_API_KEY", ""),
    }


def _validate_secret_key_config(secret_api_key):
    if not secret_api_key:
        raise RuntimeError("Missing SECRET_API_KEY in environment")


def _resolve_daily_filename(date_override=None):
    if date_override:
        datetime.datetime.strptime(date_override, "%Y%m%d")
        date_str = date_override
    else:
        date_str = datetime.datetime.now().strftime("%Y%m%d")
    return f"links_{date_str}.txt"


def _extract_text_from_request(request):
    request_json = request.get_json(silent=True)

    if request_json and "url" in request_json:
        return str(request_json["url"])
    if request_json and "content" in request_json:
        return str(request_json["content"])
    if request.data:
        raw_text = request.data.decode("utf-8")
        try:
            payload = json.loads(raw_text)
            if isinstance(payload, dict) and "url" in payload:
                return str(payload["url"])
        except (json.JSONDecodeError, TypeError):
            pass
        return raw_text
    return None


def run_collect_step(link_text, *, bucket_name, date_override=None):
    if not link_text or not str(link_text).strip():
        raise ValueError("Error: No text content detected")

    filename = _resolve_daily_filename(date_override)
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(filename)

    existing_content = ""
    if blob.exists():
        existing_content = blob.download_as_text()

    blob.upload_from_string(existing_content + str(link_text).strip() + "\n")
    return filename


@functions_framework.http
def upload_text(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
    }

    if request.method == "OPTIONS":
        return ("", 204, headers)

    config = _get_runtime_config()
    try:
        _validate_secret_key_config(config["secret_api_key"])
    except RuntimeError as exc:
        return (f"Technical error: {str(exc)}", 500, headers)

    client_key = request.headers.get("X-API-Key")
    if not client_key or client_key != config["secret_api_key"]:
        return ("Access denied: Missing or invalid API key", 403, headers)

    text_to_save = _extract_text_from_request(request)
    if not text_to_save:
        return ("Error: No text content detected", 400, headers)

    try:
        filename = run_collect_step(
            text_to_save,
            bucket_name=config["bucket_name"],
        )
        return (f"Success: Link added to file {filename}.", 200, headers)
    except ValueError as exc:
        return (str(exc), 400, headers)
    except Exception as exc:
        return (f"Technical error: {str(exc)}", 500, headers)

