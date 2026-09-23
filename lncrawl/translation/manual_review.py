"""Narrow, auditable human decisions for failed translation paragraphs."""

from .dictionary import relevant_entries
from .store import digest


def review_path(chapter, identifier):
    return f"manual-reviews/{chapter}/{identifier}.json"


def dictionary_fingerprint(dictionary, raw):
    return digest(relevant_entries(dictionary, raw))


def review_fingerprint(raw, text, dictionary, findings):
    return digest({
        "raw": raw,
        "text": text,
        "dictionary": relevant_entries(dictionary, raw),
        "findings": findings,
    })


def accepted_review(store, chapter, identifier, raw, text, dictionary):
    review = store.read(review_path(chapter, identifier))
    if not isinstance(review, dict) or review.get("decision") != "accept":
        return None
    if (review.get("raw_hash") != digest(raw)
            or review.get("text_hash") != digest(text)
            or review.get("dictionary_hash") != dictionary_fingerprint(dictionary, raw)):
        return None
    return review


def accepted_review_ids(store, chapter, result, dictionary):
    text_by_id = {segment.id: segment.text for segment in result.segments}
    return [identifier for identifier, raw in chapter.paragraph_items
            if identifier in text_by_id and accepted_review(
                store, chapter.number, identifier, raw, text_by_id[identifier], dictionary)]
