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

try:
	import google.auth
	from google.auth.transport.requests import AuthorizedSession
except ImportError:  # pragma: no cover - only hit when google-auth deps are unavailable.
	google = None
	AuthorizedSession = None


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 180


def _get_runtime_config():
	return {
		"secret_api_key": os.getenv("SECRET_API_KEY", ""),
		"workflow_project_id": os.getenv("GOOGLE_CLOUD_PROJECT", ""),
		"workflow_location": os.getenv("WORKFLOW_LOCATION", ""),
		"workflow_name": os.getenv("WORKFLOW_NAME", ""),
	}


def _validate_secret_key_config(secret_api_key):
	if not secret_api_key:
		raise RuntimeError("Missing SECRET_API_KEY in environment")


def _validate_workflow_config(project_id, location, workflow_name):
	if not project_id:
		raise RuntimeError("Missing GOOGLE_CLOUD_PROJECT in environment")
	if not location:
		raise RuntimeError("Missing WORKFLOW_LOCATION in environment")
	if not workflow_name:
		raise RuntimeError("Missing WORKFLOW_NAME in environment")


def _resolve_run_date(date_override=None):
	if date_override:
		datetime.datetime.strptime(date_override, "%Y%m%d")
		return date_override
	return datetime.datetime.now().strftime("%Y%m%d")


def _build_execution_url(project_id, location, workflow_name):
	return (
		"https://workflowexecutions.googleapis.com/v1/"
		f"projects/{project_id}/locations/{location}/workflows/{workflow_name}/executions"
	)


def _trigger_workflow_execution(*, project_id, location, workflow_name, run_date):
	if google is None or AuthorizedSession is None:
		raise RuntimeError("google-auth is required to call Workflows API")

	credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
	session = AuthorizedSession(credentials)
	execution_url = _build_execution_url(project_id, location, workflow_name)

	payload = {
		"argument": json.dumps({"date": run_date}, ensure_ascii=False),
	}
	response = session.post(
		execution_url,
		json=payload,
		timeout=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
	)

	if response.status_code >= 400:
		try:
			error_payload = response.json()
		except ValueError:
			error_payload = {"raw_response": response.text}
		raise RuntimeError(
			"Failed to trigger workflow execution: "
			f"status={response.status_code}, body={json.dumps(error_payload, ensure_ascii=False)}"
		)

	try:
		execution_payload = response.json()
	except ValueError as exc:
		raise RuntimeError("Workflows API returned a non-JSON response") from exc

	return {
		"date": run_date,
		"project_id": project_id,
		"location": location,
		"workflow_name": workflow_name,
		"execution_name": execution_payload.get("name"),
		"execution_state": execution_payload.get("state"),
		"execution_start_time": execution_payload.get("startTime"),
		"execution_payload": execution_payload,
	}


@functions_framework.http
def call_workflow(request):
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
		_validate_workflow_config(
			config["workflow_project_id"],
			config["workflow_location"],
			config["workflow_name"],
		)
	except RuntimeError as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

	client_key = request.headers.get("X-API-Key")
	if not client_key or client_key != config["secret_api_key"]:
		return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

	request_json = request.get_json(silent=True) or {}
	run_date = request_json.get("date")

	try:
		result = _trigger_workflow_execution(
			project_id=config["workflow_project_id"],
			location=config["workflow_location"],
			workflow_name=config["workflow_name"],
			run_date=_resolve_run_date(run_date),
		)
		response = {
			"status": "ok",
			"date": result["date"],
			"project_id": result["project_id"],
			"location": result["location"],
			"workflow_name": result["workflow_name"],
			"execution_name": result["execution_name"],
			"execution_state": result["execution_state"],
			"execution_start_time": result["execution_start_time"],
		}
		return (json.dumps(response), 200, headers)

	except ValueError as exc:
		return (json.dumps({"error": str(exc)}), 400, headers)
	except Exception as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
