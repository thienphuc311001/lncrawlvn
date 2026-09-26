import base64
import json
import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)
search_url = "%s/tim-kiem-nang-cao?title=%s"


class ListNovelCrawler(LegacyCrawler):
    has_mtl = True
    base_url = [
        "https://ln.hako.vn/",
        "https://docln.net/",
    ]

    def initialize(self):
        # Some local resolvers return 127.0.0.1 for both Hako hosts. Keep the
        # original URL, TLS verification, cookies and scraper pacing; only
        # resolve its hostname through curl's DNS-over-HTTPS support.
        self.scraper.transport._session.doh_url = "https://cloudflare-dns.com/dns-query"

    def search_novel(self, query):
        soup = self.get_soup(
            search_url % (self.scraper.origin.rstrip("/"), quote_plus(query))
        )

        results = []
        for tab in soup.select(".sect-body .thumb-item-flow"):
            a = tab.select_one(".series-title a")
            if not a or not a.get("href"):
                continue
            latest_vol = tab.select_one(".volume-title")
            latest_chap = tab.select_one(".chapter-title a")
            info = [
                tag.get("title") or tag.text.strip()
                for tag in (latest_vol, latest_chap)
                if tag
            ]
            results.append(
                {
                    "title": a.get("title") or a.text.strip(),
                    "url": self.absolute_url(a["href"]),
                    "info": " | ".join(info),
                }
            )
        return results

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one(".series-name a")
        if not title:
            raise ValueError("No Hako novel title found")
        self.novel_title = title.text.strip()

        self.novel_author = ", ".join(
            a.text.strip() for a in soup.select('.info-value a[href*="/tac-gia/"]')
        )
        cover = soup.select_one(".series-cover .img-in-ratio")
        if cover:
            match = re.search(r"""url\(\s*['"]?([^'")]+)""", cover.get("style", ""))
            if match:
                self.novel_cover = self.absolute_url(match.group(1))
        summary = soup.select_one(".series-summary .summary-content")
        if summary:
            paragraphs = summary.tag.select("p")
            self.novel_synopsis = (
                "\n".join(p.get_text().strip() for p in paragraphs)
                if paragraphs
                else summary.tag.get_text(" ", strip=True)
            )
        self.novel_tags = [
            a.text.strip() for a in soup.select('.series-gernes a[href*="/the-loai/"]')
        ]

        for section in soup.select(".volume-list"):
            links = section.select(".list-chapters a[href]")
            if not links:
                continue
            heading = section.select_one(".sect-title")
            vol_id = len(self.volumes) + 1
            self.volumes.append(
                Volume(id=vol_id, title=heading.text.strip() if heading else "")
            )
            for a in links:
                self.chapters.append(
                    Chapter(
                        id=len(self.chapters) + 1,
                        volume=vol_id,
                        title=a.get("title") or a.text.strip(),
                        url=self.absolute_url(a["href"]),
                    )
                )
        if not self.chapters:
            raise ValueError("No Hako chapters found")
        logger.info(
            "Hako novel %s: %d volumes, %d chapters",
            self.novel_title,
            len(self.volumes),
            len(self.chapters),
        )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        content = soup.select_one("#chapter-content")
        if not content:
            raise ValueError(f"No Hako chapter content at {chapter['url']}")

        protected = content.select_one("#chapter-c-protected")
        if protected:
            scheme = protected.get("data-s") or "none"
            chunks = json.loads(protected.get("data-c", "[]"))
            if not isinstance(chunks, list) or not chunks:
                raise ValueError(f"Empty protected Hako chapter at {chapter['url']}")
            key = (protected.get("data-k") or "").encode("utf-8")
            if scheme == "xor_shuffle" and not key:
                raise ValueError("Missing Hako chapter decoding key")
            if scheme not in ("xor_shuffle", "base64_reverse", "none"):
                raise ValueError(f"Unsupported Hako chapter encoding: {scheme}")

            parts = []
            for chunk in sorted(chunks, key=lambda item: int(item[:4])):
                encoded = chunk[4:]
                if scheme == "base64_reverse":
                    encoded = encoded[::-1]
                data = base64.b64decode(encoded)
                if scheme == "xor_shuffle":
                    data = bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))
                parts.append(data.decode("utf-8"))
            body = self.make_soup("<div>" + "".join(parts) + "</div>").select_one("div")
        else:
            body = content
            for hidden in body.tag.select(
                '[style*="display: none"], [style*="display:none"]'
            ):
                hidden.decompose()

        result = self.cleaner.extract_contents(body)
        if not result:
            raise ValueError(f"Empty Hako chapter body at {chapter['url']}")
        return result
