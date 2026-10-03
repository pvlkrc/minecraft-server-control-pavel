"""Minimal reader for Minecraft's NBT format (uncompressed bytes).

Only reading is supported. Used for level.dat (world spawn).
"""

import struct
from typing import Any


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("NBT data ends too early")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        return self.take(self.unpack(">H")).decode("utf-8", "replace")

    def payload(self, tag: int) -> Any:
        if tag == 1:
            return self.unpack(">b")
        if tag == 2:
            return self.unpack(">h")
        if tag == 3:
            return self.unpack(">i")
        if tag == 4:
            return self.unpack(">q")
        if tag == 5:
            return self.unpack(">f")
        if tag == 6:
            return self.unpack(">d")
        if tag == 7:
            return self.take(self.unpack(">i"))
        if tag == 8:
            return self.string()
        if tag == 9:
            item_tag = self.unpack(">b")
            return [self.payload(item_tag) for _ in range(self.unpack(">i"))]
        if tag == 10:
            result: dict[str, Any] = {}
            while (child := self.unpack(">b")) != 0:
                name = self.string()
                result[name] = self.payload(child)
            return result
        if tag == 11:
            n = self.unpack(">i")
            return list(struct.unpack(f">{n}i", self.take(4 * n)))
        if tag == 12:
            n = self.unpack(">i")
            return list(struct.unpack(f">{n}q", self.take(8 * n)))
        raise ValueError(f"unknown NBT tag {tag}")


def read_nbt(data: bytes) -> dict[str, Any]:
    reader = _Reader(data)
    tag = reader.unpack(">b")
    if tag != 10:
        raise ValueError("NBT root must be a compound")
    reader.string()  # root name, usually empty
    result: dict[str, Any] = reader.payload(tag)
    return result
