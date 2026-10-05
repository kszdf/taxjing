# -*- coding: utf-8 -*-
"""
慧根堂·财税AI智库 — RAG 检索模块 (M4+ 大模型接入)

在政策时效引擎的 STORE 上做零依赖关键词检索，返回与问题最相关的
『现行有效』条款，作为大模型的 grounding context。

铁律（呼应"不可有任何错误"）：
  - 只取 clause_status == 'active' 的条款；失效/被废止条款绝不进入 grounding。
  - 严格基于 policy_engine 的时效裁决结果，不自行判断有效性。
  - 检索范围 = 已载入 STORE 的 'active' / 'partially_invalid' 政策（含 seed 与
    经运营复核生效的联网政策）；原始 pending_review 队列不进检索（待人工复核）。

设计：retrieve(store, question, ...) 以"依赖注入"方式接收 store，
避免与 server 形成循环 import。
"""
import re
from datetime import date

# 停用词：出现在财税问答里几乎不携带检索信号的虚词
_STOP = set("的了吗呢吧啊把被和对与及或等在是有了不和这那哪个该各等其之将已")


def is_in_force(policy, as_of=None):
    """是否已生效。生效日在未来（官方标"尚未生效"）的政策**不进 grounding**——
    否则会拿还没生效的政策去回答客户，属实质性错误。"""
    eff = str(getattr(policy, "effective_date", "") or "").strip()
    if not eff:
        return True          # 无生效日则不拦（由状态字段兜底）
    return eff <= (as_of or date.today().isoformat())


def tokenize(text):
    text = (text or "").lower()
    tokens = set()
    # 中文：单字 + 相邻二字（bigram）提升召回
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if not seg:
            continue
        for ch in seg:
            tokens.add(ch)
        for i in range(len(seg) - 1):
            tokens.add(seg[i:i + 2])
    # 英文/数字词
    for w in re.findall(r"[a-z0-9]+", text):
        if len(w) >= 2:
            tokens.add(w)
    tokens -= _STOP
    return tokens


def _score(text, qtokens):
    if not qtokens:
        return 0.0
    tt = tokenize(text)
    if not tt:
        return 0.0
    inter = tt & qtokens
    if not inter:
        return 0.0
    # 词级门槛：必须命中至少一个长度≥2 的 token（真实词/二字），
    # 否则单字偶然命中会造成"误接地"（拿不相关政策当依据）——宁可不答，不可错答。
    if not any(len(t) >= 2 for t in inter):
        return 0.0
    # 交并比思路：命中越多得分越高，按问题长度做轻微归一避免长问题占优
    return len(inter) / (len(qtokens) ** 0.5 + 1.0)


# ============================ 领域映射（未精确命中时的"同领域"召回） ============================
# 说明：这里只是"问题 → 领域"的**线索词**，最终以 STORE 中**实际存在的 category** 为准，
# 因此政策库新增分类时无需改本表（避免 schema 漂移、符合"可升级不推倒"）。
_CATEGORY_HINTS = {
    "增值税": ["增值税", "小规模", "简易计税", "进项", "销项", "留抵", "免税", "零税率",
             "征收率", "专票", "普票", "开票", "纳税人"],
    "企业所得税": ["企业所得税", "应纳税所得额", "税前扣除", "小型微利", "汇算清缴",
              "固定资产", "折旧", "研发费用", "加计扣除", "不征税收入", "亏损弥补", "成本费用"],
    "个人所得税": ["个人所得税", "个税", "工资薪金", "劳务报酬", "经营所得", "股息红利",
              "综合所得", "专项附加扣除", "代扣代缴", "年终奖", "分红"],
    "个税": ["个税", "个人所得税", "工资", "劳务", "分红", "代扣代缴", "年终奖"],
    "社保": ["社保", "社会保险", "养老", "医疗", "失业", "工伤", "公积金", "用工", "劳动合同"],
    "税收优惠": ["优惠", "减免", "小微企业", "六税两费", "减半", "免征", "政策", "补贴"],
    "注销": ["注销", "清算", "清税", "吊销", "停业", "简易注销"],
    "注销登记": ["注销", "清算", "清税", "吊销", "停业", "简易注销"],
    "股权": ["股权", "转让", "增资", "减资", "重组", "并购", "股东", "实缴", "认缴"],
    "发票管理": ["发票", "虚开", "红冲", "作废", "开票", "税控", "抵扣"],
    "征收管理": ["申报", "征收", "核定", "税务登记", "变更", "逾期", "滞纳金", "备案", "资料"],
    "建筑": ["建筑", "工程", "分包", "挂靠", "甲供材", "异地预缴", "农民工", "劳务"],
    "电商": ["电商", "平台", "直播", "网店", "刷单", "线上"],
}


def guess_category(question, available):
    """把问题映射到 STORE 中**实际存在**的 category；无匹配返回 None。"""
    q = question or ""
    best, best_n = None, 0
    for cat in available:
        hints = _CATEGORY_HINTS.get(cat) or ([cat] if cat else [])
        n = sum(1 for h in hints if h and h in q)
        if n > best_n:
            best, best_n = cat, n
    return best


def _row(pol, clause, score):
    return {
        "doc_number": pol.doc_number,
        "title": pol.title,
        "effective_date": getattr(pol, "effective_date", ""),
        "category": getattr(pol, "category", ""),
        "clause_no": clause.clause_no,
        "content": clause.content,
        "policy_status": pol.status,
        "score": score,
    }


def _recency(row):
    y = str(row.get("effective_date") or "")[:4]
    return int(y) if y.isdigit() else 0


def _pack(rows, tier):
    chunks, refs = [], []
    for s in rows:
        if s.get("doc_number") and s["doc_number"] not in refs:
            refs.append(s["doc_number"])
        eff = f"（施行 {s['effective_date']}）" if s.get("effective_date") else ""
        chunks.append(f"【{s['doc_number'] or s['title']}{eff}】{s['clause_no']}：{s['content']}")
    return chunks, refs, rows, tier


def _collect_hits(store, qtokens, category, top_k):
    """精确层：要求条款正文命中（含词级门槛），返回带分结果。"""
    scored = []
    for pol in store.policies.values():
        if pol.status not in ("active", "partially_invalid"):
            continue
        if not is_in_force(pol):
            continue          # 尚未生效的文件不参与作答
        pscore = _score(pol.title + " " + (pol.doc_number or ""), qtokens)
        if category and pol.category == category:
            pscore += 1.0
        for c in pol.clauses:
            if getattr(c, "clause_status", "active") != "active":
                continue  # 失效/被废止条款绝不进入 grounding
            cscore = _score(c.content, qtokens) + pscore * 0.3
            if cscore <= 0:
                continue
            scored.append(_row(pol, c, cscore))
    scored.sort(key=lambda x: (-x["score"], -_recency(x)))
    return scored


def retrieve_tiered(store, question, top_k=6, category=None):
    """分层检索 —— 核心目的：**绝不空手而归**，但绝不"错接地"。

    返回 (chunks, refs, ranked, tier)：
      exact  —— 精确命中现行有效条款，可直接引用、可直接对客；
      domain —— 未精确命中，但可判定领域 → 召回该领域全部现行有效条款，供判断适用性；
      index  —— 领域也判不出 → 给出可查的现行政策线索（文号+标题），供进一步检索；
      none   —— 库中确无可检索内容（此时才允许回退规则引擎）。
    """
    qtokens = tokenize(question)
    categories = {p.category for p in store.policies.values()
                  if p.status in ("active", "partially_invalid") and p.category}
    cat = category if (category in categories) else guess_category(question, categories)

    scored = _collect_hits(store, qtokens, cat, top_k)
    if scored:
        return _pack(scored[:top_k], "exact")

    # —— 未精确命中：同领域召回（不要求词命中，但必须同领域，供判断"是否适用"）——
    if cat:
        rows = []
        for pol in store.policies.values():
            if pol.status not in ("active", "partially_invalid") or pol.category != cat:
                continue
            if not is_in_force(pol):
                continue
            for c in pol.clauses:
                if getattr(c, "clause_status", "active") != "active":
                    continue
                rows.append(_row(pol, c, 0.5))
        if rows:
            rows.sort(key=lambda x: -_recency(x))
            return _pack(rows[:top_k], "domain")

    # —— 领域也判不出：给"可查文件线索"，而不是一句"没有" ——
    idx = []
    for pol in store.policies.values():
        if pol.status not in ("active", "partially_invalid"):
            continue
        if not is_in_force(pol):
            continue
        idx.append({"doc_number": pol.doc_number or "", "title": pol.title,
                    "effective_date": getattr(pol, "effective_date", ""),
                    "category": getattr(pol, "category", "")})
    idx.sort(key=lambda x: -_recency(x))
    if idx:
        top = idx[:top_k]
        chunks = [f"【现行有效文件索引】{i['doc_number']} {i['title']}".strip() for i in top]
        refs = [i["doc_number"] for i in top if i["doc_number"]]
        return chunks, refs, top, "index"
    return [], [], [], "none"


def retrieve(store, question, top_k=6, category=None):
    """兼容旧签名：返回 (chunks, refs, ranked)，丢弃 tier。"""
    chunks, refs, ranked, _tier = retrieve_tiered(store, question, top_k, category)
    return chunks, refs, ranked
