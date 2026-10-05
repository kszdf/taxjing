# -*- coding: utf-8 -*-
"""
税镜 — 官网政策语料定向抓取（零依赖 · 仅标准库）

数据源：国家税务总局**政策法规库** `fgk.chinatax.gov.cn`（结构化 JSON + 静态详情页）。

为什么不用首页抓取：门户首页全是新闻/会议稿（历史教训：抓了 127 条噪声、0 条款）。
法规库有结构化接口，**官方自带文号 / 时效性 / 税费类型**，可直接落库。

## 已实测的事实
- 列表接口：`POST https://www.chinatax.gov.cn/getFileListByCodeId`
    body: `codeId=&channelId=<cid>&page=1&size=50` → `{"code":200,"results":{"data":{...}}}`
- 每条 result：`url / title / subTitleHtml / publishedTimeStr / channelName / domainMetaList[]`
- `domainMetaList[].resultList[]` 里的 `writtentext`(发文字号)、`aging`(时效性)、
  `taxpolicy`(税费类型)、`effectlevel`(效力等级)、`writtendate`(成文日期) 等 → 用 `meta_map()` 拍平。
- ⚠️ **接口返回的 url 主机名是 `www.chinatax.gov.cn`，那是 404**；
  详情页真实主机是 **`fgk.chinatax.gov.cn`** —— `fix_detail_url()` 负责改写。
- 详情页正文容器：`<div class="arc_cont">` … 结束于 `<div class="bot-btns-box">` 之前。

## 铁律
- **原文照录**：正文只去 HTML 标签与多余空白，**不改写、不概括、不补写**。
- **默认待复核**：一律 `pending_review`，人工复核后才生效（前 50 条逐条人工核对原文）。
- 官方标「失效/废止」的**不抓正文**，只存目录行做时效图谱。

运行：
    python scripts/policy_sources.py --probe
    python scripts/policy_sources.py --list-channels
    python scripts/policy_sources.py --topic 增值税,企业所得税 --trial 50 --dry-run
    python scripts/policy_sources.py --topic 增值税,企业所得税 --trial 50
    python scripts/policy_sources.py --resume --pages 5
"""
from __future__ import annotations
import argparse
import datetime
import html as html_mod
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import policy_ingest as pi  # noqa: E402  语料灌入通道

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
API = "https://www.chinatax.gov.cn/getFileListByCodeId"
DETAIL_HOST = "https://fgk.chinatax.gov.cn"
INBOX_DIR = os.path.join(ROOT, "data", "policies", "inbox")
CKPT_PATH = os.path.join(ROOT, "data", "policies", ".checkpoint.json")
SIZE = 50

# 栏目（已实测；其余栏目 channelId 需从列表页 var channelId="…" 提取）
CHANNELS = {
    "税务规范性文件": {"channelId": "470b437b304f434396500a1e2edc7f28", "code": "c100012", "total": 1924, "aging": True},
    "财税文件":      {"channelId": "2cb303fdee614232b79552d52bb057d6", "code": "c102416", "total": 1532, "aging": False},
    "工作通知":      {"channelId": "7778c3a40a344de3a36ca88a2548f5f8", "code": "c102424", "total": 813,  "aging": False},
    "其他文件":      {"channelId": "4c1a5be62f6d44d48f386f630dcebbc5", "code": "c100013", "total": 487,  "aging": False},
    "法律":          {"channelId": "d34fa7ad03f84f4caed12f5c2beae099", "code": "c100009", "total": 75,   "aging": True},
    "行政法规":      {"channelId": "e1cd1569d1ea4a25a11041248925a081", "code": "c100010", "total": 65,   "aging": True},
    "国务院文件":    {"channelId": "fa1726b47078490fa0a4522194185e8d", "code": "c102440", "total": 35,   "aging": True},
}
# ⚠️「最新文件」(c100006, 5017) **是上面栏目的聚合视图**（实测 30 条里覆盖四类、重叠 8 条），
#    计入会重复，故**故意不纳入**。
# ⚠️ 财税文件 / 工作通知 / 其他文件三栏目**不带 aging（时效性）字段**（实测为空），
#    时效须靠详情页头部"全文废止/全文有效"标记或人工判断 —— 不可默认视为有效。
# → 这三栏目只做**目录索引（indexed）**，被问到再升级为依据（抓正文+复核）。
DEFAULT_CHANNEL = "税务规范性文件"
# 全量索引时的栏目顺序（带官方时效的优先，抓取价值最高）
INDEX_CHANNEL_ORDER = ["税务规范性文件", "财税文件", "工作通知", "其他文件",
                       "法律", "行政法规", "国务院文件"]

# 官方「时效性」取值分流（白名单外的值一律当作未知 → 仍进待复核，不猜）
AGING_IN_FORCE = {"有效", "全文有效", "部分有效"}          # 实测取值含"全文有效"
AGING_NOT_YET = {"尚未生效"}
AGING_DEAD = {"失效", "废止", "已废止", "全文失效", "全文废止",
              "部分失效", "部分废止", "已失效"}


# ============================ HTTP ============================
def http_post(url, data, timeout=30, retries=3):
    body = urllib.parse.urlencode(data).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:          # 超时/连接重置：退避重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def http_get(url, timeout=35, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def polite_sleep():
    """礼貌抓取：单线程 + 1.0~2.0s 随机间隔，避免给官网压力。"""
    time.sleep(random.uniform(1.0, 2.0))


# ============================ 列表 ============================
def enumerate_channel(channel_id, page=1, size=SIZE):
    """取一页列表，返回 {"total":N,"rows":R,"results":[...]} 或 None。"""
    raw = http_post(API, {"codeId": "", "channelId": channel_id, "page": page, "size": size})
    try:
        d = json.loads(raw)
        return ((d.get("results") or {}).get("data")) or None
    except Exception:
        return None


def meta_map(rec):
    """把 domainMetaList 拍平成 {key: value}。"""
    out = {}
    for grp in rec.get("domainMetaList") or []:
        for m in grp.get("resultList") or []:
            k = m.get("key")
            v = (m.get("value") or "").strip()
            if k and v:
                out[k] = v
    return out


def taxpolicy_to_category(taxpolicy):
    """`税收政策-增值税,税费征管` → `增值税`（取**第一个**税种，去掉"税收政策-"前缀）。
    取单个值是为了让 RAG 的"同领域召回"能可靠匹配 policy.category。"""
    for p in re.split(r'[,，;；]', taxpolicy or ""):
        p = p.strip()
        if not p:
            continue
        for sep in ("-", "－", "—"):
            if sep in p:
                p = p.split(sep, 1)[1].strip()
                break
        if p:
            return p
    return ""


def topic_match(meta, topics):
    """topics 为空则全收；否则 taxpolicy / effectlevel / labels 命中任一即可。"""
    if not topics:
        return True
    hay = " ".join([meta.get("taxpolicy", ""), meta.get("effectlevel", ""),
                    meta.get("labels", ""), meta.get("writtentext", "")])
    return any(t and t in hay for t in topics)


def fix_detail_url(url):
    """接口返回的 url 主机名是错的（www→404），统一改写为 fgk 主机。"""
    url = (url or "").strip()
    if not url:
        return ""
    m = re.match(r'^https?://[^/]+(/.*)$', url)
    path = m.group(1) if m else url
    return DETAIL_HOST + path


# ============================ 详情页正文 ============================
def _strip_html(seg):
    seg = re.sub(r'<!--.*?-->', '', seg, flags=re.S)
    seg = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', seg, flags=re.S | re.I)
    seg = re.sub(r'</(p|div|tr|h[1-6]|li)>', '\n', seg, flags=re.I)
    seg = re.sub(r'<br\s*/?>', '\n', seg, flags=re.I)
    seg = re.sub(r'</t[dh]>', '\t', seg, flags=re.I)
    seg = re.sub(r'<[^>]+>', '', seg)
    seg = html_mod.unescape(seg)
    seg = seg.replace('\u3000', ' ').replace('\xa0', ' ')
    seg = re.sub(r'[ \t]+', ' ', seg)
    seg = re.sub(r' *\n *', '\n', seg)
    seg = re.sub(r'\n{2,}', '\n', seg)
    return seg.strip()


def extract_body(html):
    """抽正文：<div class="arc_cont"> 起 → <div class="bot-btns-box"> 止。只去标签，不改内容。"""
    if not html:
        return ""
    i = html.find('class="arc_cont"')
    if i < 0:
        return ""
    gt = html.find('>', i)
    start = gt + 1 if gt > i else i
    j = html.find('<div class="bot-btns-box"', start)
    seg = html[start:j] if j > start else html[start:start + 20000]
    k = seg.rfind('</div>')
    if k > 0:
        seg = seg[:k]
    text = _strip_html(seg)
    # 页面按钮噪声（官网正文里不会出现）
    for marker in ('【打印】', '【下载】'):
        p = text.find(marker)
        if p > 0:
            text = text[:p]
    return text.strip()


def extract_effective_date(body, fallback=""):
    """从正文抽施行日期（‘自X年X月X日起施行/执行’），抽不到退回成文日期。"""
    for pat in (r'自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起(?:施行|执行|生效)',
                r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起(?:施行|执行|生效)'):
        m = re.search(pat, body or "")
        if m:
            return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return fallback or ""


# ============================ 条款切分 ============================
# 锚点必须**行首**，否则会误切"适用《XX实施条例》第四十四条的规定"这类跨法引用
_ITEM_ANCHOR = re.compile(r'(?m)^[ \t\u3000]*[一二三四五六七八九十]{1,3}、')
_ART_ANCHOR = re.compile(r'(?m)^[ \t\u3000]*第[一二三四五六七八九十百零〇0-9]{1,6}条')


def _clause_no(text):
    t = (text or "").lstrip()
    m = re.match(r'第([一二三四五六七八九十百零〇0-9]{1,6})条', t)
    if m:
        return "第%s条" % m.group(1)
    m = re.match(r'([一二三四五六七八九十]{1,3})、', t)
    if m:
        return "%s、" % m.group(1)
    return "正文"


def _split_at(body, anchor_re):
    """按锚点位置切分；锚点不足 2 个返回 None。行首之前的内容保留为「正文」（前言）。"""
    pos = [m.start() for m in anchor_re.finditer(body)]
    if len(pos) < 2:
        return None
    if pos[0] != 0:
        pos = [0] + pos
    parts = []
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(body)
        seg = body[p:end].strip()
        if seg:
            parts.append(seg)
    return parts if len(parts) >= 2 else None


_SUB_ANCHOR = re.compile(
    r'(?m)^[ \t\u3000]*(?:\d{1,2}\s*[．.、]|（[一二三四五六七八九十]{1,4}）)')


def _refine_long_parts(parts, limit=1800):
    """超长条目再按 （一）/(1)/1. 细分——避免单条上万字拖垮检索与 prompt 预算。"""
    out = []
    for p in parts:
        if len(p) <= limit:
            out.append(p)
            continue
        sub = [x.strip() for x in _SUB_ANCHOR.split(p) if x.strip()]
        if len(sub) >= 2 and max(len(s) for s in sub) <= max(limit, int(len(p) * 0.8)):
            out.extend(sub)
        else:
            out.append(p)
    return out


def split_clauses(body, limit=1800):
    """条款切分。**优先用文件自身"一、二、三…"的条目编号**——公告类文件几乎都用它，
    而"第X条"常出现在**引用其他法规**的句子里（误切会把上下文劈开）。
    超长条目再按 （一）/1. 细分。两者皆无则整篇作单条「正文」。全程照录原文，不改写。"""
    body = (body or "").strip()
    if not body:
        return []
    for anchor in (_ITEM_ANCHOR, _ART_ANCHOR):
        parts = _split_at(body, anchor)
        if parts:
            parts = _refine_long_parts(parts, limit)
            return [{"no": _clause_no(p), "content": p} for p in parts]
    return [{"no": "正文", "content": body}]


# ============================ 废止关系（只抽官方明写的） ============================
_SUPER = re.compile(
    r'(?:同时|予以|相应)?废止《[^》]{2,60}》'
    r'(?:[（(][^）)]{0,30}号[）)])?'
    r'[^。；;]{0,80}')


def extract_supersede_notes(body):
    """抽取官方明写的废止语句（原文片段），供人工复核；**不自动生成废止关系**。"""
    notes = []
    for m in _SUPER.finditer(body or ""):
        sentence = m.group(0).strip()
        if sentence and sentence not in notes:
            notes.append(sentence)
    return notes


# ============================ 组装 payload ============================
def build_payload(rec, meta, detail_html=None, with_body=True):
    """组装成 policy_ingest.ingest_payload 可吃的结构。"""
    raw_url = rec.get("url") or ""
    url = fix_detail_url(raw_url)
    title = (rec.get("title") or rec.get("subTitleHtml") or "").strip()
    writtendate = meta.get("writtendate") or ""
    aging = meta.get("aging") or ""
    taxpolicy = meta.get("taxpolicy") or ""

    doc_number = (meta.get("writtentext") or "").strip()
    if not doc_number:
        # 无文号 → 用标题生成临时键，强制进复核队列，不静默丢
        doc_number = "未标文号-" + (rec.get("publishedTimeStr") or "")[:10] + "-" + title[:18]

    body = extract_body(detail_html) if (with_body and detail_html) else ""
    clauses = split_clauses(body) if body else []

    payload = {
        "title": title,
        "doc_number": doc_number,
        "issuing_authority": meta.get("writtendepartment") or "",
        "publish_date": (rec.get("publishedTimeStr") or "")[:10],
        "effective_date": extract_effective_date(body, writtendate),
        "category": taxpolicy_to_category(taxpolicy),
        "source_url": url,
        "content_text": body or "",       # 全文永久保留：便于日后重新切条款、复核溯源
        "clauses": clauses,
        "official_aging": aging,
        "official_effectlevel": meta.get("effectlevel") or "",
        "official_taxpolicy": taxpolicy,
        "source_meta": {
            "channel": rec.get("channelName") or "",
            "writtendate": writtendate,
            "labels": meta.get("labels") or "",
            "resdepartment": meta.get("resdepartment") or "",
            "formulatedyear": meta.get("formulatedyear") or "",
            "raw_url": raw_url,
            "body_chars": len(body or ""),
            "supersede_notes": extract_supersede_notes(body) if body else [],
            "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    return payload


# ============================ 目录索引 L1（不抓正文） ============================
def build_index_row(rec, meta):
    """只取列表页元数据，**不抓详情页正文**。
    用途：把官网全量目录纳入"可查到"的范围（有文号/时效/官方链接），但不作依据。"""
    url = fix_detail_url(rec.get("url") or "")
    title = (rec.get("title") or rec.get("subTitleHtml") or "").strip()
    doc_number = (meta.get("writtentext") or "").strip()
    if not doc_number:
        doc_number = "未标文号-" + (rec.get("publishedTimeStr") or "")[:10] + "-" + title[:18]
    return {
        "title": title,
        "doc_number": doc_number,
        "issuing_authority": meta.get("writtendepartment") or "",
        "publish_date": (rec.get("publishedTimeStr") or "")[:10],
        "effective_date": meta.get("writtendate") or "",
        "category": taxpolicy_to_category(meta.get("taxpolicy") or ""),
        "source_url": url,
        "official_aging": meta.get("aging") or "",
        "official_effectlevel": meta.get("effectlevel") or "",
        "official_taxpolicy": meta.get("taxpolicy") or "",
        "source_meta": {
            "channel": rec.get("channelName") or "",
            "writtendate": meta.get("writtendate") or "",
            "labels": meta.get("labels") or "",
            "mode": "index_only",
            "indexed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "status": "indexed",
    }


def upsert_index_rows(cx, rows):
    """目录索引专用写入（铁律）：
      ① 库中不存在 → 插入 status='indexed'（可查到+有官方链接，但**不可作依据**）；
      ② 已存在 → **只更新官方元数据**（时效/链接/发布日期），
         **绝不触碰 content_text / clauses / status** —— 保护依据库与人工复核成果。
    返回 (新增, 更新元数据, 官方标废止数)
    """
    new = upd = dead = 0
    for r in rows:
        doc_number = (r.get("doc_number") or "").strip()
        if not doc_number:
            continue
        aging = (r.get("official_aging") or "").strip()
        row = cx.execute(
            "SELECT id,status FROM policy_registry WHERE doc_number=?", (doc_number,)).fetchone()
        if row is None:
            st = "invalid" if aging in AGING_DEAD else "indexed"
            cx.execute(
                "INSERT INTO policy_registry (title,doc_number,doc_type,issuing_authority,"
                "publish_date,effective_date,status,category,content_text,source_url,"
                "official_aging,official_effectlevel,official_taxpolicy,source_meta) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("title") or "", doc_number, "policy",
                 r.get("issuing_authority") or "", r.get("publish_date") or "",
                 r.get("effective_date") or "", st, r.get("category") or "", "",
                 r.get("source_url") or "", aging, r.get("official_effectlevel") or "",
                 r.get("official_taxpolicy") or "",
                 json.dumps(r.get("source_meta"), ensure_ascii=False)))
            new += 1
            if st == "invalid":
                dead += 1
        else:
            cx.execute(
                "UPDATE policy_registry SET official_aging=?, official_effectlevel=?, "
                "official_taxpolicy=?, source_url=COALESCE(NULLIF(?,''),source_url), "
                "publish_date=COALESCE(NULLIF(?,''),publish_date), "
                "effective_date=COALESCE(NULLIF(?,''),effective_date), "
                "category=COALESCE(NULLIF(?,''),category) WHERE id=?",
                (aging, r.get("official_effectlevel") or "", r.get("official_taxpolicy") or "",
                 r.get("source_url") or "", r.get("publish_date") or "",
                 r.get("effective_date") or "", r.get("category") or "", row["id"]))
            # 官方明确标"废止/失效"时，仅当原状态不是依据库(active)才下调——依据库的状态由人工复核掌控
            if aging in AGING_DEAD and row["status"] in ("indexed", "pending_review"):
                cx.execute("UPDATE policy_registry SET status='invalid' WHERE id=?", (row["id"],))
                dead += 1
            upd += 1
    cx.commit()
    return new, upd, dead


# ============================ checkpoint ============================
def load_checkpoint():
    if os.path.exists(CKPT_PATH):
        try:
            with open(CKPT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_checkpoint(ck):
    os.makedirs(os.path.dirname(CKPT_PATH), exist_ok=True)
    with open(CKPT_PATH, "w", encoding="utf-8") as f:
        json.dump(ck, f, ensure_ascii=False, indent=2)


def write_inbox(payload):
    """旁路留痕：把抓到的 payload 存成 JSON，供人工复核时对照。"""
    os.makedirs(INBOX_DIR, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", payload.get("doc_number") or "unknown")[:80]
    with open(os.path.join(INBOX_DIR, safe + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ============================ 主流程 ============================
def resolve_channel(name):
    if name in CHANNELS:
        return CHANNELS[name]["channelId"], name
    for label, cfg in CHANNELS.items():
        if cfg["channelId"] == name or cfg["code"] == name:
            return cfg["channelId"], label
    raise SystemExit("未知栏目：%s（可用：%s）" % (name, "、".join(CHANNELS)))


def run(channel=DEFAULT_CHANNEL, trial=50, topics=None, since=None, pages=None,
        resume=False, dry_run=False, index_only=False):
    cid, label = resolve_channel(channel)
    topics = [t.strip() for t in (topics or []) if t.strip()]
    print("=== 官网%s ===" % ("目录索引（仅元数据，不抓正文）" if index_only else "定向抓取"))
    print("栏目：%s  目标条数：%d  税费类型：%s  起始日期：%s%s%s"
          % (label, trial, ("、".join(topics) or "不限"), (since or "不限"),
             "  [dry-run 不写库]" if dry_run else "",
             "  [index-only]" if index_only else ""))

    ck = load_checkpoint() if resume else {}
    st = ck.get(cid) or {"last_page": 0, "seen": []}
    seen = set(st.get("seen") or [])
    page = (st.get("last_page") or 0) + 1 if resume else 1

    cx = None if dry_run else pi.db()
    if cx is not None:
        pi.migrate(cx)

    done = skipped_topic = skipped_dead = no_body = 0
    idx_new = idx_upd = idx_dead = 0
    rows_buf = []
    stats_aging = {}
    page_data = None
    while done < trial:
        if pages and page > pages:
            break
        page_data = enumerate_channel(cid, page, SIZE)
        if not page_data:
            print("  第 %d 页无数据，结束" % page)
            break
        recs = page_data.get("results") or []
        print("  第 %d 页：%d 条（total=%s）" % (page, len(recs), page_data.get("total")))
        if not recs:
            break
        for rec in recs:
            if done >= trial:
                break
            url = rec.get("url") or ""
            if url and url in seen:
                continue
            meta = meta_map(rec)
            if not topic_match(meta, topics):
                skipped_topic += 1
                continue
            if since and (rec.get("publishedTimeStr") or "")[:10] < since:
                continue
            if url:
                seen.add(url)
            done += 1
            aging = meta.get("aging") or "(未标)"
            stats_aging[aging] = stats_aging.get(aging, 0) + 1

            if index_only:
                # 只收元数据，页末统一落盘（不抓详情页，快且不产生噪声条款）
                rows_buf.append(build_index_row(rec, meta))
                continue

            if aging in AGING_DEAD:
                # 官方标失效/废止：不抓正文，只存目录行做时效图谱
                skipped_dead += 1
                payload = build_payload(rec, meta, None, with_body=False)
                payload["status"] = "invalid"
                if dry_run:
                    print("    [失效存目] %s | %s" % (payload["doc_number"], payload["title"][:34]))
                else:
                    pi.ingest_payload(cx, {"policies": [payload]}, activate=False)
                    write_inbox(payload)
                continue

            detail = ""
            try:
                detail = http_get(fix_detail_url(url))
                polite_sleep()
            except Exception as e:
                print("    ! 详情抓取失败 %s: %s" % (url[-40:], str(e)[:60]))
            payload = build_payload(rec, meta, detail, with_body=True)
            if not payload["clauses"]:
                no_body += 1
                payload["content_text"] = payload["content_text"] or "正文见附件/未取到"
            ncl = len(payload["clauses"])
            if dry_run:
                print("    [%s] %s | 条款%d | %s"
                      % (aging, payload["doc_number"], ncl, payload["title"][:34]))
            else:
                new, upd, n_c, n_s = pi.ingest_payload(cx, {"policies": [payload]}, activate=False)
                write_inbox(payload)
                print("    [%s] %s | 条款%d | %s%s"
                      % (aging, payload["doc_number"], ncl, payload["title"][:34],
                         "" if n_c else "  ⚠️未切出条款"))
        if index_only and rows_buf:
            if dry_run:
                for r in rows_buf:
                    print("    [索引] %s | %s" % (r["doc_number"], r["title"][:38]))
            elif cx is not None:
                n1, n2, n3 = upsert_index_rows(cx, rows_buf)
                idx_new += n1; idx_upd += n2; idx_dead += n3
                print("    ↗ 本页索引：新增 %d | 更新元数据 %d | 官方废止 %d" % (n1, n2, n3))
            rows_buf = []
        if not dry_run:
            ck[cid] = {"last_page": page, "seen": sorted(seen)[-3000:]}
            save_checkpoint(ck)
        page += 1
        polite_sleep()
        if page_data and len(recs) < SIZE:
            break

    if cx is not None:
        cx.close()

    print("\n=== 本次小结 ===")
    print("  处理 %d 条 | 官方时效性分布：%s" % (done, stats_aging or "{}"))
    if index_only:
        print("  索引：新增 %d 条 | 已有则更新元数据 %d 条 | 官方标废止 %d 条"
              % (idx_new, idx_upd, idx_dead))
        print("  ⚠️ 索引条目 status='indexed'：**可被查到、附官方链接，但不作依据**；")
        print("     被问到再按需升级（抓正文 → 人工复核 → status='active'）。")
        print("  因税费类型不符跳过 %d 条" % skipped_topic)
    else:
        print("  因税费类型不符跳过 %d 条 | 官方失效仅存目 %d 条 | 未取到条款 %d 条"
              % (skipped_topic, skipped_dead, no_body))
    if not dry_run:
        print("  留痕文件：%s" % INBOX_DIR)
        if not index_only:
            print("  入库状态：pending_review（需在运营后台「政策复核」逐条核对原文后生效）")
        print("  断点文件：%s（用 --resume 续跑）" % CKPT_PATH)
    return done


# ============================ L2 升级：把「仅目录」抓成「依据」 ============================
_REPEAL_DOCNOS = None


def _norm_docno(s):
    """文号规范化：统一括号、去空白，便于跨源比对。"""
    s = (s or "").strip()
    s = (s.replace("〔", "[").replace("〕", "]")
          .replace("（", "(").replace("）", ")"))
    return re.sub(r"\s+", "", s)


def load_repeal_docnos():
    """官方《失效废止…目录》公告附件里的**被废止文号**集合 → 零人工复核的权威依据。

    官方清单带**条款级**精度，比 `aging` 字段更细、更及时（aging 会滞后，
    实例：2023年第19号 官方页未标废止、实际已停止执行）。
    """
    global _REPEAL_DOCNOS
    if _REPEAL_DOCNOS is not None:
        return _REPEAL_DOCNOS
    out = set()
    p = os.path.join(ROOT, "data", "policies", "_annex", "repeal_catalog.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            for it in (d.get("items") or []):
                dn = _norm_docno(it.get("doc_number"))
                if dn:
                    out.add(dn)
        except Exception as e:
            print("  ! 废止目录读取失败：%s" % str(e)[:70])
    _REPEAL_DOCNOS = out
    return out


# 判定「国内环节增值税优惠政策」的标题用语（与 policy_rescan._VAT_PREFER_ACTION 同口径）
_VAT_PREFER_ACTION = ("优惠", "减免", "免征", "减征", "减按", "起征点", "免税", "即征即退", "先征后退")
# 财政部 税务总局公告2026年第10号 第六条明确保留的例外
_VAT_BATCH_EXCEPTIONS = {"财政部税务总局公告2025年第17号", "财政部 税务总局公告2025年第17号"}


def hit_batch_repeal(title, doc_number, publish_date):
    """是否落入「批量停止执行」范围。

    《财政部 税务总局公告2026年第10号》第六条原文：
      "除本公告和增值税法、增值税法实施条例、《财政部 税务总局关于个人销售住房增值税政策的
       公告》（2025年第17号）外，在2025年12月31日前制发文件规定的国内环节增值税优惠政策
       同时停止执行。"

    → 制发日 ≤ 2025-12-31 的「增值税 + 优惠用语」文件已停止执行。
    这类**不能自动放行**（官方 aging 常常没同步标），一律转人工复核。
    """
    dn = _norm_docno(doc_number)
    if dn in {_norm_docno(x) for x in _VAT_BATCH_EXCEPTIONS}:
        return False
    blob = (title or "") + " " + (doc_number or "")
    if "增值税" not in blob:
        return False
    if not any(k in (title or "") for k in _VAT_PREFER_ACTION):
        return False
    d = (publish_date or "")[:10]
    return bool(d) and d <= "2025-12-31"


def auto_review(doc_number, aging, body, clauses, effective_date,
                dead_docnos, superseded_ids, doc_id,
                publish_date="", recent_since="", title=""):
    """零人工的自动复核判定 → (放行?, 理由)。

    放行是**合取条件**，任一不满足即转人工复核队列（宁慢勿错）：
      ① 时效可靠：官方明示现行有效，**或**虽无官方时效字段但发布日在 `recent_since` 之后
         （财税/工作通知/其他文件三栏目官方不带 aging，新政可推定有效）
      ② 正文完整（≥80 字且切出条款，无过短残条）
      ③ 文号不在官方废止目录里
      ④ 未被其他文件明令废止（`policy_supersession`）
      ⑤ 不落入「批量停止执行」范围（2026年第10号第六条）
      ⑥ 施行日已到（未生效的绝不进 grounding）
    """
    a = (aging or "").strip()
    if a in AGING_DEAD:
        return False, "官方标注失效/废止（%s）" % a
    if a in AGING_IN_FORCE:
        basis = "官方明示现行有效"
    elif recent_since and publish_date and publish_date >= recent_since:
        basis = "无官方时效字段，发布于 %s 之后（时效可推定）" % recent_since
    else:
        return False, "官方未标现行有效（%s）" % (a or "无时效标注")
    if len(body) < 80 or not clauses:
        return False, "正文过短或未切出条款"
    short = [c["no"] for c in clauses if len((c.get("content") or "").strip()) < 8]
    if short:
        return False, "存在过短条款（疑似抽到目录/残留）：%s" % "、".join(short[:5])
    if _norm_docno(doc_number) in dead_docnos:
        return False, "命中官方废止目录"
    if doc_id in superseded_ids:
        return False, "已被其他文件明令废止"
    if hit_batch_repeal(title, doc_number, publish_date):
        return False, "落入 2026年第10号第六条批量停止执行范围（2025-12-31 前的增值税优惠）"
    if effective_date and effective_date > datetime.date.today().isoformat():
        return False, "施行日在未来（尚未生效）"
    return True, basis + " · 未命中任何废止来源"


def upgrade_indexed(topics=None, limit=100, dry_run=False, channels=None,
                    auto_approve=False, min_body=80, recent_since=""):
    """把 status='indexed'（仅收录目录、无正文）批量升级为依据：

        抓详情页正文 → 切条款 → 写 policy_clause → 转 L2。

    两种落地方式：
      · 默认：一律转 `pending_review`（进人工复核队列）；
      · `--auto-approve`：通过 `auto_review()` 合取条件者**直接 active**，
        其余仍转 `pending_review` —— 条件不满足宁可转人工，不赌。

    只处理官方未标「失效/废止」的文件。返回 (尝试, 转active, 转待复核, 跳过)。
    """
    topics = [t.strip() for t in (topics or []) if t.strip()]
    channels = [c.strip() for c in (channels or []) if c.strip()]
    cx = pi.db()
    try:
        pi.migrate(cx)
        sql = ("SELECT id,doc_number,title,source_url,category,official_aging,"
               "effective_date,publish_date "
               "FROM policy_registry WHERE status='indexed' AND IFNULL(source_url,'')<>''")
        params = []
        if topics:
            sql += " AND (" + " OR ".join(["IFNULL(category,'') LIKE ?"] * len(topics)) + ")"
            params += ["%" + t + "%" for t in topics]
        if channels:
            sql += (" AND json_extract(source_meta,'$.channel') IN (%s)"
                    % ",".join(["?"] * len(channels)))
            params += channels
        sql += (" ORDER BY CASE json_extract(source_meta,'$.channel')"
                " WHEN '法律' THEN 0 WHEN '行政法规' THEN 1 WHEN '国务院文件' THEN 2 ELSE 3 END,"
                " CASE WHEN IFNULL(effective_date,'')='' THEN 1 ELSE 0 END,"
                " effective_date DESC LIMIT ?")
        params.append(limit)
        rows = cx.execute(sql, tuple(params)).fetchall()

        dead_docnos = load_repeal_docnos()
        superseded_ids = {x[0] for x in
                          cx.execute("SELECT DISTINCT target_doc_id FROM policy_supersession "
                                     "WHERE target_doc_id IS NOT NULL")}
        print("=== 目录 → 依据 升级 ===")
        print("候选 %d 条%s%s%s" % (
            len(rows),
            ("（税费类型：%s）" % "、".join(topics)) if topics else "",
            ("（栏目：%s）" % "、".join(channels)) if channels else "",
            "  [dry-run 不写库]" if dry_run else ""))
        print("废止依据：官方废止目录 %d 条文号 + 库内废止关系 %d 条"
              % (len(dead_docnos), len(superseded_ids)))
        if auto_approve:
            print("自动复核：开启（合取条件全满足才直接生效，否则转人工）")

        tried = n_active = n_pending = skip = 0
        for r in rows:
            if (r["official_aging"] or "").strip() in AGING_DEAD:
                skip += 1
                continue
            tried += 1
            try:
                html = http_get(r["source_url"])
                polite_sleep()
            except Exception as e:
                print("    ! 抓取失败 %s: %s" % (r["doc_number"][:34], str(e)[:60]))
                continue
            body = extract_body(html)
            clauses = split_clauses(body)
            if not clauses or len(body) < min_body:
                print("    · 正文过短/见附件，保持「仅目录」：%s" % r["doc_number"][:36])
                skip += 1
                continue

            eff = extract_effective_date(body, r["effective_date"] or "")
            passed, why = (False, "未开启自动复核")
            if auto_approve:
                passed, why = auto_review(r["doc_number"], r["official_aging"], body,
                                          clauses, eff, dead_docnos, superseded_ids, r["id"],
                                          publish_date=r["publish_date"] or "",
                                          recent_since=recent_since,
                                          title=r["title"] or "")
            status = "active" if passed else "pending_review"
            print("    %s %s | 正文 %d 字 | 条款 %d | %s"
                  % ("★" if passed else "✓", r["doc_number"][:34],
                     len(body), len(clauses), why))
            if dry_run:
                if passed:
                    n_active += 1
                else:
                    n_pending += 1
                continue
            cx.execute("UPDATE policy_registry SET content_text=?, status=?, "
                       "effective_date=CASE WHEN IFNULL(?,'')='' THEN effective_date ELSE ? END, "
                       "last_verified_at=? WHERE id=?",
                       (body, status, eff, eff,
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            cx.execute("DELETE FROM policy_clause WHERE doc_id=?", (r["id"],))
            for i, c in enumerate(clauses):
                cx.execute("INSERT INTO policy_clause (doc_id,clause_no,content,clause_status,display_order) "
                           "VALUES (?,?,?,'active',?)", (r["id"], c["no"], c["content"], i))
            if passed:
                n_active += 1
            else:
                n_pending += 1
        if not dry_run:
            cx.commit()
        print("  直接生效 %d 条 | 转待复核 %d 条 | 跳过 %d 条（共尝试 %d）"
              % (n_active, n_pending, skip, tried))
        return tried, n_active, n_pending, skip
    finally:
        cx.close()


# ============================ 诊断命令 ============================
def cmd_list_channels():
    print("=== 栏目配置 ===")
    for label, cfg in CHANNELS.items():
        print("  %-16s channelId=%s  code=%s  已知总数=%s"
              % (label, cfg["channelId"], cfg["code"], cfg["total"]))


def cmd_probe():
    print("=== 探测 ===")
    cid, label = resolve_channel(DEFAULT_CHANNEL)
    try:
        d = enumerate_channel(cid, 1, 3)
        print("[列表接口] 可达 ✓  total=%s rows=%s" % (d.get("total"), d.get("rows")))
        rec = (d.get("results") or [])[0]
        meta = meta_map(rec)
        print("  样例：%s" % (rec.get("title") or "")[:56])
        print("        文号=%s | 时效性=%s | 税费类型=%s"
              % (meta.get("writtentext"), meta.get("aging"), meta.get("taxpolicy")))
        u = fix_detail_url(rec.get("url"))
        print("  详情页：%s" % u)
        body = extract_body(http_get(u))
        print("[详情页] 可达 ✓  正文 %d 字，切出条款 %d 条"
              % (len(body), len(split_clauses(body))))
        print("        正文开头：%s" % body[:80].replace("\n", " "))
    except Exception as e:
        print("[失败] %s: %s" % (type(e).__name__, str(e)[:150]))


def main():
    ap = argparse.ArgumentParser(description="税镜 · 官网政策语料定向抓取")
    ap.add_argument("--probe", action="store_true", help="探测列表接口与详情页")
    ap.add_argument("--list-channels", action="store_true", help="打印栏目配置")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL, help="栏目名或 channelId")
    ap.add_argument("--trial", type=int, default=0, help="最多处理多少条（目录索引默认不限，抓正文默认 50）")
    ap.add_argument("--topic", default="", help="按税费类型过滤，逗号分隔，如 增值税,企业所得税")
    ap.add_argument("--since", default="", help="仅保留该日期之后发布，YYYY-MM-DD")
    ap.add_argument("--pages", type=int, default=0, help="最多翻多少页")
    ap.add_argument("--resume", action="store_true", help="从断点续跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--index-only", action="store_true",
                    help="仅建目录索引（只取元数据、不抓正文；用于把官网全量目录纳入可查范围）")
    ap.add_argument("--all-channels", action="store_true",
                    help="配合 --index-only：遍历全部栏目建索引")
    ap.add_argument("--upgrade-indexed", action="store_true",
                    help="把「仅目录」(indexed) 的文件抓正文升级为依据")
    ap.add_argument("--channels", default="",
                    help="--upgrade-indexed 时按栏目过滤，逗号分隔，如 法律,行政法规")
    ap.add_argument("--auto-approve", action="store_true",
                    help="--upgrade-indexed 时开启自动复核：满足合取条件者直接生效，否则转人工")
    ap.add_argument("--recent-since", default="",
                    help="无官方时效字段的栏目（财税文件等）可推定的发布日期下限，如 2024-01-01")
    ap.add_argument("--min-body", type=int, default=80, help="正文最小字数（默认 80，低于则维持仅目录）")
    ap.add_argument("--limit", type=int, default=100, help="--upgrade-indexed 的最大条数（默认 100）")
    args = ap.parse_args()

    if args.probe:
        return cmd_probe()
    if args.list_channels:
        return cmd_list_channels()

    trial = args.trial or (100000 if (args.index_only or args.all_channels) else 50)
    topics = args.topic.split(",") if args.topic else []

    if args.upgrade_indexed:
        return upgrade_indexed(topics, args.limit, args.dry_run,
                               channels=args.channels.split(",") if args.channels else None,
                               auto_approve=args.auto_approve, min_body=args.min_body,
                               recent_since=args.recent_since)

    if args.all_channels:
        grand = 0
        print("### 全栏目目录索引：%s" % " → ".join(INDEX_CHANNEL_ORDER))
        for name in INDEX_CHANNEL_ORDER:
            grand += run(channel=name, trial=trial, topics=topics,
                         since=args.since or None, pages=args.pages or None,
                         resume=args.resume, dry_run=args.dry_run, index_only=True)
            print()
        print("### 全部栏目合计索引 %d 条" % grand)
        return grand

    return run(channel=args.channel, trial=trial, topics=topics,
               since=args.since or None, pages=args.pages or None,
               resume=args.resume, dry_run=args.dry_run,
               index_only=args.index_only)


if __name__ == "__main__":
    main()
