"""Run the RAW-only translation pipeline from UTF-8 files."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from ..context import APP_DIR
from .dictionary import load_dictionary, load_unresolved
from .models import PARSER_VERSION, PIPELINE_VERSION, Inputs
from .parsing import parse_chapters
from .pipeline import Pipeline
from .scheduler import Scheduler, api_keys
from .store import Store

class ConsoleStore(Store):
    def progress(self, status="running", **fields):
        result = super().progress(status, **fields)
        print(json.dumps({k: v for k, v in result.items() if k in (
            "status", "stage", "total_chapters", "completed_chapters",
            "current_chapter", "candidate_count", "error", "request_statistics",
        )}, ensure_ascii=False), flush=True)
        return result

    def diagnostic(self, metadata):
        super().diagnostic(metadata)
        print(json.dumps(metadata, ensure_ascii=False), flush=True)

def parser():
    args = argparse.ArgumentParser(description="Translate Chinese RAW chapters into Vietnamese.")
    args.add_argument("--raw", type=Path, required=True)
    args.add_argument("--dictionary", type=Path)
    args.add_argument("--output", type=Path, default=Path("translation-output"))
    args.add_argument("--workers", type=int, choices=(1, 2, 3),
                      default=int(os.getenv("TRANSLATION_WORKERS", "2")))
    args.add_argument("--first", type=int, help="Translate only the first N chapters.")
    args.add_argument("--check-inputs", action="store_true", help="Validate RAW without API requests.")
    return args

def first_chapters(text, count, label="RAW"):
    chapters = parse_chapters(text, label)
    if count < 1:
        raise ValueError("--first must be positive")
    if count >= len(chapters):
        return text
    return "\n".join(text.splitlines()[:chapters[count].source_line - 1])

async def run_owned(store, workers):
    store.write("cancel-request.json", {"requested": False})
    runner = asyncio.current_task()
    async def watch_cancellation():
        while True:
            await asyncio.sleep(0.3)
            if store.read("cancel-request.json", {}).get("requested"):
                runner.cancel()
                return
    watcher = asyncio.create_task(watch_cancellation())
    try:
        if (store.read("progress.json", {}).get("status") != "done"
                or store.read("pipeline-version.json", {}).get("version") != PIPELINE_VERSION):
            await Pipeline(store, Scheduler(concurrency=workers)).run()
    except asyncio.CancelledError:
        store.progress("cancelled", stage="Cancelled; checkpoints preserved")
        raise
    except Exception as exc:
        store.progress("failed", stage="Stopped; run the same command to resume", error=str(exc))
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

async def translate(arguments):
    raw = arguments.raw.read_text(encoding="utf-8-sig")
    dictionary = json.loads(arguments.dictionary.read_text(encoding="utf-8-sig")) if arguments.dictionary else None
    chapters = parse_chapters(raw)
    load_dictionary(dictionary)
    load_unresolved(dictionary)
    if arguments.first is not None:
        raw = first_chapters(raw, arguments.first)
        chapters = parse_chapters(raw)
    print(f"Validated {len(chapters)} RAW chapters.", flush=True)
    if arguments.check_inputs:
        for chapter in chapters:
            print(f"Chapter {chapter.number}: {len(chapter.paragraphs)} paragraphs")
        return 0
    if not api_keys():
        raise ValueError("Set GOOGLE_AI_API_KEY in the project-root .env file")
    inputs = Inputs(raw=raw, dictionary=dictionary, book_title=arguments.raw.stem,
                    source_name=arguments.raw.name).model_dump()
    store = ConsoleStore(APP_DIR / "translations", inputs=inputs)
    store.write("parser-version.json", {"version": PARSER_VERSION})
    print(f"Checkpoint: {store.path}", flush=True)
    with store.execution():
        await run_owned(store, arguments.workers)
    arguments.output.mkdir(parents=True, exist_ok=True)
    for name in ("translated.json", "translated.txt", "dictionary.json", "unresolved.json",
                 "author-notes.txt"):
        source = store.path / name
        if not source.exists():
            continue
        target = arguments.output / name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError(f"Output already exists with different content: {target}")
        shutil.copyfile(source, target)
    print(f"Completed translation and full merged dictionary in {arguments.output.resolve()}", flush=True)
    return 0

def main():
    try:
        return asyncio.run(translate(parser().parse_args()))
    except KeyboardInterrupt:
        print("Cancelled; run the same command to resume.", file=sys.stderr)
        return 130
    except asyncio.CancelledError:
        print("Cancelled; run the same command to resume.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
