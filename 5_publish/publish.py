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


DEFAULT_SOUNDTRACK_TITLE_PREFIX = "Techwatch"
DEFAULT_SOUNDCLOUD_SHARING = "private"
DEFAULT_SOUNDCLOUD_TOKEN_BLOB = "soundcloud-token.json"
TOKEN_ENDPOINT = "https://api.soundcloud.com/oauth2/token"
TRACK_UPLOAD_ENDPOINT = "https://api.soundcloud.com/tracks"
REQUEST_TIMEOUT_SECONDS = 60


class _SoundCloudUnauthorizedError(RuntimeError):
    pass


def _get_runtime_config():
    return {
        "bucket_name": os.getenv("BUCKET_NAME", ""),
        "secret_api_key": os.getenv("SECRET_API_KEY", ""),
        "soundcloud_client_id": os.getenv("SOUNDCLOUD_CLIENT_ID", ""),
        "soundcloud_client_secret": os.getenv("SOUNDCLOUD_CLIENT_SECRET", ""),
        "soundcloud_token_blob": os.getenv("SOUNDCLOUD_TOKEN_BLOB", DEFAULT_SOUNDCLOUD_TOKEN_BLOB),
        "token_endpoint": os.getenv("TOKEN_ENDPOINT", TOKEN_ENDPOINT),
        "track_upload_endpoint": os.getenv("TRACK_UPLOAD_ENDPOINT", TRACK_UPLOAD_ENDPOINT),
    }


def _validate_secret_key_config(secret_api_key):
    if not secret_api_key:
        raise RuntimeError("Missing SECRET_API_KEY in environment")


def _validate_soundcloud_config(client_id, client_secret):
    if not client_id:
        raise RuntimeError("Missing SOUNDCLOUD_CLIENT_ID in environment")
    if not client_secret:
        raise RuntimeError("Missing SOUNDCLOUD_CLIENT_SECRET in environment")


def _validate_soundcloud_token_store_config(bucket_name, token_blob):
    if not bucket_name:
        raise RuntimeError("Missing BUCKET_NAME in environment")
    if not token_blob or not token_blob.strip():
        raise RuntimeError("Missing SOUNDCLOUD_TOKEN_BLOB in environment")


def _resolve_daily_defaults(date_override=None):
    if date_override:
        datetime.datetime.strptime(date_override, "%Y%m%d")
        date_str = date_override
    else:
        date_str = datetime.datetime.now().strftime("%Y%m%d")

    pretty_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    title_prefix = os.getenv("SOUNDTRACK_TITLE_PREFIX", DEFAULT_SOUNDTRACK_TITLE_PREFIX)
    return {
        "date_str": date_str,
        "input_filename": f"podcast_{date_str}.wav",
        "title": f"{title_prefix} {pretty_date}",
    }


def _load_audio_from_bucket(bucket_name, filename):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(filename)
    if not blob.exists():
        raise FileNotFoundError(f"Input file not found: {filename}")
    return blob.download_as_bytes()


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


def _load_soundcloud_tokens(bucket_name, token_blob):
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


def _save_soundcloud_tokens(bucket_name, token_blob, token_payload):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(token_blob)

    persisted_payload = {
        "refresh_token": token_payload.get("refresh_token"),
        "access_token": token_payload.get("access_token"),
        "token_type": token_payload.get("token_type", "Bearer"),
        "expires_in": token_payload.get("expires_in"),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    if not persisted_payload["refresh_token"]:
        raise RuntimeError("Refusing to persist SoundCloud tokens without refresh_token")

    blob.upload_from_string(json.dumps(persisted_payload, ensure_ascii=False), content_type="application/json")


def _refresh_soundcloud_access_token(client_id, client_secret, refresh_token, *, token_endpoint):
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


def _upload_to_soundcloud(
    access_token,
    audio_bytes,
    filename,
    *,
    title,
    description,
    sharing,
    track_upload_endpoint,
):
    files = {
        "track[asset_data]": (filename, audio_bytes, "audio/wav"),
    }
    data = {
        "track[title]": title,
        "track[description]": description,
        "track[sharing]": sharing,
    }
    headers = {
        "Authorization": f"OAuth {access_token}",
    }

    response = requests.post(
        track_upload_endpoint,
        headers=headers,
        data=data,
        files=files,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 401:
        raise _SoundCloudUnauthorizedError(
            f"SoundCloud upload failed ({response.status_code}): {_extract_json_error(response)}"
        )
    if response.status_code >= 400:
        raise RuntimeError(f"SoundCloud upload failed ({response.status_code}): {_extract_json_error(response)}")

    return response.json()


def _validate_publish_inputs(title, sharing):
    if not title or not title.strip():
        raise ValueError("Track title cannot be empty")
    if sharing not in {"private", "public"}:
        raise ValueError("Invalid sharing value; allowed values are: private, public")


def run_publish_step(
    input_filename,
    *,
    bucket_name,
    token_blob,
    client_id,
    client_secret,
    title,
    description,
    sharing,
    token_endpoint=TOKEN_ENDPOINT,
    track_upload_endpoint=TRACK_UPLOAD_ENDPOINT,
):
    _validate_publish_inputs(title, sharing)

    audio_bytes = _load_audio_from_bucket(bucket_name, input_filename)

    # Track uploads act on behalf of a user account, so we must rotate refresh tokens.
    token_store = _load_soundcloud_tokens(bucket_name, token_blob)
    token_data = _refresh_soundcloud_access_token(
        client_id,
        client_secret,
        token_store["refresh_token"],
        token_endpoint=token_endpoint,
    )
    _save_soundcloud_tokens(bucket_name, token_blob, token_data)

    try:
        track_payload = _upload_to_soundcloud(
            token_data["access_token"],
            audio_bytes,
            input_filename,
            title=title,
            description=description,
            sharing=sharing,
            track_upload_endpoint=track_upload_endpoint,
        )
    except _SoundCloudUnauthorizedError:
        latest_token_store = _load_soundcloud_tokens(bucket_name, token_blob)
        token_data = _refresh_soundcloud_access_token(
            client_id,
            client_secret,
            latest_token_store["refresh_token"],
            token_endpoint=token_endpoint,
        )
        _save_soundcloud_tokens(bucket_name, token_blob, token_data)
        track_payload = _upload_to_soundcloud(
            token_data["access_token"],
            audio_bytes,
            input_filename,
            title=title,
            description=description,
            sharing=sharing,
            track_upload_endpoint=track_upload_endpoint,
        )

    return {
        "input_filename": input_filename,
        "title": title,
        "description": description,
        "sharing": sharing,
        "audio_size_bytes": len(audio_bytes),
        "track_id": track_payload.get("id"),
        "track_permalink_url": track_payload.get("permalink_url"),
        "track_uri": track_payload.get("uri"),
        "token_type": token_data.get("token_type"),
        "token_expires_in": token_data.get("expires_in"),
        "token_store_blob": token_blob,
    }


@functions_framework.http
def publish_content(request):
    config = _get_runtime_config()

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
        "Content-Type": "application/json",
    }

    if request.method == "OPTIONS":
        return ("", 204, headers)

    try:
        _validate_secret_key_config(config["secret_api_key"])
        _validate_soundcloud_config(
            config["soundcloud_client_id"],
            config["soundcloud_client_secret"],
        )
        _validate_soundcloud_token_store_config(
            config["bucket_name"],
            config["soundcloud_token_blob"],
        )
    except RuntimeError as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

    client_key = request.headers.get("X-API-Key")
    if not client_key or client_key != config["secret_api_key"]:
        return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

    request_json = request.get_json(silent=True) or {}
    defaults = _resolve_daily_defaults(request_json.get("date"))
    input_filename = request_json.get("input_filename", defaults["input_filename"])
    title = request_json.get("title", defaults["title"])
    description = request_json.get("description", "")
    sharing = request_json.get("sharing", DEFAULT_SOUNDCLOUD_SHARING)

    try:
        result = run_publish_step(
            input_filename,
            bucket_name=config["bucket_name"],
            token_blob=config["soundcloud_token_blob"],
            client_id=config["soundcloud_client_id"],
            client_secret=config["soundcloud_client_secret"],
            title=title,
            description=description,
            sharing=sharing,
            token_endpoint=config["token_endpoint"],
            track_upload_endpoint=config["track_upload_endpoint"],
        )
        response = {
            "status": "ok",
            "bucket": config["bucket_name"],
            **result,
        }
        return (json.dumps(response), 200, headers)

    except FileNotFoundError as exc:
        return (json.dumps({"error": str(exc)}), 404, headers)
    except ValueError as exc:
        return (json.dumps({"error": str(exc)}), 400, headers)
    except requests.RequestException as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
    except Exception as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
