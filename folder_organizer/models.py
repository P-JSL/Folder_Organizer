from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
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
        # Inventory fields are immutable scalars. dataclasses.asdict recursively
        # visits/deepcopies every value; a flat copy keeps large exports linear
        # with a much smaller constant without sharing any mutable container.
        return {
            "path": self.path, "relative_path": self.relative_path,
            "name": self.name, "size": self.size, "modified": self.modified,
            "created": self.created, "created_source": self.created_source,
            "kind": self.kind, "suffix": self.suffix, "mtime_ns": self.mtime_ns,
            "device": self.device, "inode": self.inode,
        }


@dataclass
class ScanResult:
    root: str
    files: list[FileEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    filtered_files: int = 0
    excluded_directories: int = 0
    scan_options: dict = field(default_factory=dict)
    scan_metrics: dict = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)


@dataclass
class MatchGroup:
    kind: str
    paths: list[str]
    reason: str
    score: float = 1.0
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
