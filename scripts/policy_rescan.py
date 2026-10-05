#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""税镜 · 失效回扫（时效兜底闭环）

背景：官网「时效性」字段会滞后——有文件已被后文废止/停止执行，官方页面却未标注。
（实例：财政部 税务总局公告2023年第19号，官方页面未标废止，但2026年第10号第六条
「在2025年12月31日前制发文件规定的国内环节增值税优惠政策同时停止执行」已使其失效。）

本脚本从**已入库正文**中抽取官方明写的废止语句，据此把被点名的旧文件标为失效，
避免拿着已停止执行的文件去答客户。这是「零错误」的最后一道自动兜底。

用法：
  python scripts/policy_rescan.py --supersede            # 扫描废止语句并落库
  python scripts/policy_rescan.py --supersede --dry-run  # 只报告，不改库
  python scripts/policy_rescan.py --aging-check          # 官方标废止却仍在依据库 → 告警
  python scripts/policy_rescan.py --report               # 汇总
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "db", "app.sqlite")
TRIAGE_DIR = os.path.join(ROOT, "data", "policies", "_triage")

# 中止/失效语义（出现这些词的句子才认为在"点名废止"）
KILL_WORDS = ("废止", "停止执行", "予以失效", "已失效", "不再执行")

# 「《名称》（文号）」紧邻结构
_PAIR_AFTER = re.compile(r'《([^》]{2,90})》\s*[（(]\s*([^）)]{2,60}?号)\s*[）)]')
# 「《名称（文号）》」文号写在书名号内
_PAIR_INNER = re.compile(r'《([^》]{2,90}?)[（(]([^）)]{2,60}?号)[）)]》')


def db():
    cx = sqlite3.connect(DB_PATH, timeout=30)
    cx.row_factory = sqlite3.Row
    return cx


def doc_key(s):
    """把文号归一成可比较的 key（跨机关同年同号仍需名称二次校验）。"""
    s = s or ""
    m = re.search(r'(\d{4})\s*年第\s*([0-9]+)\s*号', s)
    if m:
        return "YN:%s-%s" % (m.group(1), m.group(2))
    m = re.search(r'〔\s*(\d{4})\s*〕\s*第?\s*([0-9]+)\s*号', s)
    if m:
        return "BR:%s-%s" % (m.group(1), m.group(2))
    return ""


def jac(a, b):
    A = set(re.sub(r'[\s《》()（）]', '', a or ""))
    B = set(re.sub(r'[\s《》()（）]', '', b or ""))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


# 「除……外」是**例外保留**结构，绝不能把里面的文件当成废止对象。
# 实例（财政部 税务总局公告2026年第10号 第六条）：
#   "除本公告和增值税法、增值税法实施条例、《……个人销售住房增值税政策的公告》
#    （2025年第17号）外，在2025年12月31日前制发文件规定的国内环节增值税优惠政策同时停止执行。"
#   → 2025年第17号 是被**明确保留**的；不剔除就会把"保留"扫成"废止"（语义反转，会害人）。
_EXCEPT_RE = re.compile(r'除[^。；;]{2,240}?外[，,]?')


def _strip_exceptions(s):
    """剔除句子里的「除……外」片段（例外保留清单），只对它之外的部分做废止提取。"""
    return _EXCEPT_RE.sub("（除外条款）", s or "")


def kill_sentences(text):
    """按句切分，返回含废止语义的句子。"""
    out = []
    for p in re.split(r'[。；;]', text or ""):
        p = p.strip()
        if p and any(k in p for k in KILL_WORDS):
            out.append(p)
    return out


def extract_kills(text, limit=60):
    """从正文中抽取被点名废止的文件。返回 [{name, doc_number, sentence}]。"""
    res, seen = [], set()
    for s in kill_sentences(text):
        body = _strip_exceptions(s)          # 例外保留清单先剔除，再提取
        pairs = _PAIR_AFTER.findall(body) + _PAIR_INNER.findall(body)
        for name, num in pairs:
            k = (name.strip(), num.strip())
            if k in seen:
                continue
            seen.add(k)
            res.append({"name": name.strip(), "doc_number": num.strip(), "sentence": s[:260]})
            if len(res) >= limit:
                return res
        if not pairs:
            for m in re.finditer(r'《([^》]{4,90})》', body):
                k = (m.group(1).strip(), "")
                if k in seen:
                    continue
                seen.add(k)
                res.append({"name": m.group(1).strip(), "doc_number": "", "sentence": s[:260]})
    return res


def _apply_kill(cx, src, tgt, eff_date, reason, dry_run):
    """把 target 的现行条款置为失效，并按情况调整文件状态，记录废止关系。"""
    if dry_run:
        return "dry"
    cx.execute(
        "UPDATE policy_clause SET clause_status='invalid', invalid_since=?, superseded_by=? "
        "WHERE doc_id=? AND clause_status='active'",
        (eff_date or "", src["doc_number"], tgt["id"]))
    left = cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE doc_id=? AND clause_status='active'",
                      (tgt["id"],)).fetchone()["c"]
    allc = cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE doc_id=?", (tgt["id"],)).fetchone()["c"]
    new_status = "invalid" if (allc and left == 0) else ("partially_invalid" if allc else "invalid")
    cx.execute("UPDATE policy_registry SET status=? WHERE id=?", (new_status, tgt["id"]))
    ex = cx.execute("SELECT id FROM policy_supersession WHERE source_doc_id=? AND target_doc_id=?",
                    (src["id"], tgt["id"])).fetchone()
    if not ex:
        cx.execute(
            "INSERT INTO policy_supersession (source_doc_id,target_doc_id,reason,effective_date,source_url) "
            "VALUES (?,?,?,?,?)",
            (src["id"], tgt["id"], ("失效回扫：" + reason)[:300], eff_date or "",
             src["source_url"] or ""))
    return new_status


def scan_supersede(cx, dry_run=False, only_src=None):
    """扫描库内正文中的废止语句，把被点名的旧文件标为失效。"""
    idx = {}
    for r in cx.execute("SELECT id,doc_number,title,status,effective_date FROM policy_registry"):
        k = doc_key(r["doc_number"])
        if k:
            idx.setdefault(k, []).append(dict(r))

    sql = ("SELECT id,doc_number,title,effective_date,source_url,content_text,status "
           "FROM policy_registry WHERE LENGTH(IFNULL(content_text,''))>60 ")
    if only_src:
        sql += "AND doc_number=? "
    rows = cx.execute(sql + "ORDER BY effective_date DESC").fetchall()

    auto, review, nohit = [], [], []
    for src in rows:
        src = dict(src)
        kills = extract_kills(src["content_text"])
        if not kills:
            continue
        for k in kills:
            key = doc_key(k["doc_number"]) or doc_key(k["name"])
            cands = [c for c in idx.get(key, []) if c["id"] != src["id"]] if key else []
            if key and len(cands) == 1:
                auto.append((src, k, cands[0]))
            elif key and len(cands) > 1:
                # 同年同号多机关：用名称相似度收敛
                best = max(cands, key=lambda c: jac(k["name"], c["title"]))
                if jac(k["name"], best["title"]) >= 0.55:
                    auto.append((src, k, best))
                else:
                    review.append((src, k, cands))
            else:
                # 无文号 → 按名称模糊匹配库内文件
                hits = [c for c in idx.values() for c in c
                        if c["id"] != src["id"] and jac(k["name"], c["title"]) >= 0.62]
                if hits:
                    hits.sort(key=lambda c: -jac(k["name"], c["title"]))
                    review.append((src, k, hits[:3]))
                else:
                    nohit.append((src, k))

    # —— 落库：只对"文号精确命中且唯一"的自动生效 ——
    applied = []
    for src, k, tgt in auto:
        # 防线：不得把生效日期更晚的文件标失效
        se = (src["effective_date"] or "")[:10]
        te = (tgt["effective_date"] or "")[:10]
        if se and te and te > se:
            review.append((src, k, [tgt]))
            continue
        st = _apply_kill(cx, src, tgt, src["effective_date"], k["sentence"][:120], dry_run)
        applied.append({"from": src["doc_number"], "to": tgt["doc_number"],
                        "to_title": tgt["title"][:60], "new_status": st,
                        "was_status": tgt["status"],
                        "sentence": k["sentence"][:120]})
    if not dry_run:
        cx.commit()

    os.makedirs(TRIAGE_DIR, exist_ok=True)
    out = os.path.join(TRIAGE_DIR, "supersede_candidates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "auto_applied": applied,
            "need_review": [{"from": s["doc_number"], "killed_name": k["name"],
                             "killed_no": k["doc_number"], "sentence": k["sentence"],
                             "candidates": [{"doc_number": c["doc_number"], "title": c["title"][:60]}
                                            for c in cs]}
                            for s, k, cs in review],
            "no_hit": [{"from": s["doc_number"], "killed_name": k["name"],
                        "killed_no": k["doc_number"], "sentence": k["sentence"]}
                       for s, k in nohit],
        }, f, ensure_ascii=False, indent=2)

    print("=== 失效回扫 ===")
    print("  扫描含正文的政策 %d 份" % len(rows))
    print("  ✓ 自动标记失效 %d 条（文号精确命中）" % len(applied))
    # 最要紧的是哪些会改变"可检索"状态——误标会直接影响答案依据
    hot = [a for a in applied if a["was_status"] in ("active", "partially_invalid")]
    print("     其中**会影响检索**（原为可作依据状态）%d 条：" % len(hot))
    for a in hot[:20]:
        print("       %s → 废止 %s（原 %s）" % (a["from"], a["to"], a["was_status"]))
        print("          %s" % a["to_title"])
    rest = [a for a in applied if a["was_status"] not in ("active", "partially_invalid")]
    print("     其余 %d 条原为「仅目录/待复核」，标记不影响当前检索" % len(rest))
    print("  ⚠ 待人工确认 %d 条（多候选/仅名称匹配）" % len(review))
    for s, k, cs in review[:8]:
        print("     %s → 疑废止「%s」%s" % (s["doc_number"], k["name"][:30], k["doc_number"]))
    print("  · 库内无对应文件 %d 条（对方未入库，仅留痕）" % len(nohit))
    print("  候选清单：%s%s" % (out, "  [dry-run 未改库]" if dry_run else ""))
    return len(applied), len(review), len(nohit)


def aging_check(cx):
    """官方标废止/失效，但仍处于依据库状态 → 告警（提示人工复核）。"""
    dead = ("全文废止", "全文失效", "废止", "失效", "已废止", "部分废止", "部分失效")
    q = ("SELECT id,doc_number,title,official_aging,status,effective_date FROM policy_registry "
         "WHERE official_aging IN (%s) AND status IN ('active','partially_invalid','pending_review') "
         "ORDER BY effective_date DESC" % ",".join("?" * len(dead)))
    rows = cx.execute(q, dead).fetchall()
    print("=== 时效告警：官方标废止却仍在依据库 ===")
    if not rows:
        print("  ✓ 无（依据库与官方时效一致）")
    for r in rows:
        print("  ⚠ [%s] %s | 官方=%s | 施行 %s" % (r["status"], r["doc_number"],
                                                   r["official_aging"], r["effective_date"]))
        print("      %s" % r["title"][:70])
    return len(rows)


def report(cx):
    print("=== 时效总览 ===")
    for r in cx.execute("SELECT status,COUNT(*) n FROM policy_registry GROUP BY status ORDER BY n DESC"):
        print("  %-18s %d" % (r["status"], r["n"]))
    print("  --- 官方时效分布 ---")
    for r in cx.execute("SELECT IFNULL(NULLIF(official_aging,''),'(未标)') a,COUNT(*) n "
                        "FROM policy_registry GROUP BY a ORDER BY n DESC"):
        print("  %-12s %d" % (r["a"], r["n"]))
    print("  废止关系：%d 条" % cx.execute("SELECT COUNT(*) c FROM policy_supersession").fetchone()["c"])
    print("  条款：有效 %d / 失效 %d" % (
        cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE clause_status='active'").fetchone()["c"],
        cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE clause_status<>'active'").fetchone()["c"]))


# ============================ 批量废止规则（点名式正则抓不到的） ============================
# 官方常用两种废止方式：① 点名「《XXX》（某年第N号）同时废止」——见 extract_kills；
# ② 按「日期 + 范围」整体废止——正则抓不到，**必须显式声明规则**，否则会漏判。
BATCH_REPEAL_RULES = [
    {
        "source_doc": "财政部 税务总局公告2026年第10号",
        "source_clause": "六、",
        "effective_from": "2026-01-01",
        "cutoff": "2025-12-31",
        "scope": "国内环节增值税优惠政策",
        "exceptions": ["财政部 税务总局公告2025年第17号"],
        # 仅以下文号自动标记（与 2023年第19号 同类的"小规模免征增值税"优惠，证据确凿）；
        # 其余候选只列清单交人工判断——避免误伤程序性/特殊区域/跨境类文件。
        "auto_apply": ["国家税务总局公告2022年第6号"],
        "quote": ("本公告自2026年1月1日起实施。除本公告和增值税法、增值税法实施条例、"
                  "《财政部 税务总局关于个人销售住房增值税政策的公告》"
                  "（财政部 税务总局公告2025年第17号）外，在2025年12月31日前制发文件规定的"
                  "国内环节增值税优惠政策同时停止执行。"),
    },
]
# 判定"国内环节增值税优惠政策"的标题用语（保守：只认明确的优惠用语，避免误伤"标准/征管"类）
_VAT_PREFER_ACTION = ("优惠", "减免", "免征", "减征", "减按", "起征点", "免税", "即征即退", "先征后退")


def batch_repeal(cx, apply=False):
    """应用"按日期 + 范围整体废止"的官方规则（点名式正则抓不到这类）。

    规则来源：见 BATCH_REPEAL_RULES（内置官方原文，可增补）。
    候选筛选（保守四项皆需满足）：
      ① 标题/文号含「增值税」② 标题含明确优惠用语 ③ 制发日在 cutoff 之前
      ④ 处于可检索/待复核状态、且不在例外清单。
    默认**只输出清单**；apply=True 才落库（置条款失效 + 记废止关系 + 留痕）。
    """
    total = 0
    for rule in BATCH_REPEAL_RULES:
        src = cx.execute("SELECT id,doc_number,title,source_url FROM policy_registry WHERE doc_number=?",
                         (rule["source_doc"],)).fetchone()
        print("=== 批量废止规则：%s %s ===" % (rule["source_doc"], rule["source_clause"]))
        print("  范围：%s；制发日 ≤ %s" % (rule["scope"], rule["cutoff"]))
        if not src:
            print("  ! 规则源文件不在库中，请先入库该文号")
            continue
        rows = cx.execute(
            "SELECT id,doc_number,title,status,publish_date,effective_date FROM policy_registry "
            "WHERE status IN ('active','partially_invalid','pending_review')").fetchall()
        cands = []
        for r in rows:
            if (r["doc_number"] in rule["exceptions"]) or (r["doc_number"] == rule["source_doc"]):
                continue
            blob = (r["title"] or "") + " " + (r["doc_number"] or "")
            if "增值税" not in blob:
                continue
            if not any(k in (r["title"] or "") for k in _VAT_PREFER_ACTION):
                continue
            d = (r["publish_date"] or r["effective_date"] or "")[:10]
            if d and d > rule["cutoff"]:
                continue
            cands.append(r)
        print("  命中候选 %d 条：" % len(cands))
        for r in cands[:40]:
            print("    [%s] %s | 制发 %s" % (r["status"], (r["doc_number"] or "")[:38],
                                            r["publish_date"] or r["effective_date"] or "-"))
            print("         %s" % (r["title"] or "")[:62])
        total += len(cands)
        if apply and cands:
            allow = rule.get("auto_apply") or []
            auto = [r for r in cands if r["doc_number"] in allow]
            hold = [r for r in cands if r["doc_number"] not in allow]
            for r in auto:
                cx.execute("UPDATE policy_clause SET clause_status='invalid', invalid_since=?, "
                           "superseded_by=? WHERE doc_id=? AND clause_status='active'",
                           (rule["effective_from"], rule["source_doc"], r["id"]))
                cx.execute("UPDATE policy_registry SET status='invalid' WHERE id=?", (r["id"],))
                ex = cx.execute("SELECT id FROM policy_supersession WHERE source_doc_id=? AND target_doc_id=?",
                                (src["id"], r["id"])).fetchone()
                if not ex:
                    cx.execute("INSERT INTO policy_supersession (source_doc_id,target_doc_id,reason,"
                               "effective_date,source_url) VALUES (?,?,?,?,?)",
                               (src["id"], r["id"], ("批量废止：" + rule["quote"])[:400],
                                rule["effective_from"], src["source_url"] or ""))
            cx.commit()
            print("  ✓ 自动标记 %d 条失效；留待人工判断 %d 条：" % (len(auto), len(hold)))
            for r in hold:
                print("     待人工：%s | %s" % ((r["doc_number"] or "")[:36], (r["title"] or "")[:50]))
        elif cands:
            print("  （未落库；确认无误后加 --apply 执行；"
                  "本规则自动标记范围：%s）" % ("、".join(rule.get("auto_apply") or []) or "无"))
    return total


def main():
    ap = argparse.ArgumentParser(description="税镜 · 失效回扫")
    ap.add_argument("--supersede", action="store_true", help="扫描废止语句并落库")
    ap.add_argument("--batch-repeal", action="store_true",
                    help="应用『按日期+范围整体废止』规则（默认只列清单）")
    ap.add_argument("--apply", action="store_true", help="配合 --batch-repeal：确认落库")
    ap.add_argument("--dry-run", action="store_true", help="只报告不改库")
    ap.add_argument("--only-src", default="", help="只扫描指定文号的正文（调试用）")
    ap.add_argument("--aging-check", action="store_true", help="官方标废止却仍在依据库")
    ap.add_argument("--report", action="store_true", help="汇总")
    args = ap.parse_args()

    cx = db()
    try:
        if args.aging_check:
            return aging_check(cx)
        if args.report:
            return report(cx)
        if args.batch_repeal:
            return batch_repeal(cx, apply=args.apply and not args.dry_run)
        return scan_supersede(cx, dry_run=args.dry_run, only_src=args.only_src or None)
    finally:
        cx.close()


if __name__ == "__main__":
    main()
