# -*- coding: utf-8 -*-
"""网卡枚举：拿本机 IPv4 与各网段的广播地址。

为什么不用 255.255.255.255？
    全局广播在部分 Windows 配置 / 多网卡机器上会被路由到错误的网卡，
    或者被防火墙直接丢弃。按每个网卡各自的定向广播地址（directed
    broadcast，例如 192.168.1.255）分别发一份，命中率高得多。

掩码从 Windows IP Helper API（GetIpAddrTable）读取，因此 /23、/16
之类的非 /24 网段也算得对（本机实测是 192.168.0.45/23）。
"""
import socket
import struct
import sys

_IS_WINDOWS = sys.platform.startswith("win")


# ------------------------------------------------------------------ Windows
if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _MIB_IPADDRROW(ctypes.Structure):
        _fields_ = [
            ("dwAddr", wintypes.DWORD),
            ("dwIndex", wintypes.DWORD),
            ("dwMask", wintypes.DWORD),
            ("dwBCastAddr", wintypes.DWORD),
            ("dwReasmSize", wintypes.DWORD),
            ("unused1", ctypes.c_ushort),
            ("unused2", ctypes.c_ushort),
        ]

    class _MIB_IPADDRTABLE_HDR(ctypes.Structure):
        _fields_ = [("dwNumEntries", wintypes.DWORD)]

    def _int2ip(value):
        return socket.inet_ntoa(struct.pack("<I", value & 0xFFFFFFFF))

    def _windows_interfaces():
        """返回 [(ip, mask), ...]。失败时返回空列表。"""
        try:
            iphlpapi = ctypes.windll.iphlpapi
            size = wintypes.ULONG(0)
            iphlpapi.GetIpAddrTable(None, ctypes.byref(size), False)
            if size.value <= 0:
                return []
            buf = ctypes.create_string_buffer(size.value)
            ret = iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), False)
            if ret != 0:
                return []
            count = _MIB_IPADDRTABLE_HDR.from_buffer(buf).dwNumEntries
            if count <= 0:
                return []
            rows = (_MIB_IPADDRROW * count).from_buffer(
                buf, ctypes.sizeof(_MIB_IPADDRTABLE_HDR)
            )
            return [(_int2ip(r.dwAddr), _int2ip(r.dwMask)) for r in rows]
        except Exception:
            return []


def _fallback_interfaces():
    """非 Windows 或 API 失败时的兜底：用 hostname 反查本机地址，掩码按 /24 猜。"""
    result = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return result
    for info in infos:
        ip = info[4][0]
        if ip and not ip.startswith("127."):
            result.append((ip, "255.255.255.0"))
    return result


def get_interfaces():
    """返回本机所有 IPv4 接口 [(ip, mask), ...]，已过滤回环与 169.254 链路本地。"""
    ifaces = _windows_interfaces() if _IS_WINDOWS else []
    if not ifaces:
        ifaces = _fallback_interfaces()
    out = []
    seen = set()
    for ip, mask in ifaces:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip in seen:
            continue
        seen.add(ip)
        out.append((ip, mask))
    return out


def get_broadcast_addrs():
    """返回所有需要尝试的广播地址列表（已去重，全局广播放最后兜底）。"""
    addrs = []
    for ip, mask in get_interfaces():
        try:
            ip_i = struct.unpack("<I", socket.inet_aton(ip))[0]
            mask_i = struct.unpack("<I", socket.inet_aton(mask))[0]
            if mask_i == 0 or mask_i == 0xFFFFFFFF:
                continue
            bcast = socket.inet_ntoa(struct.pack("<I", ip_i | (~mask_i & 0xFFFFFFFF)))
            if bcast not in addrs:
                addrs.append(bcast)
        except OSError:
            continue
    if "255.255.255.255" not in addrs:
        addrs.append("255.255.255.255")
    return addrs


def get_primary_ip():
    """返回最可能用来和主控通信的本机 IP。"""
    for ip, _mask in get_interfaces():
        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
            return ip
    ifaces = get_interfaces()
    return ifaces[0][0] if ifaces else "127.0.0.1"


def get_mac():
    """返回本机某个物理网卡 MAC（无依赖实现）。"""
    import uuid
    node = uuid.getnode()
    return ":".join("%02X" % ((node >> (8 * i)) & 0xFF) for i in range(6))


if __name__ == "__main__":  # 便于单独自测
    print("interfaces :", get_interfaces())
    print("broadcasts :", get_broadcast_addrs())
    print("primary ip :", get_primary_ip())
    print("mac        :", get_mac())
