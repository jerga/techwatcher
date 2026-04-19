import argparse
import importlib.util
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent


def _load_module(name, relative_path):
    file_path = ROOT_DIR / relative_path
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {relative_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COLLECT_MODULE = _load_module("collect_step_module", Path("1_collect") / "collect.py")
EXTRACT_MODULE = _load_module("extract_step_module", Path("2_extract") / "extract.py")
SUMMARIZE_MODULE = _load_module("summarize_step_module", Path("3_summarize") / "summarize.py")
GENERATE_MODULE = _load_module("generate_step_module", Path("4_generate") / "generate_tts.py")
PUBLISH_MODULE = _load_module("publish_step_module", Path("5_publish") / "publish.py")

STEP_ORDER = ["collect", "extract", "summarize", "generate", "publish"]


def _resolve_date(date_override, module_with_datetime):
    if date_override:
        return module_with_datetime.datetime.datetime.strptime(date_override, "%Y%m%d").strftime("%Y%m%d")
    return module_with_datetime.datetime.datetime.now().strftime("%Y%m%d")


def _run_collect(args):
    config = COLLECT_MODULE._get_runtime_config()
    bucket_name = args.bucket_name or config["bucket_name"]
    link_text = args.url or args.content
    filename = COLLECT_MODULE.run_collect_step(
        link_text,
        bucket_name=bucket_name,
        date_override=args.date,
    )
    return {"status": "ok", "filename": filename, "bucket_name": bucket_name}


def _run_extract(args):
    config = EXTRACT_MODULE._get_runtime_config()
    bucket_name = args.bucket_name or config["bucket_name"]
    date_str = _resolve_date(args.date, EXTRACT_MODULE)
    input_filename = args.input_filename or f"links_{date_str}.txt"
    output_filename = args.output_filename or f"full_content_{date_str}.jsonl"
    error_output_filename = args.error_output_filename or f"fetch_errors_{date_str}.jsonl"
    result = EXTRACT_MODULE.run_extract_step(
        input_filename,
        output_filename,
        error_output_filename,
        bucket_name=bucket_name,
    )
    return {"status": "ok", "bucket_name": bucket_name, **result}


def _run_summarize(args):
    config = SUMMARIZE_MODULE._get_runtime_config()
    bucket_name = args.bucket_name or config["bucket_name"]
    SUMMARIZE_MODULE._validate_llm_config(config["llm_provider"], config["google_api_key"])
    defaults = SUMMARIZE_MODULE._resolve_daily_filenames(args.date)
    input_filename = args.input_filename or defaults["input_filename"]
    output_filename = args.output_filename or defaults["output_filename"]
    llm_model = args.llm_model or config["llm_model"]
    result = SUMMARIZE_MODULE.run_summarize_step(
        input_filename,
        output_filename,
        bucket_name=bucket_name,
        llm_provider=config["llm_provider"],
        llm_model=llm_model,
        google_api_key=config["google_api_key"],
        tts_mode=config["tts_mode"],
    )
    return {"status": "ok", "bucket_name": bucket_name, **result}


def _run_generate(args):
    config = GENERATE_MODULE._get_runtime_config()
    bucket_name = args.bucket_name or config["bucket_name"]
    tts_provider = args.tts_provider or config["tts_provider"]
    tts_mode = args.tts_mode or config["tts_mode"]
    tts_model = args.tts_model or GENERATE_MODULE._default_tts_model_for_provider(tts_provider)
    voice_id = args.voice_id or GENERATE_MODULE._default_voice_id_for_provider(tts_provider)
    GENERATE_MODULE._validate_tts_config(tts_mode, tts_provider, config["google_api_key"], config["mistral_api_key"])
    defaults = GENERATE_MODULE._resolve_daily_filenames(args.date)
    input_filename = args.input_filename or defaults["input_filename"]
    output_filename = args.output_filename or defaults["output_filename"]
    result = GENERATE_MODULE.run_generate_step(
        input_filename,
        output_filename,
        bucket_name=bucket_name,
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
    return {"status": "ok", "bucket_name": bucket_name, **result}


def _run_publish(args):
    config = PUBLISH_MODULE._get_runtime_config()
    bucket_name = args.bucket_name or config["bucket_name"]
    token_blob = args.token_blob or config["soundcloud_token_blob"]
    PUBLISH_MODULE._validate_soundcloud_config(config["soundcloud_client_id"], config["soundcloud_client_secret"])
    PUBLISH_MODULE._validate_soundcloud_token_store_config(bucket_name, token_blob)
    defaults = PUBLISH_MODULE._resolve_daily_defaults(args.date)
    input_filename = args.input_filename or defaults["input_filename"]
    title = args.title or defaults["title"]
    result = PUBLISH_MODULE.run_publish_step(
        input_filename,
        bucket_name=bucket_name,
        token_blob=token_blob,
        client_id=config["soundcloud_client_id"],
        client_secret=config["soundcloud_client_secret"],
        title=title,
        description=args.description,
        sharing=args.sharing,
        token_endpoint=config["token_endpoint"],
        track_upload_endpoint=config["track_upload_endpoint"],
    )
    return {
        "status": "ok",
        "bucket_name": bucket_name,
        **result,
    }


def _run_pipeline(args):
    start_index = STEP_ORDER.index(args.from_step)
    end_index = STEP_ORDER.index(args.to_step)
    if start_index > end_index:
        raise ValueError("from-step must come before to-step")

    selected_steps = STEP_ORDER[start_index : end_index + 1]
    date_str = _resolve_date(args.date, EXTRACT_MODULE)
    results = []

    if "collect" in selected_steps:
        if not args.url and not args.content:
            raise ValueError("pipeline collect step requires --url or --content")
        collect_args = argparse.Namespace(
            url=args.url,
            content=args.content,
            date=date_str,
            bucket_name=args.bucket_name,
        )
        results.append({"step": "collect", **_run_collect(collect_args)})

    if "extract" in selected_steps:
        extract_args = argparse.Namespace(
            input_filename=args.extract_input_filename,
            output_filename=args.extract_output_filename,
            error_output_filename=args.extract_error_output_filename,
            date=date_str,
            bucket_name=args.bucket_name,
        )
        results.append({"step": "extract", **_run_extract(extract_args)})

    if "summarize" in selected_steps:
        summarize_args = argparse.Namespace(
            input_filename=args.summarize_input_filename,
            output_filename=args.summarize_output_filename,
            llm_model=args.llm_model,
            date=date_str,
            bucket_name=args.bucket_name,
        )
        results.append({"step": "summarize", **_run_summarize(summarize_args)})

    if "generate" in selected_steps:
        generate_args = argparse.Namespace(
            input_filename=args.generate_input_filename,
            output_filename=args.generate_output_filename,
            tts_mode=args.tts_mode,
            tts_provider=args.tts_provider,
            tts_model=args.tts_model,
            voice_id=args.voice_id,
            date=date_str,
            bucket_name=args.bucket_name,
        )
        results.append({"step": "generate", **_run_generate(generate_args)})

    if "publish" in selected_steps:
        publish_args = argparse.Namespace(
            input_filename=args.publish_input_filename,
            title=args.title,
            description=args.description,
            sharing=args.sharing,
            token_blob=args.token_blob,
            date=date_str,
            bucket_name=args.bucket_name,
        )
        results.append({"step": "publish", **_run_publish(publish_args)})

    return {
        "status": "ok",
        "date": date_str,
        "steps": selected_steps,
        "results": results,
    }


def _build_parser():
    parser = argparse.ArgumentParser(description="Run a pipeline step directly without going through HTTP handlers.")
    subparsers = parser.add_subparsers(dest="step", required=True)

    collect_parser = subparsers.add_parser("collect", help="Run the collect step directly")
    collect_group = collect_parser.add_mutually_exclusive_group(required=True)
    collect_group.add_argument("--url", help="URL to append to the daily links file")
    collect_group.add_argument("--content", help="Raw content to append to the daily links file")
    collect_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    collect_parser.add_argument("--bucket-name", help="Bucket name override")
    collect_parser.set_defaults(run_step=_run_collect)

    extract_parser = subparsers.add_parser("extract", help="Run the extract step directly")
    extract_parser.add_argument("--input-filename", help="Input filename override")
    extract_parser.add_argument("--output-filename", help="Output filename override")
    extract_parser.add_argument("--error-output-filename", help="Error output filename override")
    extract_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    extract_parser.add_argument("--bucket-name", help="Bucket name override")
    extract_parser.set_defaults(run_step=_run_extract)

    summarize_parser = subparsers.add_parser("summarize", help="Run the summarize step directly")
    summarize_parser.add_argument("--input-filename", help="Input filename override")
    summarize_parser.add_argument("--output-filename", help="Output filename override")
    summarize_parser.add_argument("--llm-model", help="Model override")
    summarize_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    summarize_parser.add_argument("--bucket-name", help="Bucket name override")
    summarize_parser.set_defaults(run_step=_run_summarize)

    generate_parser = subparsers.add_parser("generate", help="Run the generate step directly")
    generate_parser.add_argument("--input-filename", help="Input filename override")
    generate_parser.add_argument("--output-filename", help="Output filename override")
    generate_parser.add_argument("--tts-mode", choices=["mono", "multi"], help="TTS mode override")
    generate_parser.add_argument("--tts-provider", help="TTS provider override")
    generate_parser.add_argument("--tts-model", help="TTS model override")
    generate_parser.add_argument("--voice-id", help="Voice ID override for mono mode")
    generate_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    generate_parser.add_argument("--bucket-name", help="Bucket name override")
    generate_parser.set_defaults(run_step=_run_generate)

    publish_parser = subparsers.add_parser("publish", help="Run the publish step directly")
    publish_parser.add_argument("--input-filename", help="Input filename override")
    publish_parser.add_argument("--title", help="Track title override")
    publish_parser.add_argument("--description", default="", help="Track description")
    publish_parser.add_argument("--sharing", choices=["private", "public"], default=PUBLISH_MODULE.DEFAULT_SOUNDCLOUD_SHARING, help="Track visibility")
    publish_parser.add_argument("--token-blob", help="SoundCloud token blob override")
    publish_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    publish_parser.add_argument("--bucket-name", help="Bucket name override")
    publish_parser.set_defaults(run_step=_run_publish)

    pipeline_parser = subparsers.add_parser("pipeline", help="Run several steps in sequence")
    pipeline_parser.add_argument("--from-step", choices=STEP_ORDER, default="extract", help="First step to run")
    pipeline_parser.add_argument("--to-step", choices=STEP_ORDER, default="publish", help="Last step to run")
    pipeline_parser.add_argument("--date", help="Date override in YYYYMMDD format")
    pipeline_collect_group = pipeline_parser.add_mutually_exclusive_group(required=False)
    pipeline_collect_group.add_argument("--url", help="URL to collect before running the rest of the pipeline")
    pipeline_collect_group.add_argument("--content", help="Raw content to collect before running the rest of the pipeline")
    pipeline_parser.add_argument("--extract-input-filename", help="Extract input filename override")
    pipeline_parser.add_argument("--extract-output-filename", help="Extract output filename override")
    pipeline_parser.add_argument("--extract-error-output-filename", help="Extract error filename override")
    pipeline_parser.add_argument("--summarize-input-filename", help="Summarize input filename override")
    pipeline_parser.add_argument("--summarize-output-filename", help="Summarize output filename override")
    pipeline_parser.add_argument("--generate-input-filename", help="Generate input filename override")
    pipeline_parser.add_argument("--generate-output-filename", help="Generate output filename override")
    pipeline_parser.add_argument("--publish-input-filename", help="Publish input filename override")
    pipeline_parser.add_argument("--llm-model", help="Summarize model override")
    pipeline_parser.add_argument("--tts-mode", choices=["mono", "multi"], help="Generate TTS mode override")
    pipeline_parser.add_argument("--tts-provider", help="Generate TTS provider override")
    pipeline_parser.add_argument("--tts-model", help="Generate TTS model override")
    pipeline_parser.add_argument("--voice-id", help="Generate voice ID override")
    pipeline_parser.add_argument("--title", help="Publish track title override")
    pipeline_parser.add_argument("--description", default="", help="Publish track description")
    pipeline_parser.add_argument("--sharing", choices=["private", "public"], default=PUBLISH_MODULE.DEFAULT_SOUNDCLOUD_SHARING, help="Publish sharing mode")
    pipeline_parser.add_argument("--token-blob", help="Publish token blob override")
    pipeline_parser.add_argument("--bucket-name", help="Bucket name override")
    pipeline_parser.set_defaults(run_step=_run_pipeline)

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()
    result = args.run_step(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()