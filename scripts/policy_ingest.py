# -*- coding: utf-8 -*-
"""
税镜 — 政策语料灌入通道（零依赖 · 仅标准库）

把结构化政策 JSON 导入 policy_registry + policy_clause + policy_supersession，
使其成为 RAG 可检索的『现行有效』语料。

三条铁律：
  1. **原文照录**：content_text / clause.content 必须是官方原文（照抄），
     不改写、不概括、不编造；文号/施行日期必须可在 source_url 溯源。
  2. **复核闸门**：默认导入为 pending_review，必须人工复核置 active 才进入检索；
     只有确认无误时才用 --activate 直接生效。
  3. **可升级不推倒**：建表全 CREATE TABLE IF NOT EXISTS，绝不 DROP；重复导入按
     doc_number 幂等更新（同文号覆盖内容），不影响用户资产。

用法：
  python scripts/policy_ingest.py --template                 # 打印 JSON 模板
  python scripts/policy_ingest.py --file data/policies/x.json
  python scripts/policy_ingest.py --dir  data/policies
  python scripts/policy_ingest.py --file x.json --activate    # 确认原文无误后直接生效
  python scripts/policy_ingest.py --list                      # 列出库中政策
  python scripts/policy_ingest.py --stats                     # 统计（政策/条款/待复核）
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "db", "app.sqlite")

# 与 server.py 保持一致的建表语句（IF NOT EXISTS，已存在则为空操作）
SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_registry (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  title           TEXT NOT NULL,
  doc_type        TEXT DEFAULT 'policy',
  issuing_authority TEXT,
  doc_number      TEXT,
  publish_date    TEXT,
  effective_date  TEXT,
  status          TEXT NOT NULL DEFAULT 'active',
  province        TEXT,
  city            TEXT,
  category        TEXT,
  content_text    TEXT,
  source_url      TEXT,
  last_verified_at TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(doc_number)
);
CREATE TABLE IF NOT EXISTS policy_clause (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id           INTEGER NOT NULL,
  clause_no        TEXT NOT NULL,
  content          TEXT NOT NULL,
  clause_status    TEXT NOT NULL DEFAULT 'active',
  invalid_since    TEXT,
  superseded_by    TEXT,
  display_order    INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS policy_supersession (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  source_doc_id     INTEGER NOT NULL,
  target_doc_id     INTEGER,
  target_clause_id  INTEGER,
  reason            TEXT,
  effective_date    TEXT,
  source_url        TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

TEMPLATE = {
    "_说明": [
        "本文件用于向「税镜」灌入政策语料。所有正文必须是官方原文，照抄不改写。",
        "source_url 必须指向可溯源的官方页面（优先 chinatax.gov.cn）。",
        "clauses 可省略；省略时用 content_text 全文作为一条『正文』条款入库。",
        "supersedes 用于声明废止关系（新文件 → 旧文件），也可精确到某一旧条款。",
    ],
    "policies": [
        {
            "title": "（官方标题全文，例如：财政部 税务总局关于明确增值税小规模纳税人减免政策的公告）",
            "doc_number": "财政部 税务总局公告2023年第19号",
            "issuing_authority": "财政部 税务总局",
            "publish_date": "2023-01-09",
            "effective_date": "2023-01-01",
            "category": "增值税",
            "source_url": "https://www.chinatax.gov.cn/...",
            "content_text": "（官方原文全文；如已在 clauses 中逐条给出，此处可留空）",
            "clauses": [
                {"no": "第一条", "content": "（该条官方原文，照抄）", "status": "active"}
            ],
            "supersedes": [
                {
                    "doc_number": "国家税务总局公告2019年第4号",
                    "clause_no": "第二条",
                    "effective_date": "2023-01-01",
                    "reason": "提高免征标准",
                }
            ],
        }
    ],
}


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=30)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA busy_timeout=30000")
    return cx


# 后续版本新增的列。CREATE TABLE IF NOT EXISTS **不会**给已存在的表补列，
# 故必须显式 ALTER；且只做 ADD COLUMN，绝不 DROP / 不改类型（可升级不推倒）。
EXTRA_COLUMNS = {
    "policy_registry": [
        ("official_aging", "TEXT"),         # 官方「时效性」字段（尚未生效/有效/失效…）
        ("official_effectlevel", "TEXT"),   # 官方「效力等级」
        ("official_taxpolicy", "TEXT"),     # 官方「税费类型」
        ("source_meta", "TEXT"),            # 抓取原始元数据 JSON（留痕）
    ],
    # 同行问 M2：声望（从业者的可信度信号，与积分货币分离）
    "users": [
        ("reputation", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def migrate(cx):
    """幂等加列迁移。返回本次实际新增的列名列表。"""
    added = []
    for table, cols in EXTRA_COLUMNS.items():
        have = {r["name"] for r in cx.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for name, decl in cols:
            if name in have:
                continue
            cx.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
            added.append("%s.%s" % (table, name))
    cx.commit()
    return added


def ensure_schema(cx):
    cx.executescript(SCHEMA)
    cx.commit()
    migrate(cx)          # 建表后立即补列（幂等）


def _upsert_policy(cx, p, status):
    doc_number = (p.get("doc_number") or "").strip()
    title = (p.get("title") or "").strip()
    if not doc_number or not title:
        raise ValueError("政策缺少 doc_number 或 title：" + repr(p.get("title")))
    row = cx.execute("SELECT id FROM policy_registry WHERE doc_number=?", (doc_number,)).fetchone()
    fields = dict(
        title=title,
        doc_number=doc_number,          # ⚠️ 必须写入：去重(UNIQUE)与检索依赖它
        doc_type=p.get("doc_type") or "policy",
        issuing_authority=p.get("issuing_authority") or "",
        publish_date=p.get("publish_date") or "",
        effective_date=p.get("effective_date") or "",
        category=p.get("category") or "",
        content_text=p.get("content_text") or "",
        source_url=p.get("source_url") or "",
        last_verified_at=p.get("last_verified_at") or "",
        # 官方元数据（官网抓取时落库；手工语料留空）
        official_aging=p.get("official_aging") or "",
        official_effectlevel=p.get("official_effectlevel") or "",
        official_taxpolicy=p.get("official_taxpolicy") or "",
        source_meta=(json.dumps(p.get("source_meta"), ensure_ascii=False)
                     if p.get("source_meta") else ""),
    )
    if row:
        # ⚠️ 重复抓取**不得把已生效的政策退回待复核**——否则每次重跑都会"撤销"人工复核成果。
        # 仅在抓取方明确给出非待复核状态（如官方标"全文废止"→invalid）时才允许变更状态。
        final_status = status
        if status == "pending_review" and row["status"] in (
                "active", "partially_invalid", "archived", "invalid"):
            final_status = row["status"]
        sets = ",".join(f"{k}=?" for k in fields)
        cx.execute(f"UPDATE policy_registry SET {sets}, status=? WHERE id=?",
                   (*fields.values(), final_status, row["id"]))
        return row["id"], False
    cols = ",".join(fields)
    qs = ",".join("?" * len(fields))
    cur = cx.execute(
        f"INSERT INTO policy_registry ({cols},status) VALUES ({qs},?)",
        (*fields.values(), status))
    return cur.lastrowid, True


def _save_clauses(cx, doc_id, clauses):
    """按 doc_id 刷新条款（同文件重复导入即覆盖，不影响其它文件）。"""
    cx.execute("DELETE FROM policy_clause WHERE doc_id=?", (doc_id,))
    for i, c in enumerate(clauses):
        cx.execute(
            "INSERT INTO policy_clause (doc_id,clause_no,content,clause_status,"
            "invalid_since,superseded_by,display_order) VALUES (?,?,?,?,?,?,?)",
            (doc_id, (c.get("no") or "").strip(), (c.get("content") or "").strip(),
             c.get("status") or "active", c.get("invalid_since"), c.get("superseded_by"), i))


def _save_supersessions(cx, source_doc_number, items):
    """写入废止关系，并同步置旧条款为失效（条款级或整份）。"""
    src = cx.execute("SELECT id FROM policy_registry WHERE doc_number=?",
                     (source_doc_number,)).fetchone()
    if not src:
        return 0
    # 先解析目标，再按"关系对"清空既有记录后重建。
    # 原因：条款每次重导都会重新生成 id，按 clause_id 去重会在重复导入时产生重复关系。
    pairs = []
    for s in items or []:
        tgt_dn = (s.get("doc_number") or "").strip()
        if not tgt_dn:
            continue
        tgt = cx.execute("SELECT id FROM policy_registry WHERE doc_number=?", (tgt_dn,)).fetchone()
        if not tgt:
            print(f"  ! 废止关系跳过：目标文件不在库中 → {tgt_dn}")
            continue
        pairs.append((s, tgt))
    if pairs:
        tids = [t["id"] for _s, t in pairs]
        cx.execute(
            "DELETE FROM policy_supersession WHERE source_doc_id=? AND target_doc_id IN (%s)"
            % ",".join("?" * len(tids)), (src["id"], *tids))

    n = 0
    for s, tgt in pairs:
        eff = s.get("effective_date") or ""
        clause_no = (s.get("clause_no") or "").strip()
        clause_id = None
        if clause_no:
            r = cx.execute("SELECT id FROM policy_clause WHERE doc_id=? AND clause_no=?",
                           (tgt["id"], clause_no)).fetchone()
            clause_id = r["id"] if r else None
            if clause_id is None:
                print(f"  ! 废止关系跳过：{s.get('doc_number')} 无条款 {clause_no}（条款需先导入）")
                continue
        cx.execute(
            "INSERT INTO policy_supersession (source_doc_id,target_doc_id,target_clause_id,"
            "reason,effective_date,source_url) VALUES (?,?,?,?,?,?)",
            (src["id"], tgt["id"], clause_id, s.get("reason") or "", eff,
             s.get("source_url") or ""))
        # 同步置失效
        if clause_id:
            cx.execute("UPDATE policy_clause SET clause_status='invalid', invalid_since=?, "
                       "superseded_by=? WHERE id=?", (eff, source_doc_number, clause_id))
            left = cx.execute("SELECT COUNT(*) c FROM policy_clause WHERE doc_id=? AND clause_status='active'",
                              (tgt["id"],)).fetchone()["c"]
            cx.execute("UPDATE policy_registry SET status=? WHERE id=?",
                       ("partially_invalid" if left else "invalid", tgt["id"]))
        else:
            cx.execute("UPDATE policy_clause SET clause_status='invalid', invalid_since=?, "
                       "superseded_by=? WHERE doc_id=?", (eff, source_doc_number, tgt["id"]))
            cx.execute("UPDATE policy_registry SET status='invalid' WHERE id=?", (tgt["id"],))
        n += 1
    return n


def ingest_payload(cx, payload, activate=False):
    """导入一份 payload（含 policies 数组）。返回 (新增, 更新, 条款数, 废止数)。"""
    default_status = "active" if activate else "pending_review"
    new = upd = ncl = 0
    pols = payload.get("policies") or ([payload] if payload.get("doc_number") else [])
    for p in pols:
        # 条目可自带 status（如已废止档案 status=invalid），否则用默认
        doc_id, is_new = _upsert_policy(cx, p, p.get("status") or default_status)
        new += 1 if is_new else 0
        upd += 0 if is_new else 1
        clauses = p.get("clauses") or []
        if not clauses and (p.get("content_text") or "").strip():
            clauses = [{"no": "正文", "content": p["content_text"].strip()}]
        if clauses:
            _save_clauses(cx, doc_id, clauses)
            ncl += len(clauses)
    cx.commit()
    nsup = 0
    for p in pols:
        if p.get("supersedes"):
            nsup += _save_supersessions(cx, (p.get("doc_number") or "").strip(), p["supersedes"])
    cx.commit()
    return new, upd, ncl, nsup


def ingest_path(cx, path, activate=False):
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    print(f"→ 导入 {os.path.relpath(path, ROOT)}")
    new, upd, ncl, nsup = ingest_payload(cx, payload, activate)
    print(f"   新增 {new} 条 / 更新 {upd} 条 / 条款 {ncl} 条 / 废止关系 {nsup} 条"
          f"（状态：{'active' if activate else 'pending_review'}）")
    return new, upd, ncl, nsup


def _cmd_list(cx, limit=None):
    sql = ("SELECT p.id,p.doc_number,p.title,p.status,p.category,p.effective_date,"
           "p.official_aging,"
           "(SELECT COUNT(*) FROM policy_clause c WHERE c.doc_id=p.id) AS n_all,"
           "(SELECT COUNT(*) FROM policy_clause c WHERE c.doc_id=p.id "
           " AND c.clause_status='active') AS n_act "
           "FROM policy_registry p ORDER BY p.status, p.effective_date DESC")
    rows = cx.execute(sql).fetchall()
    if not rows:
        print("（库中暂无政策）")
        return
    print(f"共 {len(rows)} 条：" + (f"（仅显示前 {limit}）" if limit else ""))
    for r in rows[:limit] if limit else rows:
        aging = f" 官方:{r['official_aging']}" if r["official_aging"] else ""
        print(f"  [{r['status']:<16}] {(r['doc_number'] or '(无文号)'):<42} "
              f"条款{r['n_act']}/{r['n_all']:<3} {(r['category'] or ''):<10}{aging} {r['title'][:30]}")


def _cmd_stats(cx):
    def c(sql, *a):
        return cx.execute(sql, a).fetchone()[0]
    total = c("SELECT COUNT(*) FROM policy_registry")
    active = c("SELECT COUNT(*) FROM policy_registry WHERE status='active'")
    pend = c("SELECT COUNT(*) FROM policy_registry WHERE status='pending_review'")
    cls = c("SELECT COUNT(*) FROM policy_clause")
    cls_act = c("SELECT COUNT(*) FROM policy_clause WHERE clause_status='active'")
    sup = c("SELECT COUNT(*) FROM policy_supersession")
    with_clause = c("SELECT COUNT(DISTINCT doc_id) FROM policy_clause")
    print("政策语料统计")
    print(f"  政策总数      : {total}")
    print(f"  其中 active   : {active}（可被 RAG 检索）")
    print(f"  其中待复核    : {pend}")
    print(f"  含条款的政策  : {with_clause}")
    print(f"  条款总数      : {cls}（有效 {cls_act}）")
    print(f"  废止关系      : {sup}")
    agings = cx.execute(
        "SELECT IFNULL(NULLIF(official_aging,''),'(未标)') a, COUNT(*) c "
        "FROM policy_registry GROUP BY a ORDER BY c DESC").fetchall()
    if agings:
        print("  官方时效性    : " + " / ".join(f"{r['a']}={r['c']}" for r in agings))
    if total and with_clause * 2 < total:
        print("  ⚠️ 提示：超过一半的政策没有条款 → RAG 检索不到条文，建议补 clauses 或用 content_text 全文。")


def main():
    ap = argparse.ArgumentParser(description="税镜 · 政策语料灌入通道")
    ap.add_argument("--file", help="单个 JSON 文件路径")
    ap.add_argument("--dir", help="目录（导入其中所有 *.json）")
    ap.add_argument("--activate", action="store_true", help="直接置为 active（确认原文无误才用）")
    ap.add_argument("--template", action="store_true", help="打印 JSON 模板")
    ap.add_argument("--list", action="store_true", help="列出库中政策")
    ap.add_argument("--stats", action="store_true", help="统计")
    args = ap.parse_args()

    if args.template:
        print(json.dumps(TEMPLATE, ensure_ascii=False, indent=2))
        return
    cx = db()
    ensure_schema(cx)
    if args.list:
        _cmd_list(cx)
    elif args.stats:
        _cmd_stats(cx)
    elif args.file or args.dir:
        files = []
        if args.file:
            files.append(args.file)
        if args.dir:
            files += sorted(glob.glob(os.path.join(args.dir, "*.json")))
        if not files:
            print("未找到可导入的 JSON 文件")
            sys.exit(1)
        tn = tu = tc = ts = 0
        for f in files:
            n, u, c, s = ingest_path(cx, f, args.activate)
            tn += n; tu += u; tc += c; ts += s
        print(f"\n合计：新增 {tn} / 更新 {tu} / 条款 {tc} / 废止 {ts}")
        if not args.activate:
            print("提示：以上为 pending_review，需在运营后台「政策复核」中点『复核生效』，或重跑本脚本加 --activate。")
        _cmd_stats(cx)
    else:
        ap.print_help()
    cx.close()


if __name__ == "__main__":
    main()
