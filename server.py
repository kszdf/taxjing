# -*- coding: utf-8 -*-
"""
慧根堂·财税AI智库 — 可运行后端 Demo (M3 核心)
====================================================================
零依赖：仅使用 Python 标准库（http.server + sqlite3 + re/datetime）。
复用 scripts/policy_engine.py 的「政策时效裁决 + 免责注入」逻辑。

启动：
    python server.py
默认监听 http://localhost:8080 ，浏览器打开即可看到原型在真实数据上运行。

设计铁律（与方案一致）：
  - 向前兼容：建表全 IF NOT EXISTS，绝不 DROP；用户资产独立持久。
  - 小程序不碰法币：买积分走企微私域，本后端 /api/buy-points 仅为「企微侧充值后
    后台落账」的模拟接口（真实场景由企微会话触发、人工在后台确认）。
  - 政策时效：resolve_active 永远取「现行有效 + 最新施行」，失效条款不参与生成。
  - 每次回答统一注入免责声明（由 policy_engine.generate_answer 强制）。
"""
import sys
import os
import re
import json
import glob
import sqlite3
import datetime
import urllib.parse
import threading
import hashlib
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
import policy_engine as pe  # 复用政策时效裁决引擎
import policy_fetcher as pf  # 联网政策抓取管道（M4b）
import policy_ingest as pi   # 语料灌入通道 + 幂等加列迁移（M1/M2）
import rag                 # RAG 检索（M4+ 大模型接入）
import llm_adapter as la   # 零依赖 LLM 适配器（M4+ 大模型接入）

DB_PATH = os.path.join(HERE, "db", "app.sqlite")

# ---- 自动复核的「废止」判据（与 scripts/policy_sources.auto_review 同口径）----
# 官方 aging 字段**会滞后**（实例：财政部 税务总局公告2023年第19号 官方页未标废止，
# 实际已被 2026年第10号第六条批量停止执行）→ 自动放行不能只看 aging，
# 必须再叠一层权威废止来源，否则会把已失效文件当依据发给从业者。
_REPEAL_DOCNOS = None


def _norm_docno(s):
    """文号规范化：统一括号、去空白，便于跨源比对。"""
    s = (s or "").strip()
    s = (s.replace("〔", "[").replace("〕", "]")
          .replace("（", "(").replace("）", ")"))
    return re.sub(r"\s+", "", s)


def _repeal_docnos():
    """官方《失效废止…目录》公告附件里的被废止文号集合（进程内缓存一次）。"""
    global _REPEAL_DOCNOS
    if _REPEAL_DOCNOS is not None:
        return _REPEAL_DOCNOS
    out = set()
    p = os.path.join(HERE, "data", "policies", "_annex", "repeal_catalog.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            for it in (d.get("items") or []):
                dn = _norm_docno(it.get("doc_number"))
                if dn:
                    out.add(dn)
        except Exception as e:
            print("[语料] 废止目录读取失败：%s" % str(e)[:80])
    _REPEAL_DOCNOS = out
    return out


def _superseded_doc_ids():
    """库内已被其他文件明令废止的政策 id（量小，每次现查，不做缓存）。"""
    try:
        return {r[0] for r in query(
            "SELECT DISTINCT target_doc_id FROM policy_supersession "
            "WHERE target_doc_id IS NOT NULL")}
    except Exception:
        return set()
HTML_PATH = os.path.join(HERE, "tax-ai-prototype.html")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
DEMO_OPENID = "demo-user-zhang"

# =====================================================================
# 1) SQLite Schema（向前兼容：全 IF NOT EXISTS，绝不 DROP）
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  openid        TEXT NOT NULL UNIQUE,
  unionid       TEXT,
  phone         TEXT,
  nickname      TEXT,
  avatar        TEXT,
  identity_tag  TEXT,
  status        INTEGER NOT NULL DEFAULT 1,
  invite_code   TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta          TEXT
);

CREATE TABLE IF NOT EXISTS membership (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  level       TEXT NOT NULL DEFAULT 'free',
  start_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  end_at      TEXT,
  order_ref   TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta        TEXT
);

CREATE TABLE IF NOT EXISTS points_account (
  user_id     INTEGER PRIMARY KEY,
  balance     INTEGER NOT NULL DEFAULT 0,
  frozen      INTEGER NOT NULL DEFAULT 0,
  last_settle_at TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS points_ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  txn_type    TEXT NOT NULL,
  amount      INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  ref_id      TEXT,
  expire_at   TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta        TEXT
);

CREATE TABLE IF NOT EXISTS invite_chain (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  inviter_id   INTEGER NOT NULL,
  invitee_id   INTEGER NOT NULL,
  level        INTEGER NOT NULL DEFAULT 1,
  reward_status TEXT NOT NULL DEFAULT 'pending',
  reward_points INTEGER,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS pricing_config (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  category       TEXT NOT NULL,
  name           TEXT NOT NULL,
  price_points   INTEGER NOT NULL,
  route_to       TEXT NOT NULL DEFAULT 'app',
  enabled        INTEGER NOT NULL DEFAULT 1,
  effective_from TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  note           TEXT
);

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

CREATE TABLE IF NOT EXISTS case_lib (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,
  category      TEXT NOT NULL,
  industry      TEXT,
  region        TEXT,
  penalty_amount TEXT,
  publish_date  TEXT,
  source        TEXT,
  summary       TEXT,
  risk_points   TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS industry_topic (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  industry     TEXT NOT NULL,
  summary      TEXT,
  local_note   TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS question_record (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,
  session_id    TEXT,
  question      TEXT NOT NULL,
  answer        TEXT,
  answer_type   TEXT NOT NULL DEFAULT 'ai',
  policy_refs   TEXT,
  cost_points   INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'answered',
  routed_to     TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS collection (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER NOT NULL,
  collect_type   TEXT NOT NULL,
  ref_id         TEXT NOT NULL,
  snapshot_status TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS expert (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT,
  title         TEXT,
  cert_no_enc   TEXT,
  bio           TEXT,
  status        TEXT NOT NULL DEFAULT 'coming',
  display_order INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS consult_dynamics (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_mask    TEXT NOT NULL,
  action_type  TEXT NOT NULL,
  topic        TEXT,
  is_paid      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS human_ticket (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  openid       TEXT NOT NULL,
  question     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  reply        TEXT,
  reply_by     TEXT,
  cost_points  INTEGER NOT NULL DEFAULT 30,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  replied_at   TEXT
);

CREATE TABLE IF NOT EXISTS admin_audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  operator     TEXT,
  action       TEXT,
  target       TEXT,
  detail       TEXT,
  ip           TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ===================== 机构版（M1）=====================
CREATE TABLE IF NOT EXISTS organizations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL,
  org_code          TEXT UNIQUE,
  unified_credit_no TEXT,
  contact           TEXT,
  plan              TEXT NOT NULL DEFAULT 'trial',
  seat_total        INTEGER NOT NULL DEFAULT 5,
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta              TEXT
);

CREATE TABLE IF NOT EXISTS org_member (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL,
  user_id      INTEGER,
  login_name   TEXT NOT NULL,
  pwd_hash     TEXT,
  pwd_salt     TEXT,
  name         TEXT,
  role         TEXT NOT NULL DEFAULT 'member',
  seat_status  TEXT NOT NULL DEFAULT 'active',
  invited_by   INTEGER,
  joined_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta         TEXT,
  UNIQUE(org_id, login_name)
);

CREATE TABLE IF NOT EXISTS org_session (
  token        TEXT PRIMARY KEY,
  user_id      INTEGER,
  org_id       INTEGER NOT NULL,
  member_id    INTEGER,
  role         TEXT,
  expire_at    TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS client_profile (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id          INTEGER NOT NULL,
  name            TEXT NOT NULL,
  credit_no       TEXT,
  industry        TEXT,
  region          TEXT,
  taxpayer_type   TEXT,
  risk_level      TEXT,
  owner_member_id INTEGER,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta            TEXT
);

CREATE TABLE IF NOT EXISTS org_standard_answer (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id           INTEGER NOT NULL,
  category         TEXT,
  question_pattern TEXT NOT NULL,
  answer_md        TEXT NOT NULL,
  policy_refs      TEXT,
  steps            TEXT,
  status           TEXT NOT NULL DEFAULT 'draft',
  approved_by      TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta             TEXT
);

-- 全局口径锁定层：政策/程序性问题的唯一真相源（版本化 + 唯一活跃锁）
-- 同一 signature 同时只允许一条 status='active'，从约束上杜绝“同一题多个答案”。
CREATE TABLE IF NOT EXISTS canonical_answer (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  domain        TEXT NOT NULL DEFAULT 'policy',   -- policy | procedure | general
  title         TEXT,
  signature     TEXT NOT NULL,                     -- 归一化问题指纹（锁定键）
  keywords      TEXT,                              -- 逗号分隔触发词，用于近义命中
  answer_md     TEXT NOT NULL,                     -- 锁定答案（不随 LLM 漂移）
  policy_refs   TEXT,                              -- JSON 文号数组（时效驱动版更）
  version       INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'active',    -- active | deprecated | draft
  source        TEXT DEFAULT 'seed',               -- seed | community | llm | user | human
  supersedes_id INTEGER,                           -- 被本版取代的旧版 id
  flag_review   TEXT DEFAULT '',                   -- 非空表示待复核（如政策已废止）
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_sig ON canonical_answer(signature) WHERE status='active';

CREATE TABLE IF NOT EXISTS org_order (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL,
  plan         TEXT,
  seats        INTEGER,
  amount_cny   INTEGER,
  status       TEXT NOT NULL DEFAULT 'pending',
  contract_ref TEXT,
  signed_at    TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta         TEXT
);

-- ===================== 机构版 M2：风险指标初筛 =====================
CREATE TABLE IF NOT EXISTS risk_rule (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  code         TEXT,
  name         TEXT NOT NULL,
  metric       TEXT NOT NULL,
  op           TEXT NOT NULL,
  threshold    REAL,
  level        TEXT NOT NULL DEFAULT 'mid',
  basis        TEXT,
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS client_risk_input (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL,
  client_id    INTEGER NOT NULL,
  period       TEXT NOT NULL DEFAULT '',
  data_json    TEXT,
  updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(org_id, client_id, period)
);

CREATE TABLE IF NOT EXISTS org_report (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL,
  client_id    INTEGER,
  task_id      TEXT,
  title        TEXT,
  risk_items   TEXT,
  policy_refs  TEXT,
  status       TEXT NOT NULL DEFAULT 'done',
  generated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  meta         TEXT
);

-- ===================== 机构版：作业任务分派（工作流锁定核心） =====================
-- 机构主/管理员下派日常作业任务（申报/风控/工商/其他）给成员，关联客户与征期场景。
-- 这是“工作流深度”的落地：把代账日常作业流固化进系统，机构离不开。
CREATE TABLE IF NOT EXISTS org_task (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id              INTEGER NOT NULL,
  creator_member_id   INTEGER NOT NULL,
  assignee_member_id  INTEGER NOT NULL,
  title               TEXT NOT NULL,
  scene_tag           TEXT NOT NULL DEFAULT '其他',
  due_date            TEXT,
  related_client_id   INTEGER,
  status              TEXT NOT NULL DEFAULT 'todo',
  note                TEXT,
  created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_org_task_org ON org_task(org_id, status, due_date);

-- ===================== 同行问（M1 用户互助社区）=====================
CREATE TABLE IF NOT EXISTS community_post (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  openid       TEXT NOT NULL,
  title        TEXT NOT NULL,
  content      TEXT NOT NULL,
  scene_tag    TEXT NOT NULL DEFAULT '其他',
  is_anonymous INTEGER NOT NULL DEFAULT 0,
  bounty_points INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'open',   -- open/accepted/removed
  ai_answer    TEXT,                            -- JSON {answer, policy_refs}
  ai_mode      TEXT,
  view_count   INTEGER NOT NULL DEFAULT 0,
  reply_count  INTEGER NOT NULL DEFAULT 0,
  pinned       INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_community_post_list ON community_post(status, scene_tag, id);

CREATE TABLE IF NOT EXISTS post_reply (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id      INTEGER NOT NULL,
  user_id      INTEGER NOT NULL,
  openid       TEXT NOT NULL,
  content      TEXT NOT NULL,
  policy_refs  TEXT,                            -- JSON：引用政策文号数组
  like_count   INTEGER NOT NULL DEFAULT 0,
  is_accepted  INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'open',   -- open/removed
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_post_reply_post ON post_reply(post_id, id);

CREATE TABLE IF NOT EXISTS post_like (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  reply_id     INTEGER NOT NULL,
  user_id      INTEGER NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(reply_id, user_id)
);

-- ===================== 微信订阅消息（征期提醒）=====================
CREATE TABLE IF NOT EXISTS user_subscription (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  openid           TEXT NOT NULL,
  tmpl_id          TEXT NOT NULL,
  granted_at       TEXT NOT NULL,                 -- 最近一次授权时间（7天发送窗口起点）
  last_deadline_key TEXT,                         -- 上次已推送的征期标识，防重复推送
  status           TEXT NOT NULL DEFAULT 'active',-- active/revoked
  UNIQUE(openid, tmpl_id)
);

CREATE TABLE IF NOT EXISTS wx_token (
  id         INTEGER PRIMARY KEY CHECK (id=1),
  token      TEXT,
  expire_at  TEXT,
  updated_at TEXT
);
"""

# =====================================================================
# 2) 政策引擎（内存 PolicyStore，复用 policy_engine 强制时效裁决）
# =====================================================================
STORE = pe.PolicyStore()

# ---- 运行环境与密钥加载 ----------------------------------------------------
# APP_ENV: development（默认，本地演示） / production（公网部署）
APP_ENV = os.environ.get("APP_ENV", "development").lower()

def _load_env_file():
    """零依赖读取 HERE/.env，避免把密钥硬编码进源码。
    不覆盖已存在的环境变量（命令行 / systemd Environment 优先）。本地开发用。"""
    p = os.path.join(HERE, ".env")
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print("[env] .env 读取失败：%s" % str(e)[:80])

_load_env_file()

# 运营后台鉴权：production 下缺省为空（强制设置，否则拒绝启动）；
# development 下用演示值便于本地调试。生产务必设置强随机 ADMIN_TOKEN。
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "" if APP_ENV == "production" else "admin-dev-2026")

# 定时任务触发鉴权（推送征期提醒等）；production 下强制设置强随机 CRON_TOKEN。
CRON_TOKEN = os.environ.get("CRON_TOKEN", "" if APP_ENV == "production" else "cron-dev-2026")

# 运营/定时接口来源 IP 白名单（公网防御第一道）。
# 默认仅本机；nginx 反代时由 TRUST_PROXY=1 读取 X-Forwarded-For 真实 IP。
ADMIN_ALLOWED_IPS = [x.strip() for x in
                     os.environ.get("ADMIN_ALLOWED_IPS", "127.0.0.1,::1").split(",") if x.strip()]

# 微信订阅消息（征期提醒）：AppID 默认取小程序 project.config.json；
# AppSecret 与模板ID 必须环境变量注入（敏感，禁止硬编码到仓库）。
WX_APPID = os.environ.get("WX_APPID", "wx1dbe0f380c108b9f")
WX_APPSECRET = os.environ.get("WX_APPSECRET", "")
WX_SUBSCRIBE_TMPL_ID = os.environ.get("WX_SUBSCRIBE_TMPL_ID", "")
WX_SUB_AUTH_WINDOW_DAYS = 7  # 一次性订阅授权后 7 天内的发送窗口

# 联网抓取任务状态（异步，避免阻塞请求）
_FETCH_STATE = {"running": False, "last": None, "count": 0, "error": None}
_FETCH_LOCK = threading.Lock()

def _fetch_worker():
    with _FETCH_LOCK:
        _FETCH_STATE["running"] = True
        _FETCH_STATE["error"] = None
        try:
            pf.cmd_live()
            n = query("SELECT COUNT(*) c FROM policy_registry WHERE status='pending_review'")
            _FETCH_STATE["count"] = n[0]["c"] if n else 0
            _FETCH_STATE["last"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            _FETCH_STATE["error"] = str(e)[:200]
        finally:
            _FETCH_STATE["running"] = False

# 管理端登录失败锁定（防撞库，不限制登录地点；任何设备/网络均可登录，仅限制连续失败）
ADMIN_FAIL = {}            # ip -> [失败时间戳]
ADMIN_FAIL_MAX = 5         # 15 分钟内失败超过 5 次即临时锁定
ADMIN_FAIL_WINDOW = 900    # 窗口 15 分钟，自动过期恢复
_REQ_CTX = threading.local()

def _real_client_ip(handler):
    ip = handler.client_address[0] if handler.client_address else ""
    if os.environ.get("TRUST_PROXY") == "1":
        xff = handler.headers.get("X-Forwarded-For", "") or ""
        if xff:
            ip = xff.split(",")[0].strip()
    if ip.startswith("::ffff:"):
        ip = ip[len("::ffff:"):]
    return ip

def _register_admin_fail(ip):
    now = time.time()
    lst = [t for t in ADMIN_FAIL.get(ip, []) if now - t < ADMIN_FAIL_WINDOW]
    lst.append(now)
    ADMIN_FAIL[ip] = lst

def _admin_rate_ok(ip):
    now = time.time()
    lst = [t for t in ADMIN_FAIL.get(ip, []) if now - t < ADMIN_FAIL_WINDOW]
    ADMIN_FAIL[ip] = lst
    return len(lst) < ADMIN_FAIL_MAX

def admin_authorized(body, headers):
    if not ADMIN_TOKEN:
        return False  # 空口令直接拒绝，杜绝空==空绕过
    token = (body or {}).get("admin_auth") or ""
    if not token and headers:
        token = headers.get("X-Admin-Token", "") or ""
    ok = token == ADMIN_TOKEN
    if not ok:
        ip = getattr(_REQ_CTX, "client_ip", "0.0.0.0")
        _register_admin_fail(ip)
    return ok

def write_audit(operator, action, target, detail, ip):
    try:
        exec("INSERT INTO admin_audit (operator,action,target,detail,ip) VALUES (?,?,?,?,?)",
             (operator or "unknown", action or "", target or "", detail or "", ip or ""))
    except Exception:
        pass

def _norm_doc(s):
    return re.sub(r"\s+", "", s or "")

def validate_answer_refs(text, refs, extra_ok=None):
    """第二道防幻觉护栏：检查答案中《...》文号是否都在检索到的现行有效政策内。

    extra_ok：允许出现但不作为「依据」展示的文号（如全量目录线索中的文件），
    用于避免把"真实的目录文号"误判为编造。
    """
    ref_set = {_norm_doc(r) for r in (refs or [])}
    ref_set |= {_norm_doc(r) for r in (extra_ok or [])}
    found = re.findall(r"《([^》]+)》", text or "")
    return [f for f in found if _norm_doc(f) not in ref_set]


# ---------------------------------------------------------------------
# 机构版（M1）辅助：口令散列 / 会话 / 鉴权
# ---------------------------------------------------------------------
def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------
# 征期日历（留存/订阅消息底座）：纯 stdlib，确定性计算
# 说明：征期截止日按"当月15日"估算，遇法定节假日实际顺延——此处不内置节假日表，
#       仅标注"遇法定节假日顺延"；精确节假日顺延待后续数据表补全。
# ---------------------------------------------------------------------
def tax_calendar_deadlines(today=None, count=8):
    today = today or datetime.date.today()
    items = []
    # 月度征期：增值税/附加税/按月个税，滚动未来 5 个月的 15 日
    for i in range(5):
        y = today.year + (today.month - 1 + i) // 12
        m = (today.month - 1 + i) % 12 + 1
        due = datetime.date(y, m, 15)
        if due >= today:
            items.append({
                "name": "增值税·附加税·个税（按月）申报",
                "due": due.isoformat(),
                "period": "%d年%d月" % (y, m),
                "note": "征期一般为当月15日，遇法定节假日顺延",
            })
    # 企业所得税季度预缴（1/4/7/10 月申报上一季度）
    qmap = {1: 4, 4: 1, 7: 2, 10: 3}
    for m in (1, 4, 7, 10):
        due = datetime.date(today.year, m, 15)
        if due >= today:
            items.append({
                "name": "企业所得税（季度预缴）申报",
                "due": due.isoformat(),
                "period": "%d年第%d季度" % (today.year, qmap[m]),
                "note": "季度终了后15日内；年度汇算于次年5月31日前",
            })
    # 企业工商年报（1/1-6/30）
    due6 = datetime.date(today.year, 6, 30)
    if due6 >= today:
        items.append({
            "name": "企业工商年报（上一年度）",
            "due": due6.isoformat(),
            "period": "%d年度" % (today.year - 1),
            "note": "1月1日-6月30日，逾期列入经营异常名录",
        })
    # 个人所得税（经营所得）汇算（3/31 前）
    due3 = datetime.date(today.year, 3, 31)
    if due3 >= today:
        items.append({
            "name": "个人所得税（经营所得）汇算",
            "due": due3.isoformat(),
            "period": "%d年度" % (today.year - 1),
            "note": "3月31日前",
        })
    items.sort(key=lambda x: x["due"])
    out = []
    for it in items[:count]:
        d = datetime.date.fromisoformat(it["due"])
        out.append(dict(it, days_left=(d - today).days))
    return out


# ---------------------------------------------------------------------
# 微信订阅消息（征期提醒推送）：纯 stdlib，缺密钥/模板时优雅降级
# 约束：小程序订阅消息默认「一次性订阅」——用户授权后 7 天内开发者可发 1 条。
#   本设计在征期日历页引导用户每次来访点「开启提醒」即触发授权，并在授权后
#   7 天窗口内、距征期 <=3 天时发送。长期订阅资质难申请，MVP 采用此务实方案。
# ---------------------------------------------------------------------
def _wx_http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "suijing/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wx_http_post(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_wx_access_token():
    """取小程序 access_token，DB 缓存，留 300s 余量。缺 AppSecret 返回 None。"""
    if not WX_APPSECRET:
        return None
    row = query("SELECT token, expire_at FROM wx_token WHERE id=1")
    now = datetime.datetime.now()
    if row and row[0]["token"]:
        try:
            exp = datetime.datetime.strptime(row[0]["expire_at"], "%Y-%m-%d %H:%M:%S")
            if (exp - now).total_seconds() > 300:
                return row[0]["token"]
        except Exception:
            pass
    try:
        url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
               "&appid=%s&secret=%s" % (WX_APPID, WX_APPSECRET))
        r = _wx_http_get(url)
        if "access_token" not in r:
            return None
        token = r["access_token"]
        exp_at = (now + datetime.timedelta(seconds=int(r.get("expires_in", 7200))))\
            .strftime("%Y-%m-%d %H:%M:%S")
        exec("INSERT OR REPLACE INTO wx_token (id,token,expire_at,updated_at) VALUES (1,?,?,?)",
             (token, exp_at, now.strftime("%Y-%m-%d %H:%M:%S")))
        return token
    except Exception:
        return None


def wx_code_to_openid(code):
    """用 wx.login 拿到的 code 交换真实 openid。缺 AppSecret 或失败返回 None（调用方优雅降级）。"""
    if not code or not WX_APPSECRET:
        return None
    try:
        url = ("https://api.weixin.qq.com/sns/jscode2session"
               "?appid=%s&secret=%s&js_code=%s&grant_type=authorization_code"
               % (WX_APPID, WX_APPSECRET, urllib.parse.quote(code)))
        r = _wx_http_get(url)
        if r.get("openid"):
            return r["openid"]
    except Exception:
        pass
    return None


def send_subscribe_message(openid, data):
    """发送订阅消息。缺密钥/模板或网络失败返回 (False, reason)，不影响主流程。"""
    if not WX_SUBSCRIBE_TMPL_ID or not WX_APPSECRET:
        return False, "未配置订阅消息模板或密钥"
    token = get_wx_access_token()
    if not token:
        return False, "无法获取 access_token"
    try:
        url = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token=" + token
        payload = {"touser": openid, "template_id": WX_SUBSCRIBE_TMPL_ID, "data": data}
        r = _wx_http_post(url, payload)
        if r.get("errcode") == 0:
            return True, "ok"
        return False, r.get("errmsg", "unknown")
    except Exception as e:
        return False, str(e)[:120]


def push_deadline_reminders():
    """征期前 3 天批量推送。供 systemd timer 或 GET /api/cron/push-deadlines 触发。
    仅对「授权在 7 天窗口内 + 本次征期未推过」的活跃订阅用户发送。
    注：模板字段(thing1/time2/thing3)需按公众平台实际申请的模板调整。"""
    if not WX_SUBSCRIBE_TMPL_ID or not WX_APPSECRET:
        return {"ok": False, "msg": "未配置订阅消息模板或密钥，跳过推送", "sent": 0, "skipped": 0}
    deadlines = tax_calendar_deadlines()
    now = datetime.datetime.now()
    sent = skipped = 0
    for d in deadlines:
        dl = d.get("days_left")
        if dl is None or dl < 0 or dl > 3:
            continue
        key = "deadline:" + d["due"] + ":" + d["name"]
        subs = query(
            "SELECT openid, granted_at FROM user_subscription "
            "WHERE tmpl_id=? AND status='active' "
            "AND (last_deadline_key IS NULL OR last_deadline_key<>?)",
            (WX_SUBSCRIBE_TMPL_ID, key))
        for s in subs:
            try:
                g = datetime.datetime.strptime(s["granted_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if (now - g).days > WX_SUB_AUTH_WINDOW_DAYS:
                skipped += 1
                continue
            data = {
                "thing1": {"value": d["name"][:20]},
                "time2": {"value": d["due"]},
                "thing3": {"value": ("距申报还有 %d 天，请提前安排" % dl)[:20]},
            }
            ok, _ = send_subscribe_message(s["openid"], data)
            if ok:
                exec("UPDATE user_subscription SET last_deadline_key=? WHERE openid=? AND tmpl_id=?",
                     (key, s["openid"], WX_SUBSCRIBE_TMPL_ID))
                sent += 1
            else:
                skipped += 1
    return {"ok": True, "sent": sent, "skipped": skipped}


def pwd_hash(pwd, salt):
    return hashlib.sha256(((salt or "") + "::" + (pwd or "")).encode("utf-8")).hexdigest()


def org_session_lookup(token):
    """根据会话 token 返回会话行（未过期），否则 None。"""
    if not token:
        return None
    rows = query("SELECT * FROM org_session WHERE token=?", (token,))
    if not rows:
        return None
    r = rows[0]
    if r["expire_at"] and r["expire_at"] < _now_str():
        return None
    return r


def org_authorized(body, headers):
    """机构端鉴权：从 body.token 或头 X-Org-Token 取会话，返回会话行或 None。"""
    token = ""
    if body:
        token = body.get("token") or ""
    if not token and headers:
        token = headers.get("X-Org-Token", "") or ""
    return org_session_lookup(token)


# 机构版按角色可见模块（统一账号缝：个人/机构成员/机构负责人显隐不同）
ORG_MODULES_MEMBER = [
    {"key": "clients", "name": "客户档案"},
    {"key": "answers", "name": "机构口径库"},
    {"key": "ask", "name": "团队问答"},
    {"key": "tasks", "name": "作业任务"},
    {"key": "risk", "name": "风险扫描"},
]
ORG_MODULES_OWNER = ORG_MODULES_MEMBER + [{"key": "members", "name": "席位成员"}]
ORG_MODULES_BY_ROLE = {"owner": ORG_MODULES_OWNER, "admin": ORG_MODULES_OWNER, "member": ORG_MODULES_MEMBER}


# ---------------------------------------------------------------------
# 机构版（M2）辅助：风险指标初筛
# ---------------------------------------------------------------------
_RISK_STATE = {"running": False, "task_id": None, "done": 0, "total": 0, "last": None, "error": None}
_RISK_LOCK = threading.Lock()

# 手工填报指标字段（前端表单与之对应）
RISK_METRICS = [
    ("vat_burden_rate", "增值税税负率(%)"),
    ("zero_declare_months", "连续零/负申报月数"),
    ("invoice_void_rate", "发票作废红冲占比(%)"),
    ("payroll_vs_si_gap", "个税人数-社保人数(人)"),
    ("other_receivable_ratio", "其他应收/收入(%)"),
    ("gross_margin", "毛利率(%)"),
    ("revenue_yoy", "收入同比(%)"),
]

_OPS = {"<": lambda a, b: a < b, ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
        "!=": lambda a, b: a != b, "==": lambda a, b: a == b}


def evaluate_rules(data, rules):
    """对一户的填报数据跑规则；返回 (命中项, 数据不足项)。AI 只列命中项+依据，不下结论。"""
    hits, missing = [], []
    for r in rules:
        m = r["metric"]
        v = (data or {}).get(m)
        if v is None or v == "":
            missing.append(r["name"]); continue
        try:
            v = float(v)
        except Exception:
            missing.append(r["name"]); continue
        fn = _OPS.get(r["op"])
        if fn and r["threshold"] is not None and fn(v, float(r["threshold"])):
            hits.append({"code": r["code"], "name": r["name"], "level": r["level"], "metric": m,
                         "value": round(v, 2), "op": r["op"], "threshold": r["threshold"],
                         "basis": r["basis"] or ""})
    return hits, missing


def seed_risk_rules():
    if query("SELECT id FROM risk_rule LIMIT 1"):
        return
    rows = [
        ("vat_burden", "增值税税负率偏低", "vat_burden_rate", "<", 1.5, "mid", "增值税税负率明显低于行业参考水平（口径以主管税务机关为准）"),
        ("zero_declare", "连续零/负申报", "zero_declare_months", ">=", 3, "mid", "连续 3 个月及以上零申报或负申报"),
        ("void_rate", "发票作废/红冲占比偏高", "invoice_void_rate", ">", 20, "mid", "发票作废或红冲占比超过 20%"),
        ("payroll_si", "个税与社保人数不符", "payroll_vs_si_gap", "!=", 0, "mid", "个税申报人数与社保缴纳人数不一致"),
        ("other_receiv", "其他应收长期挂账", "other_receivable_ratio", ">", 30, "mid", "其他应收款占收入比超过 30%"),
        ("gross_neg", "毛利率异常（为负）", "gross_margin", "<", 0, "mid", "毛利率为负，收入成本可能异常"),
        ("rev_drop", "收入大幅下滑", "revenue_yoy", "<", -30, "mid", "收入同比下滑超过 30%"),
    ]
    for code, name, metric, op, th, lvl, basis in rows:
        exec("INSERT INTO risk_rule (code,name,metric,op,threshold,level,basis) VALUES (?,?,?,?,?,?,?)",
             (code, name, metric, op, th, lvl, basis))


def run_client_scan(org_id, client_id, period):
    """单户风险初筛：读填报数据 → 跑规则 → 落 org_report。绝不输出'没问题'结论。"""
    row = query("SELECT data_json FROM client_risk_input WHERE org_id=? AND client_id=? AND period=?",
                (org_id, client_id, period))
    data = {}
    if row and row[0]["data_json"]:
        try:
            data = json.loads(row[0]["data_json"])
        except Exception:
            data = {}
    rules = query("SELECT * FROM risk_rule WHERE enabled=1")
    hits, missing = evaluate_rules(data, rules)
    c = query("SELECT name FROM client_profile WHERE id=? AND org_id=?", (client_id, org_id))
    cname = c[0]["name"] if c else ("客户#" + str(client_id))
    title = cname + " · 涉税风险初筛（" + (period or "未标期") + "）"
    data_empty = not data
    if data_empty:
        level, status = "unknown", "insufficient"
    else:
        level = "high" if any(h["level"] == "high" for h in hits) else ("mid" if hits else "low")
        status = "done"
    meta = json.dumps({"missing": missing, "data_empty": data_empty}, ensure_ascii=False)
    cx = db(); cur = cx.cursor()
    cur.execute("INSERT INTO org_report (org_id,client_id,title,risk_items,policy_refs,status,meta) VALUES (?,?,?,?,?,?,?)",
                (org_id, client_id, title, json.dumps(hits, ensure_ascii=False),
                 json.dumps([], ensure_ascii=False), status, meta))
    rid = cur.lastrowid
    cx.commit(); cx.close()
    return {"report_id": rid, "client": cname, "level": level, "status": status, "hits": hits, "missing": missing,
            "disclaimer": "本报告为风险初筛，仅列示命中项与依据，不构成风险结论；须由专业人员复核，口径以主管税务机关为准。"}


def _risk_batch_worker(org_id, client_ids, period):
    with _RISK_LOCK:
        _RISK_STATE["running"] = True
        _RISK_STATE["done"] = 0
        _RISK_STATE["total"] = len(client_ids)
        _RISK_STATE["error"] = None
        try:
            for cid in client_ids:
                run_client_scan(org_id, cid, period)
                _RISK_STATE["done"] += 1
            _RISK_STATE["last"] = _now_str()
        except Exception as e:
            _RISK_STATE["error"] = str(e)[:200]
        finally:
            _RISK_STATE["running"] = False

CORE_CORPUS = os.path.join(HERE, "data", "policies", "core.json")


def seed_policies():
    """把**已人工核实**的『核心政策语料包』载入内存裁决引擎 STORE。

    单一数据源：`data/policies/core.json`（正文一律官方原文照录、source_url 可溯源）。

    ⚠️ 历史教训：此处原为硬编码演示条款，其中「国家税务总局公告2023年第1号 第八条」
    的内容被错误写成"其他个人出租不动产按5%减按1.5%"（实为该公告第八条是纳税期限选择），
    属于**错误引用**。现改为读语料文件，杜绝再写错——凡入库正文必须可溯源。

    文件缺失时退回最小内置种子，保证服务可起。
    """
    # 载入所有 data/policies/core*.json（core.json 基础包 + core_<pack>.json 专题包）
    files = sorted(glob.glob(os.path.join(HERE, "data", "policies", "core*.json")))
    if files:
        merged, bad = [], []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                pols = payload.get("policies") or []
                if pols:
                    merged.extend(pols)
                    print("[语料] + %s（%d 份政策）" % (os.path.basename(fp), len(pols)))
                else:
                    bad.append(os.path.basename(fp))
            except Exception as e:
                bad.append("%s(%s)" % (os.path.basename(fp), e))
        if bad:
            print("[语料] ⚠️ 以下语料文件未载入，请检查：%s" % "、".join(bad))
        if merged:
            by_dn = {}
            for p in merged:
                dn = (p.get("doc_number") or "").strip()
                if not dn:
                    continue
                if dn in by_dn:
                    continue          # 同名文号以后载入者忽略（先到先得，避免覆盖基础包）
                pol = STORE.add_policy(p.get("title") or dn, dn,
                                       p.get("effective_date") or "1970-01-01",
                                       category=p.get("category") or "",
                                       source_url=p.get("source_url") or "")
                pol.status = p.get("status") or "active"
                for c in (p.get("clauses") or []):
                    STORE.add_clause(pol, c.get("no") or "", c.get("content") or "",
                                     c.get("status") or "active")
                by_dn[dn] = pol
            for p in merged:
                src = by_dn.get((p.get("doc_number") or "").strip())
                if not src:
                    continue
                for s in (p.get("supersedes") or []):
                    tgt = by_dn.get((s.get("doc_number") or "").strip())
                    if not tgt:
                        continue
                    STORE.add_supersession(src.doc_id, tgt.doc_id, s.get("clause_no"),
                                           s.get("effective_date"), s.get("reason") or "")
            STORE.apply_supersessions()
            n_act = sum(1 for p in STORE.policies.values() if p.status in ("active", "partially_invalid"))
            n_cls = sum(1 for p in STORE.policies.values() if p.status in ("active", "partially_invalid")
                        for c in p.clauses if c.clause_status == "active")
            print("[语料] 共 %d 个语料包载入 %d 份政策 / %d 条有效条款（可检索 %d 份）"
                  % (len(files), len(by_dn), n_cls, n_act))
            sync_core_corpus_to_db(by_dn)
            return by_dn
        print("[语料] ⚠️ 所有语料文件均未解析出政策，已退回最小兜底种子——请检查！")
    else:
        print("[语料] ⚠️ 未找到 %s（core*.json），已退回最小兜底种子！" % CORE_CORPUS)
    # —— 兜底最小种子（语料文件缺失时，保证服务可用）——
    p19 = STORE.add_policy("财政部 税务总局关于增值税小规模纳税人减免增值税政策的公告",
                           "财政部 税务总局公告2023年第19号", "2023-01-01", category="增值税")
    STORE.add_clause(p19, "第一条", "对月销售额10万元以下（含本数）的增值税小规模纳税人，免征增值税。")
    return {p19.doc_number: p19}


def sync_core_corpus_to_db(by_dn):
    """把 core 语料（**人工核实过的正文 + 条款**）同步进 `policy_registry`。

    为什么必须做：目录检索（`/api/policy/search`）、覆盖统计（`catalog-stats`）、
    失效回扫（`policy_rescan.py`）**都读 policy_registry**。此前 core 语料只进内存
    STORE，导致「答得出、却搜不到」「回扫扫不到 core 正文」的口径不一致——
    典型案例：`财政部 税务总局公告2026年第10号` 在 STORE 有 144 条条款，库里却是空壳。

    规则：**以 core 为准**写入该文号的行；**不动**抓取得到的其它行。
    幂等：重复启动只覆盖同一文号的正文与条款，不产生重复。
    """
    if not by_dn:
        return 0
    cx = db()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n_new = n_upd = 0
        for dn, pol in by_dn.items():
            body = "\n".join("%s %s" % (c.clause_no, c.content) for c in pol.clauses)
            row = cx.execute("SELECT id FROM policy_registry WHERE doc_number=?", (dn,)).fetchone()
            if row is None:
                cur = cx.execute(
                    "INSERT INTO policy_registry (title,doc_number,doc_type,effective_date,status,"
                    "category,content_text,source_url,last_verified_at,official_effectlevel,source_meta) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pol.title, dn, "policy", getattr(pol, "effective_date", ""), pol.status,
                     getattr(pol, "category", ""), body, getattr(pol, "source_url", ""), now,
                     "人工核实语料包（core）",
                     json.dumps({"origin": "core_corpus"}, ensure_ascii=False)))
                pid = cur.lastrowid
                n_new += 1
            else:
                pid = row["id"]
                cx.execute(
                    "UPDATE policy_registry SET title=?, "
                    "content_text=CASE WHEN ?<>'' THEN ? ELSE content_text END, "
                    "status=?, category=COALESCE(NULLIF(?,''),category), "
                    "effective_date=COALESCE(NULLIF(?,''),effective_date), "
                    "source_url=COALESCE(NULLIF(?,''),source_url), last_verified_at=? WHERE id=?",
                    (pol.title, body, body, pol.status, getattr(pol, "category", ""),
                     getattr(pol, "effective_date", ""), getattr(pol, "source_url", ""), now, pid))
                cx.execute("DELETE FROM policy_clause WHERE doc_id=?", (pid,))
                n_upd += 1
            for i, c in enumerate(pol.clauses):
                cx.execute(
                    "INSERT INTO policy_clause (doc_id,clause_no,content,clause_status,display_order) "
                    "VALUES (?,?,?,?,?)",
                    (pid, c.clause_no, c.content, getattr(c, "clause_status", "active"), i))
        cx.commit()
        print("[语料] core 语料已同步入库：新增 %d 份 / 更新 %d 份（供目录检索、覆盖统计、失效回扫使用）"
              % (n_new, n_upd))
        return n_new + n_upd
    except Exception as e:
        print("[语料] ⚠️ core 同步入库失败（不影响检索，仅影响目录口径）：", e)
        return 0
    finally:
        cx.close()


def load_policies_from_db():
    """把 SQLite policy_registry 中已生效(active)的政策**连同条款**合并进内存裁决引擎 STORE。

    ⚠️ 关键修复：此前本函数**只载入标题、不载入条款**，导致"复核生效"的政策
    在 RAG 里检索不到任何条文（这是"总是查不到依据"的结构性根因之一）。
    现在按 doc_id 载入 policy_clause；若某文件没有条款但有全文 content_text，
    则以"正文"整体作为一条条款载入，保证可被检索。

    返回：STORE 中当前可检索（active/partially_invalid）的政策总数。
    """
    rows = query(
        "SELECT id,doc_number,title,effective_date,category,source_url,content_text "
        "FROM policy_registry WHERE status IN ('active','partially_invalid') "
        # 只加载"有实质内容"的：空壳（无正文且无条款）进了 STORE 只会占位、污染检索
        "AND (length(coalesce(content_text,''))>0 "
        "     OR EXISTS(SELECT 1 FROM policy_clause c WHERE c.doc_id=policy_registry.id))")
    for r in rows:
        if not r["doc_number"]:
            continue
        pol = next((p for p in STORE.policies.values() if p.doc_number == r["doc_number"]), None)
        if pol is None:
            pol = STORE.add_policy(r["title"], r["doc_number"], r["effective_date"] or "1970-01-01",
                                   category=r["category"] or "", source_url=r["source_url"] or "")
        if pol.clauses:
            continue  # 已有条款（seed 或上次已载入），不重复载入
        cls = query(
            "SELECT clause_no,content,clause_status,invalid_since,superseded_by FROM policy_clause "
            "WHERE doc_id=? ORDER BY display_order,id", (r["id"],))
        for c in cls:
            STORE.add_clause(pol, c["clause_no"], c["content"], c["clause_status"] or "active",
                             c["invalid_since"], c["superseded_by"])
        if not cls and (r["content_text"] or "").strip():
            STORE.add_clause(pol, "正文", r["content_text"].strip())
    STORE.apply_supersessions()
    return sum(1 for p in STORE.policies.values() if p.status in ("active", "partially_invalid"))


def seed_org():
    """演示机构（幂等）：昆山示范代账服务部 + 负责人 + 2 个客户 + 1 条标准口径。"""
    if query("SELECT id FROM organizations LIMIT 1"):
        return
    cx = db(); cur = cx.cursor()
    cur.execute("INSERT INTO organizations (name,org_code,plan,seat_total,contact) VALUES (?,?,?,?,?)",
                ("昆山示范代账服务部", "ORGDEMO", "trial", 5, "张老师"))
    org_id = cur.lastrowid
    salt = secrets.token_hex(8)
    cur.execute("INSERT INTO org_member (org_id,login_name,pwd_hash,pwd_salt,name,role,seat_status) "
                "VALUES (?,?,?,?,?, 'owner', 'active')",
                (org_id, "owner", pwd_hash("demo1234", salt), salt, "机构负责人"))
    cur.execute("INSERT INTO client_profile (org_id,name,credit_no,industry,region,taxpayer_type) VALUES (?,?,?,?,?,?)",
                (org_id, "昆山某某建材有限公司", "91320583XXXXXX", "建筑", "昆山", "一般纳税人"))
    cur.execute("INSERT INTO client_profile (org_id,name,credit_no,industry,region,taxpayer_type) VALUES (?,?,?,?,?,?)",
                (org_id, "苏州某某餐饮店", "92320505XXXXXX", "餐饮", "苏州", "小规模纳税人"))
    cur.execute("INSERT INTO org_standard_answer (org_id,category,question_pattern,answer_md,policy_refs,steps,status,approved_by) "
                "VALUES (?,?,?,?,?,?, 'active', ?)",
                (org_id, "注销", "注销,清税,流程",
                 "公司注销应先办理税务注销：结清应纳税款、滞纳金、罚款，缴销发票及税控设备，取得清税文书后再办理工商注销。（具体材料与流程以主管税务机关口径为准）",
                 json.dumps(["税总发〔2018〕149号 关于进一步优化办理企业税务注销程序的通知"], ensure_ascii=False),
                 "① 结清税款/滞纳金/罚款 ② 缴销发票与税控设备 ③ 申请清税证明 ④ 办理工商注销",
                 "张老师"))
    cx.commit(); cx.close()


# =====================================================================
# 3) DB 工具
# =====================================================================
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=30)
    cx.row_factory = sqlite3.Row
    # 并发加固：后台 AI 先答线程与请求线程可能同时写库，busy_timeout 让写入者等待而非直接报错。
    cx.execute("PRAGMA busy_timeout=30000")
    return cx


def exec(sql, params=()):
    cx = db()
    try:
        cx.execute(sql, params)
        cx.commit()
    finally:
        cx.close()


def query(sql, params=()):
    cx = db()
    try:
        cur = cx.execute(sql, params)
        return cur.fetchall()
    finally:
        cx.close()


def init_db():
    cx = db()
    try:
        # WAL：允许并发读 + 单写，避免多用户/后台线程同写触发 database is locked
        try:
            cx.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cx.executescript(SCHEMA)
        cx.commit()
        # ⚠️ 必须显式迁移：CREATE TABLE IF NOT EXISTS **不会**给已存在的表补列。
        # 缺了这步，老库上任何引用新列（official_aging 等）的查询都会直接报错。
        try:
            added = pi.migrate(cx)
            if added:
                print("[迁移] 新增列：%s" % "、".join(added))
        except Exception as e:
            print("[迁移] 失败：", e)
    finally:
        cx.close()

    # pricing seed（幂等）
    for cat, name, price, route, note in [
        ("ai_free", "AI问答(免费配额)", 0, "app", "每日免费配额"),
        ("ai_deep", "AI深度问答", 8, "app", "带文号溯源长解读"),
        ("human_quick", "人工快答(单点问题)", 30, "app", "不论难易一口价，复杂转L3"),
        ("project", "项目类(筹划/稽查/股权/注销)", 0, "l3", "不按积分，转企微正式委托"),
    ]:
        query("SELECT id FROM pricing_config WHERE category=?", (cat,))
        if not query("SELECT id FROM pricing_config WHERE category=?", (cat,)):
            exec("INSERT INTO pricing_config (category,name,price_points,route_to,note) VALUES (?,?,?,?,?)",
                 (cat, name, price, route, note))

    seed_policies()
    load_policies_from_db()
    seed_org()
    seed_risk_rules()

    # demo 用户（幂等）
    if not query("SELECT id FROM users WHERE openid=?", (DEMO_OPENID,)):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        exp = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO users (openid,nickname,identity_tag,invite_code) VALUES (?,?,?,?)",
            (DEMO_OPENID, "张德富 · 张老师", "企业主/财税专家", "HG-TAX-8821"))
        uid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO membership (user_id,level) VALUES (?, 'free')", (uid,))
        cur.execute("INSERT INTO points_account (user_id,balance) VALUES (?,0)", (uid,))
        # 注册赠50（30天有效期）
        cur.execute(
            "INSERT INTO points_ledger (user_id,txn_type,amount,reason,expire_at) VALUES (?, 'earn', 50, 'register_gift', ?)",
            (uid, exp))
        cur.execute("UPDATE points_account SET balance=50 WHERE user_id=?", (uid,))
        # 几笔历史流水
        cur.execute("INSERT INTO points_ledger (user_id,txn_type,amount,reason,ref_id) VALUES (?, 'earn', 20, 'invite', '王会计')", (uid,))
        cur.execute("UPDATE points_account SET balance=balance+20 WHERE user_id=?", (uid,))
        cur.execute("INSERT INTO points_ledger (user_id,txn_type,amount,reason,ref_id) VALUES (?, 'spend', 30, 'human_answer', 'Q1002')", (uid,))
        cur.execute("UPDATE points_account SET balance=balance-30 WHERE user_id=?", (uid,))
        cur.execute("INSERT INTO points_ledger (user_id,txn_type,amount,reason,ref_id) VALUES (?, 'spend', 8, 'ai_deep', 'Q1003')", (uid,))
        cur.execute("UPDATE points_account SET balance=balance-8 WHERE user_id=?", (uid,))
        # 示例提问
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status) VALUES (?,?,?,?,?,?)",
            (uid, "小规模纳税人季度30万内免增值税怎么算？",
             "自2023-01-01起，小规模纳税人合计月销售额未超过10万元（按季30万元）的，免征增值税。",
             "ai", 0, "answered"))
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status) VALUES (?,?,?,?,?,?)",
            (uid, "跨年取得的发票能否在当年税前扣除？", "需结合业务实质与税前扣除凭证时间性规定判定，建议由专家结合具体凭证确认。",
             "human", 30, "answered"))
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,routed_to) VALUES (?,?,?,?,?,?,?)",
            (uid, "公司股权转让架构怎么设计更省税？", "", "human", 0, "l3", "l3"))
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status) VALUES (?,?,?,?,?,?)",
            (uid, "建筑劳务分包自然人代开发票个税由谁扣？", "", "ai", 0, "pending"))
        # 示例收藏
        cur.execute("INSERT OR IGNORE INTO collection (user_id,collect_type,ref_id,snapshot_status) VALUES (?, 'policy', '2023年第19号', '现行有效')", (uid,))
        cur.execute("INSERT OR IGNORE INTO collection (user_id,collect_type,ref_id,snapshot_status) VALUES (?, 'policy', '2019年第4号', '部分失效')", (uid,))
        cur.execute("INSERT OR IGNORE INTO collection (user_id,collect_type,ref_id,snapshot_status) VALUES (?, 'case', 'C001', '')", (uid,))
        # 专家占位
        cur.execute("INSERT OR IGNORE INTO expert (name,title,status,display_order) VALUES ('张老师（慧根堂）','税务服务数十年一线实务','active',1)")
        cur.execute("INSERT OR IGNORE INTO expert (name,title,status,display_order) VALUES (NULL,'资深注册会计师（邀约入驻中）', 'coming',2)")
        cur.execute("INSERT OR IGNORE INTO expert (name,title,status,display_order) VALUES (NULL,'税务师（邀约入驻中）','coming',3)")
        cx.commit(); cx.close()


# =====================================================================
# 4) 业务：积分 / 提问 / 政策裁决
# =====================================================================
def get_uid(openid):
    r = query("SELECT id FROM users WHERE openid=?", (openid,))
    return r[0]["id"] if r else None


def add_points(uid, amount, reason, ref_id=None, expire_at=None):
    cx = db()
    cx.execute(
        "INSERT INTO points_ledger (user_id,txn_type,amount,reason,ref_id,expire_at) VALUES (?,?,?,?,?,?)",
        (uid, "earn" if amount >= 0 else "spend", abs(amount), reason, ref_id, expire_at))
    cx.execute("UPDATE points_account SET balance=balance+? WHERE user_id=?", (amount, uid))
    cx.commit(); cx.close()


def add_reputation(uid, amount, reason=None):
    """声望：从业者的可信度信号（与积分货币分离）。只增不减（amount>0）；
    若误传负或为零则不动作，避免被刷。"""
    if not uid or amount <= 0:
        return
    cx = db()
    cx.execute("UPDATE users SET reputation=reputation+? WHERE id=?", (amount, uid))
    cx.commit(); cx.close()


def classify_category(q):
    q = q or ""
    if "增值税" in q or "小规模" in q or "免税" in q:
        return "增值税"
    if "企业所得税" in q:
        return "企业所得税"
    if "个税" in q or "个人所得税" in q:
        return "个税"
    if "社保" in q:
        return "社保"
    return None


def answer_question(question):
    """核心：用政策引擎裁决 → 取现行有效最新条款 → 强制免责注入。"""
    cat = classify_category(question)
    pol = STORE.resolve_active(cat) if cat else None
    if pol and pol.clauses:
        valid = STORE.valid_clauses(pol)
        if valid:
            clause = valid[0]
            raw = clause.content
            refs = [pol.doc_number]
            is_interp = False
        else:
            raw = "该文件相关条款均已失效或暂无有效条款，建议转人工快答由专家结合最新口径判定。"
            refs = []
            is_interp = True
    else:
        raw = "该问题涉及具体业务情形，AI 暂无法给出确定性结论。建议转人工快答（单点问题统一 30 积分），由资深专家结合您企业实际判定。"
        refs = []
        is_interp = True
    answer = pe.generate_answer(raw, is_interp)
    return answer, refs, is_interp


# =====================================================================
# 4.1) M4+ 大模型 RAG 接入：检索增强 + 大模型生成（保留免责+时效校验）
# =====================================================================
def llm_available():
    return la.is_configured()


def search_policy_catalog(question, limit=15):
    """全量目录检索（**含 status='indexed' 的仅索引条目**）。

    回答的是从业者最实际的问题：「**库里到底有没有这份文件**」。
    indexed 条目没有正文、**不可作依据**，但能给出文号/时效/官方链接，
    让"没收录正文"不等于"查不到"——被问到即可按需升级（抓正文→复核→active）。

    排序：① 先试**文号精确线索**（从业者常直接贴文号）；
         ② 否则按**命中关键词数**排序（标题权重 2、文号权重 4），再按
            可依据 > 部分失效 > 待复核 > 仅目录 > 失效。
    返回 [{doc_number,title,status,aging,category,effective_date,source_url,has_clauses,status_label}]
    """
    q = (question or "").strip()
    if not q:
        return []
    LABEL = {"active": "现行有效·可引用", "partially_invalid": "部分失效·注意条款时限",
             "pending_review": "待复核·未生效", "indexed": "仅目录·未收录正文",
             "invalid": "已失效/废止"}
    COLS = ("p.doc_number,p.title,p.status,p.official_aging,p.category,"
            "p.effective_date,p.source_url,"
            "(SELECT COUNT(*) FROM policy_clause c WHERE c.doc_id=p.id "
            " AND c.clause_status='active') n_act")
    PRIO = ("CASE p.status WHEN 'active' THEN 0 WHEN 'partially_invalid' THEN 1 "
            "WHEN 'pending_review' THEN 2 WHEN 'indexed' THEN 3 WHEN 'invalid' THEN 4 ELSE 5 END")
    rows = []
    # ① 文号精确线索优先（如"2023年第12号""财税〔2018〕33号"）
    m = re.search(r'(\d{4})\s*年第\s*([0-9]+)\s*号', q)
    if m:
        pat = "%" + m.group(1) + "年第" + m.group(2) + "号%"
        try:
            rows = query("SELECT " + COLS + " FROM policy_registry p "
                         "WHERE p.doc_number LIKE ? AND p.status<>'archived' "
                         "ORDER BY " + PRIO + ", p.effective_date DESC LIMIT ?", (pat, limit))
        except Exception:
            rows = []
    # ② 关键词检索：按**命中数**排序（避免"任一词命中就返回"导致离题）
    if not rows:
        toks, seen = [], set()
        for t in rag.tokenize(q):
            if len(t) >= 2 and t not in seen:
                seen.add(t)
                toks.append(t)
            if len(toks) >= 14:
                break
        if not toks:
            return []
        hit = " + ".join("(CASE WHEN p.title LIKE ? THEN 2 ELSE 0 END + "
                         "CASE WHEN p.doc_number LIKE ? THEN 4 ELSE 0 END)" for _ in toks)
        where = " OR ".join("(p.title LIKE ? OR p.doc_number LIKE ?)" for _ in toks)
        hp, wp = [], []
        for t in toks:
            hp += ["%" + t + "%", "%" + t + "%"]
            wp += ["%" + t + "%", "%" + t + "%"]
        try:
            rows = query("SELECT " + COLS + ", (" + hit + ") AS hits "
                         "FROM policy_registry p WHERE p.status<>'archived' AND (" + where + ") "
                         "ORDER BY hits DESC, " + PRIO + ", p.effective_date DESC LIMIT ?",
                         tuple(hp + wp + [limit]))
        except Exception:
            return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "doc_number": d.get("doc_number") or "", "title": d.get("title") or "",
            "status": d.get("status"), "aging": d.get("official_aging") or "",
            "category": d.get("category") or "", "effective_date": d.get("effective_date") or "",
            "source_url": d.get("source_url") or "", "has_clauses": d.get("n_act") or 0,
            "hits": d.get("hits"),
            "status_label": LABEL.get(d.get("status"), d.get("status")),
        })
    return out


def build_llm_system_prompt():
    return (
        "你是「税镜」的财税专业助手，服务对象是**财税从业者**（会计、代账、财税顾问、企业税务岗），"
        "不是终端企业老板。用户会把你的答复**拿去回答客户、写进底稿、作为判断依据**，"
        "因此答复必须『可引用、可复核、可直接使用』。\n"
        "硬性要求：\n"
        "1. 只能依据用户消息中给出的『现行有效政策原文』作答；严禁编造文号、条款、税率、日期或任何政策内容。原文没有的，不要写。\n"
        "2. 必须标注文号（如《财政部 税务总局公告2023年第19号》），能标到条款的标到条款。\n"
        "3. 多条政策对同一事项规定不同的，以施行日期更晚者为准（新优于旧），并点明该判断。\n"
        "4. 输出结构（简洁、可直接对客）：\n"
        "   ① 结论：一句话给判断；\n"
        "   ② 政策依据：文号 + 条款 + 原文要点；\n"
        "   ③ 适用条件：什么情形适用、什么情形不适用；\n"
        "   ④ 操作要点：怎么办、办什么、时限；\n"
        "   ⑤ 需向客户确认的关键信息（供从业者核对，避免答错）。\n"
        "5. 语言：专业、克制、给方案；用“建议”而非“忠告”；不口语化、不说教。\n"
        "6. 结尾由系统统一注入免责声明，你不要自己写免责语。\n"
        "7. 禁止以“没有政策依据”“无法给出确定性结论”之类空话作为答复主体——你的任务是给出可用依据与判断路径。"
    )


def build_llm_related_prompt():
    return (
        "你是「税镜」的财税专业助手，服务对象是**财税从业者**（会计、代账、财税顾问、企业税务岗）。\n"
        "本次检索**未找到与该问题完全对应**的现行条款，但**不允许**只回一句“没有政策依据”就结束——"
        "那对从业者没有任何价值。请你：\n"
        "1. 从下面给出的『相关现行政策』中挑出最相关的，说明各自管什么、与用户问题差在哪里（能否类推适用）；\n"
        "2. 给出该事项的**通行处理口径与操作要点**（基于税收征管常识）；**绝对不得编造文号、税率、日期**；"
        "不得使用《》引用未提供的文件；\n"
        "3. 明确列出**还需向客户或主管税务机关确认的关键条件**（这是从业者最需要的部分）；\n"
        "4. 如下面线索与问题确实无关，就直说“该问题超出已收录范围”，并**建议转专家快答**由资深专家定调；\n"
        "5. 语言专业、克制；结尾免责由系统统一注入；\n"
        "6. 再次强调：不要用“无政策依据”“仅供参考”这类空话作为答复主体，要给出可用的判断路径。"
    )


def answer_question_smart(question):
    """RAG + LLM：**分层检索** → grounding prompt → 大模型生成 → 系统注入免责。

    tier=exact  → mode=deep_llm           （有直接依据，可直接引用对客）
    tier=domain → mode=deep_llm_related   （相关现行政策 + 通行口径 + 待确认条件）
    tier=index  → mode=deep_llm_related   （给可查文件线索 + 通行口径）
    tier=none   → 返回 None，由调用方回退规则引擎

    设计红线：绝不"空手而归"（一不中就甩一句"没有政策依据"），也绝不"错接地"
    （把不相关政策当依据）。任意环节失败返回 None，保证体验不中断。
    """
    chunks, refs, _top, tier = rag.retrieve_tiered(
        STORE, question, top_k=6, category=classify_category(question))
    # 未精确命中时，叠加一层**全量目录线索**（含仅索引未抓正文的文件）——
    # 让"库里没收录正文"不再等于"查不到"。
    catalog = [] if tier == "exact" else search_policy_catalog(question, limit=12)
    if not chunks and not catalog:
        return None                      # 确无任何线索 → 由调用方回退规则引擎
    context = "\n\n".join(chunks)
    if tier == "exact":
        system = build_llm_system_prompt()
        user = (f"用户问题：{question}\n\n"
                f"以下为现行有效政策原文（必须且仅据此作答，并标注文号）：\n{context}")
        mode = "deep_llm"
    else:
        system = build_llm_related_prompt()
        if catalog:
            lines = []
            for c in catalog:
                bits = []
                if c["effective_date"]:
                    bits.append("施行 " + c["effective_date"])
                if c["aging"]:
                    bits.append("官方时效 " + c["aging"])
                bits.append(c["status_label"])
                lines.append("· %s《%s》（%s）%s"
                             % (c["doc_number"] or "（无文号）", c["title"][:58],
                                "；".join(bits),
                                ("  官方原文：" + c["source_url"]) if c["source_url"] else ""))
            context = (context + "\n\n【本库全量目录线索（含仅收录目录、未收录正文的文件）】\n"
                       + "\n".join(lines))
        user = (f"用户问题：{question}\n\n"
                f"以下为本库中与该问题相关的现行政策线索（可能是同领域政策，或仅为文件索引，"
                f"不必然直接适用于该问题，请自行判断相关性与适用条件）。"
                f"其中标「仅目录·未收录正文」的**只能用于提示用户去查原文，不得据其内容下结论、"
                f"不得编造其条款内容**：\n{context}")
        mode = "deep_llm_related"
    try:
        text = la.chat(system, user)
    except Exception:
        return None
    if not text:
        return None
    # 复用生成层统一免责注入（不依赖模型自觉）
    answer = pe.generate_answer(text, is_interpretation=False)
    # 第二道防幻觉护栏：答案中出现的《...》文号若不在「依据 + 目录线索」范围内，追加警示
    suspicious = validate_answer_refs(
        answer, refs, [c["doc_number"] for c in catalog if c["doc_number"]])
    if suspicious:
        answer = (answer + "\n\n※ 提示：以上作答引用了未在检索到的现行有效政策范围内的文号（"
                  + "、".join(suspicious) + "），请务必以官方发布原文或转专家快答核实，本助手不保证其准确性。")
    if tier != "exact":
        tip = ("※ 说明：库中暂无与本题完全对应的条款，以下为**相关现行政策 + 通行处理口径**，"
               "请核对适用条件后使用。")
        idx_only = [c for c in catalog if c["status"] in ("indexed", "pending_review")]
        if idx_only:
            lines = ["· %s《%s》%s" % (c["doc_number"] or "（无文号）", c["title"][:44],
                                      ("  " + c["source_url"]) if c["source_url"] else "")
                     for c in idx_only[:5]]
            tip += ("\n本库目录中另有以下文件**已收录、但尚未核对正文**，可点官方原文进一步查证：\n"
                    + "\n".join(lines))
        answer = tip + "\n\n" + answer
    return answer, refs, False, mode


def answer_dispatch(question):
    """统一入口：优先 RAG+LLM，未配置/失败时回退规则引擎。返回 (answer, refs, is_interp, mode)。"""
    answer, refs, is_interp = answer_question(question)
    mode = "rule_based"
    if llm_available():
        res = answer_question_smart(question)
        if res:
            answer, refs, is_interp, mode = res
    return answer, refs, is_interp, mode


# ---- 同行问：AI 先答（后台线程，复用 ask 管线，不扣积分）----
COMMUNITY_DISCLAIMER = ("※ 以上内容为从业经验交流与政策检索参考，不构成正式税务意见；"
                        "具体业务请以最新官方文件及主管税务机关口径为准。")
SCENE_TAGS = ("发票", "申报", "风控预警", "工商变更", "资质办理", "稽查应对", "其他")


def _community_ai_answer(post_id, question):
    """发帖后异步生成 AI 参考答案垫底（冷启动兜底：任何帖子 30 秒内有带依据的答案）。"""
    try:
        answer, refs, _is_interp, mode = answer_dispatch(question)
        payload = {"answer": answer, "policy_refs": refs}
    except Exception as e:
        payload = {"answer": "（AI 参考生成异常，请等待同行回答或转人工快答）", "policy_refs": []}
        mode = "error:" + str(e)[:80]
    try:
        exec("UPDATE community_post SET ai_answer=?, ai_mode=? WHERE id=?",
             (json.dumps(payload, ensure_ascii=False), mode, post_id))
    except Exception:
        pass


# =====================================================================
# 4.5) 全局口径锁定层：一致性引擎（确定性检索 + 版本化）
# =====================================================================
_PUNCT = "？?！!。.，,、;；:：\"'（）()[]【】\n\t\r/\\"


def normalize_sig(q):
    """问题归一化指纹：去标点、小写、压缩空白。同一问题不同表述可归一到相近 signature。"""
    s = (q or "").lower()
    for p in _PUNCT:
        s = s.replace(p, " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _derive_keywords(sig):
    """从归一化指纹抽取触发词（去掉常见停用词）。"""
    stop = {"的", "了", "吗", "呢", "是", "在", "和", "或", "与", "及", "怎么", "如何",
            "什么", "多少", "可以", "需要", "要", "有", "没有", "是否", "应该", "是否",
            "一个", "这个", "那个", "我们", "我", "你", "他", "她", "它", "们", "a", "the"}
    toks = [t for t in re.split(r"\s+", sig) if t and t not in stop]
    # 中文无空格时按 2~4 字切分做兜底
    if not toks:
        toks = [sig[i:i + 2] for i in range(0, max(1, len(sig) - 1), 2)]
    return ",".join(toks[:12])


def resolve_canonical(q):
    """确定性检索：返回命中的活跃口径（绝不重新生成）。
    命中规则：① 归一化 signature 完全相等；② 触发词全中（强置信）；③ 触发词覆盖率≥0.6。
    返回唯一一条，保证同一题答案恒定。"""
    sig = normalize_sig(q)
    if not sig:
        return None
    rows = query("SELECT * FROM canonical_answer WHERE status='active'")
    # ① 完全相等
    for r in rows:
        if normalize_sig(r["signature"]) == sig:
            return r
    # ②/③ 触发词匹配
    best = None
    best_score = 0
    for r in rows:
        kws = [k for k in (r["keywords"] or "").split(",") if k.strip()]
        if not kws:
            continue
        matched = sum(1 for k in kws if k in q)
        if matched == 0:
            continue
        # 归一指纹互为包含 → 强置信加成
        rsig = normalize_sig(r["signature"])
        if rsig in sig or sig in rsig:
            matched += 2
        ratio = matched / len(kws)
        if ratio >= 0.6 and matched > best_score:
            best_score = matched
            best = r
    return best


def _safe_refs(raw):
    """政策依据字段统一解析为列表。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def resolve_org_standard(q, org_id):
    """机构口径确定性检索：返回 (best_row, score)；无命中返回 (None, 0)。"""
    rows = query("SELECT * FROM org_standard_answer WHERE org_id=? AND status='active'", (org_id,))
    best, best_score = None, 0
    for r in rows:
        pat = (r["question_pattern"] or "").strip()
        kws = [k for k in re.split(r"[，,、\s]+", pat) if k]
        if not kws:
            continue
        score = sum(1 for k in kws if k in q)
        if pat in q:
            score += len(kws)
        if score > best_score:
            best_score, best = score, r
    return (best, best_score) if (best and best_score >= 1) else (None, 0)


def classify_intent(q):
    """对话意图分类：决定卡片动作(action)与后续建议(suggestions)，不替代知识检索。"""
    if any(w in q for w in ["派任务", "下派", "安排给", "任务给", "分配给", "谁去做", "交办", "分派", "布置", "派个", "派单", "派活", "派给"]) or ("派" in q and "任务" in q):
        return "task"
    if any(w in q for w in ["提醒", "征期", "申报期", "日历", "别忘", "什么时候申报", "截止日", "到期", "期限"]):
        return "subscribe"
    if any(w in q for w in ["同行", "案例", "有人遇到", "大家怎么", "经验", "有没有人", "群里", "同行问"]):
        return "community"
    if any(w in q for w in ["客户", "体检", "风险扫描", "应享未享", "这家公司", "我们客户", "某某公司", "企业风险"]):
        return "client_risk"
    return "policy"


# =====================================================================
# 5) API 路由
# =====================================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, cors=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gate_sensitive(self, path):
        """敏感接口网关。

        历史方案曾对 /api/admin/ 做来源 IP 白名单，但经与运营方确认：管理后台应允许
        “任何设备、任何地点、凭正确口令 + HTTPS 登录”——IP 白名单会误伤正常运维、
        违背产品使用场景，属错误且有害的设计，已废弃（2026-09-18）。

        现行公网防御实际依赖：
          ① 32 位强随机 ADMIN_TOKEN + HTTPS 加密传输（口令不落地、不在前端泄露）；
          ② 管理登录失败次数锁定（5 次/15 分钟窗口 → 429 临时锁，见 do_GET/do_POST）。
        因此 /api/admin/ 不再做来源 IP 限制，任何可达网络的设备输对口令即可登录。

        仅对真正的服务器内部接口 /api/cron/ 保留本机来源限制（由 systemd timer 本地
        调用，不存在用户登录场景），其余接口一律放行。
        """
        if path.startswith("/api/cron/"):
            ip = self.client_address[0] if self.client_address else ""
            if os.environ.get("TRUST_PROXY") == "1":
                xff = self.headers.get("X-Forwarded-For", "") or ""
                if xff:
                    ip = xff.split(",")[0].strip()
            if ip.startswith("::ffff:"):
                ip = ip[len("::ffff:"):]
            if ip not in ADMIN_ALLOWED_IPS:
                self._send(403, {"ok": False, "msg": "forbidden: cron only callable from localhost"})
                return False
        return True

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        ip = _real_client_ip(self)
        _REQ_CTX.client_ip = ip
        if path.startswith("/api/admin/") or path.startswith("/api/cron/"):
            if not _admin_rate_ok(ip):
                return self._send(429, {"ok": False, "msg": "登录尝试过于频繁，请15分钟后再试", "retry_after": 900})
        if not self._gate_sensitive(path):
            return
        qs = urllib.parse.parse_qs(p.query)
        try:
            if path in ("/", "/tax-ai-prototype.html"):
                if os.path.exists(HTML_PATH):
                    with open(HTML_PATH, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                else:
                    self._send_html("<h1>原型文件缺失</h1>")
                return
            if path in ("/admin.html", "/admin"):
                admin_path = os.path.join(HERE, "admin.html")
                if os.path.exists(admin_path):
                    with open(admin_path, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                else:
                    self._send_html("<h1>运营后台文件缺失</h1>")
                return
            if path in ("/org.html", "/org"):
                org_path = os.path.join(HERE, "org.html")
                if os.path.exists(org_path):
                    with open(org_path, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                else:
                    self._send_html("<h1>机构工作台文件缺失</h1>")
                return
            if path == "/api/state":
                return self._send(200, self.api_state(qs.get("openid", [DEMO_OPENID])[0]))
            if path == "/api/points/ledger":
                return self._send(200, self.api_ledger(qs.get("openid", [DEMO_OPENID])[0]))
            if path == "/api/my/questions":
                return self._send(200, self.api_my_questions(qs.get("openid", [DEMO_OPENID])[0]))
            if path == "/api/my/collections":
                return self._send(200, self.api_my_collections(qs.get("openid", [DEMO_OPENID])[0]))
            if path == "/api/policy/detail":
                return self._send(200, self.api_policy_detail(qs.get("doc", ["2019年第4号"])[0]))
            if path == "/api/policy/fetch":
                return self._send(200, self.api_policy_fetch())
            if path == "/api/policy/brief":
                return self._send(200, self.api_policy_brief({k: v[0] for k, v in qs.items()}))
            if path == "/api/policy/preview":
                return self._send(200, self.api_policy_preview({"id": qs.get("id", [None])[0]}))
            if path == "/api/llm/status":
                return self._send(200, {"ok": True, "enabled": la.is_configured(),
                                        "model": (os.environ.get("LLM_MODEL") or "gpt-4o-mini") if la.is_configured() else ""})
            if path == "/api/policy/list":
                return self._send(200, self.api_policy_list())
            if path == "/api/policy/search":
                try:
                    lim = int((qs.get("limit") or ["20"])[0])
                except Exception:
                    lim = 20
                _items = search_policy_catalog((qs.get("q") or [""])[0], max(1, min(lim, 100)))
                return self._send(200, {
                    "ok": True,
                    "q": (qs.get("q") or [""])[0],
                    "count": len(_items),
                    "items": _items,
                })
            if path == "/api/policy/catalog-stats":
                return self._send(200, self.api_policy_catalog_stats())
            if path == "/api/kefu/info":
                return self._send(200, self.api_kefu_info())
            if path == "/api/human/my":
                return self._send(200, self.api_human_my(qs.get("openid", [DEMO_OPENID])[0]))
            if path == "/api/human/list":
                return self._send(200, self.api_human_list())
            # ---- 同行问（M1）GET ----
            if path == "/api/community/list":
                return self._send(200, self.api_community_list(qs))
            if path == "/api/community/detail":
                return self._send(200, self.api_community_detail(qs))
            if path == "/api/community/profile":
                return self._send(200, self.api_community_profile(qs))
            if path == "/api/community/search-replies":
                return self._send(200, self.api_community_search_replies(qs))
            # ---- 征期日历（留存/订阅消息底座）----
            if path == "/api/calendar/deadlines":
                return self._send(200, {"ok": True, "deadlines": tax_calendar_deadlines(),
                                        "subscribe_tmpl_id": WX_SUBSCRIBE_TMPL_ID})
            if path == "/api/cron/push-deadlines":
                return self._send(200, self.api_cron_push_deadlines(self.headers))
            if path == "/api/admin/community/list":
                return self._send(200, self.api_admin_community_list({}, self.headers))
            if path == "/api/admin/audit":
                return self._send(200, self.api_admin_audit())
            if path == "/api/policy/fetch-status":
                return self._send(200, self.api_policy_fetch_status())
            # ---- 机构版（M1）GET ----
            if path == "/api/org/me":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_me(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/clients":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_clients(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/answers":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_answers(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/members":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_members(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/rules":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_risk_rules(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/input":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_risk_input_get(qs, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/reports":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_risk_reports(s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/task":
                return self._send(200, self.api_org_risk_task(qs))
            if path == "/api/org/tasks":
                s = org_authorized({}, self.headers)
                return self._send(200, self.api_org_tasks(s, qs) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/admin/org/list":
                return self._send(200, self.api_admin_org_list({}, self.headers))
            if path == "/api/admin/canonical/list":
                return self._send(200, self.api_admin_canonical_list(self.headers))
            self._send(404, {"error": "not found", "path": path})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        ip = _real_client_ip(self)
        _REQ_CTX.client_ip = ip
        if path.startswith("/api/admin/") or path.startswith("/api/cron/"):
            if not _admin_rate_ok(ip):
                return self._send(429, {"ok": False, "msg": "登录尝试过于频繁，请15分钟后再试", "retry_after": 900})
        if not self._gate_sensitive(path):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            if path == "/api/register":
                return self._send(200, self.api_register(body))
            if path == "/api/ask":
                return self._send(200, self.api_ask(body))
            if path == "/api/converse":
                return self._send(200, self.api_converse(body, self.headers))
            if path == "/api/canonical/submit":
                return self._send(200, self.api_canonical_submit(body))
            if path == "/api/admin/canonical/list":
                return self._send(200, self.api_admin_canonical_list(self.headers))
            if path == "/api/admin/canonical/approve":
                return self._send(200, self.api_admin_canonical_approve(body, self.headers))
            if path == "/api/admin/canonical/review-repealed":
                return self._send(200, self.api_admin_canonical_review_repealed(self.headers))
            if path == "/api/ask-human":
                return self._send(200, self.api_ask_human(body))
            if path == "/api/invite":
                return self._send(200, self.api_invite(body))
            if path == "/api/buy-points":
                return self._send(200, self.api_buy_points(body))
            if path == "/api/policy/refresh":
                return self._send(200, self.api_policy_refresh())
            if path == "/api/policy/fetch":
                return self._send(200, self.api_policy_fetch())
            if path == "/api/policy/review":
                return self._send(200, self.api_policy_review(body, self.headers))
            if path == "/api/policy/autoreview":
                return self._send(200, self.api_policy_autoreview(body, self.headers))
            if path == "/api/policy/rollback-auto":
                return self._send(200, self.api_policy_rollback_auto(body, self.headers))
            if path == "/api/admin/recharge-confirm":
                return self._send(200, self.api_admin_recharge(body, self.headers))
            if path == "/api/human/submit":
                return self._send(200, self.api_human_submit(body))
            if path == "/api/human/reply":
                return self._send(200, self.api_human_reply(body, self.headers))
            # ---- 同行问（M1）POST ----
            if path == "/api/community/post":
                return self._send(200, self.api_community_post(body))
            if path == "/api/community/reply":
                return self._send(200, self.api_community_reply(body))
            if path == "/api/community/like":
                return self._send(200, self.api_community_like(body))
            if path == "/api/community/accept":
                return self._send(200, self.api_community_accept(body))
            if path == "/api/admin/community/remove":
                return self._send(200, self.api_admin_community_remove(body, self.headers))
            if path == "/api/admin/community/pin":
                return self._send(200, self.api_admin_community_pin(body, self.headers))
            if path == "/api/admin/community/list":
                return self._send(200, self.api_admin_community_list(body, self.headers))
            if path == "/api/user/subscribe":
                return self._send(200, self.api_user_subscribe(body))
            if path == "/api/admin/login":
                return self._send(200, self.api_admin_login(body))
            # ---- 机构版（M1）POST ----
            if path == "/api/org/login":
                return self._send(200, self.api_org_login(body))
            if path == "/api/admin/org/create":
                return self._send(200, self.api_admin_org_create(body, self.headers))
            if path == "/api/org/ask":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_ask(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/client/save":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_client_save(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/client/delete":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_client_delete(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/answer/save":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_answer_save(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/answer/review":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_answer_review(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/member/invite":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_member_invite(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/input/save":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_risk_input_save(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/scan":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_risk_scan(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/risk/batch":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_risk_batch(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/task/create":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_task_create(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/task/update":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_task_update(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            if path == "/api/org/promote-reply":
                s = org_authorized(body, self.headers)
                return self._send(200, self.api_org_promote_reply(body, s) if s else {"ok": False, "msg": "未登录或会话过期"})
            self._send(404, {"error": "not found", "path": path})
        except Exception as e:
            self._send(500, {"error": str(e)})

    # ---- 具体 API ----
    def api_state(self, openid):
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
        mem = query("SELECT level FROM membership WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        inv = query("SELECT COUNT(*) c FROM invite_chain WHERE inviter_id=?", (uid,))
        code = query("SELECT invite_code FROM users WHERE id=?", (uid,))
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        ledger = query(
            "SELECT txn_type,amount,reason,created_at FROM points_ledger WHERE user_id=? AND created_at>=?",
            (uid, today + " 00:00:00"))
        earn_today = sum(r["amount"] for r in ledger if r["txn_type"] == "earn")
        total_earn = sum(r["amount"] for r in query(
            "SELECT amount FROM points_ledger WHERE user_id=? AND txn_type='earn'", (uid,)))
        # 机构身份（统一账号缝）：若该 openid 对应的主账号挂有有效机构席位，返回机构上下文
        org_ctx = None
        ou = query("SELECT om.org_id, om.role, o.name, o.org_code FROM org_member om "
                   "JOIN organizations o ON o.id=om.org_id "
                   "WHERE om.user_id=? AND om.seat_status='active'", (uid,))
        if ou:
            r = ou[0]
            org_ctx = {
                "org_id": r["org_id"], "org_name": r["name"],
                "org_code": r["org_code"], "role": r["role"],
                "modules": ORG_MODULES_BY_ROLE.get(r["role"], ORG_MODULES_MEMBER),
            }
        return {
            "ok": True,
            "openid": openid,
            "balance": acc["balance"],
            "level": mem[0]["level"] if mem else "free",
            "invite_count": inv[0]["c"],
            "invite_code": code[0]["invite_code"] if code else "",
            "today_earn": earn_today,
            "total_earn": total_earn,
            "org": org_ctx,
        }

    def api_user_subscribe(self, body):
        """记录用户对征期提醒模板的订阅授权（小程序点「开启提醒」后回调）。"""
        openid = body.get("openid") or ""
        tmpl_id = body.get("tmpl_id") or WX_SUBSCRIBE_TMPL_ID
        if not openid:
            return {"ok": False, "msg": "缺少 openid"}
        if not tmpl_id:
            return {"ok": False, "msg": "未配置订阅模板"}
        now = _now_str()
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO user_subscription (openid,tmpl_id,granted_at,status,last_deadline_key) "
            "VALUES (?,?,?,'active',NULL) "
            "ON CONFLICT(openid,tmpl_id) DO UPDATE SET granted_at=excluded.granted_at, "
            "status='active', last_deadline_key=NULL",
            (openid, tmpl_id, now))
        cx.commit(); cx.close()
        return {"ok": True, "msg": "已记录订阅授权，征期前将自动提醒"}

    def api_cron_push_deadlines(self, headers):
        t = (headers or {}).get("X-Cron-Token", "") or ""
        if t != CRON_TOKEN and t != ADMIN_TOKEN:
            return {"ok": False, "msg": "forbidden"}
        return push_deadline_reminders()

    def api_register(self, body):
        # 优先用 wx.login 的 code 换取真实 openid（稳定身份，支撑积分/订阅/账号）；
        # 缺 AppSecret 或 code 交换失败时优雅降级为传入/生成的 openid，保证可用。
        openid = ""
        code = body.get("code") or ""
        if code:
            real = wx_code_to_openid(code)
            if real:
                openid = real
        if not openid:
            openid = body.get("openid") or ("u_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        nick = body.get("nickname", "新用户")
        code = "HG" + "".join([str((hash(openid) >> i) & 1) for i in range(8)])[:8].zfill(8)
        if not get_uid(openid):
            exp = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            cx = db(); cur = cx.cursor()
            cur.execute("INSERT INTO users (openid,nickname,invite_code) VALUES (?,?,?)", (openid, nick, code))
            uid = cur.lastrowid
            cur.execute("INSERT OR IGNORE INTO membership (user_id) VALUES (?)", (uid,))
            cur.execute("INSERT INTO points_account (user_id,balance) VALUES (?,0)", (uid,))
            cur.execute("INSERT INTO points_ledger (user_id,txn_type,amount,reason,expire_at) VALUES (?, 'earn', 50, 'register_gift', ?)",
                        (uid, exp))
            cur.execute("UPDATE points_account SET balance=50 WHERE user_id=?", (uid,))
            cx.commit(); cx.close()
        return {"ok": True, "openid": openid, "invite_code": code}

    # ===================== 全局口径锁定层（一致性引擎） =====================
    def _load_repeal_docnos(self):
        """读取官方废止目录中的文号集合，用于政策变更新口径标记待复核。"""
        try:
            import os as _os
            p = _os.path.join(HERE, "data", "policies", "_annex", "repeal_catalog.json")
            if _os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                nos = set()
                items = data.get("repealed") or data.get("items") or []
                for it in items:
                    if isinstance(it, dict):
                        for k in ("doc_no", "docno", "文号", "file_no"):
                            if it.get(k):
                                nos.add(str(it[k]).strip())
                    elif isinstance(it, str):
                        nos.add(it.strip())
                return nos
        except Exception:
            pass
        return set()

    def api_ask(self, body):
        openid = body.get("openid", DEMO_OPENID)
        q = (body.get("question") or "").strip()
        qtype = body.get("type", "ai_deep")
        if not q:
            return {"ok": False, "msg": "问题为空"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}

        # —— 第一关：全局口径锁定检索（命中即返回版本化固定答案，绝不重新生成）——
        ca = resolve_canonical(q)
        if ca:
            answer = pe.generate_answer(ca["answer_md"], is_interpretation=False)
            try:
                refs = json.loads(ca["policy_refs"]) if ca["policy_refs"] else []
            except Exception:
                refs = []
            cost = 1  # 锁定检索近乎零成本
            acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
            if acc["balance"] < cost:
                return {"ok": False, "msg": "积分不足，请先获取积分", "balance": acc["balance"], "need": cost}
            add_points(uid, -cost, "canonical")
            cx = db(); cur = cx.cursor()
            cur.execute(
                "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,policy_refs) VALUES (?,?,?,?,?,?,?)",
                (uid, q, answer, "canonical", cost, "answered", json.dumps(refs, ensure_ascii=False)))
            cx.commit(); cx.close()
            return {"ok": True, "answer": answer, "policy_refs": refs,
                    "locked": True, "mode": "canonical", "version": ca["version"],
                    "canonical_id": ca["id"], "domain": ca["domain"],
                    "flag_review": ca["flag_review"], "cost": cost,
                    "balance": acc["balance"] - cost,
                    "msg": ("📋 锁定口径 v%s · 现行有效" % ca["version"]) +
                           ("（政策有变更，待复核）" if ca["flag_review"] else "")}

        # —— 未命中：走原 RAG+大模型，并标记为待沉淀（进入学习闭环）——
        price = query("SELECT price_points FROM pricing_config WHERE category=?", (qtype,))
        cost = price[0]["price_points"] if price else 8
        acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
        if acc["balance"] < cost:
            return {"ok": False, "msg": "积分不足，请先获取积分", "balance": acc["balance"], "need": cost}
        answer, refs, is_interp, mode = answer_dispatch(q)
        add_points(uid, -cost, "ai_deep" if qtype == "ai_deep" else "ai_answer")
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,policy_refs) VALUES (?,?,?,?,?,?,?)",
            (uid, q, answer, "ai", cost, "answered", json.dumps(refs, ensure_ascii=False)))
        cx.commit(); cx.close()
        return {"ok": True, "answer": answer, "policy_refs": refs,
                "is_interpretation": is_interp, "cost": cost, "mode": mode,
                "locked": False, "pending_review": True,
                "balance": acc["balance"] - cost,
                "msg": "未命中锁定口径，已由 AI 生成（待沉淀）；可在回答下「提交为口径」审定后锁定"}

    # ------------------------------------------------------------------
    # 对话编排器：一切基于对话、后端调知识、持续沉淀闭环
    # ------------------------------------------------------------------
    def _converse_card(self, ok, ctype, text, refs, locked, version, canonical_id,
                       domain, flag_review, learnable, source, skills,
                       msg="", pending_review=False, action=None, suggestions=None,
                       cost=0, balance=None):
        card = {
            "type": ctype,
            "text": text,
            "refs": refs or [],
            "locked": locked,
            "version": version,
            "canonical_id": canonical_id,
            "domain": domain,
            "flag_review": flag_review,
            "learnable": learnable,
            "pending_review": pending_review,
            "source": source,
        }
        return {
            "ok": ok,
            "card": card,
            "skills_called": skills,
            "action": action,
            "suggestions": suggestions or [],
            "cost": cost,
            "balance": balance,
            "msg": msg,
        }

    def api_converse(self, body, headers=None):
        """对话统一入口（对话优先 UI 的后端大脑）。

        链路：① 全局口径锁定（一致性保证，命中即返回版本化固定答案）
             ② 机构口径（机构上下文命中即锁定）
             ③ 意图识别（决定卡片动作/建议，不替代知识检索）
             ④ RAG+大模型兜底（标记 pending_review，进入学习闭环）
        返回统一 typed card + skills_called（🧠本次调用）+ suggestions（后续建议）。
        """
        openid = body.get("openid", DEMO_OPENID)
        q = (body.get("question") or "").strip()
        if not q:
            return {"ok": False, "msg": "问题为空"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}

        s = None
        org_token = (headers or {}).get("X-Org-Token") or body.get("org_token")
        if org_token:
            s = org_session_lookup(org_token)

        # ① 全局口径锁定（同一题恒定答案）
        ca = resolve_canonical(q)
        if ca:
            answer = pe.generate_answer(ca["answer_md"], is_interpretation=False)
            refs = _safe_refs(ca["policy_refs"])
            cost = 1
            acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
            if acc["balance"] < cost:
                return {"ok": False, "msg": "积分不足，请先获取积分", "balance": acc["balance"], "need": cost}
            add_points(uid, -cost, "canonical")
            cx = db(); cur = cx.cursor()
            cur.execute(
                "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,policy_refs) VALUES (?,?,?,?,?,?,?)",
                (uid, q, answer, "canonical", cost, "answered", json.dumps(refs, ensure_ascii=False)))
            cx.commit(); cx.close()
            return self._converse_card(
                ok=True, ctype="qa", text=answer, refs=refs, locked=True,
                version=ca["version"], canonical_id=ca["id"], domain=ca["domain"],
                flag_review=ca["flag_review"], learnable=False, source="canonical",
                skills=["口径库(锁定 v%s)" % ca["version"]],
                msg="📋 锁定口径 v%s · 现行有效%s" % (ca["version"],
                    "（政策有变更，待复核）" if ca["flag_review"] else ""),
                cost=cost, balance=acc["balance"] - cost,
                suggestions=[{"label": "查相关政策原文", "intent": "policy_detail"},
                             {"label": "去同行问讨论", "intent": "community"},
                             {"label": "标记此口径", "intent": "mark"}])

        # ② 机构口径
        if s:
            oa, _score = resolve_org_standard(q, s["org_id"])
            if oa:
                answer = pe.generate_answer(oa["answer_md"], is_interpretation=False)
                refs = _safe_refs(oa["policy_refs"])
                cx = db(); cur = cx.cursor()
                cur.execute(
                    "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,policy_refs) VALUES (?,?,?,?,?,?,?)",
                    (uid, q, answer, "org_standard", 0, "answered", json.dumps(refs, ensure_ascii=False)))
                cx.commit(); cx.close()
                return self._converse_card(
                    ok=True, ctype="qa", text=answer, refs=refs, locked=True,
                    version=0, canonical_id=oa["id"], domain="org",
                    flag_review="", learnable=False, source="org_standard",
                    skills=["机构口径库"],
                    msg="🏢 机构标准口径（已锁定）",
                    cost=0, balance=None,
                    suggestions=[{"label": "沉淀为全局口径", "intent": "promote"}])

        # ③ 意图识别（卡片动作 + 建议）
        intent = classify_intent(q)
        skills = ["政策引擎"]

        # ④ 兜底：RAG+大模型，标记待沉淀
        qtype = body.get("type", "ai_deep")
        price = query("SELECT price_points FROM pricing_config WHERE category=?", (qtype,))
        cost = price[0]["price_points"] if price else 8
        acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
        if acc["balance"] < cost:
            return {"ok": False, "msg": "积分不足，请先获取积分", "balance": acc["balance"], "need": cost}
        answer, refs, is_interp, mode = answer_dispatch(q)
        add_points(uid, -cost, "ai_deep" if qtype == "ai_deep" else "ai_answer")
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer,answer_type,cost_points,status,policy_refs) VALUES (?,?,?,?,?,?,?)",
            (uid, q, answer, "ai", cost, "answered", json.dumps(refs, ensure_ascii=False)))
        cx.commit(); cx.close()
        skills.append("RAG+大模型" if llm_available() else "规则引擎")

        # 动作与建议按意图派生
        action = None
        if intent == "community":
            action = {"type": "open_community"}
            suggestions = [{"label": "去同行问发帖", "intent": "community"},
                          {"label": "换个角度问", "intent": "reask"}]
        elif intent == "task":
            action = {"type": "open_org_task"}
            suggestions = [{"label": "去机构工作台派任务", "intent": "task"},
                          {"label": "换个角度问", "intent": "reask"}]
        elif intent == "subscribe":
            action = {"type": "open_calendar"}
            suggestions = [{"label": "看征期日历", "intent": "calendar"},
                          {"label": "设置提醒", "intent": "subscribe"}]
        elif intent == "client_risk":
            action = {"type": "open_org_client"}
            suggestions = [{"label": "去客户体检", "intent": "client"},
                          {"label": "换个角度问", "intent": "reask"}]
        else:
            suggestions = [{"label": "查相关政策原文", "intent": "policy_detail"},
                          {"label": "去同行问讨论", "intent": "community"},
                          {"label": "教我一下（沉淀口径）", "intent": "teach"}]

        return self._converse_card(
            ok=True, ctype="qa", text=answer, refs=refs, locked=False,
            version=0, canonical_id=None, domain="", flag_review="",
            learnable=True, pending_review=True, source="rag_llm",
            skills=skills, action=action, suggestions=suggestions,
            msg="未命中锁定口径，已由 AI 生成（待沉淀）；可在回答下「教我一下」提交为口径",
            cost=cost, balance=acc["balance"] - cost)

    def api_canonical_submit(self, body):
        """用户/同行提交草稿口径（待沉淀入口）：插入 draft，等管理端审定。"""
        uid = get_uid(body.get("openid", DEMO_OPENID))
        q = (body.get("question") or "").strip()
        answer_md = (body.get("answer") or "").strip()
        if not q or not answer_md:
            return {"ok": False, "msg": "问题与答案均必填"}
        sig = normalize_sig(q)
        refs = body.get("policy_refs") or []
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except Exception:
                refs = []
        keywords = body.get("keywords") or _derive_keywords(sig)
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO canonical_answer (domain,title,signature,keywords,answer_md,policy_refs,version,status,source,flag_review) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (body.get("domain") or "policy", body.get("title") or q[:30], sig, keywords,
             answer_md, json.dumps(refs, ensure_ascii=False), 1, "draft", "user", ""))
        nid = cur.lastrowid
        cx.commit(); cx.close()
        return {"ok": True, "id": nid, "msg": "已提交为草稿口径，待管理端审定后锁定"}

    def api_admin_canonical_list(self, headers):
        if not admin_authorized({}, headers):
            return {"ok": False, "msg": "未授权"}
        rows = query(
            "SELECT id,domain,title,signature,version,status,source,flag_review,created_at "
            "FROM canonical_answer ORDER BY (status='active') DESC, flag_review!='' DESC, id DESC")
        return {"ok": True, "items": [dict(r) for r in rows]}

    def api_admin_canonical_approve(self, body, headers):
        """审定：draft→active。若同签名已有活跃版，则旧版置 deprecated 并升版（version+1）。"""
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "未授权"}
        cid = body.get("id")
        row = query("SELECT * FROM canonical_answer WHERE id=?", (cid,))
        if not row:
            return {"ok": False, "msg": "口径不存在"}
        r = row[0]
        cx = db(); cur = cx.cursor()
        if r["status"] == "active":
            cx.commit(); cx.close()
            return {"ok": True, "msg": "已是活跃版", "version": r["version"]}
        # 旧活跃版降级
        cur.execute("UPDATE canonical_answer SET status='deprecated' WHERE signature=? AND status='active'", (r["signature"],))
        cur.execute("SELECT version FROM canonical_answer WHERE signature=? AND status='deprecated' ORDER BY version DESC LIMIT 1", (r["signature"],))
        old = cur.fetchone()
        new_ver = (old["version"] + 1) if old else 1
        cur.execute(
            "UPDATE canonical_answer SET status='active', version=?, supersedes_id=(SELECT id FROM canonical_answer WHERE signature=? AND status='deprecated' ORDER BY version DESC LIMIT 1), flag_review='', updated_at=(datetime('now','localtime')) WHERE id=?",
            (new_ver, r["signature"], cid))
        cx.commit(); cx.close()
        return {"ok": True, "msg": "已审定锁定，版本 v%s" % new_ver, "version": new_ver}

    def api_admin_canonical_review_repealed(self, headers):
        """政策时效驱动：把引用了已废止文号的活跃口径标记为待复核（不静默变更答案）。"""
        if not admin_authorized({}, headers):
            return {"ok": False, "msg": "未授权"}
        docnos = self._load_repeal_docnos()
        if not docnos:
            return {"ok": True, "reviewed": 0, "msg": "未读取到废止目录"}
        rows = query("SELECT id,policy_refs,signature FROM canonical_answer WHERE status='active'")
        hit = 0
        cx = db(); cur = cx.cursor()
        for r in rows:
            refs = []
            try:
                refs = json.loads(r["policy_refs"]) if r["policy_refs"] else []
            except Exception:
                refs = []
            matched = [d for d in refs if any(dn in str(d) for dn in docnos)]
            if matched:
                cur.execute("UPDATE canonical_answer SET flag_review=? WHERE id=?",
                            ("policy_repealed:" + ";".join(matched), r["id"]))
                hit += 1
        cx.commit(); cx.close()
        return {"ok": True, "reviewed": hit, "msg": "已将 %d 条口径标记待复核" % hit}

    def api_ask_human(self, body):
        openid = body.get("openid", DEMO_OPENID)
        q = (body.get("question") or "").strip()
        if not q:
            return {"ok": False, "msg": "问题为空"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        cost = 30
        acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
        if acc["balance"] < cost:
            return {"ok": False, "msg": "积分不足", "balance": acc["balance"]}
        add_points(uid, -cost, "human_answer")
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO human_ticket (user_id,openid,question,status,cost_points) VALUES (?,?,?, 'pending',?)",
            (uid, openid, q, cost))
        tid = cur.lastrowid
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer_type,cost_points,status,routed_to) VALUES (?,?,?,?,?,?)",
            (uid, q, "human", cost, "consult", "app"))
        cx.commit(); cx.close()
        return {"ok": True, "ticket_id": tid, "msg": "已提交人工快答，张老师将在工作时间内答复",
                "cost": cost, "balance": acc["balance"] - cost}

    def api_invite(self, body):
        inviter_code = body.get("inviter_code")
        openid = body.get("openid")
        if not inviter_code or not openid:
            return {"ok": False, "msg": "参数缺失"}
        inv = query("SELECT id FROM users WHERE invite_code=?", (inviter_code,))
        if not inv:
            return {"ok": False, "msg": "邀请码无效"}
        inviter = inv[0]["id"]
        if get_uid(openid):
            return {"ok": False, "msg": "该用户已注册"}
        # 简化：仅演示关系链绑定与双方奖励
        reg = self.api_register({"openid": openid, "nickname": "受邀用户"})
        uid = get_uid(openid)
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT OR IGNORE INTO invite_chain (inviter_id,invitee_id,reward_status,reward_points) VALUES (?,?, 'granted', 10)",
                    (inviter, uid))
        cur.execute("UPDATE points_account SET balance=balance+10 WHERE user_id=?", (inviter,))
        cur.execute("INSERT INTO points_ledger (user_id,txn_type,amount,reason,ref_id) VALUES (?, 'earn', 10, 'invite', ?)",
                    (inviter, openid))
        cx.commit(); cx.close()
        return {"ok": True, "msg": "邀请绑定成功，双方各得积分", "inviter_balance_add": 10}

    def api_buy_points(self, body):
        """模拟『企微侧充值后后台落账』：真实场景由企业微信会话触发，人工确认后调用。"""
        openid = body.get("openid", DEMO_OPENID)
        amount = int(body.get("amount", 100))
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        add_points(uid, amount, "buy")
        return {"ok": True, "msg": f"已到账 {amount} 积分（1:1）", "balance":
                query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]["balance"]}

    def api_ledger(self, openid):
        uid = get_uid(openid)
        if not uid:
            return {"ok": False}
        rows = query(
            "SELECT txn_type,amount,reason,ref_id,created_at FROM points_ledger WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (uid,))
        return {"ok": True, "ledger": [dict(r) for r in rows]}

    def api_my_questions(self, openid):
        uid = get_uid(openid)
        if not uid:
            return {"ok": False}
        rows = query(
            "SELECT question,answer_type,cost_points,status,routed_to,created_at FROM question_record WHERE user_id=? ORDER BY id DESC",
            (uid,))
        return {"ok": True, "questions": [dict(r) for r in rows]}

    def api_my_collections(self, openid):
        uid = get_uid(openid)
        if not uid:
            return {"ok": False}
        rows = query("SELECT collect_type,ref_id,snapshot_status,created_at FROM collection WHERE user_id=? ORDER BY id DESC",
                     (uid,))
        return {"ok": True, "collections": [dict(r) for r in rows]}

    def api_policy_detail(self, doc):
        # 在内存 store 中查找（demo），支持文号/标题模糊
        target = None
        for p in STORE.policies.values():
            if doc in p.doc_number or doc in p.title:
                target = p
                break
        if not target:
            return {"ok": False, "msg": "未找到"}
        clauses = []
        for c in target.clauses:
            clauses.append({
                "no": c.clause_no,
                "content": c.content,
                "status": c.clause_status,
                "invalid_since": c.invalid_since,
                "superseded_by": c.superseded_by,
            })
        return {"ok": True, "title": target.title, "doc_number": target.doc_number,
                "status": target.status, "clauses": clauses}

    def api_policy_refresh(self):
        # 联网自动更新骨架（部署时接 requests）。本环境仅重新应用废止关系，验证时效逻辑仍可跑。
        STORE.apply_supersessions()
        return {"ok": True, "msg": "已重算时效图谱（联网抓取接口待部署接入 chinatax.gov.cn / 税屋网）",
                "policies": len(STORE.policies)}

    # ---- M4b: 联网政策抓取管道接口（异步，避免阻塞请求）----
    def api_policy_fetch(self):
        if _FETCH_STATE["running"]:
            return {"ok": True, "msg": "抓取任务进行中，请稍候查询 /api/policy/fetch-status", "running": True}
        t = threading.Thread(target=_fetch_worker, daemon=True)
        t.start()
        return {"ok": True, "msg": "已后台触发联网抓取 chinatax.gov.cn，新政策进入待复核", "running": True}

    def api_policy_fetch_status(self):
        return {"ok": True, "running": _FETCH_STATE["running"], "last": _FETCH_STATE["last"],
                "count": _FETCH_STATE["count"], "error": _FETCH_STATE["error"]}

    # =================== M2 政策复核台（面向运营/专家的"三栏对照"）===================
    # 设计要点：队列**实时**读 policy_registry（不再读 policy_brief 快照）；
    # 复核动作支持单条与批量；生效时写 last_verified_at + admin_audit 留痕。
    AGING_IN_FORCE = ("全文有效", "部分有效", "有效")
    AGING_AMENDED = ("已修改",)          # 修改过但仍有效 → 放行但列入抽查
    AGING_DEAD = ("全文废止", "部分废止", "全文失效", "部分失效",
                  "失效", "废止", "已废止", "已失效")

    @staticmethod
    def _policy_quality(r, clauses):
        """自动质量校验：为"能否自动生效"提供**客观依据**（不替代人工抽查）。
        返回 {pass, blocking[], warnings[]}。
        规则（刻意保守）：官方标"已修改"可放行并标记抽查；**官方未标时效性一律拦住**
        ——因为这正是"看起来没问题、实则可能已被取代"的高风险情形。"""
        blocking, warnings = [], []
        dn = (r.get("doc_number") or "").strip()
        if not dn:
            blocking.append("无文号")
        elif dn.startswith("未标文号-"):
            warnings.append("文号为系统临时键（官方未给文号）")
        aging = (r.get("official_aging") or "").strip()
        if aging in Handler.AGING_IN_FORCE:
            pass
        elif aging in Handler.AGING_AMENDED:
            warnings.append("官方时效=已修改（文件仍有效，但已被修订，建议抽查）")
        elif aging in Handler.AGING_DEAD:
            blocking.append("官方时效=%s（已废止/失效）" % aging)
        elif not aging:
            blocking.append("官方未标时效性，无法自动判定 → 需人工判断")
        else:
            blocking.append("官方时效性取值未知：%s → 需人工判断" % aging)
        if not clauses:
            blocking.append("未切出任何条款")
        else:
            short = [c.get("clause_no") or "?" for c in clauses
                     if len((c.get("content") or "").strip()) < 8]
            if short:
                blocking.append("存在过短条款：%s" % "、".join(short[:5]))
        # 权威废止来源校验 —— aging 会滞后，这一层不能省（见模块顶部注释）
        if _norm_docno(dn) in _repeal_docnos():
            blocking.append("命中官方《失效废止目录》公告附件清单")
        if r.get("id") in _superseded_doc_ids():
            blocking.append("已被其他文件明令废止")
        blen = len(r.get("content_text") or "")
        if blen < 80:
            blocking.append("正文过短(%d 字)，疑似未取到正文" % blen)
        eff = (r.get("effective_date") or "").strip()
        today = datetime.date.today().isoformat()
        if eff and eff > today:
            blocking.append("尚未生效（施行日 %s）" % eff)
        if len((r.get("title") or "")) < 8:
            blocking.append("标题异常")
        return {"pass": not blocking, "blocking": blocking, "warnings": warnings}

    def api_policy_brief(self, params=None):
        """待复核队列（实时）。params: status(默认 pending_review) / limit"""
        params = params or {}
        status = (params.get("status") or "pending_review").strip()
        try:
            limit = min(int(params.get("limit") or 100), 500)
        except Exception:
            limit = 100
        rows = query(
            "SELECT p.id,p.title,p.doc_number,p.status,p.category,p.publish_date,"
            "p.effective_date,p.source_url,p.official_aging,p.official_effectlevel,"
            "(SELECT COUNT(*) FROM policy_clause c WHERE c.doc_id=p.id) AS n_all,"
            "(SELECT COUNT(*) FROM policy_clause c WHERE c.doc_id=p.id "
            " AND c.clause_status='active') AS n_act,"
            "LENGTH(IFNULL(p.content_text,'')) AS body_len "
            "FROM policy_registry p WHERE p.status=? ORDER BY p.id DESC LIMIT ?",
            (status, limit))
        items = [dict(r) for r in rows]
        total = query("SELECT COUNT(*) c FROM policy_registry WHERE status=?", (status,))[0]["c"]
        dist = [dict(r) for r in query(
            "SELECT IFNULL(NULLIF(official_aging,''),'(未标)') a,COUNT(*) c "
            "FROM policy_registry WHERE status=? GROUP BY a ORDER BY c DESC", (status,))]
        return {"ok": True, "count": len(items), "total": total,
                "status": status, "aging_dist": dist, "items": items}

    def api_policy_preview(self, params):
        """单条政策详情：文号 / 官方时效 / 来源链接 / 全部条款原文 + 自动质量校验结果。"""
        pid = (params or {}).get("id")
        if not pid:
            return {"ok": False, "msg": "缺少 id"}
        rows = query("SELECT * FROM policy_registry WHERE id=?", (pid,))
        if not rows:
            return {"ok": False, "msg": "政策不存在"}
        r = dict(rows[0])
        cls = [dict(c) for c in query(
            "SELECT clause_no,content,clause_status,invalid_since,superseded_by "
            "FROM policy_clause WHERE doc_id=? ORDER BY display_order,id", (pid,))]
        try:
            r["source_meta"] = json.loads(r.get("source_meta") or "{}")
        except Exception:
            r["source_meta"] = {}
        r["clauses"] = cls
        r["body_len"] = len(r.get("content_text") or "")
        r["quality"] = self._policy_quality(r, cls)
        return {"ok": True, "policy": r}

    def api_policy_review(self, body, headers=None):
        """复核动作。body: {ids:[..] 或 id, action: approve|skip|archive, operator, admin_auth}"""
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        action = (body.get("action") or "approve").strip()
        if action not in ("approve", "skip", "archive"):
            return {"ok": False, "msg": "未知动作：%s" % action}
        raw_ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
        ids = []
        for i in raw_ids:
            s = str(i).strip()
            if s.isdigit():
                ids.append(int(s))
        if not ids:
            return {"ok": False, "msg": "缺少政策 id"}
        operator = (body.get("operator") or "admin")[:32]
        note = (body.get("note") or "")[:200]
        force = bool(body.get("force"))   # 人工已核对过原文 → 可覆盖自动校验
        new_status = {"approve": "active", "skip": "pending_review", "archive": "archived"}[action]
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ip = self.client_address[0] if self.client_address else ""
        done, skipped = [], []
        for pid in ids:
            row = query("SELECT id,doc_number,status FROM policy_registry WHERE id=?", (pid,))
            if not row:
                skipped.append({"id": pid, "why": "不存在"})
                continue
            if action == "approve" and not force:
                cls = query("SELECT clause_no,content FROM policy_clause WHERE doc_id=?", (pid,))
                qc = self._policy_quality(dict(row[0]), [dict(c) for c in cls])
                if not qc["pass"]:
                    skipped.append({"id": pid, "why": "；".join(qc["blocking"])})
                    continue
            exec("UPDATE policy_registry SET status=?, last_verified_at=? WHERE id=?",
                 (new_status, now, pid))
            done.append(pid)
        n = load_policies_from_db()
        if done:
            write_audit(operator, "policy_%s" % action,
                        "%d 条" % len(done), "ids=%s note=%s" % (done[:20], note), ip)
        msg = {"approve": "已复核生效", "skip": "已标记待定", "archive": "已归档"}[action]
        return {"ok": True, "msg": "%s %d 条（本次跳过 %d 条）；当前可检索政策 %d 份"
                                   % (msg, len(done), len(skipped), n),
                "done": done, "skipped": skipped, "loaded": n}

    def api_policy_autoreview(self, body, headers=None):
        """按官方时效**自动分流**（用户已批准的二期策略）：
        官方 aging=现行有效 且通过自动质量校验 → 生效；其余保持待复核。
        留痕：last_verified_at 标注"系统预校验"，并写 admin_audit；可一键回滚。"""
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        try:
            limit = min(int(body.get("limit") or 500), 2000)
        except Exception:
            limit = 500
        only_category = (body.get("category") or "").strip()
        sql = ("SELECT id,doc_number,title,official_aging,effective_date,content_text,status "
               "FROM policy_registry WHERE status='pending_review'")
        args = []
        if only_category:
            sql += " AND category=?"
            args.append(only_category)
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        rows = query(sql, tuple(args))
        approved, rejected = [], []
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            cls = [dict(c) for c in query(
                "SELECT clause_no,content FROM policy_clause WHERE doc_id=?", (r["id"],))]
            qc = self._policy_quality(dict(r), cls)
            if qc["pass"]:
                exec("UPDATE policy_registry SET status='active', last_verified_at=? WHERE id=?",
                     ("%s 系统预校验(官方时效=%s)" % (now, r["official_aging"] or "未标"), r["id"]))
                approved.append(r["id"])
            else:
                rejected.append({"id": r["id"], "doc_number": r["doc_number"],
                                 "why": qc["blocking"]})
        n = load_policies_from_db()
        ip = self.client_address[0] if self.client_address else ""
        if approved:
            write_audit("system", "policy_autoreview", "%d 条" % len(approved),
                        "自动分流生效 ids=%s" % approved[:30], ip)
        return {"ok": True,
                "msg": "自动分流完成：生效 %d 条、保留待复核 %d 条；当前可检索政策 %d 份"
                       % (len(approved), len(rejected), n),
                "approved": len(approved), "rejected": rejected[:50], "loaded": n}

    def api_policy_rollback_auto(self, body, headers=None):
        """一键回滚"系统预校验"自动生效的政策 → 回到待复核（人工复核前安全兜底）。"""
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        rows = query("SELECT id,doc_number FROM policy_registry "
                     "WHERE status='active' AND IFNULL(last_verified_at,'') LIKE '%系统预校验%'")
        ids = [r["id"] for r in rows]
        for pid in ids:
            exec("UPDATE policy_registry SET status='pending_review' WHERE id=?", (pid,))
        n = load_policies_from_db()
        ip = self.client_address[0] if self.client_address else ""
        write_audit("admin", "policy_rollback_auto", "%d 条" % len(ids),
                    "回滚系统预校验生效 ids=%s" % ids[:30], ip)
        return {"ok": True, "msg": "已回滚 %d 条系统预校验的政策，当前可检索政策 %d 份" % (len(ids), n),
                "ids": ids, "loaded": n}

    # ---- M4a: 企微充值闭环（运营后台确认）----
    # 真实场景：用户在企微把法币转给张总/公司，运营在后台点「确认充值」，
    # 积分落到用户账户。平台不碰资金池、不经手法币，规避二清与支付牌照。
    def api_admin_recharge(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        openid = body.get("openid", DEMO_OPENID)
        try:
            amount = int(body.get("amount", 0))
        except Exception:
            return {"ok": False, "msg": "金额无效"}
        ref = (body.get("ref") or "")[:64]
        if amount <= 0:
            return {"ok": False, "msg": "充值金额须为正"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        add_points(uid, amount, "recharge", ref_id=ref or None)
        bal = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]["balance"]
        ip = self.client_address[0] if self.client_address else ""
        write_audit("admin", "recharge_confirm", f"user#{openid}", f"+{amount} 积分 ref={ref}", ip)
        return {"ok": True, "balance": bal,
                "msg": f"企微充值已确认，到账 {amount} 积分（1:1），当前余额 {bal}"}

    # ---- 小程序辅助接口 ----
    def api_policy_list(self):
        out = []
        for p in STORE.policies.values():
            if not getattr(p, "doc_number", None):
                continue
            valid = STORE.valid_clauses(p)
            out.append({
                "doc_number": p.doc_number,
                "title": p.title,
                "status": p.status,
                "category": getattr(p, "category", ""),
                "valid_clauses": len(valid),
                "total_clauses": len(p.clauses),
            })
        return {"ok": True, "policies": out}

    def api_policy_catalog_stats(self):
        """全量目录覆盖情况：让运营/用户一眼看到"库里有份量"。"""
        rows = query("SELECT status,COUNT(*) n FROM policy_registry GROUP BY status")
        st = {r["status"]: r["n"] for r in rows}
        n_clause = query("SELECT COUNT(*) c FROM policy_clause WHERE clause_status='active'")[0]["c"]
        return {"ok": True, "by_status": st,
                "total": sum(st.values()),
                "searchable": st.get("active", 0) + st.get("partially_invalid", 0),
                "catalog_only": st.get("indexed", 0),
                "active_clauses": n_clause}

    def api_kefu_info(self):
        return {"ok": True,
                "corpid": os.environ.get("WX_CORPID", ""),
                "qrcode_url": os.environ.get("WX_KEFU_QR", ""),
                "tip": "在小程序内点击「联系客服」可唤起企业微信会话；法币充值请于会话中转账给公司，运营确认后积分到账。"}

    # ---- 转人工快答 运营侧 ----
    def api_human_submit(self, body):
        openid = body.get("openid", DEMO_OPENID)
        q = (body.get("question") or "").strip()
        if not q:
            return {"ok": False, "msg": "问题为空"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        cost = 30
        acc = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))[0]
        if acc["balance"] < cost:
            return {"ok": False, "msg": "积分不足，请先获取积分", "balance": acc["balance"]}
        add_points(uid, -cost, "human_answer")
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO human_ticket (user_id,openid,question,status,cost_points) VALUES (?,?,?, 'pending',?)",
            (uid, openid, q, cost))
        tid = cur.lastrowid
        cur.execute(
            "INSERT INTO question_record (user_id,question,answer_type,cost_points,status,routed_to) VALUES (?,?,?,?,?,?)",
            (uid, q, "human", cost, "consult", "app"))
        cx.commit(); cx.close()
        return {"ok": True, "ticket_id": tid, "cost": cost, "balance": acc["balance"] - cost,
                "msg": "已提交人工快答，张老师将在工作时间内答复"}

    def api_human_my(self, openid):
        uid = get_uid(openid)
        if not uid:
            return {"ok": False}
        rows = query(
            "SELECT id,question,status,reply,created_at,replied_at FROM human_ticket WHERE user_id=? ORDER BY id DESC",
            (uid,))
        return {"ok": True, "tickets": [dict(r) for r in rows]}

    def api_human_list(self):
        rows = query(
            "SELECT id,openid,question,status,reply,created_at,replied_at FROM human_ticket ORDER BY "
            "CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC")
        return {"ok": True, "tickets": [dict(r) for r in rows]}

    def api_human_reply(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        tid = body.get("id")
        reply = (body.get("reply") or "").strip()
        operator = (body.get("operator") or "operator")
        if not tid or not reply:
            return {"ok": False, "msg": "缺少参数"}
        exec("UPDATE human_ticket SET status='answered', reply=?, reply_by=?, replied_at=? WHERE id=?",
             (reply, operator, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tid))
        ip = self.client_address[0] if self.client_address else ""
        write_audit(operator, "human_reply", f"ticket#{tid}", reply[:60], ip)
        return {"ok": True, "msg": "已回复用户"}

    # ---- 同行问（M1）：用户互助社区 ----
    @staticmethod
    def _mask_author(row):
        """匿名脱敏：仅展示层隐藏，运营接口另查真实身份。"""
        if row.get("is_anonymous"):
            return "匿名从业者"
        return row.get("nickname") or "从业者"

    def api_community_post(self, body):
        openid = body.get("openid", DEMO_OPENID)
        title = (body.get("title") or "").strip()
        content = (body.get("content") or "").strip()
        scene = (body.get("scene_tag") or "其他").strip()
        anonymous = 1 if body.get("is_anonymous") else 0
        if not title or len(title) > 60:
            return {"ok": False, "msg": "标题必填且不超过 60 字"}
        if not content or len(content) > 2000:
            return {"ok": False, "msg": "正文必填且不超过 2000 字"}
        if scene not in SCENE_TAGS:
            scene = "其他"
        # 悬赏（M2）：0~500 积分，发帖即冻结扣除，采纳时发放给答主；未采纳被运营删除则退款
        try:
            bounty = int(body.get("bounty_points") or 0)
        except Exception:
            bounty = 0
        if bounty < 0 or bounty > 500:
            return {"ok": False, "msg": "悬赏积分须在 0~500 之间"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        if bounty > 0:
            bal = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))
            bal = bal[0]["balance"] if bal else 0
            if bal < bounty:
                return {"ok": False, "msg": "积分不足，无法设置悬赏（需要 %d）" % bounty}
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        cnt = query("SELECT COUNT(*) c FROM community_post WHERE user_id=? AND created_at LIKE ?",
                    (uid, today + "%"))[0]["c"]
        if cnt >= 3:
            return {"ok": False, "msg": "每日最多发帖 3 条，明天再来"}
        cx = db(); cur = cx.cursor()
        if bounty > 0:
            add_points(uid, -bounty, "bounty_set", ref_id="post#pending")
        cur.execute(
            "INSERT INTO community_post (user_id,openid,title,content,scene_tag,is_anonymous,bounty_points) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, openid, title, content, scene, anonymous, bounty))
        pid = cur.lastrowid
        cx.commit(); cx.close()
        # AI 先答：后台线程生成，不阻塞响应；详情页刷新即可见
        threading.Thread(target=_community_ai_answer, args=(pid, title + "\n" + content), daemon=True).start()
        return {"ok": True, "post_id": pid,
                "bounty": bounty,
                "msg": ("发布成功，已冻结 %d 积分悬赏；AI 参考答案生成中" % bounty) if bounty
                else "发布成功，AI 参考答案生成中"}

    def api_community_list(self, qs):
        tab = (qs.get("tab") or ["latest"])[0]
        scene = (qs.get("scene") or [""])[0]
        if tab.startswith("scene:"):
            scene = tab.split(":", 1)[1]
        try:
            page = max(1, int((qs.get("page") or ["1"])[0]))
        except Exception:
            page = 1
        size = 20
        where, args = "p.status<>'removed'", []
        if scene and scene in SCENE_TAGS:
            where += " AND p.scene_tag=?"
            args.append(scene)
        order = "p.pinned DESC, p.id DESC"
        if tab == "hot":
            order = "p.pinned DESC, (p.reply_count*3 + p.view_count) DESC, p.id DESC"
        elif tab == "unanswered":
            where += " AND p.reply_count=0"
        elif tab == "featured":
            # 精华：帖子本身已采纳，或存在高赞(>=5)/已采纳的回答（M2）
            where += (" AND (p.status='accepted' OR p.id IN "
                      "(SELECT post_id FROM post_reply WHERE status<>'removed' "
                      "AND (like_count>=5 OR is_accepted=1)))")
            order = "p.pinned DESC, (p.reply_count*3 + p.view_count) DESC, p.id DESC"
        rows = query(
            "SELECT p.id,p.title,p.scene_tag,p.is_anonymous,p.status,p.view_count,p.reply_count,"
            "p.pinned,p.created_at,p.bounty_points,u.nickname FROM community_post p "
            "LEFT JOIN users u ON u.id=p.user_id WHERE " + where +
            " ORDER BY " + order + " LIMIT ? OFFSET ?",
            tuple(args + [size, (page - 1) * size]))
        posts = []
        for r in rows:
            d = dict(r)
            d["author"] = self._mask_author(d)
            posts.append({k: d.get(k) for k in
                         ("id", "title", "scene_tag", "author", "status",
                          "view_count", "reply_count", "pinned", "created_at", "bounty_points")})
        return {"ok": True, "tab": tab, "scene": scene, "page": page,
                "scene_tags": list(SCENE_TAGS), "posts": posts}

    def api_community_detail(self, qs):
        try:
            pid = int((qs.get("id") or ["0"])[0])
        except Exception:
            pid = 0
        openid = (qs.get("openid") or [""])[0]
        row = query(
            "SELECT p.*, u.nickname FROM community_post p LEFT JOIN users u ON u.id=p.user_id "
            "WHERE p.id=? AND p.status<>'removed'", (pid,))
        if not row:
            return {"ok": False, "msg": "帖子不存在或已删除"}
        p = dict(row[0])
        exec("UPDATE community_post SET view_count=view_count+1 WHERE id=?", (pid,))
        ai = {}
        try:
            ai = json.loads(p.get("ai_answer") or "{}")
        except Exception:
            ai = {}
        uid = get_uid(openid) if openid else None
        liked = set()
        if uid:
            liked = {r["reply_id"] for r in query(
                "SELECT reply_id FROM post_like WHERE user_id=?", (uid,))}
        replies = []
        for r in query(
                "SELECT r.id,r.content,r.policy_refs,r.like_count,r.is_accepted,r.created_at,u.nickname,u.reputation "
                "FROM post_reply r LEFT JOIN users u ON u.id=r.user_id "
                "WHERE r.post_id=? AND r.status<>'removed' "
                "ORDER BY r.is_accepted DESC, r.like_count DESC, r.id ASC", (pid,)):
            d = dict(r)
            try:
                refs = json.loads(d.get("policy_refs") or "[]")
            except Exception:
                refs = []
            replies.append({
                "id": d["id"], "author": d.get("nickname") or "从业者",
                "reputation": d.get("reputation") or 0,
                "content": d["content"], "policy_refs": refs,
                "like_count": d["like_count"], "liked": d["id"] in liked,
                "is_accepted": bool(d["is_accepted"]), "created_at": d["created_at"]})
        return {"ok": True, "post": {
                "id": p["id"], "title": p["title"], "content": p["content"],
                "scene_tag": p["scene_tag"], "author": self._mask_author(p),
                "is_anonymous": bool(p["is_anonymous"]), "status": p["status"],
                "bounty_points": p.get("bounty_points") or 0,
                "view_count": p["view_count"] + 1, "reply_count": p["reply_count"],
                "created_at": p["created_at"]},
                "ai_answer": ai.get("answer") or "",
                "ai_policy_refs": ai.get("policy_refs") or [],
                "ai_mode": p.get("ai_mode") or "",
                "is_owner": bool(uid and uid == p["user_id"]),
                "replies": replies,
                "disclaimer": COMMUNITY_DISCLAIMER}

    def api_community_profile(self, qs):
        """我的社区档案（M2）：声望、积分、发帖/回答/被采纳/获赞统计。"""
        openid = (qs.get("openid") or [""])[0]
        uid = get_uid(openid) if openid else None
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        u = query("SELECT nickname,reputation FROM users WHERE id=?", (uid,))
        bal = query("SELECT balance FROM points_account WHERE user_id=?", (uid,))
        posts = query("SELECT COUNT(*) c FROM community_post WHERE user_id=? AND status<>'removed'", (uid,))[0]["c"]
        answers = query("SELECT COUNT(*) c FROM post_reply WHERE user_id=? AND status<>'removed'", (uid,))[0]["c"]
        accepted = query("SELECT COUNT(*) c FROM post_reply WHERE user_id=? AND status<>'removed' AND is_accepted=1",
                         (uid,))[0]["c"]
        likes = query("SELECT COALESCE(SUM(like_count),0) s FROM post_reply WHERE user_id=? AND status<>'removed'",
                      (uid,))[0]["s"]
        return {"ok": True, "profile": {
            "nickname": u[0]["nickname"] or "从业者",
            "reputation": u[0]["reputation"] or 0,
            "points_balance": bal[0]["balance"] if bal else 0,
            "posts_count": posts, "answers_count": answers,
            "accepted_count": accepted, "likes_received": likes}}

    def api_community_search_replies(self, qs):
        """社区精华检索（M2，公开）：按关键词搜「高赞/已采纳」的同行回答，
        供个人浏览与机构「沉淀为机构口径」使用。仅返回高价值回答，避免噪声。"""
        q = (qs.get("q") or [""])[0].strip()
        try:
            limit = min(50, max(1, int((qs.get("limit") or ["20"])[0])))
        except Exception:
            limit = 20
        if not q:
            return {"ok": True, "replies": []}
        like = "%" + q + "%"
        rows = query(
            "SELECT r.id rid,r.post_id,r.content,r.like_count,r.is_accepted,r.created_at,"
            "p.title,p.scene_tag,u.nickname,u.reputation "
            "FROM post_reply r "
            "JOIN community_post p ON p.id=r.post_id AND p.status<>'removed' "
            "LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.status<>'removed' AND (r.like_count>=3 OR r.is_accepted=1) "
            "AND (r.content LIKE ? OR p.title LIKE ?) "
            "ORDER BY r.is_accepted DESC, r.like_count DESC, r.id DESC LIMIT ?",
            (like, like, limit))
        out = []
        for r in rows:
            d = dict(r)
            content = d["content"] or ""
            out.append({
                "reply_id": d["rid"], "post_id": d["post_id"],
                "post_title": d["title"], "scene_tag": d["scene_tag"],
                "content": content[:300], "like_count": d["like_count"],
                "is_accepted": bool(d["is_accepted"]),
                "author": d.get("nickname") or "从业者",
                "reputation": d.get("reputation") or 0,
                "created_at": d["created_at"]})
        return {"ok": True, "replies": out}

    def api_community_reply(self, body):
        openid = body.get("openid", DEMO_OPENID)
        try:
            pid = int(body.get("post_id") or 0)
        except Exception:
            pid = 0
        content = (body.get("content") or "").strip()
        refs = body.get("policy_refs") or []
        if not content or len(content) > 1500:
            return {"ok": False, "msg": "回答必填且不超过 1500 字"}
        if not isinstance(refs, list):
            refs = []
        refs = [str(r)[:64] for r in refs][:8]
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        post = query("SELECT id,status FROM community_post WHERE id=? AND status<>'removed'", (pid,))
        if not post:
            return {"ok": False, "msg": "帖子不存在或已删除"}
        # 引用政策校验：只保留库内现行/部分失效的真实文号（零错误铁律）
        valid_refs = []
        if refs:
            ph = ",".join("?" * len(refs))
            known = {r["doc_number"] for r in query(
                "SELECT doc_number FROM policy_registry WHERE doc_number IN (%s) "
                "AND status IN ('active','partially_invalid')" % ph, tuple(refs))}
            valid_refs = [r for r in refs if r in known]
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO post_reply (post_id,user_id,openid,content,policy_refs) VALUES (?,?,?,?,?)",
            (pid, uid, openid, content, json.dumps(valid_refs, ensure_ascii=False)))
        cur.execute("UPDATE community_post SET reply_count=reply_count+1 WHERE id=?", (pid,))
        cx.commit(); cx.close()
        return {"ok": True, "msg": "回答已提交", "policy_refs": valid_refs}

    def api_community_like(self, body):
        openid = body.get("openid", DEMO_OPENID)
        try:
            rid = int(body.get("reply_id") or 0)
        except Exception:
            rid = 0
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        rep = query("SELECT id,user_id FROM post_reply WHERE id=? AND status<>'removed'", (rid,))
        if not rep:
            return {"ok": False, "msg": "回答不存在"}
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT OR IGNORE INTO post_like (reply_id,user_id) VALUES (?,?)", (rid, uid))
        inserted = cur.rowcount > 0
        if inserted:
            cur.execute("UPDATE post_reply SET like_count=like_count+1 WHERE id=?", (rid,))
        # 先提交并关闭 cx，再单独更新声望——避免同一请求内两个写连接互锁（database is locked）
        cx.commit(); cx.close()
        if inserted:
            # 被点赞者声望 +1（M2，按不同点赞人计数，天然防刷）
            add_reputation(rep[0]["user_id"], 1, "reply_liked")
        cnt = query("SELECT like_count FROM post_reply WHERE id=?", (rid,))[0]["like_count"]
        return {"ok": True, "liked": inserted, "like_count": cnt}

    def api_community_accept(self, body):
        openid = body.get("openid", DEMO_OPENID)
        try:
            pid = int(body.get("post_id") or 0)
            rid = int(body.get("reply_id") or 0)
        except Exception:
            return {"ok": False, "msg": "参数无效"}
        uid = get_uid(openid)
        if not uid:
            return {"ok": False, "msg": "用户不存在"}
        post = query("SELECT id,user_id,status,bounty_points FROM community_post WHERE id=? AND status<>'removed'",
                     (pid,))
        if not post or post[0]["user_id"] != uid:
            return {"ok": False, "msg": "只有发帖人可以采纳"}
        if post[0]["status"] == "accepted":
            return {"ok": False, "msg": "该帖已采纳过答案"}
        rep = query("SELECT id,user_id FROM post_reply WHERE id=? AND post_id=? AND status<>'removed'",
                    (rid, pid))
        if not rep:
            return {"ok": False, "msg": "回答不存在"}
        ans_uid = rep[0]["user_id"]
        bounty = post[0]["bounty_points"] or 0
        cx = db(); cur = cx.cursor()
        cur.execute("UPDATE post_reply SET is_accepted=1 WHERE id=?", (rid,))
        cur.execute("UPDATE community_post SET status='accepted' WHERE id=?", (pid,))
        cx.commit(); cx.close()
        # 采纳奖励：基础 20 积分 + 声望 +10（M2）
        add_points(ans_uid, 20, "answer_adopted", ref_id="post#%d" % pid)
        add_reputation(ans_uid, 10, "answer_adopted")
        # 悬赏发放（M2）
        if bounty > 0:
            add_points(ans_uid, bounty, "bounty_award", ref_id="post#%d" % pid)
        return {"ok": True,
                "bounty_award": bounty,
                "msg": ("已采纳，答主获得 20 积分 + %d 悬赏 + 10 声望" % bounty) if bounty
                else "已采纳，答主获得 20 积分 + 10 声望"}

    # ---- 同行问：运营侧 ----
    def api_admin_community_list(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        rows = query(
            "SELECT p.id,p.openid,p.title,p.scene_tag,p.status,p.is_anonymous,p.view_count,"
            "p.reply_count,p.pinned,p.created_at,u.nickname FROM community_post p "
            "LEFT JOIN users u ON u.id=p.user_id ORDER BY p.pinned DESC, p.id DESC LIMIT 200")
        return {"ok": True, "posts": [dict(r) for r in rows]}

    def api_admin_community_remove(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        operator = body.get("operator") or "operator"
        ip = self.client_address[0] if self.client_address else ""
        try:
            rid = int(body.get("reply_id") or 0)
        except Exception:
            rid = 0
        if rid:
            exec("UPDATE post_reply SET status='removed' WHERE id=?", (rid,))
            write_audit(operator, "community_reply_remove", f"reply#{rid}", "", ip)
            return {"ok": True, "msg": "已删除该回答"}
        try:
            pid = int(body.get("post_id") or 0)
        except Exception:
            return {"ok": False, "msg": "缺少 post_id 或 reply_id"}
        # 悬赏退款（M2）：删除尚未采纳的悬赏帖，冻结的悬赏退回发帖人
        p = query("SELECT user_id,bounty_points,status FROM community_post WHERE id=?", (pid,))
        if p and (p[0]["bounty_points"] or 0) > 0 and p[0]["status"] != "accepted":
            add_points(p[0]["user_id"], p[0]["bounty_points"], "bounty_refund", ref_id="post#%d" % pid)
        exec("UPDATE community_post SET status='removed' WHERE id=?", (pid,))
        exec("UPDATE post_reply SET status='removed' WHERE post_id=?", (pid,))
        write_audit(operator, "community_post_remove", f"post#{pid}", "", ip)
        return {"ok": True, "msg": "已删除帖子（含其下回答）"}

    def api_admin_community_pin(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限（需 admin_auth）"}
        try:
            pid = int(body.get("post_id") or 0)
        except Exception:
            return {"ok": False, "msg": "参数无效"}
        cur_v = query("SELECT pinned FROM community_post WHERE id=?", (pid,))
        if not cur_v:
            return {"ok": False, "msg": "帖子不存在"}
        new_v = 0 if cur_v[0]["pinned"] else 1
        exec("UPDATE community_post SET pinned=? WHERE id=?", (new_v, pid))
        ip = self.client_address[0] if self.client_address else ""
        write_audit(body.get("operator") or "operator", "community_pin",
                    f"post#{pid}", "pinned=%d" % new_v, ip)
        return {"ok": True, "pinned": new_v, "msg": "已置顶" if new_v else "已取消置顶"}

    # ---- 运营后台鉴权 + 留痕 ----
    def api_admin_login(self, body):
        ip = _real_client_ip(self)
        if not _admin_rate_ok(ip):
            return {"ok": False, "msg": "尝试过于频繁，请15分钟后再试"}
        token = body.get("token", "")
        if token == ADMIN_TOKEN:
            return {"ok": True, "admin_token": ADMIN_TOKEN, "msg": "登录成功"}
        _register_admin_fail(ip)
        return {"ok": False, "msg": "口令错误"}

    def api_admin_audit(self):
        rows = query("SELECT operator,action,target,detail,ip,created_at FROM admin_audit ORDER BY id DESC LIMIT 50")
        return {"ok": True, "logs": [dict(r) for r in rows]}

    # ===================== 机构版（M1）API =====================
    # 运营开户（需 ADMIN_TOKEN）
    def api_admin_org_create(self, body, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限"}
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "msg": "机构名称必填"}
        org_code = (body.get("org_code") or ("ORG" + secrets.token_hex(3).upper())).strip()
        if query("SELECT id FROM organizations WHERE org_code=?", (org_code,)):
            return {"ok": False, "msg": "机构码已存在"}
        login_name = (body.get("owner_login") or "owner").strip()
        pwd = body.get("owner_password") or secrets.token_hex(4)
        owner_name = (body.get("owner_name") or "机构负责人").strip()
        try:
            seat_total = int(body.get("seat_total") or 5)
        except Exception:
            seat_total = 5
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT INTO organizations (name,org_code,unified_credit_no,contact,plan,seat_total) VALUES (?,?,?,?,?,?)",
                    (name, org_code, body.get("unified_credit_no") or "", body.get("contact") or "",
                     body.get("plan") or "trial", seat_total))
        org_id = cur.lastrowid
        salt = secrets.token_hex(8)
        cur.execute("INSERT INTO org_member (org_id,login_name,pwd_hash,pwd_salt,name,role,seat_status) "
                    "VALUES (?,?,?,?,?, 'owner', 'active')",
                    (org_id, login_name, pwd_hash(pwd, salt), salt, owner_name))
        cx.commit(); cx.close()
        ip = self.client_address[0] if self.client_address else ""
        write_audit("admin", "org_create", f"org#{org_id}", f"{name} code={org_code}", ip)
        return {"ok": True, "org_id": org_id, "org_code": org_code,
                "owner_login": login_name, "owner_password": pwd,
                "msg": "机构已创建，请把「机构码 + 登录名 + 口令」交给负责人"}

    def api_admin_org_list(self, body=None, headers=None):
        if not admin_authorized(body, headers):
            return {"ok": False, "msg": "无权限"}
        rows = query("SELECT id,name,org_code,plan,seat_total,status,created_at FROM organizations ORDER BY id DESC")
        return {"ok": True, "orgs": [dict(r) for r in rows]}

    def api_org_login(self, body):
        org_code = (body.get("org_code") or "").strip()
        login_name = (body.get("login") or "").strip()
        pwd = body.get("password") or ""
        if not org_code or not login_name:
            return {"ok": False, "msg": "机构码与登录名必填"}
        org = query("SELECT * FROM organizations WHERE org_code=?", (org_code,))
        if not org:
            return {"ok": False, "msg": "机构码无效"}
        org = org[0]
        m = query("SELECT * FROM org_member WHERE org_id=? AND login_name=?", (org["id"], login_name))
        if not m:
            return {"ok": False, "msg": "账号不存在"}
        m = m[0]
        if m["seat_status"] != "active":
            return {"ok": False, "msg": "该席位已停用"}
        if pwd_hash(pwd, m["pwd_salt"]) != m["pwd_hash"]:
            return {"ok": False, "msg": "口令错误"}
        # —— 统一账号缝：把机构席位挂到「手机号主账号」users 行 ——
        # 个人主账号（openid 所在行）优先；手机号主账号体系由此打通，双端按身份显隐。
        openid = (body.get("openid") or "").strip()
        personal_uid = None
        if openid:
            pu = query("SELECT id FROM users WHERE openid=?", (openid,))
            personal_uid = pu[0]["id"] if pu else None
        uid = personal_uid if personal_uid else m["user_id"]
        if not uid:
            cx = db(); cur = cx.cursor()
            cur.execute("INSERT INTO users (openid,nickname) VALUES (?,?)",
                        ("org_" + secrets.token_hex(8), m["name"] or login_name))
            uid = cur.lastrowid
            cx.commit(); cx.close()
        if m["user_id"] != uid:
            exec("UPDATE org_member SET user_id=? WHERE id=?", (uid, m["id"]))
        # 若统一账号还没有 openid 且本次带了 openid：补上（不与他人冲突）
        if openid and personal_uid is None:
            u = query("SELECT openid FROM users WHERE id=?", (uid,))
            if u and (not u[0]["openid"] or u[0]["openid"].startswith("org_")):
                exec("UPDATE users SET openid=? WHERE id=?", (openid, uid))
        token = secrets.token_hex(24)
        exp = (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        exec("INSERT INTO org_session (token,user_id,org_id,member_id,role,expire_at) VALUES (?,?,?,?,?,?)",
             (token, uid, org["id"], m["id"], m["role"], exp))
        return {"ok": True, "token": token,
                "org": {"id": org["id"], "name": org["name"], "org_code": org["org_code"], "plan": org["plan"]},
                "member": {"id": m["id"], "name": m["name"], "role": m["role"]}}

    def api_org_me(self, s):
        org = query("SELECT id,name,org_code,plan,seat_total,status FROM organizations WHERE id=?", (s["org_id"],))
        used = query("SELECT COUNT(*) c FROM org_member WHERE org_id=? AND seat_status='active'", (s["org_id"],))
        clients = query("SELECT COUNT(*) c FROM client_profile WHERE org_id=?", (s["org_id"],))
        answers = query("SELECT COUNT(*) c FROM org_standard_answer WHERE org_id=? AND status='active'", (s["org_id"],))
        return {"ok": True, "org": dict(org[0]) if org else {}, "role": s["role"],
                "seat_used": used[0]["c"] if used else 0,
                "client_count": clients[0]["c"] if clients else 0,
                "answer_count": answers[0]["c"] if answers else 0}

    def api_org_clients(self, s):
        rows = query("SELECT id,name,credit_no,industry,region,taxpayer_type,risk_level,created_at "
                     "FROM client_profile WHERE org_id=? ORDER BY id DESC", (s["org_id"],))
        return {"ok": True, "clients": [dict(r) for r in rows]}

    def api_org_client_save(self, body, s):
        name = (body.get("name") or "").strip()
        cid = body.get("id")
        if cid:
            if not query("SELECT id FROM client_profile WHERE id=? AND org_id=?", (cid, s["org_id"])):
                return {"ok": False, "msg": "客户不存在"}
            exec("UPDATE client_profile SET name=?,credit_no=?,industry=?,region=?,taxpayer_type=? WHERE id=? AND org_id=?",
                 (name, body.get("credit_no") or "", body.get("industry") or "", body.get("region") or "",
                  body.get("taxpayer_type") or "", cid, s["org_id"]))
            return {"ok": True, "id": cid}
        if not name:
            return {"ok": False, "msg": "客户名称必填"}
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT INTO client_profile (org_id,name,credit_no,industry,region,taxpayer_type,owner_member_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (s["org_id"], name, body.get("credit_no") or "", body.get("industry") or "",
                     body.get("region") or "", body.get("taxpayer_type") or "", s["member_id"]))
        cid = cur.lastrowid
        cx.commit(); cx.close()
        return {"ok": True, "id": cid}

    def api_org_client_delete(self, body, s):
        exec("DELETE FROM client_profile WHERE id=? AND org_id=?", (body.get("id"), s["org_id"]))
        return {"ok": True}

    # =====================================================================
    # 隐私铁律（刀1，2026-09-13 确立，不可违反）：
    # 机构版所有端点（org_*/api_org_*）只能触达 org 域数据
    # （org_standard_answer / org_member / org_client / client_risk_input 等），
    # 严禁以任何方式 join 个人版 question_record / community_post / human_ticket。
    # 个人版用户的提问与社区活动，仅本人与平台运营（/api/admin/*）可见，机构主不可见。
    # 任何新增机构端点若需引用个人数据，必须先经产品+合规评审，否则视为越权 bug。
    # =====================================================================
    def api_org_answers(self, s):
        rows = query("SELECT id,category,question_pattern,answer_md,policy_refs,steps,status,approved_by,created_at "
                     "FROM org_standard_answer WHERE org_id=? ORDER BY (status='active') DESC, id DESC", (s["org_id"],))
        return {"ok": True, "answers": [dict(r) for r in rows]}

    def api_org_answer_save(self, body, s):
        aid = body.get("id")
        pattern = (body.get("question_pattern") or "").strip()
        ans = (body.get("answer_md") or "").strip()
        refs = body.get("policy_refs")
        if isinstance(refs, (list, tuple)):
            refs = json.dumps(list(refs), ensure_ascii=False)
        status = body.get("status") or "draft"
        if aid:
            if not query("SELECT id FROM org_standard_answer WHERE id=? AND org_id=?", (aid, s["org_id"])):
                return {"ok": False, "msg": "口径不存在"}
            exec("UPDATE org_standard_answer SET category=?,question_pattern=?,answer_md=?,policy_refs=?,steps=?,status=? "
                 "WHERE id=? AND org_id=?",
                 (body.get("category") or "", pattern, ans, refs or "", body.get("steps") or "", status, aid, s["org_id"]))
            return {"ok": True, "id": aid}
        if not pattern or not ans:
            return {"ok": False, "msg": "命中关键词与标准答案必填"}
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT INTO org_standard_answer (org_id,category,question_pattern,answer_md,policy_refs,steps,status,approved_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (s["org_id"], body.get("category") or "", pattern, ans, refs or "",
                     body.get("steps") or "", status, body.get("operator") or "operator"))
        aid = cur.lastrowid
        cx.commit(); cx.close()
        return {"ok": True, "id": aid}

    def api_org_answer_review(self, body, s):
        if s["role"] not in ("owner", "admin"):
            return {"ok": False, "msg": "无权限（仅负责人/管理员可审定）"}
        aid = body.get("id")
        exec("UPDATE org_standard_answer SET status='active', approved_by=? WHERE id=? AND org_id=?",
             (body.get("operator") or "operator", aid, s["org_id"]))
        ip = self.client_address[0] if self.client_address else ""
        write_audit("org", "answer_review", f"answer#{aid}", f"org={s['org_id']}", ip)
        return {"ok": True}

    def api_org_promote_reply(self, body, s):
        """M2 核心闭环：把社区里的高赞/已采纳同行回答，复制沉淀为本机构「标准口径」草稿。
        ※ 隐私铁律：此处是一次性的「副本拷贝」，仅读取公开的 community_post/post_reply 内容；
          写入的是 org 域表 org_standard_answer，绝不在机构查询中 JOIN 个人表。"""
        try:
            pid = int(body.get("post_id") or 0)
            rid = int(body.get("reply_id") or 0)
        except Exception:
            return {"ok": False, "msg": "参数无效"}
        if not pid or not rid:
            return {"ok": False, "msg": "post_id 与 reply_id 必填"}
        src = query(
            "SELECT r.content,r.policy_refs,r.like_count,r.is_accepted,p.title,p.scene_tag "
            "FROM post_reply r JOIN community_post p ON p.id=r.post_id "
            "WHERE r.id=? AND r.status<>'removed' AND p.status<>'removed' AND p.id=?",
            (rid, pid))
        if not src:
            return {"ok": False, "msg": "源回答不存在或已删除"}
        d = dict(src[0])
        category = (body.get("category") or d["scene_tag"] or "").strip()
        pattern = (body.get("question_pattern") or d["title"] or "").strip()
        ans = (body.get("answer_md") or d["content"] or "").strip()
        refs = d["policy_refs"]
        if isinstance(refs, (list, tuple)):
            refs = json.dumps(list(refs), ensure_ascii=False)
        if not pattern or not ans:
            return {"ok": False, "msg": "命中关键词与标准答案必填"}
        meta = json.dumps({
            "source": "community", "post_id": pid, "reply_id": rid,
            "likes": d["like_count"], "accepted": bool(d["is_accepted"]),
            "promoted_by": s["user_id"], "promoted_at": _now_str()
        }, ensure_ascii=False)
        cx = db(); cur = cx.cursor()
        cur.execute(
            "INSERT INTO org_standard_answer (org_id,category,question_pattern,answer_md,policy_refs,steps,status,approved_by,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (s["org_id"], category, pattern, ans, refs or "", "", "draft", "", meta))
        aid = cur.lastrowid
        cx.commit(); cx.close()
        return {"ok": True, "id": aid,
                "msg": "已沉淀为机构口径草稿，待负责人审定后生效"}

    def api_org_members(self, s):
        rows = query("SELECT id,name,login_name,role,seat_status,joined_at FROM org_member WHERE org_id=? ORDER BY id",
                     (s["org_id"],))
        return {"ok": True, "members": [dict(r) for r in rows]}

    def api_org_member_invite(self, body, s):
        if s["role"] not in ("owner", "admin"):
            return {"ok": False, "msg": "无权限（仅负责人/管理员可加人）"}
        login_name = (body.get("login") or "").strip()
        if not login_name:
            return {"ok": False, "msg": "登录名必填"}
        if query("SELECT id FROM org_member WHERE org_id=? AND login_name=?", (s["org_id"], login_name)):
            return {"ok": False, "msg": "登录名已存在"}
        used = query("SELECT COUNT(*) c FROM org_member WHERE org_id=? AND seat_status='active'", (s["org_id"],))[0]["c"]
        total = query("SELECT seat_total FROM organizations WHERE id=?", (s["org_id"],))[0]["seat_total"]
        if used >= total:
            return {"ok": False, "msg": "席位已满，请增加席位"}
        pwd = body.get("password") or secrets.token_hex(4)
        salt = secrets.token_hex(8)
        exec("INSERT INTO org_member (org_id,login_name,pwd_hash,pwd_salt,name,role,seat_status,invited_by) "
             "VALUES (?,?,?,?,?,?, 'active', ?)",
             (s["org_id"], login_name, pwd_hash(pwd, salt), salt,
              body.get("name") or login_name, body.get("role") or "member", s["member_id"]))
        return {"ok": True, "login": login_name, "password": pwd}

    def api_org_ask(self, body, s):
        """机构成员提问：优先命中机构口径库（命中即锁定）；未命中走 RAG+大模型并标记待专家确认。"""
        q = (body.get("question") or "").strip()
        if not q:
            return {"ok": False, "msg": "问题为空"}
        best, best_score = resolve_org_standard(q, s["org_id"])
        if best and best_score >= 1:
            answer = pe.generate_answer(best["answer_md"], is_interpretation=False)
            try:
                refs = json.loads(best["policy_refs"]) if best["policy_refs"] else []
            except Exception:
                refs = []
            return {"ok": True, "answer": answer, "policy_refs": refs,
                    "source": "org_standard", "locked": True, "mode": "org_standard",
                    "matched_pattern": best["question_pattern"]}
        answer, refs, is_interp, mode = answer_dispatch(q)
        return {"ok": True, "answer": answer, "policy_refs": refs,
                "source": "ai", "locked": False, "mode": mode, "pending_review": True,
                "msg": "未命中机构标准口径，已由 AI 生成，建议审定后回写口径库"}

    # ===================== 机构版（M2）：风险指标初筛 =====================
    def api_org_risk_rules(self, s):
        rows = query("SELECT id,code,name,metric,op,threshold,level,basis,enabled FROM risk_rule WHERE enabled=1 ORDER BY id")
        return {"ok": True, "rules": [dict(r) for r in rows],
                "metrics": [{"key": k, "label": l} for k, l in RISK_METRICS]}

    def api_org_risk_input_get(self, qs, s):
        cid = qs.get("client_id", [""])[0]
        period = qs.get("period", [""])[0]
        row = query("SELECT data_json FROM client_risk_input WHERE org_id=? AND client_id=? AND period=?",
                    (s["org_id"], cid, period))
        data = {}
        if row and row[0]["data_json"]:
            try:
                data = json.loads(row[0]["data_json"])
            except Exception:
                data = {}
        return {"ok": True, "data": data}

    def api_org_risk_input_save(self, body, s):
        cid = body.get("client_id")
        if not cid:
            return {"ok": False, "msg": "请选择客户"}
        period = (body.get("period") or "").strip()
        js = json.dumps(body.get("data") or {}, ensure_ascii=False)
        if query("SELECT id FROM client_risk_input WHERE org_id=? AND client_id=? AND period=?",
                 (s["org_id"], cid, period)):
            exec("UPDATE client_risk_input SET data_json=?, updated_at=? WHERE org_id=? AND client_id=? AND period=?",
                 (js, _now_str(), s["org_id"], cid, period))
        else:
            exec("INSERT INTO client_risk_input (org_id,client_id,period,data_json) VALUES (?,?,?,?)",
                 (s["org_id"], cid, period, js))
        return {"ok": True, "msg": "已保存"}

    def api_org_risk_scan(self, body, s):
        cid = body.get("client_id")
        if not cid:
            return {"ok": False, "msg": "请选择客户"}
        res = run_client_scan(s["org_id"], cid, (body.get("period") or "").strip())
        return dict({"ok": True}, **res)

    def api_org_risk_batch(self, body, s):
        ids = body.get("client_ids") or []
        if not ids:
            return {"ok": False, "msg": "请选择客户"}
        if _RISK_STATE.get("running"):
            return {"ok": True, "running": True, "msg": "已有批量任务进行中"}
        task_id = secrets.token_hex(6)
        _RISK_STATE.update({"running": True, "task_id": task_id, "done": 0, "total": len(ids), "error": None})
        threading.Thread(target=_risk_batch_worker,
                         args=(s["org_id"], list(ids), (body.get("period") or "").strip()), daemon=True).start()
        return {"ok": True, "task_id": task_id, "total": len(ids)}

    def api_org_risk_task(self, qs):
        return {"ok": True, "running": _RISK_STATE.get("running"), "done": _RISK_STATE.get("done"),
                "total": _RISK_STATE.get("total"), "last": _RISK_STATE.get("last"), "error": _RISK_STATE.get("error")}

    def api_org_risk_reports(self, s):
        rows = query("SELECT id,client_id,title,risk_items,status,generated_at,meta FROM org_report "
                     "WHERE org_id=? ORDER BY id DESC LIMIT 50", (s["org_id"],))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["risk_items"] = json.loads(r["risk_items"] or "[]")
            except Exception:
                d["risk_items"] = []
            try:
                meta = json.loads(r["meta"] or "{}")
            except Exception:
                meta = {}
            d["missing_count"] = len(meta.get("missing") or [])
            d["data_empty"] = bool(meta.get("data_empty"))
            d.pop("meta", None)
            out.append(d)
        return {"ok": True, "reports": out}

    # =====================================================================
    # 机构版：作业任务分派（工作流锁定核心，2026-09-13 落地）
    # 机构主/管理员下派日常作业（申报/风控/工商/其他）给成员；成员可见并流转状态。
    # =====================================================================
    def api_org_tasks(self, s, qs):
        mine = qs.get("mine", ["0"])[0] == "1"
        sql = ("SELECT t.id,t.title,t.scene_tag,t.due_date,t.status,t.note,t.created_at,"
               "t.assignee_member_id,t.creator_member_id,t.related_client_id,"
               "a.name AS assignee_name,c.name AS client_name "
               "FROM org_task t "
               "LEFT JOIN org_member a ON a.id=t.assignee_member_id "
               "LEFT JOIN client_profile c ON c.id=t.related_client_id "
               "WHERE t.org_id=?")
        params = [s["org_id"]]
        if mine:
            sql += " AND t.assignee_member_id=?"
            params.append(s["member_id"])
        sql += " ORDER BY (t.status='done'), t.due_date IS NULL, t.due_date, t.id DESC"
        rows = query(sql, tuple(params))
        today = datetime.date.today()
        tasks = []
        for r in rows:
            d = dict(r)
            dl = None
            overdue = False
            if r["due_date"]:
                try:
                    dd = datetime.date.fromisoformat(r["due_date"])
                    dl = (dd - today).days
                    overdue = dl < 0 and r["status"] != "done"
                except Exception:
                    pass
            d["days_left"] = dl
            d["overdue"] = overdue
            tasks.append(d)
        members = query("SELECT id,name,role FROM org_member WHERE org_id=? AND seat_status='active' ORDER BY id",
                       (s["org_id"],))
        clients = query("SELECT id,name FROM client_profile WHERE org_id=? ORDER BY id DESC", (s["org_id"],))
        return {"ok": True, "tasks": tasks,
                "members": [dict(m) for m in members],
                "clients": [dict(c) for c in clients]}

    def api_org_task_create(self, body, s):
        if s["role"] not in ("owner", "admin"):
            return {"ok": False, "msg": "无权限（仅负责人/管理员可下派任务）"}
        title = (body.get("title") or "").strip()
        assignee = body.get("assignee_member_id")
        if not title:
            return {"ok": False, "msg": "任务标题必填"}
        if not assignee:
            return {"ok": False, "msg": "请指定执行成员"}
        if not query("SELECT id FROM org_member WHERE id=? AND org_id=? AND seat_status='active'",
                     (assignee, s["org_id"])):
            return {"ok": False, "msg": "执行成员不存在或已停用"}
        cid = body.get("related_client_id")
        if cid and not query("SELECT id FROM client_profile WHERE id=? AND org_id=?", (cid, s["org_id"])):
            return {"ok": False, "msg": "关联客户不存在"}
        cx = db(); cur = cx.cursor()
        cur.execute("INSERT INTO org_task (org_id,creator_member_id,assignee_member_id,title,scene_tag,due_date,related_client_id,note) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (s["org_id"], s["member_id"], assignee, title,
                     body.get("scene_tag") or "其他", body.get("due_date") or None,
                     cid or None, body.get("note") or ""))
        tid = cur.lastrowid
        cx.commit(); cx.close()
        ip = self.client_address[0] if self.client_address else ""
        write_audit("org", "task_create", f"task#{tid}", f"org={s['org_id']} assignee={assignee}", ip)
        return {"ok": True, "id": tid}

    def api_org_task_update(self, body, s):
        tid = body.get("id")
        row = query("SELECT * FROM org_task WHERE id=? AND org_id=?", (tid, s["org_id"]))
        if not row:
            return {"ok": False, "msg": "任务不存在"}
        t = row[0]
        # 权限：被指派的成员本人，或机构负责人/管理员
        if t["assignee_member_id"] != s["member_id"] and s["role"] not in ("owner", "admin"):
            return {"ok": False, "msg": "无权限（仅执行人或负责人/管理员可流转）"}
        # 负责人/管理员可改执行人、截止日、场景；执行人可改状态
        if s["role"] in ("owner", "admin"):
            if body.get("assignee_member_id") and body["assignee_member_id"] != t["assignee_member_id"]:
                new_a = body["assignee_member_id"]
                if not query("SELECT id FROM org_member WHERE id=? AND org_id=? AND seat_status='active'",
                             (new_a, s["org_id"])):
                    return {"ok": False, "msg": "执行成员不存在或已停用"}
                exec("UPDATE org_task SET assignee_member_id=? WHERE id=? AND org_id=?",
                     (new_a, tid, s["org_id"]))
            if body.get("due_date") is not None:
                exec("UPDATE org_task SET due_date=? WHERE id=? AND org_id=?",
                     (body["due_date"] or None, tid, s["org_id"]))
            if body.get("scene_tag"):
                exec("UPDATE org_task SET scene_tag=? WHERE id=? AND org_id=?",
                     (body["scene_tag"], tid, s["org_id"]))
        status = body.get("status")
        if status in ("todo", "doing", "done"):
            exec("UPDATE org_task SET status=? WHERE id=? AND org_id=?", (status, tid, s["org_id"]))
        if body.get("note") is not None:
            exec("UPDATE org_task SET note=? WHERE id=? AND org_id=?", (body["note"], tid, s["org_id"]))
        return {"ok": True}

    def log_message(self, fmt, *args):
        pass  # 静默日志


# =====================================================================
# 6) 启动
# =====================================================================
if __name__ == "__main__":
    # 生产环境强制校验：禁止以弱口令/空口令启动，杜绝公网暴露后门。
    if APP_ENV == "production":
        _fatal = []
        if not ADMIN_TOKEN or ADMIN_TOKEN == "admin-dev-2026":
            _fatal.append("ADMIN_TOKEN")
        if not CRON_TOKEN or CRON_TOKEN == "cron-dev-2026":
            _fatal.append("CRON_TOKEN")
        if _fatal:
            print("❌ 生产环境禁止以弱口令/空口令启动，请先设置环境变量：%s" % ", ".join(_fatal))
            sys.exit(1)
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"✅ 慧根堂·财税AI智库 后端已启动")
    print(f"   访问： http://localhost:{PORT}")
    print(f"   （原型由本服务同源托管，可真实拉取积分 / 提问 / 收藏等数据）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
