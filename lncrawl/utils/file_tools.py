import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from slugify import slugify


@contextmanager
def atomic_write(path: Union[str, Path], mode: str = "wb") -> Iterator:
    """Atomically write to `path` via a sibling temp file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, mode) as f:
            yield f
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def format_size(num_bytes: int, decimals: int = 1, suffix: str = "B") -> str:
    units = ["", "K", "M", "G", "T", "P", "E", "Z"]
    size = float(num_bytes)
    for unit in units:
        if abs(size) < 1024.0:
            return f"{size:.{decimals}f} {unit}{suffix}"
        size /= 1024.0
    return f"{size:.{decimals}f} Y{suffix}"


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    name = (
        slugify(
            name,
            max_length=255,
            separator=" ",
            regex_pattern=r'[<>:"/\\|?*#%&+;=@^`{}!\x00-\x1F\x7F]',
        )
        .strip(" .")
        .strip("-")
        or "untitled"
    )
    if name.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name or "untitled"
