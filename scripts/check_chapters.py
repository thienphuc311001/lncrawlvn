# -*- coding: utf-8 -*-
"""Check every chapter of a novel against the source, automatically.

Flags chapters whose bodies are unusually short, then re-crawls each flagged
chapter serially to tell two kinds of shortfall apart:

- A transient crawl fault (a missed decode, a skipped split chain) — the
  re-crawl comes back complete, so the chapter is recoverable.
- The site serving truncated content at the source — the re-crawl comes back
  with the same short body it gave the first time, so the chapter cannot be
  re-crawled from this source at all.
"""

import argparse
import logging
import statistics
import sys

from lncrawl.context import ctx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_chapters",
        description="Check each chapter's content against the source.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://www.qbmfxs.com/book_1/93387.html",
        help="Novel details page URL (default: 大宋神探录 on qbmfxs.com).",
    )
    parser.add_argument(
        "--first",
        type=int,
        default=None,
        metavar="N",
        help="Check only the first N chapters.",
    )
    parser.add_argument(
        "--min-threshold",
        type=int,
        default=300,
        metavar="N",
        help="Bodies shorter than N characters are flagged (default: 300).",
    )
    return parser


def fetch_chapter(crawler, chapter):
    try:
        crawler.download_chapter(chapter)
    except Exception as e:
        logging.getLogger("check_chapters").warning(
            f"Chapter {chapter.id} failed: {e}"
        )
        chapter.success = False
        return chapter
    crawler.format_chapter(chapter)
    return chapter


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    ctx.setup(log_level=logging.WARNING)

    from lncrawl.core import Chapter, Novel
    from lncrawl.services.sources import Sources

    crawler = None
    try:
        sources = Sources()
        crawler = sources.init_crawler(args.url)

        novel = Novel(url=args.url)
        crawler.read_novel(novel)
        crawler.format_novel(novel)

        chapters = list(novel.chapters)
        if args.first:
            chapters = chapters[: args.first]
        if not chapters:
            print("No chapters found")
            raise SystemExit(1)

        # ----- first pass: crawl every chapter in parallel ---------------- #
        futures = {
            crawler.taskman.submit_task(fetch_chapter, crawler, chapter): chapter
            for chapter in chapters
        }
        for _ in crawler.taskman.resolve(futures, desc="Chapters", unit="chap"):
            pass

        # ----- flag unusually short bodies -------------------------------- #
        lengths = {ch.id: len(ch.body or "") for ch in chapters}
        median = statistics.median(lengths.values())
        flagged = [
            ch
            for ch in chapters
            if lengths[ch.id] < 0.3 * median or lengths[ch.id] < args.min_threshold
        ]

        # ----- serial re-crawl of every flagged chapter ------------------- #
        recovered = []
        site_limited = []
        for ch in flagged:
            retry = Chapter(id=ch.id, url=ch.url, title=ch.title)
            fetch_chapter(crawler, retry)
            retry_len = len(retry.body or "")
            if retry_len >= 0.3 * median and retry_len >= args.min_threshold:
                ch.body = retry.body
                ch.success = retry.success
                recovered.append((ch, retry_len))
            else:
                site_limited.append((ch, retry_len))

        # ----- final report ----------------------------------------------- #
        print()
        print("=" * 60)
        print("CHAPTER CONTENT CHECK REPORT")
        print("=" * 60)
        print(f"Source median body length : {median:.0f} chars")
        print(f"Total chapters checked    : {len(chapters)}")
        print(f"Complete on first crawl   : {len(chapters) - len(flagged)}")
        print(f"Flagged as short          : {len(flagged)}")
        print(f"  Recovered by re-crawl   : {len(recovered)}")
        print(f"  Site limit (unfixable)  : {len(site_limited)}")
        if site_limited:
            print()
            print("Chapters the site serves truncated at the source:")
            for ch, retry_len in site_limited:
                head = (ch.body or "").strip().replace("\n", " ")[:40]
                print(
                    f"  - #{ch.id} {ch.title} "
                    f"(body {lengths[ch.id]}/{retry_len} chars): {head}..."
                )
        print("=" * 60)

        raise SystemExit(1 if site_limited else 0)
    finally:
        if crawler is not None:
            crawler.close()
        ctx.destroy()


if __name__ == "__main__":
    main()
