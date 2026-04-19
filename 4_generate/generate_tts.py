import datetime
import importlib
import io
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

from google.cloud import storage

# Add current directory to path to import flat connector modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
	functions_framework = importlib.import_module("functions_framework")
except ModuleNotFoundError:  # pragma: no cover - local runner/tests can run without this dependency.
	class _FunctionsFrameworkShim:
		@staticmethod
		def http(func):
			return func

	functions_framework = _FunctionsFrameworkShim()


DEFAULT_TTS_PROVIDER = "google_ai_studio"
DEFAULT_TTS_MODE = "multi"
# gemini-2.5-flash-preview-tts (legacy - replaced with 3.1 Flash TTS)
DEFAULT_TTS_MODEL_GOOGLE = "gemini-3.1-flash-tts-preview"
DEFAULT_TTS_MODEL_MISTRAL = "voxtral-mini-tts-2603"
DEFAULT_VOICE_ID_GOOGLE = "Charon"
DEFAULT_VOICE_ID_MISTRAL = "fr_marie_curious"
DEFAULT_SPEAKER1_NAME = "Anna"
DEFAULT_SPEAKER2_NAME = "Hugo"
DEFAULT_SPEAKER1_VOICE = "Zephyr"
DEFAULT_SPEAKER2_VOICE = "Puck"
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_CHANNELS = 1
DEFAULT_ARTICLES_PER_SEGMENT = 1


def _default_tts_model_for_provider(tts_provider: str) -> str:
	if tts_provider == "mistral":
		return DEFAULT_TTS_MODEL_MISTRAL
	return DEFAULT_TTS_MODEL_GOOGLE


def _default_voice_id_for_provider(tts_provider: str) -> str:
	if tts_provider == "mistral":
		return DEFAULT_VOICE_ID_MISTRAL
	return DEFAULT_VOICE_ID_GOOGLE


def _get_runtime_config():
	tts_provider = os.getenv("TTS_PROVIDER", DEFAULT_TTS_PROVIDER).lower().strip()
	tts_mode = os.getenv("TTS_MODE", DEFAULT_TTS_MODE).lower().strip()
	tts_model = os.getenv("TTS_MODEL", "").strip() or _default_tts_model_for_provider(tts_provider)
	voice_id = os.getenv("VOICE_ID", "").strip() or _default_voice_id_for_provider(tts_provider)
	
	# Multi-speaker voice configuration
	speaker1_name = os.getenv("SPEAKER1_NAME", DEFAULT_SPEAKER1_NAME).strip()
	speaker2_name = os.getenv("SPEAKER2_NAME", DEFAULT_SPEAKER2_NAME).strip()
	speaker1_voice = os.getenv("SPEAKER1_VOICE", DEFAULT_SPEAKER1_VOICE).strip()
	speaker2_voice = os.getenv("SPEAKER2_VOICE", DEFAULT_SPEAKER2_VOICE).strip()

	return {
		"bucket_name": os.getenv("BUCKET_NAME", ""),
		"secret_api_key": os.getenv("SECRET_API_KEY", ""),
		"tts_provider": tts_provider,
		"tts_mode": tts_mode,
		"tts_model": tts_model,
		"voice_id": voice_id,
		"speaker1_name": speaker1_name,
		"speaker2_name": speaker2_name,
		"speaker1_voice": speaker1_voice,
		"speaker2_voice": speaker2_voice,
		"google_api_key": os.getenv("GOOGLE_API_KEY", ""),
		"mistral_api_key": os.getenv("MISTRAL_API_KEY", ""),
	}


def _validate_secret_key_config(secret_api_key):
	if not secret_api_key:
		raise RuntimeError("Missing SECRET_API_KEY in environment")


def _validate_tts_config(tts_mode, tts_provider, google_api_key, mistral_api_key):
	if tts_mode not in ("mono", "multi"):
		raise ValueError(f"Unsupported TTS_MODE: {tts_mode} (expected 'mono' or 'multi')")
	
	if tts_mode == "multi":
		if tts_provider != "google_ai_studio":
			raise ValueError(f"TTS_MODE=multi is only supported with TTS_PROVIDER=google_ai_studio (got {tts_provider})")
		if not google_api_key:
			raise RuntimeError("Missing GOOGLE_API_KEY in environment")
	elif tts_provider == "google_ai_studio":
		if not google_api_key:
			raise RuntimeError("Missing GOOGLE_API_KEY in environment")
	elif tts_provider == "mistral":
		if not mistral_api_key:
			raise RuntimeError("Missing MISTRAL_API_KEY in environment")
	else:
		raise ValueError(f"Unsupported TTS_PROVIDER: {tts_provider}")


def _get_tts_connector(tts_mode: str, tts_provider: str, google_api_key: str, mistral_api_key: str):
	"""Get the appropriate TTS connector based on mode and provider.
	
	Args:
		tts_mode: TTS mode ("mono" or "multi").
		tts_provider: Provider name ("google_ai_studio" or "mistral").
		google_api_key: Google API key (required for google_ai_studio).
		mistral_api_key: Mistral API key (required for mistral).
	
	Returns:
		A TTS connector instance.
	
	Raises:
		ValueError: If provider or mode is unsupported.
		RuntimeError: If required API key is missing.
	"""
	if tts_mode == "multi":
		if tts_provider != "google_ai_studio":
			raise ValueError(f"Multi-speaker mode only supported with google_ai_studio (got {tts_provider})")
		from connector_google_ai_studio_multispeaker import MultiSpeakerGoogleAIStudioTTSConnector
		return MultiSpeakerGoogleAIStudioTTSConnector(google_api_key)
	else:  # mono mode
		if tts_provider == "google_ai_studio":
			from connector_google_ai_studio import GoogleAIStudioTTSConnector
			return GoogleAIStudioTTSConnector(google_api_key)
		elif tts_provider == "mistral":
			from connector_mistral import MistralVoxtralTTSConnector
			return MistralVoxtralTTSConnector(mistral_api_key)
		else:
			raise ValueError(f"Unsupported TTS provider: {tts_provider}")


def _resolve_daily_filenames(date_override=None):
	if date_override:
		datetime.datetime.strptime(date_override, "%Y%m%d")
		date_str = date_override
	else:
		date_str = datetime.datetime.now().strftime("%Y%m%d")

	return {
		"date_str": date_str,
		"input_filename": f"podcast_{date_str}.jsonl",
		"output_filename": f"podcast_{date_str}.wav",
	}


def _load_text_from_blob(bucket_name, filename):
	storage_client = storage.Client()
	bucket = storage_client.bucket(bucket_name)
	blob = bucket.blob(filename)
	if not blob.exists():
		raise FileNotFoundError(f"Input file not found: {filename}")
	return blob.download_as_text()


def _load_podcast_jsonl(filename, *, bucket_name):
	raw_text = _load_text_from_blob(bucket_name, filename)

	system_text = None
	intro_parts = []
	conclusion_parts = []
	articles = []

	# The summarize step writes one JSON object per line.
	# We parse line by line to keep precise error messages (line numbers).
	for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
		line = raw_line.strip()
		if not line:
			continue
		try:
			row = json.loads(line)
		except json.JSONDecodeError as exc:
			raise ValueError(f"Invalid JSONL at line {line_no}: {str(exc)}") from exc

		if not isinstance(row, dict):
			raise ValueError(f"Invalid JSONL at line {line_no}: each line must be a JSON object")

		entry_type = str(row.get("type", "")).strip().lower()
		text = str(row.get("text", "")).strip()
		if not text:
			continue

		if entry_type == "system":
			system_text = text
		elif entry_type == "intro":
			intro_parts.append(text)
		elif entry_type == "conclusion":
			conclusion_parts.append(text)
		elif entry_type == "article":
			article_id = row.get("id")
			try:
				normalized_id = int(article_id)
			except (TypeError, ValueError):
				normalized_id = line_no
			articles.append({"id": normalized_id, "text": text, "line_no": line_no})
		else:
			raise ValueError(
				f"Invalid JSONL at line {line_no}: unsupported type '{entry_type}' (expected system/intro/article/conclusion)"
			)

	# Keep article order deterministic even if ids are missing/duplicated.
	articles.sort(key=lambda article: (article["id"], article["line_no"]))

	return {
		"system": system_text,
		"intro": "\n\n".join(intro_parts).strip(),
		"articles": [{"id": article["id"], "text": article["text"]} for article in articles],
		"conclusion": "\n\n".join(conclusion_parts).strip(),
	}


def _build_tts_segments(payload, *, articles_per_segment=DEFAULT_ARTICLES_PER_SEGMENT):
	if articles_per_segment <= 0:
		raise ValueError("articles_per_segment must be greater than 0")

	system_text = payload.get("system", "")
	intro = payload.get("intro", "").strip()
	conclusion = payload.get("conclusion", "").strip()
	articles = payload.get("articles", [])

	segments = []
	if articles:
		# Build groups of N articles. Intro is prepended once, conclusion appended once.
		for start_idx in range(0, len(articles), articles_per_segment):
			chunk = articles[start_idx : start_idx + articles_per_segment]
			chunk_parts = []

			if start_idx == 0 and intro:
				chunk_parts.append(intro)

			chunk_parts.extend(article["text"].strip() for article in chunk if article.get("text", "").strip())

			if start_idx + articles_per_segment >= len(articles) and conclusion:
				chunk_parts.append(conclusion)

			segment_text = "\n\n".join(part for part in chunk_parts if part)
			if segment_text:
				# Prepend system text (without [SYSTEM] tag) if available
				if system_text:
					segment_text = f"{system_text}\n\n{segment_text}"
				segments.append(segment_text)
	else:
		fallback_parts = [part for part in [intro, conclusion] if part]
		if fallback_parts:
			segment_text = "\n\n".join(fallback_parts)
			# Prepend system text (without [SYSTEM] tag) if available
			if system_text:
				segment_text = f"{system_text}\n\n{segment_text}"
			segments.append(segment_text)

	if not segments:
		raise ValueError("No non-empty content found in JSONL input")

	return segments


def _generate_audio_bytes(
	podcast_script,
	*,
	tts_mode,
	tts_provider,
	tts_model,
	voice_id,
	speaker1_name,
	speaker1_voice,
	speaker2_name,
	speaker2_voice,
	google_api_key,
	mistral_api_key
):
	"""Generate audio bytes using the configured TTS provider.
	
	Args:
		podcast_script: Text to convert to speech.
		tts_mode: TTS mode ("mono" or "multi").
		tts_provider: TTS provider ("google_ai_studio" or "mistral").
		tts_model: Model name for the provider.
		voice_id: Voice identifier (mono mode only).
		speaker1_name: Speaker 1 name (multi-speaker mode only).
		speaker1_voice: Speaker 1 voice (multi-speaker mode only).
		speaker2_name: Speaker 2 name (multi-speaker mode only).
		speaker2_voice: Speaker 2 voice (multi-speaker mode only).
		google_api_key: Google API key.
		mistral_api_key: Mistral API key.
	
	Returns:
		Raw audio bytes (format: PCM/WAV depending on provider).
	
	Raises:
		ValueError: If input is invalid.
		RuntimeError: If TTS API call fails.
	"""
	if not podcast_script or not podcast_script.strip():
		raise ValueError("Podcast script is empty")

	connector = _get_tts_connector(tts_mode, tts_provider, google_api_key, mistral_api_key)
	
	if tts_mode == "multi":
		# Multi-speaker mode: pass speaker configuration
		return connector.generate_audio(
			tts_model,
			podcast_script,
			speaker1_name=speaker1_name,
			speaker1_voice=speaker1_voice,
			speaker2_name=speaker2_name,
			speaker2_voice=speaker2_voice,
		)
	else:
		# Mono-speaker mode: pass voice_id
		return connector.generate_audio(tts_model, podcast_script, voice_id)


def _audio_bytes_to_pcm_frames(audio_bytes):
	"""Convert audio bytes to PCM frames.
	
	Handles raw PCM and WAV-wrapped PCM.
	
	Args:
		audio_bytes: Raw audio bytes.
	
	Returns:
		PCM frame bytes or raw audio bytes.
	
	Raises:
		ValueError: If WAV format is incompatible.
	"""
	if not audio_bytes:
		raise ValueError("Received empty audio chunk from TTS")

	# Check if it's a WAV file (Google TTS may return PCM wrapped in WAV)
	if audio_bytes.startswith(b"RIFF") and len(audio_bytes) > 12 and audio_bytes[8:12] == b"WAVE":
		with wave.open(io.BytesIO(audio_bytes), "rb") as handle:
			if handle.getnchannels() != DEFAULT_CHANNELS:
				raise ValueError("WAV chunk has incompatible channel count")
			if handle.getsampwidth() != DEFAULT_SAMPLE_WIDTH:
				raise ValueError("WAV chunk has incompatible sample width")
			if handle.getframerate() != DEFAULT_SAMPLE_RATE:
				raise ValueError("WAV chunk has incompatible sample rate")
			return handle.readframes(handle.getnframes())

	# For raw PCM, return as-is.
	return audio_bytes


def _concatenate_audio_chunks(audio_chunks):
	if not audio_chunks:
		raise ValueError("No audio chunks to concatenate")

	pcm_frames = []
	for chunk in audio_chunks:
		if not chunk:
			continue
		pcm_frames.append(_audio_bytes_to_pcm_frames(chunk))

	if not pcm_frames:
		raise ValueError("All audio chunks are empty")

	# Final WAV header is written once by _write_wav_file; here we only concat PCM frames.
	return b"".join(pcm_frames)


def _write_wav_file(output_file, audio_bytes):
	"""Write PCM audio bytes to a WAV file.
	
	Args:
		output_file: Path to output WAV file.
		audio_bytes: Raw PCM bytes.
	"""
	with wave.open(str(output_file), "wb") as handle:
		handle.setnchannels(DEFAULT_CHANNELS)
		handle.setsampwidth(DEFAULT_SAMPLE_WIDTH)
		handle.setframerate(DEFAULT_SAMPLE_RATE)
		handle.writeframes(audio_bytes)


def _write_audio_file(output_file, audio_bytes, file_format="wav"):
	"""Write audio bytes to a file.
	
	Args:
		output_file: Path to output file.
		audio_bytes: Raw audio bytes.
		file_format: Format type ("wav" or "mp3").
	
	Raises:
		ValueError: If format is unsupported.
	"""
	if file_format.lower() == "wav":
		_write_wav_file(output_file, audio_bytes)
	elif file_format.lower() == "mp3":
		# Write MP3 bytes directly
		with open(str(output_file), "wb") as f:
			f.write(audio_bytes)
	else:
		raise ValueError(f"Unsupported audio format: {file_format}")


def _save_audio_to_bucket(bucket_name, filename, audio_bytes, file_format="wav"):
	"""Save audio to the configured storage bucket.
	
	Args:
		bucket_name: GCS bucket name.
		filename: Output filename.
		audio_bytes: Audio bytes to save.
		file_format: Format type ("wav" or "mp3").
	"""
	tmp_file = Path(tempfile.gettempdir()) / filename
	_write_audio_file(tmp_file, audio_bytes, file_format)

	storage_client = storage.Client()
	bucket = storage_client.bucket(bucket_name)
	blob = bucket.blob(filename)
	
	content_type = "audio/mpeg" if file_format.lower() == "mp3" else "audio/wav"
	blob.upload_from_filename(str(tmp_file), content_type=content_type)


def run_generate_step(
	input_filename,
	output_filename,
	*,
	bucket_name,
	tts_mode,
	tts_provider,
	tts_model,
	voice_id,
	speaker1_name,
	speaker1_voice,
	speaker2_name,
	speaker2_voice,
	google_api_key,
	mistral_api_key,
):
	"""Run the TTS pipeline to generate audio from podcast script.
	
	Args:
		input_filename: Input JSONL filename.
		output_filename: Output audio filename.
		bucket_name: GCS bucket name.
		tts_mode: TTS mode ("mono" or "multi").
		tts_provider: TTS provider to use.
		tts_model: Model name.
		voice_id: Voice identifier (mono mode).
		speaker1_name: Speaker 1 name (multi mode).
		speaker1_voice: Speaker 1 voice (multi mode).
		speaker2_name: Speaker 2 name (multi mode).
		speaker2_voice: Speaker 2 voice (multi mode).
		google_api_key: Google API key.
		mistral_api_key: Mistral API key.
	
	Returns:
		Dictionary with pipeline result information.
	"""
	podcast_payload = _load_podcast_jsonl(
		input_filename,
		bucket_name=bucket_name,
	)
	segments = _build_tts_segments(podcast_payload, articles_per_segment=DEFAULT_ARTICLES_PER_SEGMENT)

	audio_chunks = []
	# One TTS call per segment (2 articles per segment by default).
	for segment in segments:
		audio_chunks.append(
			_generate_audio_bytes(
				segment,
				tts_mode=tts_mode,
				tts_provider=tts_provider,
				tts_model=tts_model,
				voice_id=voice_id,
				speaker1_name=speaker1_name,
				speaker1_voice=speaker1_voice,
				speaker2_name=speaker2_name,
				speaker2_voice=speaker2_voice,
				google_api_key=google_api_key,
				mistral_api_key=mistral_api_key,
			)
		)

	audio_bytes = _concatenate_audio_chunks(audio_chunks)
	audio_format = "wav"
	output_filename_with_ext = output_filename
	
	_save_audio_to_bucket(
		bucket_name,
		output_filename_with_ext,
		audio_bytes,
		file_format=audio_format,
	)

	return {
		"input_filename": input_filename,
		"output_filename": output_filename_with_ext,
		"tts_mode": tts_mode,
		"tts_provider": tts_provider,
		"tts_model": tts_model,
		"voice_id": voice_id if tts_mode == "mono" else None,
		"speaker1_name": speaker1_name if tts_mode == "multi" else None,
		"speaker1_voice": speaker1_voice if tts_mode == "multi" else None,
		"speaker2_name": speaker2_name if tts_mode == "multi" else None,
		"speaker2_voice": speaker2_voice if tts_mode == "multi" else None,
		"audio_format": audio_format,
		"article_count": len(podcast_payload.get("articles", [])),
		"segment_count": len(segments),
		"audio_size_bytes": len(audio_bytes),
	}


@functions_framework.http
def generate_tts_content(request):
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
		_validate_tts_config(config["tts_mode"], config["tts_provider"], config["google_api_key"], config["mistral_api_key"])
	except RuntimeError as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)

	client_key = request.headers.get("X-API-Key")
	if not client_key or client_key != config["secret_api_key"]:
		return (json.dumps({"error": "Access denied: Missing or invalid API key"}), 403, headers)

	defaults = _resolve_daily_filenames()
	request_json = request.get_json(silent=True) or {}
	input_filename = request_json.get("input_filename", defaults["input_filename"])
	output_filename = request_json.get("output_filename", defaults["output_filename"])
	tts_mode = str(request_json.get("tts_mode", config["tts_mode"])).lower().strip()
	tts_provider = str(request_json.get("tts_provider", config["tts_provider"])).lower().strip()
	tts_model = request_json.get("tts_model") or _default_tts_model_for_provider(tts_provider)
	voice_id = request_json.get("voice_id") or _default_voice_id_for_provider(tts_provider)

	try:
		_validate_tts_config(tts_mode, tts_provider, config["google_api_key"], config["mistral_api_key"])
		pipeline_result = run_generate_step(
			input_filename,
			output_filename,
			bucket_name=config["bucket_name"],
			tts_mode=tts_mode,
			tts_provider=tts_provider,
			tts_model=tts_model,
			voice_id=voice_id,
			speaker1_name=config["speaker1_name"],
			speaker1_voice=config["speaker1_voice"],
			speaker2_name=config["speaker2_name"],
			speaker2_voice=config["speaker2_voice"],
			google_api_key=config["google_api_key"],
			mistral_api_key=config["mistral_api_key"],
		)
		response = {
			"status": "ok",
			"bucket": config["bucket_name"],
			**pipeline_result,
		}
		return (json.dumps(response), 200, headers)

	except FileNotFoundError as exc:
		return (json.dumps({"error": str(exc)}), 404, headers)
	except ValueError as exc:
		return (json.dumps({"error": str(exc)}), 400, headers)
	except Exception as exc:
		return (json.dumps({"error": f"Technical error: {str(exc)}"}), 500, headers)
