# -*- coding: utf-8 -*-
"""
税镜 — 核心语料包生成器（零依赖）

用途：把**官方原文页**直接转成 `data/policies/core_<pack>.json`，
      避免手工抄录（手抄是出错之源）。生成的包由 `server.seed_policies()` 在建库时载入。

铁律：正文一律从官方页面抽取，**只去 HTML 标签、不改写**；source_url 可溯源。

用法：
  1) 写一份 spec（见 --template）
  2) python scripts/build_core_pack.py --spec spec.json
  3) 校验：python -c "import json;print(len(json.load(open(...))['policies']))"
  4) 部署后重启服务，看启动日志 `[语料] core*.json 载入 N 份政策`

spec 结构：
{
  "pack": "vat2026",
  "policies": [
    {"url": "https://fgk.chinatax.gov.cn/.../content.html",
     "doc_number": "财政部 税务总局公告2026年第10号",
     "title": "财政部 税务总局关于增值税法施行后增值税优惠政策衔接事项的公告",
     "category": "增值税",
     "issuing_authority": "财政部 税务总局",
     "publish_date": "2026-01-30",
     "effective_date": "2026-01-01",
     "status": "active",
     "supersedes": [{"doc_number": "...", "reason": "...", "effective_date": "2026-01-01"}]}
  ]
}
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import policy_sources as ps  # noqa: E402

OUT_DIR = os.path.join(ROOT, "data", "policies")

TEMPLATE = {
    "pack": "示例包名",
    "policies": [{
        "url": "(官方详情页 URL，主机必须是 fgk.chinatax.gov.cn)",
        "doc_number": "财政部 税务总局公告2026年第10号",
        "title": "官方标题全文",
        "category": "增值税",
        "issuing_authority": "财政部 税务总局",
        "publish_date": "YYYY-MM-DD",
        "effective_date": "YYYY-MM-DD",
        "status": "active",
        "supersedes": [{"doc_number": "被废止文号", "effective_date": "YYYY-MM-DD", "reason": "官方原文依据"}],
    }],
}


def build(spec_path):
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    pack = (spec.get("pack") or "pack").strip()
    out_pol = []
    print("=== 生成 core_%s.json ===" % pack)
    for it in spec.get("policies") or []:
        url = ps.fix_detail_url(it.get("url") or "")
        print("  → 抓取 %s" % url)
        try:
            html = ps.http_get(url)
        except Exception as e:
            print("    ✗ 抓取失败：%s" % str(e)[:80])
            continue
        body = ps.extract_body(html)
        if len(body) < 50:
            print("    ✗ 正文抽取过短(%d 字)，跳过（避免录入残缺内容）" % len(body))
            continue
        clauses = ps.split_clauses(body)
        rec = {
            "title": it.get("title") or "",
            "doc_number": it.get("doc_number") or "",
            "issuing_authority": it.get("issuing_authority") or "",
            "publish_date": it.get("publish_date") or "",
            "effective_date": it.get("effective_date") or ps.extract_effective_date(body, ""),
            "category": it.get("category") or "",
            "source_url": url,
            "content_text": body,
            "clauses": clauses,
            "verified_at": datetime.datetime.now().strftime("%Y-%m-%d"),
        }
        if it.get("status"):
            rec["status"] = it["status"]
        if it.get("supersedes"):
            rec["supersedes"] = it["supersedes"]
        if it.get("note"):
            rec["_note"] = it["note"]
        out_pol.append(rec)
        print("    ✓ %s ｜ 正文 %d 字 ｜ 条款 %d 条" % (rec["doc_number"], len(body), len(clauses)))
        ps.polite_sleep()

    out = {
        "_meta": {
            "name": "税镜 · 核心语料包（%s）" % pack,
            "rule": "正文一律由官方页面抽取、原文照录，不做改写；source_url 可溯源。",
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "scripts/build_core_pack.py",
        },
        "policies": out_pol,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "core_%s.json" % pack)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # 立刻自校验，防止生成非法 JSON（历史踩坑：静默退回兜底种子）
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)
    print("\n✓ 已写出 %s（%d 份政策，JSON 自校验通过）" % (path, len(out_pol)))
    return path


def main():
    ap = argparse.ArgumentParser(description="税镜 · 核心语料包生成器")
    ap.add_argument("--spec", help="spec JSON 路径")
    ap.add_argument("--template", action="store_true", help="打印 spec 模板")
    args = ap.parse_args()
    if args.template or not args.spec:
        print(json.dumps(TEMPLATE, ensure_ascii=False, indent=2))
        return
    build(args.spec)


if __name__ == "__main__":
    main()
