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

import requests
from google.cloud import storage


DEFAULT_SOUNDCLOUD_TOKEN_BLOB = "soundcloud-token.json"
TOKEN_ENDPOINT = "https://api.soundcloud.com/oauth2/token"
REQUEST_TIMEOUT_SECONDS = 60


def _get_runtime_config():
    return {
        "bucket_name": os.getenv("BUCKET_NAME", ""),
        "token_blob": os.getenv("SOUNDCLOUD_TOKEN_BLOB", DEFAULT_SOUNDCLOUD_TOKEN_BLOB),
        "client_id": os.getenv("SOUNDCLOUD_CLIENT_ID", ""),
        "client_secret": os.getenv("SOUNDCLOUD_CLIENT_SECRET", ""),
        "secret_api_key": os.getenv("SECRET_API_KEY", ""),
        "token_endpoint": os.getenv("TOKEN_ENDPOINT", TOKEN_ENDPOINT),
    }


def _validate_config(config):
    if not config["bucket_name"]:
        raise RuntimeError("Missing BUCKET_NAME in environment")
    if not config["token_blob"]:
        raise RuntimeError("Missing SOUNDCLOUD_TOKEN_BLOB in environment")
    if not config["client_id"]:
        raise RuntimeError("Missing SOUNDCLOUD_CLIENT_ID in environment")
    if not config["client_secret"]:
        raise RuntimeError("Missing SOUNDCLOUD_CLIENT_SECRET in environment")


def _extract_json_error(response):
    try:
        payload = response.json()
    except ValueError:
        return response.text

    if isinstance(payload, dict):
        for key in ["errors", "error", "message", "error_description"]:
            if key in payload and payload[key]:
                return str(payload[key])
    return json.dumps(payload, ensure_ascii=False)


def _is_refresh_token_invalid(error):
    message = str(error).lower()
    return "invalid_grant" in message or "invalid refresh token" in message


def _load_token_store(bucket_name, token_blob):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(token_blob)

    if not blob.exists():
        raise RuntimeError(
            "SoundCloud token store not found in GCS. Bootstrap it first with auth_script.py "
            f"(bucket={bucket_name}, blob={token_blob})"
        )

    try:
        payload = json.loads(blob.download_as_text())
    except ValueError as exc:
        raise RuntimeError(f"SoundCloud token store is not valid JSON: {str(exc)}") from exc

    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not refresh_token:
        raise RuntimeError("SoundCloud token store is missing refresh_token")

    return payload


def _save_token_store(bucket_name, token_blob, token_payload):
    persisted_payload = {
        "refresh_token": token_payload.get("refresh_token"),
        "access_token": token_payload.get("access_token"),
        "token_type": token_payload.get("token_type", "Bearer"),
        "expires_in": token_payload.get("expires_in"),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    if not persisted_payload["refresh_token"]:
        raise RuntimeError("Refusing to persist SoundCloud tokens without refresh_token")

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(token_blob)
    blob.upload_from_string(json.dumps(persisted_payload, ensure_ascii=False), content_type="application/json")


def _refresh_token(client_id, client_secret, refresh_token, *, token_endpoint):
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    response = requests.post(token_endpoint, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise RuntimeError(
            f"SoundCloud token refresh failed ({response.status_code}): {_extract_json_error(response)}"
        )

    tokens = response.json()
    access_token = tokens.get("access_token")
    next_refresh_token = tokens.get("refresh_token")
    if not access_token:
        raise RuntimeError("SoundCloud token refresh succeeded but access_token is missing")
    if not next_refresh_token:
        raise RuntimeError("SoundCloud token refresh succeeded but refresh_token is missing")

    return {
        "access_token": access_token,
        "refresh_token": next_refresh_token,
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in"),
    }


def _run_refresh(config):
    token_store = _load_token_store(config["bucket_name"], config["token_blob"])
    try:
        token_data = _refresh_token(
            config["client_id"],
            config["client_secret"],
            token_store["refresh_token"],
            token_endpoint=config["token_endpoint"],
        )
    except RuntimeError as exc:
        if not _is_refresh_token_invalid(exc):
            raise
        # Retry once with latest token in case another process rotated it first.
        token_store = _load_token_store(config["bucket_name"], config["token_blob"])
        token_data = _refresh_token(
            config["client_id"],
            config["client_secret"],
            token_store["refresh_token"],
            token_endpoint=config["token_endpoint"],
        )

    _save_token_store(config["bucket_name"], config["token_blob"], token_data)
    return token_data


@functions_framework.http
def refresh_soundcloud_token(request):
    headers = {
        "Content-Type": "application/json",
    }

    config = _get_runtime_config()

    if request.method == "OPTIONS":
        cors_headers = {
            **headers,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
        }
        return ("", 204, cors_headers)

    if request.method != "POST":
        return (json.dumps({"error": "Method not allowed"}), 405, headers)

    try:
        _validate_config(config)
    except RuntimeError as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

    if config["secret_api_key"]:
        client_key = request.headers.get("X-API-Key")
        if client_key != config["secret_api_key"]:
            return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

    try:
        token_data = _run_refresh(config)
        response = {
            "status": "ok",
            "bucket": config["bucket_name"],
            "token_store_blob": config["token_blob"],
            "token_type": token_data.get("token_type"),
            "token_expires_in": token_data.get("expires_in"),
            "refreshed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        return (json.dumps(response, ensure_ascii=False), 200, headers)
    except requests.RequestException as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
    except Exception as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
