# -*- coding: utf-8 -*-
"""报文编解码 + 粘包/分包状态机解析。

帧格式（大端）::

    +--------+--------+---------------+--------+
    | magic  | length | payload(JSON) | crc32  |
    | 4 字节 | 4 字节 |  length 字节  | 4 字节 |
    +--------+--------+---------------+--------+

* magic  : b"MTMR"（0x4D 0x54 0x4D 0x52）
* length : payload 的字节数
* crc32  : payload 的 CRC32（IEEE，与 zlib.crc32 一致）

TCP 是字节流，一次 readyRead 不代表一整帧，必须用 FrameParser 按字节喂入。
"""
import json
import struct
import time
import zlib

from .const import MAX_FRAME_SIZE, PROTO_MAGIC

_HEADER = struct.Struct(">4sI")
_LEN = struct.Struct(">I")
_CRC = struct.Struct(">I")
_HEADER_LEN = _HEADER.size          # 8
_CRC_LEN = _CRC.size                # 4


def now_ms():
    """当前 Unix 时间戳（毫秒）。"""
    return int(time.time() * 1000)


def encode(msg_type, **payload):
    """把一条消息编码成完整帧（bytes）。"""
    obj = {"type": msg_type, "ts": now_ms()}
    obj.update(payload)
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_SIZE:
        raise ValueError("payload too large: %d" % len(body))
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return _HEADER.pack(PROTO_MAGIC, len(body)) + body + _CRC.pack(crc)


def decode(data):
    """解码"恰好一帧"的数据（用于 UDP 数据报）。失败抛 ValueError。"""
    if len(data) < _HEADER_LEN + _CRC_LEN:
        raise ValueError("frame too short")
    magic, length = _HEADER.unpack_from(data, 0)
    if magic != PROTO_MAGIC:
        raise ValueError("bad magic")
    if length <= 0 or length > MAX_FRAME_SIZE:
        raise ValueError("bad length")
    if len(data) < _HEADER_LEN + length + _CRC_LEN:
        raise ValueError("truncated frame")
    body = bytes(data[_HEADER_LEN:_HEADER_LEN + length])
    (crc,) = _CRC.unpack_from(data, _HEADER_LEN + length)
    if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
        raise ValueError("crc mismatch")
    obj = json.loads(body.decode("utf-8"))
    if not isinstance(obj, dict) or "type" not in obj:
        raise ValueError("bad payload")
    return obj


class FrameParser(object):
    """TCP 字节流 -> 消息字典 的状态机。

    * 粘包：一次喂入多帧，全部解析出来
    * 分包/半包：不足一帧的尾巴留在缓冲区，等下次喂入
    * 脏数据：CRC 错或长度非法时丢弃，并从流里重新同步到下一个魔数
    """

    __slots__ = ("_buf",)

    def __init__(self):
        self._buf = bytearray()

    def reset(self):
        del self._buf[:]

    def feed(self, data):
        """喂入新到的字节，返回本次解析出的消息列表。"""
        if data:
            self._buf.extend(data)
        messages = []

        while True:
            # ---- 1. 缓冲区里至少要有帧头 ----
            if len(self._buf) < _HEADER_LEN:
                break

            # ---- 2. 魔数不对 -> 重新同步 ----
            if bytes(self._buf[0:4]) != PROTO_MAGIC:
                idx = self._buf.find(PROTO_MAGIC, 1)
                if idx < 0:
                    # 找不到下一个魔数：只保留末尾 3 字节（可能是半个魔数）
                    if len(self._buf) > 3:
                        del self._buf[:-3]
                    break
                del self._buf[:idx]
                continue

            # ---- 3. 长度合法性 ----
            (length,) = _LEN.unpack_from(self._buf, 4)
            if length <= 0 or length > MAX_FRAME_SIZE:
                del self._buf[:4]        # 丢掉这个假帧头，继续往后找
                continue

            total = _HEADER_LEN + length + _CRC_LEN
            # ---- 4. 半包：等更多数据 ----
            if len(self._buf) < total:
                break

            body = bytes(self._buf[_HEADER_LEN:_HEADER_LEN + length])
            (crc,) = _CRC.unpack_from(self._buf, _HEADER_LEN + length)
            del self._buf[:total]

            # ---- 5. 校验 ----
            if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
                continue
            try:
                obj = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict) and "type" in obj:
                messages.append(obj)

        return messages

    def buffered(self):
        """当前缓冲区里滞留的字节数（排障用）。"""
        return len(self._buf)
