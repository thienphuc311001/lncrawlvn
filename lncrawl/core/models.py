from typing import Any, Dict, List, Optional

__all__ = [
    "Novel",
    "Volume",
    "Chapter",
    "SearchResult",
]


class _ModelBox(dict):
    """A ``dict`` that also exposes its keys as attributes.

    All model objects subclass this so crawlers can read and write fields with
    both ``chapter["url"]`` and ``chapter.url``. Unknown attributes raise
    ``AttributeError``; unknown keys raise ``KeyError``.
    """

    def __getattr__(self, key: str) -> Any:
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            raise AttributeError(f"{type(self).__name__} has no attribute {key!r}")

    def __setattr__(self, key: str, value: Any) -> None:
        dict.__setitem__(self, key, value)

    def __delattr__(self, key: str) -> None:
        try:
            dict.__delitem__(self, key)
        except KeyError:
            raise AttributeError(key)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


class SearchResult(_ModelBox):
    def __init__(
        self,
        title: str,
        url: str,
        info: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.title = str(title)
        self.url = str(url)
        self.info = str(info)
        self.update(**kwargs)


class Chapter(_ModelBox):
    def __init__(
        self,
        id: int,
        url: str = "",
        title: str = "",
        volume: Optional[int] = None,
        body: Optional[str] = None,
        images: Optional[Dict[str, str]] = None,
        success: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.id = id
        self.url = url
        self.title = title
        self.volume = volume
        self.body = body
        self.images = images or dict()
        self.success = success
        self.update(**kwargs)


class Volume(_ModelBox):
    def __init__(
        self,
        id: int,
        title: str = "",
        chapters: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.id = id
        self.title = title
        self.chapters = chapters
        self.update(**kwargs)


class Novel(_ModelBox):
    def __init__(
        self,
        url: str,
        title: str = "",
        cover_url: str = "",
        volumes: Optional[List[Volume]] = None,
        chapters: Optional[List[Chapter]] = None,
        author: str = "",
        synopsis: str = "",
        tags: Optional[List[str]] = None,
        language: Optional[str] = None,
        is_manga: Optional[bool] = None,
        is_mtl: Optional[bool] = None,
        is_rtl: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.url = url
        self.title = title
        self.cover_url = cover_url
        self.author = author
        self.language = language
        self.is_manga = is_manga
        self.is_mtl = is_mtl
        self.is_rtl = is_rtl
        self.tags = tags or []
        self.synopsis = synopsis
        self.volumes = volumes or []
        self.chapters = chapters or []
        self.update(**kwargs)

    def add_volume(
        self,
        id: Optional[int] = None,
        title: str = "",
        **kwargs: Any,
    ) -> Volume:
        if id is None:
            id = len(self.volumes) + 1
        volume = Volume(id=id, title=title, **kwargs)
        self.volumes.append(volume)
        return volume

    def add_chapter(
        self,
        id: Optional[int] = None,
        url: str = "",
        title: str = "",
        volume: Optional[int] = None,
        **kwargs: Any,
    ) -> Chapter:
        if id is None:
            id = len(self.chapters) + 1
        chapter = Chapter(id=id, url=url, title=title, volume=volume, **kwargs)
        self.chapters.append(chapter)
        return chapter
