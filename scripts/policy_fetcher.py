# -*- coding: utf-8 -*-
"""
慧根堂·财税AI智库 — 联网政策抓取管道 (M4b)
====================================================================
职责：定期/手动从权威源抓取最新税收政策，解析文号/施行日/废止关系，
      写入 policy_registry（默认 pending_review 待人工复核），
      并生成「新政策简报」供运营复核后生效。

设计要点（与方案第二十章一致）：
  - 双源：国家税务总局(主源) + 税屋网(辅源，需反爬处理)。
  - 联网自动更新：live 模式真实抓取；seed 模式用真实文号样条验证全链路。
  - 时效裁决：复用 policy_engine 的 supersession 逻辑，自动标注失效/替代关系。
  - 安全闸：抓取结果一律先入 pending_review，绝不自动置为现行有效——
    重大废止/变更必须人工确认（方案20.3"自动入库每日汇总成简报推复核"）。
  - 零依赖：仅标准库（urllib + re + sqlite3 + json + datetime）。

运行：
    python scripts/policy_fetcher.py --probe           # 探测各源可达性
    python scripts/policy_fetcher.py --live            # 真实抓取 chinatax 抽候选入库
    python scripts/policy_fetcher.py --seed            # 用真实样条验证入库+supersession+简报
    python scripts/policy_fetcher.py --brief           # 打印当前待复核简报
"""
import sys
import os
import re
import json
import sqlite3
import datetime
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import policy_engine as pe

DB_PATH = os.path.join(ROOT, "db", "app.sqlite")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ----------------------------------------------------------------------
# 源配置：主源真实可达；税屋网有 WAF 挑战，生产需 cookie/代理/渲染
# ----------------------------------------------------------------------
SOURCES = [
    {
        "name": "国家税务总局",
        "url": "https://www.chinatax.gov.cn/",
        "parser": "chinatax",
        "note": "主源·权威。门户首页可抓，生产替换为具体政策法规频道/公报 RSS API。",
    },
    {
        "name": "税屋网",
        "url": "https://www.shui5.cn/article/3",
        "parser": "shui5",
        "note": "辅源·被阿里云WAF拦截(cookie挑战)，生产需cookie/代理/无头渲染。",
    },
]

# 真实样条：取自国家税务总局近年真实公告文号，用于离线验证全链路
SEED_ITEMS = [
    {
        "title": "财政部 税务总局关于先进制造业企业增值税加计抵减政策的公告",
        "doc_number": "财政部 税务总局公告2023年第43号",
        "publish_date": "2023-09-03",
        "effective_date": "2023-01-01",
        "source": "seed/国家税务总局",
        "url": "https://www.chinatax.gov.cn/seed/2023-43",
        "summary": "允许先进制造业企业按照当期可抵扣进项税额加计5%抵减应纳增值税。",
    },
    {
        "title": "财政部 税务总局关于增值税小规模纳税人减免政策的公告",
        "doc_number": "财政部 税务总局公告2023年第19号",
        "publish_date": "2023-08-01",
        "effective_date": "2023-01-01",
        "source": "seed/国家税务总局",
        "url": "https://www.chinatax.gov.cn/seed/2023-19",
        "summary": "小规模纳税人月销售额10万以下免征增值税；3%征收率减按1%。",
        # 演示 supersession：明确废止 2019年第4号第二条
        "supersedes": [{"doc_no": "财政部 税务总局公告2019年第4号",
                        "clause": "第二条", "mode": "invalid"}],
    },
]


def db():
    cx = sqlite3.connect(DB_PATH)
    cx.row_factory = sqlite3.Row
    return cx


def ensure_schema():
    """建表须与 server.py 的 policy_registry 完全一致（不冲突、不重建）。
    此处 CREATE IF NOT EXISTS 仅在独立运行(未先启 server)时生效。"""
    cx = db(); cur = cx.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS policy_registry (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        doc_type        TEXT DEFAULT 'policy',
        issuing_authority TEXT,
        doc_number      TEXT,
        publish_date    TEXT,
        effective_date  TEXT,
        status          TEXT NOT NULL DEFAULT 'pending_review',
        province        TEXT,
        city            TEXT,
        category        TEXT,
        content_text    TEXT,
        source_url      TEXT,
        last_verified_at TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(doc_number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS policy_brief (
        id INTEGER PRIMARY KEY AUTOINCREMENT, brief_date TEXT, items TEXT, status TEXT DEFAULT 'pending')""")
    cx.commit(); cx.close()


def fetch_url(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", "ignore")


def parse_chinatax(html):
    """从栏目页抽取政策候选链接。门户页结构复杂，尽力而为地抽 <a> 标题。"""
    items = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html, re.S | re.I):
        href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not text or len(text) < 6:
            continue
        if not re.search(r"公告|政策|税务|规定|办法|通知|文号", text):
            continue
        if href.startswith("/"):
            href = "https://www.chinatax.gov.cn" + href
        items.append({"title": text, "url": href, "source": "国家税务总局(live)"})
    # 去重
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"]); out.append(it)
    return out


def parse_shui5(html):
    """税屋网：被 WAF 拦截时返回 challenge 页，无法解析真实列表。"""
    if "acw_sc__v2" in html or "aliyun_waf" in html:
        return {"_challenge": True, "items": []}
    items = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html, re.S | re.I):
        href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not text or len(text) < 6:
            continue
        if re.search(r"公告|政策|税务|财税", text):
            items.append({"title": text, "url": href, "source": "税屋网(live)"})
    return {"_challenge": False, "items": items}


def ingest(items, status="pending_review"):
    """写入 policy_registry（默认待复核）。返回新插入条数。"""
    cx = db(); cur = cx.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new = 0
    for it in items:
        doc_no = it.get("doc_number") or it.get("doc_no")
        if doc_no:
            exist = cur.execute("SELECT id FROM policy_registry WHERE doc_number=?",
                                (doc_no,)).fetchone()
            if exist:
                continue
        # ⚠️ 修正历史 bug：原实现把 it["source"] 塞进 source_url、it["summary"] 塞进 content_text，
        # 导致所有抓取结果**正文恒为空**（这正是"抓了 127 条却 0 条款"的直接原因之一）。
        cur.execute(
            "INSERT INTO policy_registry (title,doc_number,publish_date,effective_date,"
            "status,source_url,content_text,last_verified_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (it.get("title"), doc_no, it.get("publish_date"), it.get("effective_date"),
             status, it.get("url") or it.get("source") or "",
             it.get("content_text") or it.get("summary") or "", now))
        new += 1
    cx.commit(); cx.close()
    return new


def apply_supersession():
    """废止关系统一由 `policy_ingest.ingest_payload` 在导入时写入 `policy_supersession`
    并同步置旧条款失效；此处**不再**自行处理。

    ⚠️ 历史 bug：原实现查询 `policy_registry.supersession` —— 该列**并不存在**（schema 里没有），
    一旦调用必然 `sqlite3.OperationalError`。为避免"看着能用、一跑就炸"，改为显式空实现。
    """
    return 0


def generate_brief():
    cx = db(); cur = cx.cursor()
    pending = cur.execute(
        "SELECT id,title,doc_number,status,publish_date,effective_date,source_url,content_text "
        "FROM policy_registry WHERE status='pending_review' ORDER BY id DESC").fetchall()
    items = [dict(r) for r in pending]
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    cur.execute("INSERT INTO policy_brief (brief_date,items,status) VALUES (?,?,?)",
                (now, json.dumps(items, ensure_ascii=False), "pending"))
    cx.commit(); cx.close()
    return items


def cmd_probe():
    print("=== 源可达性探测 ===")
    for s in SOURCES:
        try:
            html = fetch_url(s["url"], timeout=10)
            size = len(html)
            flag = "可达" if size > 500 else "返回异常"
            print(f"[{flag}] {s['name']}  {s['url']}  ({size} bytes)  {s['note']}")
        except urllib.error.HTTPError as e:
            print(f"[HTTP {e.code}] {s['name']}  {s['url']}  {s['note']}")
        except Exception as e:
            print(f"[失败: {type(e).__name__}] {s['name']}  {s['url']}  {s['note']}")


def cmd_live(trial=50):
    """真实联网抓取（兼容入口）。

    ⚠️ 已改为**走政策法规库定向抓取** `policy_sources`：
    原先"抓门户首页 + 只抽 <a> 标题"的做法只会捞到新闻/会议稿
    （历史教训：127 条噪声、0 条款、0 正文），现仅作兼容转发。
    """
    try:
        import policy_sources as ps
    except Exception as e:
        print("  ✗ 无法加载 policy_sources：", e)
        return 0
    print("=== 真实联网抓取（国家税务总局政策法规库·定向）===")
    return ps.run(trial=trial, pages=4, resume=True)


def cmd_seed():
    print("=== 离线样条验证（真实文号，验证入库+简报+supersession逻辑）===")
    ensure_schema()
    n = ingest(SEED_ITEMS)
    items = generate_brief()
    print(f"  插入 {n} 条政策候选(待复核)；待复核简报 {len(items)} 条：")
    for it in items:
        print(f"   - {it['doc_number']}  {it['title'][:30]}  [{it['status']}]")
    # 时效/废止裁决逻辑由 policy_engine 在内存验证（不触碰 DB 列结构差异）
    print("\n  裁决逻辑验证（policy_engine.self_test：部分失效 + 新优于旧 + 免责注入）：")
    try:
        pe.self_test()
    except AssertionError as e:
        print("  ✗ 自测失败:", e)


def cmd_brief():
    ensure_schema()
    cx = db(); cur = cx.cursor()
    rows = cur.execute(
        "SELECT id,title,doc_number,source_url,publish_date FROM policy_registry "
        "WHERE status='pending_review' ORDER BY id DESC").fetchall()
    cx.close()
    print(f"=== 当前待复核政策 {len(rows)} 条 ===")
    for r in rows:
        print(f"  [{r['id']}] {r['doc_number'] or '(无文号)'} | {r['title'][:28]} | {r['source_url']}")


if __name__ == "__main__":
    ensure_schema()
    args = sys.argv[1:]
    if "--probe" in args:
        cmd_probe()
    elif "--live" in args:
        cmd_live()
    elif "--seed" in args:
        cmd_seed()
    elif "--brief" in args:
        cmd_brief()
    else:
        print("用法:")
        print("  --probe   探测各源可达性")
        print("  --live    真实抓取 chinatax 抽候选入库(待复核)")
        print("  --seed    用真实文号样条验证入库+supersession+简报")
        print("  --brief   打印当前待复核简报")
