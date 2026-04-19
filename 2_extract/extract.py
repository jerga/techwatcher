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

# --- CONFIGURATION ---
MARKDOWN_NEW_PREFIX = "https://markdown.new/"
REQUEST_TIMEOUT_SECONDS = 30
CONTENT_ERROR_MARKER = "Something went wrong, but don\u2019t fret \u2014 let\u2019s give it another shot."


def _get_runtime_config():
	return {
		"bucket_name": os.getenv("BUCKET_NAME", ""),
		"secret_api_key": os.getenv("SECRET_API_KEY", ""),
	}


def _validate_secret_key_config(secret_api_key):
	if not secret_api_key:
		raise RuntimeError("Missing SECRET_API_KEY in environment")


def _count_words(text):
	# Split on whitespace to keep counting simple and robust for markdown text.
	return len(text.split()) if text else 0


def _load_links_from_blob(bucket, filename):
	blob = bucket.blob(filename)
	if not blob.exists():
		raise FileNotFoundError(f"Input file not found: {filename}")

	raw_text = blob.download_as_text()
	links = [line.strip() for line in raw_text.splitlines() if line.strip()]

	return [{"id": idx + 1, "link": link} for idx, link in enumerate(links)]


def _fetch_markdown_content(link):
	url = f"{MARKDOWN_NEW_PREFIX}{link}"
	response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
	response.raise_for_status()
	content = response.text
	if CONTENT_ERROR_MARKER in content:
		raise ValueError("markdown.new returned an application-level error message")
	return content


def _to_jsonl(rows):
	if not rows:
		return ""
	return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def _load_links(filename, *, bucket_name):
	storage_client = storage.Client()
	bucket = storage_client.bucket(bucket_name)
	return _load_links_from_blob(bucket, filename)


def _save_output(filename, content, *, bucket_name):
	storage_client = storage.Client()
	bucket = storage_client.bucket(bucket_name)
	blob = bucket.blob(filename)
	blob.upload_from_string(content, content_type="application/x-ndjson")


def run_extract_step(input_filename, output_filename, error_output_filename, *, bucket_name):
	items = _load_links(
		input_filename,
		bucket_name=bucket_name,
	)
	success_items = []
	error_items = []

	success_count = 0
	error_count = 0
	for item in items:
		try:
			content = _fetch_markdown_content(item["link"])
			success_items.append(
				{
					"id": item["id"],
					"link": item["link"],
					"content": content,
					"word_count": _count_words(content),
				}
			)
			success_count += 1
		except (requests.RequestException, ValueError) as exc:
			error_items.append(
				{
					"id": item["id"],
					"link": item["link"],
					"fetch_error": str(exc),
				}
			)
			error_count += 1

	_save_output(
		output_filename,
		_to_jsonl(success_items),
		bucket_name=bucket_name,
	)
	if error_items:
		_save_output(
			error_output_filename,
			_to_jsonl(error_items),
			bucket_name=bucket_name,
		)

	return {
		"input_filename": input_filename,
		"output_filename": output_filename,
		"error_output_filename": error_output_filename,
		"total_links": len(items),
		"success_count": success_count,
		"error_count": error_count,
	}


@functions_framework.http
def extract_content(request):
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
	except RuntimeError as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

	client_key = request.headers.get("X-API-Key")
	if not client_key or client_key != config["secret_api_key"]:
		return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

	today = datetime.datetime.now().strftime("%Y%m%d")
	default_input_filename = f"links_{today}.txt"
	default_output_filename = f"full_content_{today}.jsonl"
	default_error_output_filename = f"fetch_errors_{today}.jsonl"

	request_json = request.get_json(silent=True) or {}
	input_filename = request_json.get("input_filename", default_input_filename)
	output_filename = request_json.get("output_filename", default_output_filename)
	error_output_filename = request_json.get("error_output_filename", default_error_output_filename)

	try:
		pipeline_result = run_extract_step(
			input_filename,
			output_filename,
			error_output_filename,
			bucket_name=config["bucket_name"],
		)

		response = {
			"status": "ok",
			"bucket": config["bucket_name"],
			"input_filename": pipeline_result["input_filename"],
			"output_filename": pipeline_result["output_filename"],
			"error_output_filename": pipeline_result["error_output_filename"],
			"total_links": pipeline_result["total_links"],
			"success_count": pipeline_result["success_count"],
			"error_count": pipeline_result["error_count"],
		}
		return (json.dumps(response), 200, headers)

	except FileNotFoundError as exc:
		return (json.dumps({"error": str(exc)}), 404, headers)
	except Exception as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
