#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""税镜 · 零依赖读 .xls（OLE2 + BIFF8）——用于提取官方废止目录附件

背景：国家税务总局历次《公布失效废止…税务规范性文件目录的公告》，把清单放在
`.xls` 附件里。这是**官方权威的批量废止清单**，还带"失效废止内容"（全文 / 第X条第X项），
比 `aging` 字段更细，正好补上"官方时效字段滞后"的缺口。

零依赖实现（仅标准库 struct/re）：
  OLE2 容器 → Workbook 流 → BIFF8（SST 共享字符串表 + cell 记录）→ 表格行。

支持：LABELSST(0x00FD) / NUMBER(0x0203) / RK(0x027E) / MULRK(0x00BD) / SST(0x00FC)+CONTINUE(0x003C)
不支持的（会跳过并如实报告）：加密流、公式结果（除缓存值）、图形对象。

用法：
  python scripts/annex_xls.py --file data/policies/_annex/xxx.xls
  python scripts/annex_xls.py --file xxx.xls --json out.json
  python scripts/annex_xls.py --file xxx.xls --tables        # 只输出含文号的表
"""
import argparse
import json
import os
import re
import struct
import sys

# ============================ OLE2 容器 ============================
OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ole_streams(buf):
    """解析 OLE2 复合文档，返回 {流名: 字节}。"""
    if buf[:8] != OLE_SIG:
        return None
    ssz = 1 << struct.unpack_from("<H", buf, 30)[0]
    msz = 1 << struct.unpack_from("<H", buf, 32)[0]
    nfat = struct.unpack_from("<I", buf, 44)[0]
    dirstart = struct.unpack_from("<I", buf, 48)[0]
    cutoff = struct.unpack_from("<I", buf, 56)[0]
    minifat_start = struct.unpack_from("<I", buf, 60)[0]
    nminifat = struct.unpack_from("<I", buf, 64)[0]
    difat_start = struct.unpack_from("<I", buf, 68)[0]
    ndifat = struct.unpack_from("<I", buf, 72)[0]

    def off(s):
        return (s + 1) * ssz

    # DIFAT → FAT 扇区号
    difat = list(struct.unpack_from("<109I", buf, 76))
    sec, guard = difat_start, 0
    while sec not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < ndifat + 2:
        blk = buf[off(sec):off(sec) + ssz]
        if len(blk) < ssz:
            break
        vals = struct.unpack_from("<%dI" % (ssz // 4), blk, 0)
        difat.extend(vals[:-1]); sec = vals[-1]; guard += 1
    fat = []
    for fs in difat[:nfat]:
        if fs >= 0xFFFFFFFE:
            continue
        blk = buf[off(fs):off(fs) + ssz]
        if len(blk) < ssz:
            continue
        fat.extend(struct.unpack_from("<%dI" % (ssz // 4), blk, 0))

    def chain(start):
        out, s, g = [], start, 0
        while s not in (0xFFFFFFFE, 0xFFFFFFFF) and s < len(fat) and g < 200000:
            out.append(s); s = fat[s]; g += 1
        return out

    def read_chain(start, size=None):
        b = b"".join(buf[off(s):off(s) + ssz] for s in chain(start))
        return b[:size] if size is not None else b

    # mini stream（<cutoff 的流走 miniFAT）
    minifat = []
    if minifat_start < 0xFFFFFFFE:
        mf = read_chain(minifat_start)
        minifat = list(struct.unpack_from("<%dI" % (len(mf) // 4), mf, 0)) if len(mf) >= 4 else []

    # 目录
    dirbuf = read_chain(dirstart)
    entries = []
    for i in range(0, len(dirbuf), 128):
        e = dirbuf[i:i + 128]
        if len(e) < 128:
            break
        nl = struct.unpack_from("<H", e, 64)[0]
        if nl < 2 or nl > 64:
            continue
        name = e[:nl - 2].decode("utf-16-le", "ignore")
        etype = e[66]
        start = struct.unpack_from("<I", e, 116)[0]
        size = struct.unpack_from("<I", e, 120)[0]
        entries.append((name, etype, start, size))

    root = next((x for x in entries if x[1] == 5), None)
    ministream = b""
    if root and root[2] < 0xFFFFFFFE:
        ministream = read_chain(root[2], root[3])

    def read_mini(start, size):
        out, s, g = b"", start, 0
        while s not in (0xFFFFFFFE, 0xFFFFFFFF) and s < len(minifat) and g < 200000:
            out += ministream[s * msz:(s + 1) * msz]
            s = minifat[s]; g += 1
        return out[:size]

    streams = {}
    for name, etype, start, size in entries:
        if etype != 2 or start >= 0xFFFFFFFE or name in streams:
            continue
        try:
            streams[name] = read_mini(start, size) if size < cutoff else read_chain(start, size)
        except Exception:
            streams[name] = b""
    return streams


# ============================ BIFF8 ============================
def _biff_records(wb):
    pos = 0
    while pos + 4 <= len(wb):
        t, l = struct.unpack_from("<HH", wb, pos)
        yield t, wb[pos + 4:pos + 4 + l]
        pos += 4 + l


def parse_sst(chunks):
    """解析 SST（0x00FC）+ CONTINUE（0x003C）→ 共享字符串列表。"""
    if not chunks:
        return []
    total, unique = struct.unpack_from("<II", chunks[0], 0)
    recs = [chunks[0][8:]] + list(chunks[1:])
    ri = p = 0
    out = []
    for _ in range(unique):
        if ri >= len(recs):
            break
        if len(recs[ri]) - p < 3:          # 剩余不足一条头，跨到下一记录
            ri += 1; p = 1
            if ri >= len(recs):
                break
        cch, gb = struct.unpack_from("<HB", recs[ri], p); p += 3
        high = gb & 0x01
        rich = gb & 0x08
        ext = gb & 0x04
        cRun = cbExt = 0
        if rich:
            cRun = struct.unpack_from("<H", recs[ri], p)[0]; p += 2
        if ext:
            cbExt = struct.unpack_from("<i", recs[ri], p)[0]; p += 4
        need = cch * (2 if high else 1)
        chars = b""
        while need > 0:
            got = min(need, len(recs[ri]) - p)
            chars += recs[ri][p:p + got]; p += got; need -= got
            if need > 0:
                ri += 1
                if ri >= len(recs):
                    break
                p = 1                       # CONTINUE 开头重复 grbit
        out.append(chars.decode("utf-16-le" if high else "latin-1", "ignore"))
        skip = cRun * 4 + cbExt
        while skip > 0:
            got = min(skip, len(recs[ri]) - p)
            p += got; skip -= got
            if skip > 0:
                ri += 1
                if ri >= len(recs):
                    break
                p = 1
    return out


def rk_to_num(rk):
    """BIFF RK 编码 → 数值。"""
    if rk & 0x02:
        v = rk >> 2
        if v & 0x20000000:
            v -= 0x40000000
        val = float(v)
    else:
        val = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    return val / 100.0 if (rk & 0x01) else val


def _fmt(v):
    if isinstance(v, float):
        return str(int(v)) if abs(v - int(v)) < 1e-9 else ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def read_xls(path):
    """读 .xls → 返回 (按行组织的表格, 诊断信息)。行 = 单元格值列表。"""
    buf = open(path, "rb").read()
    streams = _ole_streams(buf)
    diag = {"ole": bool(streams), "streams": list(streams or {}), "sst": 0, "cells": 0}
    if not streams:
        return [], diag
    wb = streams.get("Workbook") or streams.get("Book")
    if not wb:
        return [], diag

    # ① 一遍遍历：收 SST 段 + 原始 cell 记录（SST 可能在 cell 之后出现，故分两步）
    sst_chunks, raw_cells = [], []
    for t, b in _biff_records(wb):
        if t == 0x00FC:
            sst_chunks = [b]
        elif t == 0x003C and sst_chunks:
            sst_chunks.append(b)
        elif t in (0x00FD, 0x0203, 0x027E, 0x00BD):
            raw_cells.append((t, b))
    sst = parse_sst(sst_chunks) if sst_chunks else []
    diag["sst"] = len(sst)

    # ② 解引用成 (row, col, value)
    cells = []
    for t, b in raw_cells:
        try:
            if t == 0x00FD and len(b) >= 10:        # LABELSST → 指向 SST
                r, c, xf, i = struct.unpack_from("<HHHI", b, 0)
                cells.append((r, c, sst[i] if 0 <= i < len(sst) else ""))
            elif t == 0x0203 and len(b) >= 14:      # NUMBER
                r, c, xf = struct.unpack_from("<HHH", b, 0)
                cells.append((r, c, struct.unpack_from("<d", b, 6)[0]))
            elif t == 0x027E and len(b) >= 10:      # RK
                r, c, xf, rk = struct.unpack_from("<HHHI", b, 0)
                cells.append((r, c, rk_to_num(rk)))
            elif t == 0x00BD and len(b) >= 6:       # MULRK（一行多列合并存储）
                r, c1 = struct.unpack_from("<HH", b, 0)
                for k in range((len(b) - 6) // 6):
                    rk = struct.unpack_from("<I", b, 4 + k * 6 + 2)[0]
                    cells.append((r, c1 + k, rk_to_num(rk)))
        except Exception:
            continue
    diag["cells"] = len(cells)

    rows = {}
    for r, c, v in cells:
        rows.setdefault(r, {})[c] = v
    out = []
    for r in sorted(rows):
        cols = rows[r]
        out.append([_fmt(cols.get(c, "")) for c in range(max(cols) + 1)])
    return out, diag


# ============================ .doc（Word 97-2003） ============================
# Word 表格控制符：0x07=单元格结束, 0x0D=行结束, 0x0B=软换行, 0x09=制表
_CELL, _ROW = "\x07", "\x0d"


def read_doc(path):
    """读 .doc（OLE2 + WordDocument 流）→ (行列表, 诊断)。

    只做**文本区粗提取**：不解析 FIB/piece table，靠"字符白名单 + Word 表格控制符"还原。
    对"废止目录清单"这类纯表格足够（不丢文字、能还原行列）；图文混排会丢排版，不影响取数。
    """
    buf = open(path, "rb").read()
    streams = _ole_streams(buf)
    diag = {"ole": bool(streams), "streams": list(streams or {}), "cells": 0, "runs": 0}
    if not streams:
        return [], diag
    wd = streams.get("WordDocument") or b""
    if not wd:
        return [], diag

    def ok_cp(cp):
        return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                or 0x20 <= cp <= 0x7E
                or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF
                or cp in (0x07, 0x0D, 0x0A, 0x0B, 0x09, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D))

    runs, cur, i = [], [], 0
    while i + 1 < len(wd):
        cp = wd[i] | (wd[i + 1] << 8)
        if ok_cp(cp):
            cur.append(chr(cp)); i += 2
        else:
            if len(cur) >= 24:
                runs.append("".join(cur))
            cur = []; i += 1
    if len(cur) >= 24:
        runs.append("".join(cur))
    diag["runs"] = len(runs)

    # 取"最像正文"的 run（含最多单元格分隔符），按 单元格/行 切分
    text = max(runs, key=lambda s: s.count(_CELL) + s.count(_ROW), default="")
    text = text.replace("\x0b", " ").replace("\x0a", " ")
    rows = []
    for line in text.split(_ROW):
        if not rows and not line.strip(_CELL):
            continue
        cells = [c.strip().strip("\x00") for c in line.split(_CELL)]
        cells = [c for c in cells if c != ""]
        if cells:
            rows.append(cells)
    diag["cells"] = sum(len(r) for r in rows)
    return rows, diag


# ============================ 抽取"废止清单"行 ============================
DOC_NO_RE = re.compile(r"(?:\d{4}年第\d+号|〔\s*\d{4}\s*〕\s*第?\s*\d+\s*号|第\d+号)")
# 严格的"整格就是文号"判定（用于锚点定位）
_DOC_NO_CELL = re.compile(r"^\s*.{0,24}?(?:\d{4}\s*年第\s*\d+\s*号|[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*第?\s*\d+\s*号)\s*$")
_SCOPE_CELL = re.compile(r"^(全文|全部|第[一二三四五六七八九十百零〇0-9]+条.*)$")
_SKIP_TITLE = re.compile(r"序号|标题|发文日期|发布日期|废止内容|目录$|^附件$")

HEADER_KW = {"标题": "title", "名称": "title", "文件字号": "doc_number", "文号": "doc_number",
             "发文字号": "doc_number", "发布时间": "publish_date", "发布日期": "publish_date",
             "发文日期": "publish_date", "失效废止内容": "scope", "废止内容": "scope",
             "失效内容": "scope", "备注": "scope"}


def _extract_flat(cells):
    """兜底提取：把单元格序列拉平，以"整格为文号"的单元格为锚点，
    向前取标题、向后取失效范围。适用于 .doc 行列结构不可靠的情形。"""
    out, seen = [], set()
    for i, c in enumerate(cells):
        s = (c or "").strip()
        if not _DOC_NO_CELL.match(s) or s in seen:
            continue
        title = ""
        for j in range(i - 1, max(-1, i - 7), -1):
            t = (cells[j] or "").strip()
            if 6 <= len(t) <= 160 and re.search(r"[\u4e00-\u9fff]{5,}", t) and not _SKIP_TITLE.search(t):
                title = t
                break
        scope = ""
        for j in range(i + 1, min(len(cells), i + 7)):
            t = (cells[j] or "").strip()
            if _SCOPE_CELL.match(t):
                scope = t
                break
        seen.add(s)
        out.append({"doc_number": s, "title": title, "scope": scope or "全文", "publish_date": ""})
    return out


def extract_repeal_rows(rows):
    """从表格里抽出废止清单行：优先定位表头；定位不到则按文号锚点兜底提取。"""
    head_idx, colmap = None, {}
    for i, row in enumerate(rows[:15]):
        m = {}
        for c, cell in enumerate(row):
            k = HEADER_KW.get(cell.strip())
            if k and k not in m:
                m[k] = c
        if "doc_number" in m and len(m) >= 2:
            head_idx, colmap = i, m
            break
    if head_idx is None:
        return _extract_flat([c for r in rows for c in r])
    out = []
    for row in rows[head_idx + 1:]:
        if not any(x.strip() for x in row):
            continue
        def g(key):
            c = colmap.get(key)
            return (row[c].strip() if (c is not None and c < len(row)) else "")
        doc_number = g("doc_number")
        title = g("title")
        if not doc_number and not title:
            continue
        out.append({"doc_number": doc_number, "title": title,
                    "scope": g("scope") or "全文", "publish_date": g("publish_date")})
    if len(out) < 3:          # 表头法产出太少 → 用锚点法补
        alt = _extract_flat([c for r in rows for c in r])
        if len(alt) > len(out):
            return alt
    return out


def main():
    ap = argparse.ArgumentParser(description="税镜 · 零依赖读取 .xls（OLE2+BIFF8）")
    ap.add_argument("--file", required=True, help="本地 .xls 路径")
    ap.add_argument("--json", default="", help="导出 JSON 到文件")
    ap.add_argument("--rows", type=int, default=50, help="打印行数上限")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print("文件不存在：%s" % args.file)
        return 2
    rows, diag = read_xls(args.file)
    print("=== 诊断 ===")
    print("  OLE2: %s | 流: %s" % (diag["ole"], "、".join(diag["streams"])))
    print("  SST 字符串: %d | 单元格: %d | 表格行: %d" % (diag["sst"], diag["cells"], len(rows)))
    print("\n=== 表格（前 %d 行）===" % args.rows)
    for r in rows[:args.rows]:
        print("   " + " | ".join(x[:52] for x in r))
    rep = extract_repeal_rows(rows)
    print("\n=== 识别为废止清单行：%d 条 ===" % len(rep))
    for x in rep[:15]:
        print("   %-28s %s  [%s]" % (x["doc_number"][:28], x["title"][:44], x["scope"]))
    if args.json:
        json.dump({"rows": rows, "repeal": rep}, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("\n已导出 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
