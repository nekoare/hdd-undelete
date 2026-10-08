#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdd-undelete : 削除したファイルの復元ツール (Python 標準ライブラリのみ)

  1. NTFS モード (scan / recover)
       MFT に残っている「削除済み」レコードを読み、ファイル名・フォルダ構成つきで復元する。
  2. カービングモード (carve)
       ディスクを先頭から読み、ファイルの署名 (JPEG/PNG/PDF/ZIP...) を探して取り出す。
       フォーマット後や FAT/exFAT、MFT レコードが消えている場合用。名前は復元できない。

復元元のドライブには一切書き込まない (読み取り専用で開く)。
"""
import argparse
import csv
import fnmatch
import os
import re
import struct
import sys
import time

VERSION = "1.0.0"
SECTOR = 512
IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------
# 低レベル読み取り
# --------------------------------------------------------------------------
class Source:
    """ドライブ (\\\\.\\D:) またはイメージファイルを読み取り専用で開く。"""

    def __init__(self, spec):
        self.spec = spec
        self.path = normalize_source(spec)
        self.is_device = self.path.startswith("\\\\.\\") or self.path.startswith("/dev/")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            self.fd = os.open(self.path, flags)
        except PermissionError:
            die("ドライブを開けません (アクセス拒否)。管理者として実行してください: %s" % self.path)
        except OSError as e:
            die("開けません: %s (%s)" % (self.path, e))
        self.errors = 0
        self.size = self._detect_size()

    def _detect_size(self):
        if not self.is_device:
            return os.fstat(self.fd).st_size
        if IS_WIN:
            try:
                import ctypes
                import msvcrt
                h = msvcrt.get_osfhandle(self.fd)
                out = ctypes.c_longlong(0)
                ret = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.DeviceIoControl(
                    ctypes.c_void_p(h), 0x7405C, None, 0,
                    ctypes.byref(out), 8, ctypes.byref(ret), None)
                if ok and out.value > 0:
                    return out.value
            except Exception:
                pass
            return 0  # 不明
        try:
            return os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            return 0

    def read(self, offset, size):
        """任意位置から読み取る。生デバイス向けにセクタ境界へそろえる。
        読めない領域はゼロで埋める (不良セクタ対策)。"""
        if size <= 0:
            return b""
        start = offset - (offset % SECTOR)
        end = offset + size
        end += (-end) % SECTOR
        data = self._read_aligned(start, end - start)
        rel = offset - start
        return data[rel:rel + size]

    def _read_aligned(self, start, length):
        try:
            os.lseek(self.fd, start, os.SEEK_SET)
            buf = os.read(self.fd, length)
            while len(buf) < length:
                more = os.read(self.fd, length - len(buf))
                if not more:
                    break
                buf += more
            return buf
        except OSError:
            if length <= 4096:
                self.errors += 1
                return b"\0" * length
            # 小さく分けて読める部分だけ救う
            out = []
            step = max(4096, (length // 16) - ((length // 16) % SECTOR))
            pos = 0
            while pos < length:
                n = min(step, length - pos)
                out.append(self._read_aligned(start + pos, n).ljust(n, b"\0"))
                pos += n
            return b"".join(out)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def normalize_source(spec):
    s = spec.strip().strip('"')
    if IS_WIN and re.fullmatch(r"[A-Za-z]:?\\?", s):
        return "\\\\.\\%s:" % s[0].upper()
    return s


def die(msg):
    print("エラー: " + msg, file=sys.stderr)
    sys.exit(1)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_output_dir(src, outdir):
    """復元先が復元元と同じドライブだと、復元データが消えたデータを上書きする。必ず止める。"""
    if not IS_WIN:
        return
    m = re.fullmatch(r"\\\\\.\\([A-Z]):", src.path)
    if not m:
        return
    drive = os.path.splitdrive(os.path.abspath(outdir))[0].upper().rstrip(":")
    if drive == m.group(1):
        die("復元先 (%s) が復元元と同じドライブです。別のドライブを指定してください。\n"
            "        同じドライブに書き込むと、消えたデータを上書きして復元できなくなります。" % outdir)


# --------------------------------------------------------------------------
# NTFS
# --------------------------------------------------------------------------
def parse_runs(buf):
    """データラン -> [(lcn または None(スパース), クラスタ数)]"""
    runs = []
    pos = 0
    lcn = 0
    n = len(buf)
    while pos < n:
        head = buf[pos]
        if head == 0:
            break
        lsz = head & 0x0F
        osz = head >> 4
        pos += 1
        if lsz == 0 or lsz > 8 or osz > 8 or pos + lsz + osz > n:
            break
        length = int.from_bytes(buf[pos:pos + lsz], "little")
        pos += lsz
        if osz == 0:
            runs.append((None, length))
        else:
            lcn += int.from_bytes(buf[pos:pos + osz], "little", signed=True)
            pos += osz
            if lcn < 0:
                break
            runs.append((lcn, length))
    return runs


def filetime_to_unix(ft):
    if ft <= 0:
        return 0
    t = (ft - 116444736000000000) / 1e7
    return t if 0 < t < 4102444800 else 0


class Entry:
    __slots__ = ("recno", "name", "parent", "size", "mtime", "resident",
                 "runs", "flags", "is_dir", "path", "health")

    def __init__(self):
        self.recno = 0
        self.name = None
        self.parent = 5
        self.size = 0
        self.mtime = 0
        self.resident = None
        self.runs = None
        self.flags = 0
        self.is_dir = False
        self.path = ""
        self.health = -1.0


class NTFS:
    def __init__(self, src):
        self.src = src
        boot = src.read(0, 512)
        if len(boot) < 512 or boot[3:11] != b"NTFS    ":
            raise ValueError("NTFS ではありません")
        self.bps = struct.unpack_from("<H", boot, 0x0B)[0]
        spc = boot[0x0D]
        if spc > 0x80:
            spc = 1 << (256 - spc)
        self.csize = self.bps * spc
        self.total_sectors = struct.unpack_from("<Q", boot, 0x28)[0]
        self.mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
        c = struct.unpack_from("<b", boot, 0x40)[0]
        self.rec_size = (1 << -c) if c < 0 else c * self.csize
        if self.bps not in (512, 1024, 2048, 4096) or self.rec_size not in (1024, 4096) \
                or self.csize == 0:
            raise ValueError("NTFS ブートセクタが壊れています")
        self.volume_size = self.total_sectors * self.bps
        self.bitmap = None
        rec0 = self.fixup(src.read(self.mft_lcn * self.csize, self.rec_size))
        if rec0 is None:
            raise ValueError("$MFT を読めません")
        info = self.parse_record(rec0, 0)
        if not info or not info["data"] or info["data"][0][1] is None:
            raise ValueError("$MFT のデータランを読めません")
        self.mft_runs = info["data"][0][1]
        self.mft_size = info["data"][0][2]
        self.mft_records = self.mft_size // self.rec_size

    # ---- レコード -------------------------------------------------------
    def fixup(self, rec):
        if len(rec) < self.rec_size or rec[:4] != b"FILE":
            return None
        usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
        if usa_cnt < 2 or usa_off + usa_cnt * 2 > len(rec):
            return None
        rec = bytearray(rec)
        usn = rec[usa_off:usa_off + 2]
        for i in range(1, usa_cnt):
            end = i * 512
            if end > len(rec):
                break
            if rec[end - 2:end] != usn:
                return None  # 破損 (途中まで上書きされたレコード)
            rec[end - 2:end] = rec[usa_off + i * 2:usa_off + i * 2 + 2]
        return rec

    def parse_record(self, rec, recno):
        flags = struct.unpack_from("<H", rec, 0x16)[0]
        attr_off = struct.unpack_from("<H", rec, 0x14)[0]
        base = struct.unpack_from("<Q", rec, 0x20)[0] & 0xFFFFFFFFFFFF
        info = {"in_use": bool(flags & 1), "is_dir": bool(flags & 2), "base": base,
                "name": None, "parent": 5, "mtime": 0, "fn_size": 0,
                "data": []}  # data: (start_vcn, runs|None, size, resident_bytes|None, flags)
        best_ns = -1
        pos = attr_off
        n = len(rec)
        while pos + 16 <= n:
            atype, alen = struct.unpack_from("<II", rec, pos)
            if atype == 0xFFFFFFFF or alen < 16 or pos + alen > n:
                break
            nonres = rec[pos + 8]
            name_len = rec[pos + 9]
            aflags = struct.unpack_from("<H", rec, pos + 12)[0]
            if atype == 0x10 and not nonres:
                clen, coff = struct.unpack_from("<IH", rec, pos + 16)
                if clen >= 16:
                    info["mtime"] = filetime_to_unix(struct.unpack_from("<Q", rec, pos + coff + 8)[0])
            elif atype == 0x30 and not nonres:
                clen, coff = struct.unpack_from("<IH", rec, pos + 16)
                c = pos + coff
                if clen >= 66 and c + 66 <= n:
                    nlen = rec[c + 64]
                    ns = rec[c + 65]
                    # 8.3 短縮名 (ns=2) より長い名前を優先
                    rank = 0 if ns == 2 else 1
                    if rank > best_ns and c + 66 + nlen * 2 <= n:
                        best_ns = rank
                        info["name"] = bytes(rec[c + 66:c + 66 + nlen * 2]).decode("utf-16-le", "replace")
                        info["parent"] = struct.unpack_from("<Q", rec, c)[0] & 0xFFFFFFFFFFFF
                        info["fn_size"] = struct.unpack_from("<Q", rec, c + 48)[0]
                        if not info["mtime"]:
                            info["mtime"] = filetime_to_unix(struct.unpack_from("<Q", rec, c + 16)[0])
            elif atype == 0x80 and name_len == 0:  # 無名 $DATA = ファイル本体
                if nonres:
                    if pos + 64 <= n:
                        svcn = struct.unpack_from("<Q", rec, pos + 16)[0]
                        roff = struct.unpack_from("<H", rec, pos + 32)[0]
                        real = struct.unpack_from("<Q", rec, pos + 48)[0]
                        runs = parse_runs(bytes(rec[pos + roff:pos + alen]))
                        info["data"].append((svcn, runs, real, None, aflags))
                else:
                    clen, coff = struct.unpack_from("<IH", rec, pos + 16)
                    if pos + coff + clen <= n:
                        info["data"].append((0, None, clen, bytes(rec[pos + coff:pos + coff + clen]), aflags))
            pos += alen
        return info

    def iter_records(self, progress=None):
        """MFT 全レコードを (番号, 修正済みバイト列) で順に返す。"""
        recno = 0
        rs = self.rec_size
        chunk = 4 * 1024 * 1024
        chunk -= chunk % rs
        leftover = b""
        for lcn, count in self.mft_runs:
            if lcn is None:
                recno += (count * self.csize) // rs
                continue
            run_bytes = count * self.csize
            done = 0
            while done < run_bytes and recno < self.mft_records:
                n = min(chunk, run_bytes - done)
                buf = leftover + self.src.read(lcn * self.csize + done, n)
                done += n
                usable = len(buf) - (len(buf) % rs)
                for off in range(0, usable, rs):
                    if recno >= self.mft_records:
                        break
                    if buf[off:off + 4] == b"FILE":
                        rec = self.fixup(buf[off:off + rs])
                        if rec is not None:
                            yield recno, rec
                    recno += 1
                leftover = buf[usable:]
                if progress:
                    progress(recno, self.mft_records)

    # ---- ビットマップ ---------------------------------------------------
    def load_bitmap(self):
        rec = self.read_record(6)
        if rec is None:
            return
        info = self.parse_record(rec, 6)
        if not info["data"]:
            return
        _, runs, size, res, _ = info["data"][0]
        if res is not None:
            self.bitmap = res
            return
        if size > 2 * 1024 ** 3:
            return
        parts = []
        for lcn, count in runs:
            if lcn is None:
                parts.append(b"\0" * (count * self.csize))
            else:
                parts.append(self.src.read(lcn * self.csize, count * self.csize))
        self.bitmap = b"".join(parts)[:size]

    def read_record(self, recno):
        off = recno * self.rec_size
        pos = 0
        for lcn, count in self.mft_runs:
            rb = count * self.csize
            if off < pos + rb:
                if lcn is None:
                    return None
                return self.fixup(self.src.read(lcn * self.csize + (off - pos), self.rec_size))
            pos += rb
        return None

    def cluster_in_use(self, lcn):
        bm = self.bitmap
        if bm is None:
            return False
        i = lcn >> 3
        if i >= len(bm):
            return False
        return bool(bm[i] & (1 << (lcn & 7)))

    def health(self, runs):
        """ランのうち「現在は空き」のクラスタの割合 (0..1)。
        空き = 他のファイルに再利用されていない = 中身が残っている可能性が高い。"""
        if self.bitmap is None:
            return -1.0
        total = sum(c for l, c in runs if l is not None)
        if total == 0:
            return 1.0
        step = max(1, total // 512)
        seen = free = 0
        idx = 0
        for lcn, count in runs:
            if lcn is None:
                continue
            k = (-idx) % step
            while k < count:
                seen += 1
                if not self.cluster_in_use(lcn + k):
                    free += 1
                k += step
            idx += count
        return free / seen if seen else 1.0

    # ---- スキャン -------------------------------------------------------
    def scan_deleted(self, progress=None):
        dirs = {}       # recno -> (name, parent)  ※使用中/削除済みどちらのフォルダも
        entries = {}    # 削除済みファイル
        ext_parts = {}  # 拡張レコードにある $DATA の続き: base -> [(svcn, runs, size)]
        for recno, rec in self.iter_records(progress):
            info = self.parse_record(rec, recno)
            if info["base"]:
                if not info["in_use"]:
                    for svcn, runs, size, res, fl in info["data"]:
                        if runs is not None:
                            ext_parts.setdefault(info["base"], []).append((svcn, runs, size, fl))
                continue
            if info["is_dir"]:
                if info["name"] is not None:
                    dirs[recno] = (info["name"], info["parent"])
                continue
            if info["in_use"] or info["name"] is None:
                continue
            e = Entry()
            e.recno = recno
            e.name = info["name"]
            e.parent = info["parent"]
            e.mtime = info["mtime"]
            parts = []
            for svcn, runs, size, res, fl in info["data"]:
                if res is not None:
                    e.resident = res
                    e.size = size
                else:
                    parts.append((svcn, runs, size, fl))
            if parts:
                e.runs = parts  # あとで結合
            entries[recno] = e
        for base, parts in ext_parts.items():
            e = entries.get(base)
            if e is not None and e.resident is None:
                e.runs = (e.runs or []) + parts
        self.load_bitmap()
        out = []
        for e in entries.values():
            if e.resident is None:
                if not e.runs:
                    continue
                parts = sorted(e.runs, key=lambda p: p[0])
                runs = []
                vcn = 0
                complete = True
                for svcn, r, size, fl in parts:
                    if svcn != vcn:
                        complete = False
                        break
                    runs.extend(r)
                    vcn += sum(c for _, c in r)
                    e.flags |= fl
                    if svcn == 0:
                        e.size = size
                e.runs = runs
                if not runs or parts[0][0] != 0:
                    continue
                alloc = sum(c for _, c in runs) * self.csize
                if e.size > alloc:
                    if complete:
                        e.size = alloc
                    # 続きのランが見つからない場合は読める所まで
                    e.size = min(e.size, alloc)
                e.health = self.health(runs)
            else:
                e.health = 1.0
            e.path = self._path(e.parent, dirs)
            out.append(e)
        out.sort(key=lambda x: (x.path.lower(), x.name.lower()))
        return out

    @staticmethod
    def _path(parent, dirs):
        parts = []
        seen = set()
        cur = parent
        while cur != 5 and cur not in seen and len(parts) < 64:
            seen.add(cur)
            d = dirs.get(cur)
            if d is None:
                parts.append("_不明なフォルダ")
                break
            parts.append(d[0])
            cur = d[1]
        return "/".join(reversed(parts))

    def extract(self, e, dst):
        """Entry の中身を dst に書き出す。書いたバイト数を返す。"""
        with open(dst, "wb") as f:
            if e.resident is not None:
                f.write(e.resident)
                return len(e.resident)
            remain = e.size
            for lcn, count in e.runs:
                if remain <= 0:
                    break
                nbytes = min(count * self.csize, remain)
                if lcn is None:
                    f.seek(nbytes, os.SEEK_CUR)
                else:
                    done = 0
                    while done < nbytes:
                        n = min(8 * 1024 * 1024, nbytes - done)
                        buf = self.src.read(lcn * self.csize + done, n)
                        if len(buf) < n:
                            buf = buf.ljust(n, b"\0")
                        f.write(buf)
                        done += n
                remain -= nbytes
            f.truncate(e.size)
            return e.size


# --------------------------------------------------------------------------
# カービング (署名スキャン)
# --------------------------------------------------------------------------
_JPG_MARK = re.compile(b"\xff[^\x00\xff\xd0-\xd7]")


def size_jpg(src, off, limit):
    p = off + 2
    end = off + limit
    segs = 0
    while p < end:
        h = src.read(p, 4)
        if len(h) < 4 or h[0] != 0xFF:
            return 0
        m = h[1]
        if m == 0xD9:
            return (p + 2 - off) if segs >= 2 else 0
        if m == 0xFF:
            p += 1
            continue
        if m in (0x01,) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        if m < 0xC0:
            return 0
        seglen = (h[2] << 8) | h[3]
        if seglen < 2:
            return 0
        p += 2 + seglen
        segs += 1
        if m == 0xDA:  # スキャンデータ: 次の本物のマーカーまで飛ばす
            found = False
            while p < end:
                buf = src.read(p, 1024 * 1024)
                if len(buf) < 2:
                    return 0
                mm = _JPG_MARK.search(buf)
                if mm:
                    p += mm.start()
                    found = True
                    break
                p += len(buf) - 1
            if not found:
                return 0
    return 0


def size_png(src, off, limit):
    p = off + 8
    end = off + limit
    first = True
    while p < end:
        h = src.read(p, 8)
        if len(h) < 8:
            return 0
        ln = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if first and typ != b"IHDR":
            return 0
        first = False
        if ln > limit or not typ.isalpha():
            return 0
        p += 12 + ln
        if typ == b"IEND":
            return p - off
    return 0


def size_gif(src, off, limit):
    buf = src.read(off, min(limit, 32 * 1024 * 1024))
    n = len(buf)
    if n < 14 or buf[:6] not in (b"GIF87a", b"GIF89a"):
        return 0
    p = 13
    if buf[10] & 0x80:
        p += 3 * (2 << (buf[10] & 7))
    images = 0
    try:
        while p < n:
            b = buf[p]
            if b == 0x3B:
                return p + 1 if images else 0
            if b == 0x21:
                p += 2
            elif b == 0x2C:
                fl = buf[p + 9]
                p += 10
                if fl & 0x80:
                    p += 3 * (2 << (fl & 7))
                p += 1
                images += 1
            else:
                return 0
            while True:
                sz = buf[p]
                p += 1 + sz
                if sz == 0:
                    break
    except IndexError:
        return 0
    return 0


def _find(src, start, end, needle, chunk=4 * 1024 * 1024):
    """start..end の範囲で needle を順に探すジェネレータ (絶対オフセット)。"""
    p = start
    ov = len(needle) - 1
    while p < end:
        buf = src.read(p, min(chunk, end - p))
        if not buf:
            return
        i = buf.find(needle)
        while i >= 0:
            yield p + i
            i = buf.find(needle, i + 1)
        if len(buf) <= ov:
            return
        p += len(buf) - ov


def size_pdf(src, off, limit):
    last = 0
    for pos in _find(src, off, off + limit, b"%%EOF"):
        if last and pos - last > 2 * 1024 * 1024:
            break
        last = pos
    if not last:
        return 0
    tail = src.read(last + 5, 2)
    extra = 2 if tail[:2] == b"\r\n" else (1 if tail[:1] in (b"\n", b"\r") else 0)
    return last + 5 + extra - off


def size_zip(src, off, limit):
    for pos in _find(src, off + 30, off + limit, b"PK\x05\x06"):
        h = src.read(pos, 22)
        if len(h) < 22:
            return 0
        cd_size, cd_off, clen = struct.unpack_from("<IIH", h, 12)
        # 中央ディレクトリの位置が計算と合う EOCD だけを本物とみなす
        if cd_off + cd_size == pos - off:
            return pos + 22 + clen - off
    return 0


_MP4_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta",
              b"moof", b"mfra", b"sidx", b"styp", b"pnot", b"udta", b"pdin", b"meco"}


def size_mp4(src, off, limit):
    p = off
    seen = set()
    while p - off < limit:
        h = src.read(p, 16)
        if len(h) < 16:
            break
        sz = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if typ not in _MP4_BOXES:
            break
        if sz == 1:
            sz = struct.unpack(">Q", h[8:16])[0]
        if sz < 8:
            break
        seen.add(typ)
        p += sz
    if b"mdat" in seen and (b"moov" in seen or b"meta" in seen) and p - off <= limit:
        return p - off
    return 0


def size_riff(src, off, limit):
    h = src.read(off, 12)
    sz = struct.unpack_from("<I", h, 4)[0] + 8
    if h[8:12] not in (b"WAVE", b"AVI ", b"WEBP") or sz < 44 or sz > limit:
        return 0
    return sz


def ext_zip(src, off, size):
    head = src.read(off, min(size, 8192))
    tail = src.read(off + max(0, size - 65536), min(size, 65536))
    blob = head + tail
    if b"word/" in blob:
        return "docx"
    if b"xl/" in blob:
        return "xlsx"
    if b"ppt/" in blob:
        return "pptx"
    return "zip"


def ext_mp4(src, off, size):
    brand = src.read(off + 8, 4)
    if brand == b"qt  ":
        return "mov"
    if brand in (b"heic", b"heix", b"mif1", b"msf1"):
        return "heic"
    if brand in (b"avif", b"avis"):
        return "avif"
    if brand in (b"M4A ", b"M4B "):
        return "m4a"
    return "mp4"


def ext_riff(src, off, size):
    return {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp"}[src.read(off + 8, 4)]


MB = 1024 * 1024
# 名前: (判定関数, サイズ計算, 最大サイズ, 拡張子 or 拡張子判定関数, 最小サイズ)
CARVERS = {
    "jpg": (lambda h: h[:3] == b"\xff\xd8\xff", size_jpg, 100 * MB, "jpg", 512),
    "png": (lambda h: h[:8] == b"\x89PNG\r\n\x1a\n", size_png, 200 * MB, "png", 67),
    "gif": (lambda h: h[:6] in (b"GIF87a", b"GIF89a"), size_gif, 32 * MB, "gif", 32),
    "pdf": (lambda h: h[:5] == b"%PDF-", size_pdf, 512 * MB, "pdf", 64),
    "zip": (lambda h: h[:4] == b"PK\x03\x04", size_zip, 2048 * MB, ext_zip, 22),
    "mp4": (lambda h: h[4:8] == b"ftyp", size_mp4, 64 * 1024 * MB, ext_mp4, 64),
    "riff": (lambda h: h[:4] == b"RIFF", size_riff, 4095 * MB, ext_riff, 44),
}
CARVE_HELP = "jpg,png,gif,pdf,zip(docx/xlsx/pptx含む),mp4(mov/heic/m4a含む),riff(wav/avi/webp)"
_FIRST = frozenset((0xFF, 0x89, 0x47, 0x25, 0x50, 0x52))


def carve(src, outdir, types, ntfs=None, all_space=False, quiet=False):
    os.makedirs(outdir, exist_ok=True)
    carvers = [(n, CARVERS[n]) for n in types]
    total = src.size or (ntfs.volume_size if ntfs else 0)
    use_bitmap = ntfs is not None and ntfs.bitmap is not None and not all_space
    csize = ntfs.csize if ntfs else 0
    chunk = 8 * MB
    pos = 0
    skip_until = 0
    counts = {}
    found_bytes = 0
    t0 = time.time()
    last_print = 0
    while True:
        if total and pos >= total:
            break
        buf = src.read(pos, chunk)
        if not buf:
            break
        b0 = buf[0::SECTOR]
        b4 = buf[4::SECTOR]
        nb4 = len(b4)
        for i in range(len(b0)):
            if b0[i] not in _FIRST and not (i < nb4 and b4[i] == 0x66):
                continue
            off = pos + i * SECTOR
            if off < skip_until:
                continue
            if use_bitmap and ntfs.cluster_in_use(off // csize):
                continue  # 現在使用中の領域 = 消えていないファイル
            head = buf[i * SECTOR:i * SECTOR + 16]
            for name, (match, sizer, maxsize, ext, minsize) in carvers:
                if not match(head):
                    continue
                limit = maxsize
                if total:
                    limit = min(limit, total - off)
                try:
                    size = sizer(src, off, limit)
                except (struct.error, IndexError):
                    size = 0
                if size < minsize:
                    break
                e = ext if isinstance(ext, str) else ext(src, off, size)
                counts[e] = counts.get(e, 0) + 1
                sub = os.path.join(outdir, e)
                os.makedirs(sub, exist_ok=True)
                dst = os.path.join(sub, "%s_%06d_at%012X.%s" % (e, counts[e], off, e))
                copy_range(src, off, size, dst)
                found_bytes += size
                skip_until = off + size
                break
        got = len(buf)
        pos += got
        if got < chunk:
            break
        now = time.time()
        if not quiet and now - last_print > 1:
            last_print = now
            n = sum(counts.values())
            if total:
                sys.stdout.write("\r  %5.1f%%  %s / %s  発見 %d 件 (%s)   " % (
                    pos * 100.0 / total, human(pos), human(total), n, human(found_bytes)))
            else:
                sys.stdout.write("\r  %s 読み取り  発見 %d 件 (%s)   " % (human(pos), n, human(found_bytes)))
            sys.stdout.flush()
    if not quiet:
        sys.stdout.write("\r" + " " * 70 + "\r")
        print("完了 (%d 秒)。%d 件 / %s を %s に保存しました。" % (
            time.time() - t0, sum(counts.values()), human(found_bytes), outdir))
        for e in sorted(counts):
            print("   %-5s %d 件" % (e, counts[e]))
    return counts


def copy_range(src, off, size, dst):
    with open(dst, "wb") as f:
        done = 0
        while done < size:
            n = min(8 * MB, size - done)
            buf = src.read(off + done, n)
            if not buf:
                break
            f.write(buf)
            done += len(buf)


# --------------------------------------------------------------------------
# コマンド
# --------------------------------------------------------------------------
_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_name(s):
    s = _BAD.sub("_", s).rstrip(" .")
    return s or "_"


def health_label(e):
    if e.health < 0:
        return "不明"
    if e.flags & 0x4000:
        return "暗号化(EFS)"
    if e.flags & 0x0001:
        return "圧縮(非対応)"
    if e.health >= 0.999:
        return "良好"
    if e.health <= 0.001:
        return "上書き済み"
    return "一部上書き(%d%%残)" % int(e.health * 100)


def mft_progress(done, total):
    if total and (done % 20480 == 0 or done >= total):
        sys.stdout.write("\r  MFT を読み取り中... %5.1f%% (%d / %d レコード)" % (
            min(100.0, done * 100.0 / total), done, total))
        sys.stdout.flush()


def do_scan(src, quiet=False):
    try:
        fs = NTFS(src)
    except ValueError as e:
        die("%s。NTFS 以外のドライブやフォーマット済みの場合は carve を使ってください。" % e)
    entries = fs.scan_deleted(None if quiet else mft_progress)
    if not quiet:
        sys.stdout.write("\r" + " " * 70 + "\r")
    return fs, entries


def select(entries, args):
    out = entries
    pats = getattr(args, "name", None)
    if pats:
        pats = [p.lower() for p in pats]
        out = [e for e in out
               if any(fnmatch.fnmatch(e.name.lower(), p) or
                      fnmatch.fnmatch((e.path + "/" + e.name).lower(), p) for p in pats)]
    ids = getattr(args, "id", None)
    if ids:
        ids = set(ids)
        out = [e for e in out if e.recno in ids]
    mn = getattr(args, "min_size", 1)
    out = [e for e in out if e.size >= mn]
    if getattr(args, "good_only", False):
        out = [e for e in out if e.health >= 0.999 and not (e.flags & 0x4001)]
    return out


def cmd_scan(args):
    src = Source(args.source)
    fs, entries = do_scan(src)
    sel = select(entries, args)
    print("削除済みファイル: %d 件 (表示 %d 件, 合計 %s)" % (
        len(entries), len(sel), human(sum(e.size for e in sel))))
    print("%8s  %10s  %-16s  %-16s  %s" % ("ID", "サイズ", "更新日時", "状態", "パス"))
    for e in sel:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else "-"
        print("%8d  %10s  %-16s  %-16s  %s" % (
            e.recno, human(e.size), t, health_label(e),
            (e.path + "/" if e.path else "") + e.name))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["ID", "サイズ(バイト)", "更新日時", "状態", "フォルダ", "ファイル名"])
            for e in sel:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.mtime)) if e.mtime else ""
                w.writerow([e.recno, e.size, t, health_label(e), e.path, e.name])
        print("一覧を %s に保存しました。" % args.csv)
    src.close()


def recover_entries(fs, sel, outdir):
    ok = warn = fail = 0
    log_rows = []
    for n, e in enumerate(sel, 1):
        label = health_label(e)
        rel = os.path.join(*[safe_name(p) for p in e.path.split("/") if p], safe_name(e.name)) \
            if e.path else safe_name(e.name)
        dst = os.path.join(outdir, rel)
        if e.flags & 0x4001:
            fail += 1
            log_rows.append([e.recno, e.size, label, rel, "スキップ"])
            continue
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            if os.path.exists(dst):
                root, ext = os.path.splitext(dst)
                dst = "%s_(%d)%s" % (root, e.recno, ext)
            fs.extract(e, dst)
            if e.mtime:
                try:
                    os.utime(dst, (e.mtime, e.mtime))
                except OSError:
                    pass
            if e.health >= 0.999:
                ok += 1
            else:
                warn += 1
            log_rows.append([e.recno, e.size, label, os.path.relpath(dst, outdir), "復元"])
        except OSError as ex:
            fail += 1
            log_rows.append([e.recno, e.size, label, rel, "失敗: %s" % ex])
        if n % 50 == 0 or n == len(sel):
            sys.stdout.write("\r  復元中... %d / %d" % (n, len(sel)))
            sys.stdout.flush()
    print()
    try:
        with open(os.path.join(outdir, "_復元ログ.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["ID", "サイズ(バイト)", "状態", "保存先", "結果"])
            w.writerows(log_rows)
    except OSError:
        pass
    print("復元 %d 件 (良好 %d / 中身が壊れている可能性あり %d)、スキップ・失敗 %d 件" % (
        ok + warn, ok, warn, fail))
    print("保存先: %s  (詳細は _復元ログ.csv)" % outdir)
    return ok, warn, fail


def cmd_recover(args):
    src = Source(args.source)
    check_output_dir(src, args.out)
    fs, entries = do_scan(src)
    sel = select(entries, args)
    if not sel:
        print("条件に合う削除済みファイルはありません (全 %d 件)。" % len(entries))
        return
    print("%d 件 / %s を復元します。" % (len(sel), human(sum(e.size for e in sel))))
    os.makedirs(args.out, exist_ok=True)
    recover_entries(fs, sel, args.out)
    src.close()


def cmd_carve(args):
    src = Source(args.source)
    check_output_dir(src, args.out)
    types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
    for t in types:
        if t not in CARVERS:
            die("未対応の種類: %s (使えるもの: %s)" % (t, ",".join(CARVERS)))
    ntfs = None
    try:
        ntfs = NTFS(src)
        ntfs.load_bitmap()
    except ValueError:
        pass
    if ntfs and ntfs.bitmap and not args.all_space:
        print("NTFS を検出: 空き領域だけを調べます (全領域を調べるには --all-space)。")
    carve(src, args.out, types, ntfs, args.all_space)
    src.close()


def list_drives():
    out = []
    if not IS_WIN:
        return out
    import ctypes
    k = ctypes.windll.kernel32
    mask = k.GetLogicalDrives()
    kinds = {2: "リムーバブル", 3: "固定ディスク", 4: "ネットワーク", 5: "CD/DVD", 6: "RAM"}
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i)
        root = letter + ":\\"
        dtype = k.GetDriveTypeW(root)
        label = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        k.GetVolumeInformationW(root, label, 261, None, None, None, fsname, 261)
        out.append((letter, kinds.get(dtype, "?"), fsname.value or "?", label.value))
    return out


def cmd_drives(args):
    drives = list_drives()
    if not drives:
        print("ドライブ一覧は Windows でのみ使えます。")
        return
    for letter, kind, fsname, label in drives:
        print("  %s:  %-10s %-6s %s" % (letter, kind, fsname, label))


def interactive():
    print("=" * 60)
    print(" hdd-undelete %s  削除ファイル復元ツール" % VERSION)
    print("=" * 60)
    if not is_admin():
        print("※ 管理者権限がありません。ドライブを直接読むには run.bat から起動するか、")
        print("   「管理者として実行」したターミナルで実行してください。\n")
    drives = list_drives()
    if drives:
        print("ドライブ一覧:")
        for letter, kind, fsname, label in drives:
            print("  %s:  %-10s %-6s %s" % (letter, kind, fsname, label))
    spec = input("\n復元元 (ドライブ文字 例: D  / またはイメージファイルのパス): ").strip()
    if not spec:
        return
    src = Source(spec)
    print("\n  1) 削除ファイルを探して復元 (NTFS。名前とフォルダも戻る) [おすすめ]")
    print("  2) 署名スキャン (フォーマット後・exFAT/FAT・1 で見つからない時。時間がかかる)")
    mode = input("モード [1]: ").strip() or "1"
    out = input("復元先フォルダ (復元元とは別のドライブ): ").strip().strip('"')
    if not out:
        die("復元先を指定してください。")
    check_output_dir(src, out)
    if mode == "2":
        ntfs = None
        try:
            ntfs = NTFS(src)
            ntfs.load_bitmap()
        except ValueError:
            pass
        carve(src, out, list(CARVERS), ntfs, False)
        return
    fs, entries = do_scan(src)
    entries = [e for e in entries if e.size >= 1]
    good = [e for e in entries if e.health >= 0.999 and not (e.flags & 0x4001)]
    print("削除済みファイル: %d 件 (うち状態が良好: %d 件 / %s)" % (
        len(entries), len(good), human(sum(e.size for e in good))))
    if not entries:
        print("見つかりませんでした。モード 2 (署名スキャン) を試してください。")
        return
    pat = input("ファイル名で絞り込み (例: *.jpg  空欄=すべて): ").strip()
    if pat:
        p = pat.lower()
        entries = [e for e in entries if fnmatch.fnmatch(e.name.lower(), p)
                   or fnmatch.fnmatch((e.path + "/" + e.name).lower(), p)]
    only = input("状態が「良好」のものだけ復元しますか? [Y/n]: ").strip().lower()
    if only != "n":
        entries = [e for e in entries if e.health >= 0.999 and not (e.flags & 0x4001)]
    if not entries:
        print("対象がありません。")
        return
    print("%d 件 / %s を復元します。" % (len(entries), human(sum(e.size for e in entries))))
    if input("実行しますか? [Y/n]: ").strip().lower() == "n":
        return
    os.makedirs(out, exist_ok=True)
    recover_entries(fs, entries, out)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        try:
            interactive()
        except (KeyboardInterrupt, EOFError):
            print("\n中断しました。")
        return
    ap = argparse.ArgumentParser(prog="undelete", description="削除ファイル復元ツール %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("drives", help="ドライブ一覧 (Windows)").set_defaults(func=cmd_drives)

    def common(p):
        p.add_argument("source", help="ドライブ文字 (例: D:) またはイメージファイル")
        p.add_argument("--name", action="append", help="ファイル名のパターン (例: *.jpg)。複数指定可")
        p.add_argument("--min-size", type=int, default=1, help="このバイト数未満は除外 (既定 1)")
        p.add_argument("--good-only", action="store_true", help="状態が「良好」のものだけ")

    p = sub.add_parser("scan", help="削除済みファイルの一覧 (NTFS)")
    common(p)
    p.add_argument("--csv", help="一覧を CSV に保存")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("recover", help="削除済みファイルを復元 (NTFS)")
    common(p)
    p.add_argument("-o", "--out", required=True, help="復元先フォルダ (別ドライブ)")
    p.add_argument("--id", type=int, action="append", help="scan で表示された ID。複数指定可")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("carve", help="署名スキャンで復元 (ファイルシステム不問)")
    p.add_argument("source")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--types", default=",".join(CARVERS), help="対象: " + CARVE_HELP)
    p.add_argument("--all-space", action="store_true", help="NTFS でも使用中の領域を含め全域を調べる")
    p.set_defaults(func=cmd_carve)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n中断しました。")


if __name__ == "__main__":
    main()
