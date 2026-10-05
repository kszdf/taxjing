#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""税镜 · 官方废止目录清单 → 自动标记失效（时效权威源）

为什么需要它：
  `aging`（官方时效性字段）会滞后——实例：财政部 税务总局公告2023年第19号 官方页
  未标废止，实际已停止执行。而国家税务总局历次《公布失效废止…税务规范性文件目录
  的公告》把**权威清单**放在 .xls 附件里，且带**条款级**精度（"第四条"、"第一条第一项"）。
  这是目前能找到的、最权威且最细的批量废止来源。

管道：抓公告详情页 → 找 .xls 附件 → 零依赖解析（annex_xls）→ 与政策库比对 → 标记失效。

用法：
  python scripts/repeal_catalog.py --fetch            # 下载全部附件到 data/policies/_annex/
  python scripts/repeal_catalog.py --parse            # 解析成 repeal_catalog.json
  python scripts/repeal_catalog.py --apply --dry-run  # 比对并预览（不改库）
  python scripts/repeal_catalog.py --apply            # 落库：标失效 + 记废止关系
  python scripts/repeal_catalog.py --all              # fetch + parse + apply(dry-run)

合规：只访问国家税务总局官方站（fgk.chinatax.gov.cn），礼貌间隔、只读公告页与附件。
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DB_PATH = os.path.join(ROOT, "db", "app.sqlite")
ANNEX_DIR = os.path.join(ROOT, "data", "policies", "_annex")
CATALOG_JSON = os.path.join(ANNEX_DIR, "repeal_catalog.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

import annex_xls  # noqa: E402  零依赖 .xls 读取器


def db():
    cx = sqlite3.connect(DB_PATH, timeout=30)
    cx.row_factory = sqlite3.Row
    return cx


def _safe_url(u):
    """把含中文/空格的 URL 规范化（urllib 不接受非 ASCII 路径）。"""
    u = re.sub(r"[\x00-\x1f\x7f]", "", u or "")          # 去掉控制字符
    parts = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%"),
        urllib.parse.quote(parts.query, safe="=&%"),
        parts.fragment))


def http_get(url, timeout=40, retries=3, referer="https://fgk.chinatax.gov.cn/"):
    last = None
    url = _safe_url(url)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": referer,
                "Accept": "text/html,application/octet-stream,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def find_announcements(cx):
    """库里所有「公布失效废止…目录」类公告（仅取有 http 链接的）。"""
    return cx.execute(
        "SELECT id,doc_number,title,source_url,status FROM policy_registry "
        "WHERE (title LIKE '%失效%废止%目录%' OR title LIKE '%废止%规范性文件目录%' "
        "       OR title LIKE '%公布失效%') AND source_url LIKE 'http%' "
        "ORDER BY effective_date DESC").fetchall()


def _clean_name(s, limit=80):
    s = re.sub(r"[\x00-\x1f\x7f]", "", s or "")
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:limit]


def find_attachments(page_url, html):
    """从公告详情页找 .xls/.xlsx 附件（相对路径规律：<dir>/<cid>/files/<name>）。"""
    out = []
    base = page_url.rsplit("/", 1)[0] + "/"          # .../c5251854/
    for m in re.finditer(r'href=["\']([^"\']+\.(xlsx?|docx?))["\']', html, re.I):
        href = m.group(1)
        if href.startswith("http"):
            out.append(href)
        else:
            # 实测规律：href 形如 5251854/files/x.xls，需叠在 <dir>/ 之下
            out.append(base + href.lstrip("/"))
    # 去重保序
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); res.append(u)
    return res


def cmd_fetch(cx):
    os.makedirs(ANNEX_DIR, exist_ok=True)
    anns = find_announcements(cx)
    print("=== 抓取公告附件（共 %d 份公告）===" % len(anns))
    saved = []
    for a in anns:
        page = a["source_url"]
        tag = (a["doc_number"] or str(a["id"]))
        try:
            html = http_get(page).decode("utf-8", "ignore")
        except Exception as e:
            print("  ✗ %s 详情页失败 %s" % (tag[:34], type(e).__name__))
            continue
        urls = find_attachments(page, html)
        if not urls:
            print("  · %s 无附件（正文即清单或附件为其他格式）" % tag[:34])
            continue
        for u in urls[:3]:
            fn = _clean_name(os.path.basename(urllib.parse.unquote(u)))
            fn_full = "%s__%s" % (_clean_name(tag, 26), fn)
            dest = os.path.join(ANNEX_DIR, fn_full)
            if os.path.exists(dest) and os.path.getsize(dest) > 200:
                print("  = %s 已存在，跳过" % fn_full[:60])
                saved.append({"ann_doc": tag, "ann_id": a["id"], "page": page,
                              "file": dest, "url": u})
                continue
            try:
                blob = http_get(u, referer=page)
                open(dest, "wb").write(blob)
                kind = ("OLE2-xls" if blob[:4] == b"\xd0\xcf\x11\xe0"
                        else "zip" if blob[:2] == b"PK" else "其他")
                print("  ✓ %s（%d bytes, %s）" % (fn_full[:58], len(blob), kind))
                saved.append({"ann_doc": tag, "ann_id": a["id"], "page": page,
                              "file": dest, "url": u})
            except Exception as e:
                print("  ✗ 下载失败 %s: %s" % (fn[:40], str(e)[:60]))
            time.sleep(1.0)
        time.sleep(0.8)
    json.dump(saved, open(os.path.join(ANNEX_DIR, "attachments.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("→ 附件清单：%s（%d 个）" % (os.path.join(ANNEX_DIR, "attachments.json"), len(saved)))
    return saved


def cmd_parse():
    """解析 _annex 下全部 .xls → 汇总废止清单。"""
    att = []
    p = os.path.join(ANNEX_DIR, "attachments.json")
    if os.path.exists(p):
        att = json.load(open(p, encoding="utf-8"))
    files = [a for a in att if os.path.exists(a.get("file", ""))]
    if not files:      # 兜底：直接扫目录
        files = [{"ann_doc": os.path.basename(f).split("__")[0], "ann_id": None,
                  "file": os.path.join(ANNEX_DIR, f), "page": ""}
                 for f in sorted(os.listdir(ANNEX_DIR)) if f.lower().endswith(".xls")]
    print("=== 解析附件（%d 个）===" % len(files))
    all_items, stats = [], []
    for f in files:
        fp = f["file"]
        try:
            if fp.lower().endswith(".doc"):
                rows, diag = annex_xls.read_doc(fp)          # Word 97-2003
            else:
                rows, diag = annex_xls.read_xls(fp)          # Excel .xls
        except Exception as e:
            print("  ✗ %s 解析失败：%s" % (os.path.basename(fp)[:44], e))
            continue
        items = annex_xls.extract_repeal_rows(rows)
        for it in items:
            it["ann_doc"] = f.get("ann_doc") or ""
            it["ann_id"] = f.get("ann_id")
        all_items.extend(items)
        stats.append((os.path.basename(fp)[:46], diag.get("sst", diag.get("runs", 0)),
                      len(rows), len(items)))
        print("  ✓ %-46s 桶%-5s 行 %-4d → 清单 %d 条" % stats[-1])
    # 去重（同一文号被多次公布，保留最早 scope 更全的）
    merged = {}
    for it in all_items:
        k = it["doc_number"].strip()
        if not k:
            continue
        cur = merged.get(k)
        if cur is None:
            merged[k] = it
        else:
            if len(it.get("scope") or "") > len(cur.get("scope") or ""):
                merged[k] = it
    os.makedirs(ANNEX_DIR, exist_ok=True)
    json.dump({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "raw_count": len(all_items), "unique_count": len(merged),
               "items": list(merged.values())},
              open(CATALOG_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n→ 原始 %d 条 / 去重后 %d 条 → %s" % (len(all_items), len(merged), CATALOG_JSON))
    return merged


def _norm_clause_no(s):
    m = re.match(r"第([一二三四五六七八九十百零〇0-9]+)条", (s or "").strip())
    return ("第%s条" % m.group(1)) if m else ""


def cmd_apply(cx, dry_run=True):
    if not os.path.exists(CATALOG_JSON):
        print("缺少 %s，请先 --parse" % CATALOG_JSON)
        return 0
    doc = json.load(open(CATALOG_JSON, encoding="utf-8"))
    items = doc.get("items") or []
    print("=== 与政策库比对（官方废止清单 %d 条）%s===" % (len(items), "  [dry-run]" if dry_run else ""))
    hit_full = hit_part = miss = already = 0
    applied = []
    for it in items:
        dn = (it.get("doc_number") or "").strip()
        if not dn:
            continue
        row = cx.execute("SELECT id,doc_number,title,status FROM policy_registry WHERE doc_number=?",
                         (dn,)).fetchone()
        if not row:
            miss += 1
            continue
        if row["status"] == "invalid":
            already += 1
            continue
        scope = (it.get("scope") or "全文").strip()
        is_full = ("全文" in scope) or scope in ("", "全部")
        src_id = it.get("ann_id")
        eff = ""
        if is_full:
            hit_full += 1
            if not dry_run:
                cx.execute("UPDATE policy_clause SET clause_status='invalid', invalid_since=?, "
                           "superseded_by=? WHERE doc_id=? AND clause_status='active'",
                           (eff, it.get("ann_doc") or "", row["id"]))
                cx.execute("UPDATE policy_registry SET status='invalid' WHERE id=?", (row["id"],))
        else:
            hit_part += 1
            nos = [_norm_clause_no(x) for x in re.split(r"[、,，;；]", scope)]
            nos = [n for n in nos if n]
            if not dry_run and nos:
                for n in nos:
                    cx.execute("UPDATE policy_clause SET clause_status='invalid', invalid_since=?, "
                               "superseded_by=? WHERE doc_id=? AND clause_no=? AND clause_status='active'",
                               (eff, it.get("ann_doc") or "", row["id"], n))
                left = cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE doc_id=? AND clause_status='active'",
                                  (row["id"],)).fetchone()["c"]
                if left == 0:
                    cx.execute("UPDATE policy_registry SET status='invalid' WHERE id=?", (row["id"],))
                else:
                    cx.execute("UPDATE policy_registry SET status='partially_invalid' WHERE id=?",
                               (row["id"],))
        if not dry_run and src_id:
            ex = cx.execute("SELECT id FROM policy_supersession WHERE source_doc_id=? AND target_doc_id=?",
                            (src_id, row["id"])).fetchone()
            if not ex:
                cx.execute("INSERT INTO policy_supersession (source_doc_id,target_doc_id,reason,"
                           "effective_date,source_url) VALUES (?,?,?,?,?)",
                           (src_id, row["id"],
                            ("官方废止目录：" + scope)[:200], eff, it.get("page") or ""))
        applied.append((row["doc_number"], row["title"][:34], scope, is_full))
    if not dry_run:
        cx.commit()
    print("  命中并标记：全文失效 %d 条 | 条款级失效 %d 条" % (hit_full, hit_part))
    print("  已在库中且已是失效：%d | 库中无此文件（未收录）：%d" % (already, miss))
    if applied:
        print("  明细（前 25）：")
        for dn, ti, sc, full in applied[:25]:
            print("    %-30s %s  [%s%s]" % (dn[:30], ti, sc[:22], "·全文" if full else ""))
    if dry_run and applied:
        print("\n  （未落库；确认后去掉 --dry-run 执行）")
    return hit_full + hit_part


def main():
    ap = argparse.ArgumentParser(description="税镜 · 官方废止目录清单 → 自动标记失效")
    ap.add_argument("--fetch", action="store_true", help="下载公告附件")
    ap.add_argument("--parse", action="store_true", help="解析附件成 repeal_catalog.json")
    ap.add_argument("--apply", action="store_true", help="比对政策库并标记失效")
    ap.add_argument("--dry-run", action="store_true", help="只报告不改库")
    ap.add_argument("--all", action="store_true", help="fetch + parse + apply(dry-run)")
    args = ap.parse_args()

    cx = db()
    try:
        if args.all:
            cmd_fetch(cx)
            cmd_parse()
            return cmd_apply(cx, dry_run=True)
        if args.fetch:
            cmd_fetch(cx)
        if args.parse:
            cmd_parse()
        if args.apply:
            return cmd_apply(cx, dry_run=args.dry_run)
        if not (args.fetch or args.parse or args.apply):
            ap.print_help()
    finally:
        cx.close()


if __name__ == "__main__":
    main()
