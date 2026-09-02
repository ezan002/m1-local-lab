from __future__ import annotations

import argparse
import json
import struct
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SOURCE_VERSION = "a78a57cdcdf74a080b1bec0f56f85228d86acbac"
SOURCE_URL = (
    "https://huggingface.co/datasets/TraceElephant/TraceElephant/resolve/"
    f"{SOURCE_VERSION}/data.zip?download=true"
)
EXPECTED_ARCHIVE_SIZE = 596_610_649
MAX_RANGE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
USER_AGENT = "m1-local-lab-external-dataset-smoke/1"


@dataclass(frozen=True)
class ZipMember:
    name: str
    compression: int
    crc32: int
    compressed_size: int
    size: int
    local_header_offset: int


class BoundedRemoteZip:
    def __init__(self, url: str, archive_size: int) -> None:
        if archive_size != EXPECTED_ARCHIVE_SIZE:
            raise ValueError("unexpected TraceElephant archive size")
        self.url = url
        self.archive_size = archive_size
        self.downloaded_bytes = 0
        self.members: dict[str, ZipMember] = {}

    def _range(self, start: int, end: int) -> bytes:
        if start < 0 or end < start or end >= self.archive_size:
            raise ValueError("invalid HTTP byte range")
        requested = end - start + 1
        if requested > MAX_RANGE_BYTES:
            raise ValueError("single range exceeds safety limit")
        if self.downloaded_bytes + requested > MAX_TOTAL_BYTES:
            raise ValueError("total range download exceeds safety limit")
        request = urllib.request.Request(
            self.url,
            headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise RuntimeError(f"server did not honor Range request: HTTP {response.status}")
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {start}-{end}/"):
                raise RuntimeError(f"unexpected Content-Range: {content_range}")
            data = response.read(requested + 1)
        if len(data) != requested:
            raise RuntimeError(f"range length mismatch: expected {requested}, got {len(data)}")
        self.downloaded_bytes += len(data)
        return data

    def read_index(self) -> dict[str, ZipMember]:
        tail_size = min(self.archive_size, 65_557)
        tail = self._range(self.archive_size - tail_size, self.archive_size - 1)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0:
            raise ValueError("ZIP end-of-central-directory record not found")
        fields = struct.unpack_from("<4s4H2LH", tail, position)
        entries = fields[4]
        central_size = fields[5]
        central_offset = fields[6]
        if entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
            raise ValueError("ZIP64 archive is not supported by this bounded smoke reader")
        central = self._range(central_offset, central_offset + central_size - 1)
        cursor = 0
        members: dict[str, ZipMember] = {}
        while cursor < len(central):
            if central[cursor : cursor + 4] != b"PK\x01\x02":
                raise ValueError(f"invalid central directory signature at {cursor}")
            fields = struct.unpack_from("<4s6H3I5H2I", central, cursor)
            compression = fields[4]
            crc32 = fields[7]
            compressed_size = fields[8]
            size = fields[9]
            name_length, extra_length, comment_length = fields[10:13]
            local_offset = fields[16]
            name_start = cursor + 46
            raw_name = central[name_start : name_start + name_length]
            name = raw_name.decode("utf-8")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError(f"unsafe member path: {name}")
            if name in members:
                raise ValueError(f"duplicate ZIP member: {name}")
            members[name] = ZipMember(
                name=name,
                compression=compression,
                crc32=crc32,
                compressed_size=compressed_size,
                size=size,
                local_header_offset=local_offset,
            )
            cursor = name_start + name_length + extra_length + comment_length
        if len(members) != entries:
            raise ValueError(f"central directory count mismatch: expected {entries}, got {len(members)}")
        self.members = members
        return members

    def extract(self, name: str, target_root: Path) -> Path:
        member = self.members.get(name)
        if member is None:
            raise KeyError(f"ZIP member not found: {name}")
        if not name.lower().endswith(".json"):
            raise ValueError("only JSON smoke samples may be extracted")
        if member.size > MAX_MEMBER_BYTES or member.compressed_size > MAX_MEMBER_BYTES:
            raise ValueError("member exceeds smoke sample size limit")
        header = self._range(member.local_header_offset, member.local_header_offset + 29)
        if header[:4] != b"PK\x03\x04":
            raise ValueError("invalid local file header")
        local_fields = struct.unpack("<4s5H3I2H", header)
        name_length, extra_length = local_fields[9:11]
        data_start = member.local_header_offset + 30 + name_length + extra_length
        compressed = self._range(data_start, data_start + member.compressed_size - 1)
        if member.compression == 0:
            payload = compressed
        elif member.compression == 8:
            payload = zlib.decompress(compressed, -zlib.MAX_WBITS)
        else:
            raise ValueError(f"unsupported ZIP compression method: {member.compression}")
        if len(payload) != member.size:
            raise ValueError("uncompressed member size mismatch")
        if zlib.crc32(payload) & 0xFFFFFFFF != member.crc32:
            raise ValueError("member CRC32 mismatch")
        json.loads(payload.decode("utf-8-sig"))
        target_root = target_root.resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        target = (target_root / PurePosixPath(name).name).resolve()
        if target.parent != target_root:
            raise ValueError("resolved output escaped target directory")
        target.write_bytes(payload)
        return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded TraceElephant ZIP metadata/sample fetcher")
    parser.add_argument("--list-pattern", default="magentic", help="Case-insensitive member-name filter")
    parser.add_argument(
        "--max-list",
        type=int,
        default=100,
        help="Maximum number of matching JSON members to print",
    )
    parser.add_argument("--extract", action="append", default=[], help="Exact JSON member path; repeatable")
    parser.add_argument(
        "--target", type=Path, default=Path("data/external/traceelephant"),
        help="Ignored target directory for extracted samples",
    )
    args = parser.parse_args()
    if args.max_list < 0:
        raise SystemExit("--max-list must be non-negative")
    remote = BoundedRemoteZip(SOURCE_URL, EXPECTED_ARCHIVE_SIZE)
    members = remote.read_index()
    all_matches = [
        {"name": item.name, "compressed_size": item.compressed_size, "size": item.size}
        for item in members.values()
        if item.name.lower().endswith(".json")
        and args.list_pattern.lower() in item.name.lower()
    ]
    matches = all_matches[: args.max_list]
    extracted = [
        {"member": name, "path": str(remote.extract(name, args.target))}
        for name in args.extract
    ]
    print(json.dumps({
        "source_version": SOURCE_VERSION,
        "archive_size": EXPECTED_ARCHIVE_SIZE,
        "member_count": len(members),
        "matching_members": matches,
        "matching_member_count": len(all_matches),
        "listing_truncated": len(all_matches) > len(matches),
        "extracted": extracted,
        "downloaded_bytes": remote.downloaded_bytes,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
