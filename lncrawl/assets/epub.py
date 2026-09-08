"""XHTML templates for the EPUB binder.

The cover page template mirrors upstream ``assets/epub/cover.xhtml``. It is
inlined as a constant so no package-data files are needed at install time.
``EpubCoverHtml.get_content()`` reads it through ``book.get_template("cover")``;
without a registered template the cover page body is empty and ebooklib's
``epub3_pages`` scan crashes with ``Document is empty``.
"""

from functools import lru_cache

_COVER_XHTML = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html
  xmlns="http://www.w3.org/1999/xhtml"
  xmlns:epub="http://www.idpf.org/2007/ops"
  lang="en"
  xml:lang="en"
>
  <head>
    <link href="style.css" rel="stylesheet" type="text/css" />
  </head>
  <body>
    <img id="cover" src="" alt="" />
  </body>
</html>
"""


@lru_cache
def epub_cover_xhtml() -> bytes:
    return _COVER_XHTML.encode("utf-8")
