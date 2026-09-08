"""Console entrypoint: crawl a novel URL and write EPUB + TXT files."""

import argparse
import logging
import traceback
from pathlib import Path

from .binder import make_epub, make_text
from .context import APP_DIR, ctx
from .core import Novel
from .exceptions import LNException
from .utils.file_tools import safe_filename


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lncrawl-mini",
        description="Download a web novel by URL and export EPUB / TXT.",
    )
    parser.add_argument("url", help="Novel details page URL.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=str(APP_DIR / "light_novels"),
        help="Directory to save downloaded files.",
    )
    parser.add_argument(
        "--first",
        type=int,
        default=None,
        metavar="N",
        help="Download only the first N chapters.",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="Download only the last N chapters.",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=None,
        metavar="N",
        help="Limit to N requests per second on this site.",
    )
    parser.add_argument(
        "--no-cover",
        action="store_true",
        help="Do not download or generate a cover image.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show debug logs.",
    )
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    ctx.setup(log_level=logging.DEBUG if args.verbose else logging.WARNING)

    from .services.sources import Sources

    sources = Sources()
    crawler = None
    try:
        crawler = sources.init_crawler(args.url)

        if args.rate_limit:
            from .services.scraper import ctx_scraper

            ctx_scraper.set_rate_limit(args.url, args.rate_limit)

        novel = Novel(url=args.url)
        crawler.read_novel(novel)
        crawler.format_novel(novel)
        if not novel.title:
            raise LNException("No novel title found")

        chapters = list(novel.chapters)
        if args.first:
            chapters = chapters[: args.first]
        elif args.last:
            chapters = chapters[-args.last :]
        if not chapters:
            raise LNException("No chapters to download")

        # download chapter bodies in parallel
        futures = {
            crawler.taskman.submit_task(lambda ch: _fetch_chapter(crawler, ch), chapter): chapter
            for chapter in chapters
        }
        results = []
        for chapter in crawler.taskman.resolve(futures, desc="Chapters", unit="chap"):
            if chapter:
                results.append(chapter)
        failures = [c for c in chapters if not c.success]
        if failures:
            print(f"{len(failures)} chapter(s) failed to download")

        out_dir = Path(args.output_dir) / safe_filename(novel.title or "novel")
        out_dir.mkdir(parents=True, exist_ok=True)

        cover_file = None
        if not args.no_cover and novel.cover_url:
            cover_file = out_dir / "cover.jpg"
            try:
                crawler.download_cover(novel.cover_url, cover_file)
            except Exception as e:
                print(f"Cover failed: {e}")
                cover_file = None

        epub_file = make_epub(
            novel, results, out_dir / f"{safe_filename(novel.title)}.epub", cover_file
        )
        txt_file = make_text(novel, results, out_dir / f"{safe_filename(novel.title)}.txt")

        print(f"\nEPUB: {epub_file}")
        print(f"TXT : {txt_file}")
    except Exception as e:
        traceback.print_exc()
        print(f"Failed: {e}")
        raise SystemExit(1)
    finally:
        if crawler is not None:
            crawler.close()
        ctx.destroy()


def _fetch_chapter(crawler, chapter):
    try:
        crawler.download_chapter(chapter)
    except Exception as e:
        print(f"Chapter {chapter.id} failed: {e}")
        chapter.success = False
        return chapter
    crawler.format_chapter(chapter)
    return chapter


if __name__ == "__main__":
    main()
