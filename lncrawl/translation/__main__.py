"""Run the same translation pipeline from UTF-8 files: python -m lncrawl.translation."""

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from ..context import APP_DIR
from .dictionary import load_legacy
from .models import PARSER_VERSION, Inputs
from .parsing import deterministic_alignment, parse_chapters, validate_inputs
from .pipeline import Pipeline
from .scheduler import Scheduler, api_keys
from .store import Store


class ConsoleStore(Store):
    def progress(self, status="running", **fields):
        result = super().progress(status, **fields)
        print(
            json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if key
                    in (
                        "status",
                        "stage",
                        "aligned",
                        "total_chapters",
                        "completed_chapters",
                        "scanned_chapter",
                        "candidate_count",
                        "current_term",
                        "resolved_candidates",
                        "reconciliation_round",
                        "active_chapters",
                        "error",
                        "dictionary_hash",
                        "dictionary_frozen",
                        "request_statistics",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    def diagnostic(self, metadata):
        super().diagnostic(metadata)
        print(json.dumps(metadata, ensure_ascii=False), flush=True)


def parser():
    arguments = argparse.ArgumentParser(
        description="Translate Chinese RAW + external VietPhrase into Vietnamese using server-side Gemini."
    )
    arguments.add_argument("--raw", type=Path, required=True)
    arguments.add_argument("--vietphrase", type=Path, required=True)
    arguments.add_argument("--dictionary", type=Path)
    arguments.add_argument("--output", type=Path, default=Path("translation-output"))
    arguments.add_argument(
        "--workers", type=int, choices=(1, 2, 3), default=int(os.getenv("TRANSLATION_WORKERS", "2"))
    )
    arguments.add_argument(
        "--first", type=int, help="Test only the first N chapters; omit for the full batch."
    )
    arguments.add_argument(
        "--check-inputs",
        action="store_true",
        help="Parse and pair the complete files without any API requests.",
    )
    return arguments


def first_chapters(text, count, label):
    diagnostics = []
    chapters = parse_chapters(text, label, diagnostics)
    if count >= len(chapters):
        return text
    end = chapters[count].source_line
    # A following chapter owns its preceding structural volume marker. Do not
    # accidentally append that marker to the final selected chapter's prose.
    for diagnostic in diagnostics:
        if diagnostic["reason"] in ("volume_boundary_detected", "repeated_volume_label"):
            line = diagnostic["line"]
            if chapters[count - 1].source_line < line < end:
                end = line
    return "\n".join(text.splitlines()[: end - 1])


async def translate(arguments):
    raw = arguments.raw.read_text(encoding="utf-8-sig")
    vp = arguments.vietphrase.read_text(encoding="utf-8-sig")
    dictionary = (
        json.loads(arguments.dictionary.read_text(encoding="utf-8-sig"))
        if arguments.dictionary
        else None
    )
    pairs = validate_inputs(raw, vp)
    for raw_chapter, vp_chapter in pairs:
        deterministic_alignment(raw_chapter, vp_chapter)
    load_legacy(dictionary)
    print(f"Validated {len(pairs)} chapter pairs.", flush=True)
    if arguments.check_inputs:
        for r, v in pairs:
            print(
                f"Chapter {r.key}: RAW {len(r.paragraphs)} paragraphs / VP {len(v.paragraphs)} paragraphs"
            )
        return 0
    if arguments.first is not None:
        if arguments.first < 1:
            raise ValueError("--first must be positive")
        raw = first_chapters(raw, arguments.first, "RAW")
        vp = first_chapters(vp, arguments.first, "VIETPHRASE")
    if not api_keys():
        raise ValueError("Set GOOGLE_AI_API_KEY in the project-root .env file")
    inputs = Inputs(raw=raw, vietphrase=vp, dictionary=dictionary)
    store = ConsoleStore(APP_DIR / "translations", inputs=inputs.model_dump())
    store.write("parser-version.json", {"version": PARSER_VERSION})
    print(f"Checkpoint: {store.path}", flush=True)
    with store.execution():
        await run_owned(store, arguments.workers)
    arguments.output.mkdir(parents=True, exist_ok=True)
    for name in ("translated.json", "dictionary.json"):
        target = arguments.output / name
        if target.exists() and target.read_bytes() != (store.path / name).read_bytes():
            raise ValueError(
                f"Output already exists with different content: {target}; choose another --output directory"
            )
    for name in ("translated.json", "dictionary.json"):
        shutil.copyfile(store.path / name, arguments.output / name)
    print(
        f"Completed: {arguments.output.resolve() / 'translated.json'} and {arguments.output.resolve() / 'dictionary.json'}",
        flush=True,
    )
    return 0


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
        if store.read("progress.json", {}).get("status") != "done":
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
