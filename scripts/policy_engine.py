# -*- coding: utf-8 -*-
"""
慧根堂·财税AI智库 — 政策时效校验引擎 (M1 核心)

职责（用户硬要求）：
  1. 联网自动更新政策文件（fetch 骨架：chinatax.gov.cn / 税屋网）；
  2. 能判断"同一内容有新政策则按新政策答" —— supersession 时效图谱；
  3. 条款级粒度：一份文件部分条款失效时，仅失效该条，不误杀整份；
  4. 检索/生成阶段强制只取"现行有效"且"最新/上位法"，失效内容不参与答案；
  5. 每次回答由生成层统一注入免责声明（见 generate_answer 钩子）。

设计原则：本文件是"逻辑地基"，可独立离线自测；接数据库时，把 _store 换成
MySQL/向量库实现即可，业务逻辑不变（呼应"可升级不推倒"）。
"""
from __future__ import annotations
import re
import json
import argparse
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional


# ============================ 数据模型 ============================
@dataclass
class Policy:
    doc_id: int
    title: str
    doc_number: str
    effective_date: str          # ISO: 2019-01-01
    status: str = "active"       # active / partially_invalid / invalid / superseded
    category: str = ""
    source_url: str = ""
    clauses: list = field(default_factory=list)


@dataclass
class Clause:
    clause_no: str               # 第一条 / 第二条
    content: str
    clause_status: str = "active"  # active / invalid / partially_invalid
    invalid_since: Optional[str] = None
    superseded_by: Optional[str] = None


@dataclass
class Supersession:
    source_doc_id: int           # 新文件（替代方）
    target_doc_id: Optional[int] # 旧文件（被替代方）
    target_clause_no: Optional[str] = None   # None=整份
    effective_date: Optional[str] = None
    reason: str = ""


# ============================ 存储（可替换为 DB） ============================
class PolicyStore:
    def __init__(self):
        self.policies: dict[int, Policy] = {}
        self.supersessions: list[Supersession] = []
        self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def add_policy(self, title, doc_number, effective_date, category="", source_url="", clauses=None) -> Policy:
        p = Policy(doc_id=self._next_id(), title=title, doc_number=doc_number,
                   effective_date=effective_date, category=category, source_url=source_url,
                   clauses=clauses or [])
        self.policies[p.doc_id] = p
        return p

    def add_clause(self, policy: Policy, clause_no, content, clause_status="active",
                   invalid_since=None, superseded_by=None):
        policy.clauses.append(Clause(clause_no, content, clause_status, invalid_since, superseded_by))

    def add_supersession(self, source_doc_id, target_doc_id, target_clause_no=None,
                         effective_date=None, reason=""):
        self.supersessions.append(
            Supersession(source_doc_id, target_doc_id, target_clause_no, effective_date, reason))

    # ---- 核心：应用废止关系，自动置旧为失效 ----
    def apply_supersessions(self):
        for s in self.supersessions:
            tgt = self.policies.get(s.target_doc_id)
            if not tgt:
                continue
            if s.target_clause_no:
                # 仅条款级失效
                for c in tgt.clauses:
                    if c.clause_no == s.target_clause_no:
                        c.clause_status = "invalid"
                        c.invalid_since = s.effective_date
                        c.superseded_by = f"doc#{s.source_doc_id}"
                # 若文件仍有有效条款，则整体为"部分失效"
                any_active = any(c.clause_status == "active" for c in tgt.clauses)
                tgt.status = "partially_invalid" if any_active else "invalid"
            else:
                # 整份失效
                tgt.status = "invalid"
                for c in tgt.clauses:
                    c.clause_status = "invalid"
                    c.invalid_since = s.effective_date
                    c.superseded_by = f"doc#{s.source_doc_id}"

    # ---- 核心：同主题取"现行有效 + 最新施行" ----
    def resolve_active(self, category: str, as_of: Optional[str] = None) -> Optional[Policy]:
        as_of = as_of or date.today().isoformat()
        candidates = [p for p in self.policies.values()
                      if p.category == category and p.status in ("active", "partially_invalid")]
        if not candidates:
            return None
        # 新优于旧：按施行日期降序
        candidates.sort(key=lambda p: p.effective_date, reverse=True)
        return candidates[0]

    # ---- 核心：返回某文件"仍有效"的条款（失效的剔除，供生成用） ----
    def valid_clauses(self, policy: Policy) -> list[Clause]:
        return [c for c in policy.clauses if c.clause_status == "active"]


# ============================ 废止关系自动识别（联网更新时用） ============================
#  heuristic：从新公告正文中抽取"自X起废止《Y》第Z条"类表述
SUPER_RE = re.compile(
    r"自(?P<date>\d{4}年\d{1,2}月\d{1,2}日|[\d-]+)起[，,]?"
    r"(?:废止|废除|停止执行|修订)[了]?《?(?P<title>[^》]+)》?[^第]*"
    r"(第(?P<clause>[一二三四五六七八九十]+)条)?")


def detect_supersession(text: str, source_doc_id: int, store: PolicyStore) -> list[Supersession]:
    """扫描新政策正文，自动识别对旧文件的废止关系，回写图谱。"""
    found = []
    for m in SUPER_RE.finditer(text):
        title = m.group("clause_head") if False else m.group("title")
        clause_cn = m.group("clause")
        clause_no = f"第{clause_cn}条" if clause_cn else None
        # 匹配被废止的文件
        tgt = next((p for p in store.policies.values()
                    if p.title[:6] in title or title[:6] in p.title), None)
        if tgt:
            found.append(Supersession(
                source_doc_id=source_doc_id, target_doc_id=tgt.doc_id,
                target_clause_no=clause_no, effective_date=_norm_date(m.group("date"))))
    return found


def _norm_date(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


# ============================ 生成层：强制免责注入 ============================
DISCLAIMER_STRONG = "⚠️ 本解读仅供参考，一切以税务机关最新口径为准，不作为正式税务意见；涉及您企业具体情形，请转人工答疑。"
DISCLAIMER_TEXT = "※ 以上为现行有效政策条文引用，实际操作仍以主管税务机关口径为准；如需正式意见可转人工答疑。"


def generate_answer(raw_answer: str, is_interpretation: bool) -> str:
    """生成层统一在末尾注入免责声明（不依赖模型自觉，漏一条都不行）。"""
    return raw_answer + ("\n" + DISCLAIMER_STRONG if is_interpretation else "\n" + DISCLAIMER_TEXT)


# ============================ 联网自动更新骨架 ============================
def fetch_latest_policies() -> list[dict]:
    """
    联网自动更新骨架。部署时启用：
      - 定时监控 chinatax.gov.cn、税屋网、各省级局（含昆山/苏州/江苏本地口径）
      - 解析文号/施行日/废止关系，写入 policy_registry + policy_clause
      - 调用 detect_supersession 更新图谱，再 apply_supersessions()
    此处仅留接口与说明；实际抓取需 requests + 解析 + 人工复核重大废止。
    """
    # import requests
    # sources = ["http://www.chinatax.gov.cn/", "http://www.shui5.cn/"]
    # for url in sources: ...
    raise NotImplementedError("部署时接 requests + 解析器；本环境不发起网络请求（沙箱/合规）。")


# ============================ 离线自测 ============================
def self_test():
    store = PolicyStore()
    # 旧文件：国家税务总局公告2019年第4号
    p4 = store.add_policy(
        "国家税务总局关于小规模纳税人免征增值税政策有关征管问题的公告",
        "国家税务总局公告2019年第4号", "2019-01-01", category="增值税",
        source_url="http://www.chinatax.gov.cn/")
    store.add_clause(p4, "第一条", "小规模纳税人合计月销售额未超过10万元的，免征增值税。")
    store.add_clause(p4, "第二条", "原规定：小规模纳税人月销售额不超过3万元（按季9万元）免征增值税。")
    store.add_clause(p4, "第三条", "可选择以1个月或1个季度为纳税期限，一年内不得变更。")

    # 新文件：财政部 税务总局公告2023年第19号（替代4号第二条）
    p19 = store.add_policy(
        "财政部 税务总局关于明确增值税小规模纳税人减免政策的公告",
        "财政部 税务总局公告2023年第19号", "2023-01-01", category="增值税",
        source_url="http://www.chinatax.gov.cn/")
    store.add_clause(p19, "第一条", "小规模纳税人合计月销售额未超过10万元（按季30万元）的，免征增值税。")

    # 新增废止关系：19号自2023-01-01废止4号第二条
    store.add_supersession(source_doc_id=p19.doc_id, target_doc_id=p4.doc_id,
                           target_clause_no="第二条", effective_date="2023-01-01",
                           reason="提高免征标准")
    store.apply_supersessions()

    # 断言1：4号整体应为"部分失效"
    assert p4.status == "partially_invalid", f"期望部分失效，实际 {p4.status}"
    # 断言2：4号第二条条款级失效
    c2 = next(c for c in p4.clauses if c.clause_no == "第二条")
    assert c2.clause_status == "invalid", "第二条应被置为失效"
    # 断言3：4号第一条、第三条仍有效
    assert all(c.clause_status == "active" for c in p4.clauses if c.clause_no in ("第一条", "第三条"))
    # 断言4：同主题裁决应取"现行有效+最新"=19号
    resolved = store.resolve_active("增值税")
    assert resolved.doc_number == "财政部 税务总局公告2023年第19号", f"裁决错误：{resolved.doc_number}"
    # 断言5：生成阶段只取有效条款（4号第二条不参与）
    valid = store.valid_clauses(p4)
    assert all(c.clause_status == "active" for c in valid)
    # 断言6：免责注入
    ans = generate_answer("超过部分全额按3%征收。", is_interpretation=True)
    assert DISCLAIMER_STRONG in ans

    print("✅ 自测通过：")
    print(f"   - 2019年第4号 状态 = {p4.status}（第二条条款级失效，第一/三条仍有效）")
    print(f"   - 同主题裁决取最新有效 = {resolved.doc_number}")
    print(f"   - 失效条款参与生成？{ '否（已剔除）' if all(c.clause_status=='active' for c in valid) else '是（错误）' }")
    print(f"   - 免责声明已注入 = {DISCLAIMER_STRONG in ans}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="政策时效校验引擎")
    parser.add_argument("--selftest", action="store_true", help="运行离线自测")
    args = parser.parse_args()
    if args.selftest:
        self_test()
    else:
        print("用法：python policy_engine.py --selftest")
