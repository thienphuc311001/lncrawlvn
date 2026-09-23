"""Local, configurable handling of clearly marked author and platform notes."""

from __future__ import annotations

import os
import re

NOTE_START = re.compile(
    r"^\s*(?:作者(?:有话说|的话|注|按|后记)\s*[:：]?|"
    r"P\.?S\.?\s*[:：]|PS\s*[:：]|"
    r"求(?:月票|推荐票|订阅|打赏|收藏)\b|"
    r"(?:平台|网站|站内)(?:提示|公告)\s*[:：])",
    re.I,
)
POLICIES = {"preserve", "remove", "separate"}


def author_note_policy():
    policy = os.getenv("TRANSLATION_AUTHOR_NOTE_POLICY", "preserve").strip().lower()
    if policy not in POLICIES:
        raise ValueError("TRANSLATION_AUTHOR_NOTE_POLICY must be preserve, remove, or separate")
    return policy


def is_author_note(raw):
    return bool(NOTE_START.match(raw))
