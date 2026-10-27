from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FileEntry:
    path: str
    relative_path: str
    name: str
    size: int
    modified: float
    created: float | None
    created_source: str
    kind: str
    suffix: str
    mtime_ns: int
    device: int
    inode: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    root: str
    files: list[FileEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)


@dataclass
class MatchGroup:
    kind: str
    paths: list[str]
    reason: str
    score: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)
