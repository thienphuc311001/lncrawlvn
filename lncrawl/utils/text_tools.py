import hashlib
import html
import re
import unicodedata
import uuid

_RE_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).casefold()


def format_title(text: str) -> str:
    name = html.unescape(str(text or ""))
    name = unicodedata.normalize("NFKC", name)
    return _RE_SPACES.sub(" ", name).strip().title()


def generate_md5(*texts) -> str:
    md5 = hashlib.md5()
    for text in texts:
        md5.update(str(text or "").encode())
    return md5.hexdigest()


def generate_uuid() -> str:
    return str(uuid.uuid4())
