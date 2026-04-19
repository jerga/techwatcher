import datetime
import importlib
import json
import os
import re
from pathlib import Path

try:
    functions_framework = importlib.import_module("functions_framework")
except ModuleNotFoundError:  # pragma: no cover - local runner/tests can run without this dependency.
    class _FunctionsFrameworkShim:
        @staticmethod
        def http(func):
            return func

    functions_framework = _FunctionsFrameworkShim()

from google import genai
from google.cloud import storage


DEFAULT_LLM_PROVIDER = "google_ai_studio"
DEFAULT_LLM_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_LLM_MODEL_FALLBACK = "gemini-flash-latest"
DEFAULT_TTS_MODE = "mono"


def _get_runtime_config():
    return {
        "bucket_name": os.getenv("BUCKET_NAME", ""),
        "secret_api_key": os.getenv("SECRET_API_KEY", ""),
        "llm_provider": os.getenv("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).lower(),
        "llm_model": os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        "google_api_key": os.getenv("GOOGLE_API_KEY", ""),
        "tts_mode": os.getenv("TTS_MODE", DEFAULT_TTS_MODE).lower().strip(),
    }


def _validate_secret_key_config(secret_api_key):
    if not secret_api_key:
        raise RuntimeError("Missing SECRET_API_KEY in environment")


def _validate_llm_config(llm_provider, google_api_key):
    if llm_provider != "google_ai_studio":
        raise RuntimeError(f"Unsupported LLM_PROVIDER: {llm_provider}")
    if not google_api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY in environment")


def _resolve_daily_filenames(date_override=None):
    if date_override:
        datetime.datetime.strptime(date_override, "%Y%m%d")
        date_str = date_override
    else:
        date_str = datetime.datetime.now().strftime("%Y%m%d")

    return {
        "date_str": date_str,
        "input_filename": f"full_content_{date_str}.jsonl",
        "output_filename": f"podcast_{date_str}.jsonl",
    }


def _load_text_from_blob(bucket_name, filename):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(filename)
    if not blob.exists():
        raise FileNotFoundError(f"Input file not found: {filename}")
    return blob.download_as_text()


def _load_input_jsonl(filename, *, bucket_name):
    raw_text = _load_text_from_blob(bucket_name, filename)

    rows = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _load_prompt_template(tts_mode):
    if tts_mode == "multi":
        prompt_file = Path(__file__).resolve().parent / "prompt_multispeaker.md"
    else:
        prompt_file = Path(__file__).resolve().parent / "prompt_monospeaker.md"
    
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file.name}")
    return prompt_file.read_text(encoding="utf-8")


def _format_articles_for_prompt(items):
    blocks = []
    for idx, item in enumerate(items, start=1):
        article_id = item.get("id", idx)
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        blocks.append(
            "\n".join(
                [
                    f"<<<ARTICLE_START id={article_id}>>>",
                    content,
                    f"<<<ARTICLE_END id={article_id}>>>",
                ]
            )
        )
    return "\n\n".join(blocks)


def _build_llm_input(items, tts_mode="mono"):
    prompt_template = _load_prompt_template(tts_mode=tts_mode).rstrip()
    articles_payload = _format_articles_for_prompt(items)
    if not articles_payload:
        raise ValueError("No article content found in input file")
    return f"{prompt_template}\n\n{articles_payload}\n"


def _is_overload_error(exc):
    error_str = str(exc)
    return any(code in error_str for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))


def _generate_with_google_ai_studio(llm_model, google_api_key, llm_input):
    client = genai.Client(api_key=google_api_key)
    try:
        response = client.models.generate_content(model=llm_model, contents=llm_input)
    except Exception as exc:
        if _is_overload_error(exc):
            response = client.models.generate_content(model=DEFAULT_LLM_MODEL_FALLBACK, contents=llm_input)
        else:
            raise
    text = getattr(response, "text", None)
    if text and text.strip():
        return text
    else:
        raise RuntimeError("LLM returned an empty response")


def _parse_llm_output_to_jsonl(raw_text):
    """Split the LLM raw output on section markers and return a JSONL string."""
    # Split on [SYSTEM], [INTRO], [ARTICLE N], [OUTRO] — markers are on their own lines
    pattern = re.compile(r"\[\s*(SYSTEM|INTRO|ARTICLE\s+\d+|OUTRO)\s*\]", re.IGNORECASE)
    parts = pattern.split(raw_text)
    # parts = [pre-text, marker1, content1, marker2, content2, ...]
    lines = []
    for i in range(1, len(parts) - 1, 2):
        marker = parts[i].strip().upper()
        text = parts[i + 1].strip()
        if not text:
            continue
        if marker == "SYSTEM":
            lines.append(json.dumps({"type": "system", "text": text}, ensure_ascii=False))
        elif marker == "INTRO":
            lines.append(json.dumps({"type": "intro", "text": text}, ensure_ascii=False))
        elif marker == "OUTRO":
            lines.append(json.dumps({"type": "conclusion", "text": text}, ensure_ascii=False))
        else:  # ARTICLE N
            m = re.search(r"(\d+)", marker)
            article_id = int(m.group(1)) if m else len(lines)
            lines.append(json.dumps({"type": "article", "id": article_id, "text": text}, ensure_ascii=False))
    if not lines:
        raise ValueError("LLM output contains no recognisable section markers ([SYSTEM], [INTRO], [ARTICLE N], [OUTRO])")
    return "\n".join(lines)


def _generate_podcast_script(items, *, llm_provider, llm_model, google_api_key, tts_mode="mono"):
    llm_input = _build_llm_input(items, tts_mode=tts_mode)
    if llm_provider == "google_ai_studio":
        raw = _generate_with_google_ai_studio(llm_model, google_api_key, llm_input)
        return _parse_llm_output_to_jsonl(raw)
    raise ValueError(f"Unsupported LLM_PROVIDER: {llm_provider}")


def _save_output(filename, content, *, bucket_name):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(filename)
    blob.upload_from_string(content, content_type="text/plain; charset=utf-8")


def run_summarize_step(
    input_filename,
    output_filename,
    *,
    bucket_name,
    llm_provider,
    llm_model,
    google_api_key,
    tts_mode,
):
    items = _load_input_jsonl(
        input_filename,
        bucket_name=bucket_name,
    )
    if not items:
        raise ValueError("Input file is empty")

    podcast_script = _generate_podcast_script(
        items,
        llm_provider=llm_provider,
        llm_model=llm_model,
        google_api_key=google_api_key,
        tts_mode=tts_mode,
    )
    _save_output(
        output_filename,
        podcast_script,
        bucket_name=bucket_name,
    )

    return {
        "input_filename": input_filename,
        "output_filename": output_filename,
        "article_count": len(items),
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "tts_mode": tts_mode,
    }


@functions_framework.http
def summarize_content(request):
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
        _validate_llm_config(config["llm_provider"], config["google_api_key"])
    except RuntimeError as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

    client_key = request.headers.get("X-API-Key")
    if not client_key or client_key != config["secret_api_key"]:
        return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

    defaults = _resolve_daily_filenames()
    request_json = request.get_json(silent=True) or {}
    input_filename = request_json.get("input_filename", defaults["input_filename"])
    output_filename = request_json.get("output_filename", defaults["output_filename"])
    llm_model = request_json.get("llm_model", config["llm_model"])

    try:
        pipeline_result = run_summarize_step(
            input_filename,
            output_filename,
            bucket_name=config["bucket_name"],
            llm_provider=config["llm_provider"],
            llm_model=llm_model,
            google_api_key=config["google_api_key"],
            tts_mode=config["tts_mode"],
        )
        response = {
            "status": "ok",
            "bucket": config["bucket_name"],
            **pipeline_result,
        }
        return (json.dumps(response), 200, headers)

    except FileNotFoundError as exc:
        return (json.dumps({"error": str(exc)}), 404, headers)
    except (ValueError, json.JSONDecodeError) as exc:
        return (json.dumps({"error": str(exc)}), 400, headers)
    except Exception as exc:
        return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
