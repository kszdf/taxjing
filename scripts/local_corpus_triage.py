# -*- coding: utf-8 -*-
"""
税镜 — 本地财税文件分流（零依赖 · 仅标准库）

**政策库只放官方原文**。本地文件里没有完整政策原文（实测：全是解读稿/案例/培训/数据），
所以本脚本按性质**分流**，各归其位：

  B 类 解读稿/公众号稿 → 只抽**文号线索**（产出待补清单，回官网取原文）
  C 类 案例/稽查/争议 → 结构化入 `case_lib`（案例库）
  速查清单           → 转 `org_standard_answer` 草稿（机构口径库）
  D 类 培训/教材     → 仅登记（暂不入库）
  E 类 数据(xlsx/csv) → 仅登记

解析能力（零依赖）：
  .md/.txt/.json/.csv 直接读；**.docx 用 zipfile 读 word/document.xml + 正则去标签**（不能用 python-docx）；
  .pdf/.xlsx 不解析，只登记（正文在附件里的情形一律交人工）。

运行：
    python scripts/local_corpus_triage.py --scan            # 只扫描分类，出报告
    python scripts/local_corpus_triage.py --docnumbers      # 抽文号线索 → data/policies/_triage/
    python scripts/local_corpus_triage.py --cases           # 案例入 case_lib
    python scripts/local_corpus_triage.py --quickref        # 速查清单 → 机构口径库草稿
    python scripts/local_corpus_triage.py --all             # 以上全做
"""
from __future__ import annotations
import argparse
import glob
import html as html_mod
import json
import os
import re
import sqlite3
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "db", "app.sqlite")
OUT_DIR = os.path.join(ROOT, "data", "policies", "_triage")

DEFAULT_ROOTS = [r"D:\WorkBuddy"]
SKIP_DIRS = {".git", "node_modules", ".workbuddy", "binaries", "__pycache__",
             "dist", "build", ".venv", "venv", "miniprogram", "db",
             ".codebuddy", ".learnings", ".claude", ".idea", ".vscode", "skills"}
SKIP_DIR_PREFIX = ("automation-", "~$", ".")
TEXT_EXT = {".md", ".txt", ".json", ".csv"}
DOCX_EXT = {".docx"}
SKIP_EXT = {".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".zip", ".pptx"}

# 文号正则（覆盖多格式）
DOC_NO_PATTERNS = [
    r'(?:财政部\s*税务总局|国家税务总局|国务院|财政部|税务总局|海关总署)\s*(?:公告|令)\s*\d{4}\s*年第\s*\d+\s*号',
    r'财税\s*〔\s*\d{4}\s*〕\s*\d+\s*号',
    r'税总(?:发|函|公告|办发)\s*〔\s*\d{4}\s*〕\s*\d+\s*号',
    r'国税(?:发|函)\s*〔\s*\d{4}\s*〕\s*\d+\s*号',
    r'〔\s*\d{4}\s*〕\s*\d+\s*号',
]
DOC_NO_RE = re.compile("|".join(DOC_NO_PATTERNS))

# 分类关键词（**以文件名为准**，内容仅作复核，避免把"运营方案/口播稿"误判成案例）
NAME_QUICKREF = ("速查清单",)
NAME_CASE = ("案例", "稽查", "风险手册", "风险清单", "税企争议")
NAME_TRAIN = ("税法一", "税法二", "涉税服务实务", "涉税服务相关法律", "财务与会计",
              "讲义", "逐字稿", "题库", "教材", "思维导图")
NAME_INTERP = ("解读", "速查", "公众号", "留资", "hotspot", "政策梳理", "政策速递",
               "完全指南", "攻略")
# 明显不是语料素材的（项目文档/运营方案等）
NAME_EXCLUDE = ("部署", "指南", "操作手册", "运营方案", "获客", "转化闭环", "交付清单",
                "产品方案", "结构", "README", "方案_", "工作计划", "排期",
                "口播", "脚本", "内容日历", "封面", "资料包", "运营资料")


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    cx = sqlite3.connect(DB_PATH)
    cx.row_factory = sqlite3.Row
    return cx


# ============================ 读取（零依赖） ============================
def read_docx(path):
    """零依赖解析 .docx：zipfile 读 word/document.xml，去标签、段落转换行。"""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception as e:
        return ""
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    xml = re.sub(r'<[^>]+>', '', xml)
    return html_mod.unescape(xml)


def read_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except Exception:
                continue
        return ""
    if ext in DOCX_EXT:
        return read_docx(path)
    return ""


def iter_files(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and not any(d.startswith(p) for p in SKIP_DIR_PREFIX)
                           and not d.startswith("~$")]
            for fn in filenames:
                if fn.startswith("~$"):
                    continue
                yield os.path.join(dirpath, fn)


def is_tax_related(path, text):
    name = os.path.basename(path)
    hay = name + "\n" + (text[:4000] if text else "")
    keys = ("税", "财税", "发票", "增值", "所得税", "申报", "稽查", "社保", "印花",
            "出口退", "留抵", "核定", "纳税")
    return any(k in hay for k in keys)


def classify(path, text):
    """以**文件名**为主判据，内容只用于复核。宁可漏判（归 ?）也不误判。"""
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    ext = os.path.splitext(path)[1].lower()
    if name == "parsed_cases.json":
        return "C"
    if ext in (".xlsx", ".xls", ".csv"):
        return "E"
    if any(k in stem for k in NAME_EXCLUDE):
        return "?"
    if any(k in stem for k in NAME_QUICKREF):
        return "QUICKREF"
    if any(k in stem for k in NAME_TRAIN) or "注册税务师考试" in path:
        return "D"
    if any(k in stem for k in NAME_CASE):
        head = (text or "")[:8000]
        # 内容也要像案例，否则只是名字里带"案例"（口播稿/运营方案）
        if ("行业：" in head) or re.search(r'(?m)^###\s*案例', head) \
                or re.search(r'(?m)^\*\*[A-Za-z]{2,8}[\-－]\w+\d{2,4}', head) \
                or ("【案例故事】" in head) or ("案例" in head[:1200]):
            return "C"
        return "?"
    if any(k in stem for k in NAME_INTERP):
        return "B"
    return "?"


def extract_doc_numbers(text):
    out = []
    for m in DOC_NO_RE.finditer(text or ""):
        s = re.sub(r'\s+', '', m.group(0))
        if s and s not in out:
            out.append(s)
    return out


# ============================ 案例解析 ============================
def parse_cases_formatA(text):
    """格式A：`**ID：标题**` 后跟 `- 行业：/场景：/预警指标：/法律依据：/实战点评：/合规建议：/风险等级：`"""
    cases = []
    blocks = re.split(r'(?m)^(?=\*\*[A-Za-z\-]{2,20}\d{2,4}\s*[:：])', text)
    for b in blocks:
        m = re.match(r'\*\*([^*]{4,120})\*\*', b.strip())
        if not m:
            continue
        head = m.group(1).strip()
        if "：" in head:
            cid, title = head.split("：", 1)
        elif ":" in head:
            cid, title = head.split(":", 1)
        else:
            cid, title = "", head
        f = {}
        for km in re.finditer(r'(?m)^-\s*([^：:\n]{2,12})\s*[：:]\s*(.+)$', b):
            f[km.group(1).strip()] = km.group(2).strip()
        cases.append({
            "case_id": cid.strip(), "title": title.strip(),
            "industry": f.get("行业", ""), "scene": f.get("场景", ""),
            "warning": f.get("预警指标", ""), "law": f.get("法律依据", ""),
            "comment": f.get("实战点评", ""), "advice": f.get("合规建议", ""),
            "risk": f.get("风险等级", ""),
        })
    return cases


def parse_cases_formatB(text):
    """格式B：`### 案例N：标题` + `**情景描述：**` 等段落。"""
    cases = []
    blocks = re.split(r'(?m)^(?=###\s*案例\d+\s*[:：])', text)
    for b in blocks:
        m = re.match(r'###\s*案例\d+\s*[:：]\s*(.+)', b.strip())
        if not m:
            continue
        title = m.group(1).strip()
        scene = ""
        ms = re.search(r'\*\*情景描述[：:]\*\*\s*\n?(.{0,800}?)(?=\n\*\*|\Z)', b, re.S)
        if ms:
            scene = re.sub(r'\s+', ' ', ms.group(1)).strip()
        cases.append({
            "case_id": "", "title": title, "industry": "", "scene": scene,
            "warning": "", "law": "", "comment": "", "advice": "", "risk": "",
            "raw": b.strip()[:3000],
        })
    return cases


# 通用切块：二级标题 / "### 案例N：" / 独立成行的 **编号：标题**
_CASE_SPLIT = re.compile(r'(?m)^(?=##\s+|###\s*案例\d+|\*\*[A-Za-z][^*\n]{3,110}\*\*\s*$)')
_CASE_MARKERS = ("【案例故事】", "【情景描述】", "行业：", "行业:", "风险等级", "实战点评",
                 "合规建议", "法律依据", "预警指标", "场景：", "场景:", "情景描述")
_SKIP_HEADS = ("目录", "说明", "前言", "作者简介", "交付清单", "附录", "案例目录",
               "税务风险案例示范", "高频税务风险特征", "案例库产品方案")


def parse_cases_auto(text):
    """通用案例解析：自动识别 4 种已知排版，输出统一结构。
    （案例库不是"政策原文"，允许摘录与归纳；但仍尽量原样保留正文。）"""
    cases = []
    for block in _CASE_SPLIT.split(text or ""):
        b = (block or "").strip()
        if len(b) < 80:
            continue
        head = b.split("\n", 1)[0].strip()
        head_txt = re.sub(r'^#{2,4}\s*', '', head)
        head_txt = head_txt.strip().strip("*").strip()
        if any(h in head_txt for h in _SKIP_HEADS):
            continue
        if ("|" in head and "编号" in head):      # 目录表格行
            continue
        score = sum(1 for k in _CASE_MARKERS if k in b[:6000])
        if score < 2:
            continue
        cid, title = "", head_txt
        m = re.match(r'^([A-Za-z][A-Za-z\-－]{1,22}\d{0,4})\s*[：:｜|]\s*(.+)$', head_txt)
        if m:
            cid, title = m.group(1), m.group(2)
        else:
            m2 = re.match(r'^(?:案例\s*\d+)\s*[：:]\s*(.+)$', head_txt)
            if m2:
                title = m2.group(1)
        f = {}
        for km in re.finditer(r'\*\*\s*([^：:*\n]{2,10})\s*[：:]\s*\*\*\s*([^\n]*)', b):
            f.setdefault(km.group(1).strip(), km.group(2).strip())
        for km in re.finditer(r'(?m)^-\s*([^：:\n]{2,10})\s*[：:]\s*(.+)$', b):
            f.setdefault(km.group(1).strip(), km.group(2).strip())

        def sec(*names):
            for nm in names:
                mm = re.search(r'[【\[]' + nm + r'[^】\]]*[】\]]\s*(.{0,1600}?)(?=\n\s*[【\[]|\n#{2,4}\s|\Z)',
                               b, re.S)
                if mm:
                    return re.sub(r'\s+', ' ', mm.group(1)).strip()
            return ""

        subs = _subsections(b)

        def sub(*keys):
            for k, v in subs.items():
                if any(x in k for x in keys):
                    return v
            return ""

        cases.append({
            "case_id": cid,
            "title": title[:120],
            "industry": f.get("行业", ""),
            "scene": (sec("案例故事", "情景描述") or f.get("场景", "")
                      or sub("发生了什么", "案情", "情景", "背景")),
            "warning": f.get("预警指标", "") or sub("怎么发现", "预警", "识别"),
            "law": sec("法律依据", "依据") or sub("法律依据", "依据"),
            "comment": sec("实战点评", "点评", "处理", "应对") or sub("点评", "分析", "处理"),
            "advice": sec("合规建议", "建议") or sub("建议", "应对", "防范"),
            "risk": f.get("风险等级", "") or sub("风险等级"),
            "raw": b[:3000],
        })
    return cases


def _subsections(b):
    """抓 `### 标题\\n正文` 形式的分节（如"### 一、发生了什么"）。"""
    out = {}
    for m in re.finditer(r'(?m)^#{3,4}[ \t]*(.+?)[ \t]*$\n(.*?)(?=^#{3,4}[ \t]|\Z)', b, re.S):
        k = m.group(1).strip()
        v = re.sub(r'\s+', ' ', m.group(2)).strip()
        if k and v:
            out[k] = v
    return out


def load_parsed_cases(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(d, list):
        return []
    out = []
    for c in d:
        if not isinstance(c, dict):
            continue
        out.append({
            "case_id": c.get("id") or "",
            "title": c.get("title") or "",
            "industry": (re.search(r'[\[【]([^\]】]{2,8})[\]】]', c.get("title") or "") or
                         [None, c.get("tax_class") or ""])[1],
            "scene": c.get("story") or "",
            "warning": c.get("warning") or "",
            "law": c.get("law") or "",
            "comment": c.get("comment") or "",
            "advice": c.get("advice") or "",
            "risk": c.get("risk") or "",
            "tax": c.get("tax") or "",
        })
    return out


def save_cases(cx, cases, source):
    """入 case_lib（按 title 幂等去重）。"""
    cur = cx.cursor()
    new = skip = 0
    for c in cases:
        title = (c.get("title") or "").strip()
        if len(title) < 4:
            skip += 1
            continue
        exist = cur.execute("SELECT id FROM case_lib WHERE title=?", (title,)).fetchone()
        if exist:
            skip += 1
            continue
        parts = []
        if c.get("scene"):
            parts.append("【场景】" + c["scene"])
        if c.get("warning"):
            parts.append("【预警指标】" + c["warning"])
        if c.get("comment"):
            parts.append("【实战点评】" + c["comment"])
        summary = "\n".join(parts)[:4000] or (c.get("raw") or "")[:4000]
        rp = []
        if c.get("risk"):
            rp.append("风险等级：" + c["risk"])
        if c.get("advice"):
            rp.append("【合规建议】" + c["advice"])
        if c.get("law"):
            rp.append("【依据】" + c["law"])
        cur.execute(
            "INSERT INTO case_lib (title,category,industry,region,penalty_amount,"
            "publish_date,source,summary,risk_points) VALUES (?,?,?,?,?,?,?,?,?)",
            (title, c.get("tax") or c.get("industry") or "涉税风险",
             c.get("industry") or "", "", "",
             "", source, summary, "\n".join(rp)[:3000]))
        new += 1
    cx.commit()
    return new, skip


def save_quickref(cx, path, text):
    """速查清单 → org_standard_answer 草稿（机构口径库）。"""
    org = cx.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
    if not org:
        return 0, "库中无机构，跳过（需先在运营后台开户）"
    org_id = org["id"]
    dn = extract_doc_numbers(text) or []
    n = 0
    for m in re.finditer(r'(?m)^\*\*优惠\d+[：:]\s*(.+?)\*\*\s*\n((?:-[^\n]*\n?)+)', text):
        title = m.group(1).strip()
        body = m.group(2).strip()
        pat = re.sub(r'[^\u4e00-\u9fff]{1,}', '', title)[:20]
        if not pat:
            continue
        exist = cx.execute("SELECT id FROM org_standard_answer WHERE org_id=? AND question_pattern=?",
                           (org_id, pat)).fetchone()
        if exist:
            continue
        cx.execute(
            "INSERT INTO org_standard_answer (org_id,category,question_pattern,answer_md,"
            "policy_refs,steps,status,approved_by) VALUES (?,?,?,?,?,?, 'draft', ?)",
            (org_id, "税收优惠", pat, title + "\n" + body,
             json.dumps(dn, ensure_ascii=False), "", "本地速查清单导入(待审定)"))
        n += 1
    cx.commit()
    return n, "已导入 %d 条草稿（status=draft，需审定后才生效）" % n


# ============================ 主流程 ============================
def scan(roots):
    report = {"B": [], "C": [], "D": [], "E": [], "QUICKREF": [], "?": [], "skipped": 0}
    for p in iter_files(roots):
        ext = os.path.splitext(p)[1].lower()
        if ext in SKIP_EXT:
            report["skipped"] += 1
            continue
        if ext not in TEXT_EXT and ext not in DOCX_EXT:
            report["skipped"] += 1
            continue
        try:
            if os.path.getsize(p) > 3 * 1024 * 1024:
                report["skipped"] += 1
                continue
        except Exception:
            continue
        text = read_text(p)
        if not text or not is_tax_related(p, text):
            report["skipped"] += 1
            continue
        k = classify(p, text)
        report[k].append(p)
    return report


def main():
    ap = argparse.ArgumentParser(description="税镜 · 本地财税文件分流")
    ap.add_argument("--root", action="append", default=[], help="扫描根目录（可多次）")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--docnumbers", action="store_true")
    ap.add_argument("--cases", action="store_true")
    ap.add_argument("--quickref", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    roots = args.root or DEFAULT_ROOTS
    do_all = args.all or not (args.scan or args.docnumbers or args.cases or args.quickref)

    rep = scan(roots)
    print("=== 扫描结果（分类统计）===")
    for k in ("QUICKREF", "B", "C", "D", "E", "?"):
        print("  %-9s %d 个" % (k, len(rep[k])))
    print("  跳过 %d 个（非文本/超大/无关）" % rep["skipped"])

    if args.scan and not do_all:
        print("\n=== 明细（最多各 15）===")
        for k in ("QUICKREF", "B", "C"):
            for p in rep[k][:15]:
                print("  [%s] %s" % (k, os.path.relpath(p, roots[0])))
        return

    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- ① 文号线索（来自 B 类解读稿 + 速查清单）----
    if do_all or args.docnumbers:
        leads = {}
        for p in rep["B"] + rep["QUICKREF"]:
            text = read_text(p)
            for dn in extract_doc_numbers(text):
                leads.setdefault(dn, []).append(os.path.basename(p))
        out = os.path.join(OUT_DIR, "pending_doc_numbers.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({k: v[:5] for k, v in sorted(leads.items(), key=lambda x: -len(x[1]))},
                      f, ensure_ascii=False, indent=2)
        print("\n=== ① 文号线索 ===")
        print("  抽出 %d 个不同文号 → %s" % (len(leads), out))
        for dn, srcs in list(leads.items())[:8]:
            print("    %s  ← %s" % (dn, srcs[0][:40]))
        print("  → 下一步：用 scripts/policy_sources.py 或手工，按这份清单回官网取原文入库。")

    cx = db()
    if do_all or args.cases:
        print("\n=== ② 案例入 case_lib ===")
        total_new = total_skip = 0
        for p in rep["C"]:
            src = os.path.relpath(p, roots[0])
            cases = []
            if os.path.basename(p) == "parsed_cases.json":
                cases = load_parsed_cases(p)
            else:
                cases = parse_cases_auto(read_text(p))
            if not cases:
                continue
            n, s = save_cases(cx, cases, src)
            total_new += n
            total_skip += s
            if n:
                print("  +%d 条 ← %s" % (n, src[:56]))
        print("  合计新增 %d 条，已存在跳过 %d 条" % (total_new, total_skip))

    if do_all or args.quickref:
        print("\n=== ③ 速查清单 → 机构口径库草稿 ===")
        for p in rep["QUICKREF"]:
            n, msg = save_quickref(cx, p, read_text(p))
            print("  %s：%s" % (os.path.basename(p)[:40], msg))

    print("\n=== 数据库现状 ===")
    print("  case_lib:", cx.execute("SELECT COUNT(*) c FROM case_lib").fetchone()["c"])
    print("  org_standard_answer:",
          cx.execute("SELECT COUNT(*) c FROM org_standard_answer").fetchone()["c"])
    cx.close()


if __name__ == "__main__":
    main()
