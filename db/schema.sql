-- =====================================================================
-- 慧根堂·财税AI智库  —  核心数据库 Schema（MySQL 8.0+）
-- 设计铁律（用户硬要求：可升级、绝不推倒重来）：
--   1. 全程 IF NOT EXISTS，绝不使用 DROP；
--   2. 每张表预留 meta JSON 装未来不确定属性；
--   3. 新增能力 = 加表/加列(带默认值)，不改动旧结构；
--   4. 用户资产(积分/收藏/关系链/提问)独立持久，升级只动"能力"不动"资产"。
-- 说明：以下为地基版，二期新增功能（如订单/企业版）将用 ALTER/新表叠加，不修改本文件既有表。
-- =====================================================================

-- ---------- 用户 ----------
CREATE TABLE IF NOT EXISTS users (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  openid        VARCHAR(64)  NOT NULL COMMENT '微信 openid（唯一身份）',
  unionid       VARCHAR(64)  DEFAULT NULL COMMENT '微信 unionid',
  phone         VARCHAR(20)  DEFAULT NULL COMMENT '绑定手机号（限领一次用）',
  nickname      VARCHAR(64)  DEFAULT NULL,
  avatar        VARCHAR(255) DEFAULT NULL,
  identity_tag  VARCHAR(32)  DEFAULT NULL COMMENT '身份标签：会计/代账/企业主',
  status        TINYINT      NOT NULL DEFAULT 1 COMMENT '1正常 0禁用',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  meta          JSON         DEFAULT NULL COMMENT '未来扩展属性（如企业信息、认证状态）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_openid (openid)
) COMMENT='用户主表';

-- ---------- 会员（免费/月卡/年卡/企业版） ----------
CREATE TABLE IF NOT EXISTS membership (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL,
  level       VARCHAR(16)  NOT NULL DEFAULT 'free' COMMENT 'free/monthly/yearly/enterprise',
  start_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  end_at      DATETIME     DEFAULT NULL COMMENT 'NULL=永久/未到期',
  order_ref   VARCHAR(64)  DEFAULT NULL COMMENT '关联订单（企微侧充值后回填）',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta        JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_user (user_id)
) COMMENT='会员状态表';

-- ---------- 积分账户（不可提现/不可转让/滚动有效期） ----------
CREATE TABLE IF NOT EXISTS points_account (
  user_id       BIGINT UNSIGNED NOT NULL,
  balance       INT          NOT NULL DEFAULT 0 COMMENT '当前可用积分（>=0）',
  frozen        INT          NOT NULL DEFAULT 0 COMMENT '冻结中积分',
  last_settle_at DATETIME    DEFAULT NULL COMMENT '上次滚动过期结算时间',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id)
) COMMENT='积分账户：余额即1:1人民币等值，仅限本平台消费';

-- ---------- 积分流水（获取/消耗，可审计） ----------
CREATE TABLE IF NOT EXISTS points_ledger (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL,
  txn_type    VARCHAR(8)   NOT NULL COMMENT 'earn 获取 / spend 消耗',
  amount      INT          NOT NULL COMMENT '正数',
  reason      VARCHAR(32)  NOT NULL COMMENT 'sign_in/invite/ai_answer/human_answer/register_gift...',
  ref_id      VARCHAR(64)  DEFAULT NULL COMMENT '关联业务ID（如 question_id）',
  expire_at   DATETIME     DEFAULT NULL COMMENT '该笔积分到期时间（滚动有效期，注册赠送/签到分桶）',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta        JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_user_created (user_id, created_at)
) COMMENT='积分流水：每笔可溯源，支撑"防白嫖+滚动清零"';

-- ---------- 邀请关系链（一级分销+排行榜，可追踪） ----------
CREATE TABLE IF NOT EXISTS invite_chain (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  inviter_id   BIGINT UNSIGNED NOT NULL COMMENT '邀请人',
  invitee_id   BIGINT UNSIGNED NOT NULL COMMENT '被邀请人',
  level        TINYINT      NOT NULL DEFAULT 1 COMMENT '仅1级（合规红线：不碰多级返利）',
  reward_status VARCHAR(12) NOT NULL DEFAULT 'pending' COMMENT 'pending/granted',
  reward_points INT         DEFAULT NULL COMMENT '已发放奖励积分',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_pair (inviter_id, invitee_id),
  KEY idx_inviter (inviter_id)
) COMMENT='邀请关系：注册即绑定，支撑裂变漏斗看板';

-- ---------- 定价配置（服务端定价，用户无填价权，配置化可调） ----------
CREATE TABLE IF NOT EXISTS pricing_config (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  category       VARCHAR(24)  NOT NULL COMMENT 'ai_free/ai_deep/human_quick/project',
  name           VARCHAR(32)  NOT NULL COMMENT '展示名（如 人工快答）',
  price_points   INT          NOT NULL COMMENT '固定积分价，用户不可自定义',
  route_to       VARCHAR(16)  NOT NULL DEFAULT 'app' COMMENT 'app内积分结清 / l3企微深度咨询',
  enabled        TINYINT      NOT NULL DEFAULT 1,
  effective_from DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note           VARCHAR(128) DEFAULT NULL,
  meta           JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_cat (category)
) COMMENT='定价表：消费价服务端定死，呼应"不能客户愿给多少给多少"';

-- ---------- 政策主表（权威规定：国家税务总局等） ----------
CREATE TABLE IF NOT EXISTS policy_registry (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title           VARCHAR(255) NOT NULL,
  doc_type        VARCHAR(16)  NOT NULL DEFAULT 'policy' COMMENT 'policy/announcement/form/interpretation',
  issuing_authority VARCHAR(128) DEFAULT NULL COMMENT '发文机关',
  doc_number      VARCHAR(64)  DEFAULT NULL COMMENT '文号（如 财政部 税务总局公告2023年第19号）',
  publish_date    DATE         DEFAULT NULL,
  effective_date  DATE         DEFAULT NULL,
  status          VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active/partially_invalid/invalid/superseded',
  province        VARCHAR(16)  DEFAULT NULL COMMENT '适用省份（本地口径用，如 江苏）',
  city            VARCHAR(16)  DEFAULT NULL COMMENT '适用城市（如 苏州/昆山）',
  category        VARCHAR(32)  DEFAULT NULL COMMENT '增值税/企业所得税/个税/社保/征管/稽查...',
  content_text    MEDIUMTEXT   DEFAULT NULL COMMENT '正文（结构化入库用）',
  source_url      VARCHAR(512) DEFAULT NULL COMMENT '官方原文链接',
  last_verified_at DATETIME    DEFAULT NULL COMMENT '最近一次联网核验时间',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  meta            JSON         DEFAULT NULL COMMENT '未来属性（如配套表单关联、解读链接）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_doc_number (doc_number),
  KEY idx_cat_status (category, status),
  KEY idx_effective (effective_date)
) COMMENT='政策主表：联网自动更新，是回答的唯一准绳';

-- ---------- 政策条款表（条款级时效粒度，支持"部分失效"） ----------
CREATE TABLE IF NOT EXISTS policy_clause (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doc_id           BIGINT UNSIGNED NOT NULL,
  clause_no        VARCHAR(32)  NOT NULL COMMENT '第一条 / 第二条 等',
  content          TEXT         NOT NULL,
  clause_status    VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active/invalid/partially_invalid',
  invalid_since    DATE         DEFAULT NULL COMMENT '失效日期',
  superseded_by    VARCHAR(255) DEFAULT NULL COMMENT '被哪个新文件/条款替代',
  display_order    INT          NOT NULL DEFAULT 0,
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta             JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_doc (doc_id),
  KEY idx_status (clause_status)
) COMMENT='条款级时效：打补丁式废止时仅失效该条，不误杀整份文件';

-- ---------- 政策废止/替代关系图（supersession 图谱） ----------
CREATE TABLE IF NOT EXISTS policy_supersession (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_doc_id     BIGINT UNSIGNED NOT NULL COMMENT '新文件（替代方）',
  target_doc_id     BIGINT UNSIGNED DEFAULT NULL COMMENT '旧文件（被替代方）',
  target_clause_id  BIGINT UNSIGNED DEFAULT NULL COMMENT '旧条款（NULL=整份）',
  reason            VARCHAR(255) DEFAULT NULL,
  effective_date    DATE         DEFAULT NULL COMMENT '替代生效日',
  source_url        VARCHAR(512) DEFAULT NULL,
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_target (target_doc_id, target_clause_id)
) COMMENT='废止关系图谱：自动置旧为失效，双向关联，检索阶段过滤';

-- ---------- 案例库（稽查/风险/行业/本地） ----------
CREATE TABLE IF NOT EXISTS case_lib (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title         VARCHAR(255) NOT NULL,
  category      VARCHAR(24)  NOT NULL COMMENT 'penalty/risk/industry/local',
  industry      VARCHAR(32)  DEFAULT NULL,
  region        VARCHAR(32)  DEFAULT NULL COMMENT '如 昆山/苏州/江苏',
  penalty_amount VARCHAR(32) DEFAULT NULL COMMENT '处罚金额（如实记录，标注真实日期）',
  publish_date  DATE         DEFAULT NULL,
  source        VARCHAR(128) DEFAULT NULL COMMENT '来源（税务局通报/官媒）',
  summary       TEXT         DEFAULT NULL,
  risk_points   TEXT         DEFAULT NULL COMMENT '可借鉴风险点',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta          JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_cat_region (category, region)
) COMMENT='案例库：按行业/风险分类，结构化呈现案情/处罚/借鉴点';

-- ---------- 行业专题 ----------
CREATE TABLE IF NOT EXISTS industry_topic (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  title        VARCHAR(128) NOT NULL,
  industry     VARCHAR(32)  NOT NULL COMMENT '建筑/电商/直播/灵活用工...',
  summary      TEXT         DEFAULT NULL,
  local_note   TEXT         DEFAULT NULL COMMENT '昆山/苏州本地口径专区',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta         JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_industry (industry)
) COMMENT='行业专题库';

-- ---------- 提问记录（用户资产，升级不丢） ----------
CREATE TABLE IF NOT EXISTS question_record (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL,
  session_id    VARCHAR(64)  DEFAULT NULL,
  question      TEXT         NOT NULL,
  answer        MEDIUMTEXT   DEFAULT NULL,
  answer_type   VARCHAR(8)   NOT NULL DEFAULT 'ai' COMMENT 'ai/human',
  policy_refs   JSON         DEFAULT NULL COMMENT '引用的政策文号数组（溯源）',
  cost_points   INT          NOT NULL DEFAULT 0,
  status        VARCHAR(12)  NOT NULL DEFAULT 'answered' COMMENT 'answered/pending/l3',
  routed_to     VARCHAR(12)  DEFAULT NULL COMMENT 'consult 待专家 / l3 深度咨询',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta          JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_user (user_id, created_at)
) COMMENT='提问历史：个人中心"我的问题"数据源';

-- ---------- 收藏（用户资产，支持状态快照+回访失效提示） ----------
CREATE TABLE IF NOT EXISTS collection (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id        BIGINT UNSIGNED NOT NULL,
  collect_type   VARCHAR(12)  NOT NULL COMMENT 'policy/case/qa/template',
  ref_id         VARCHAR(64)  NOT NULL COMMENT '关联记录ID',
  snapshot_status VARCHAR(24) DEFAULT NULL COMMENT '收藏时快照状态（如 现行有效/部分失效）',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta           JSON         DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_ref (user_id, collect_type, ref_id),
  KEY idx_user (user_id)
) COMMENT='我的收藏：回访时比对当前状态，失效则提示';

-- ---------- 专家（初期占位卡兼招募入口，不伪造真实人数） ----------
CREATE TABLE IF NOT EXISTS expert (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name          VARCHAR(64)  DEFAULT NULL COMMENT '真实入驻后填，占位卡可为NULL',
  title         VARCHAR(128) DEFAULT NULL,
  cert_no_enc   VARCHAR(255) DEFAULT NULL COMMENT '资格证书号（加密存储）',
  bio           TEXT         DEFAULT NULL,
  status        VARCHAR(12)  NOT NULL DEFAULT 'coming' COMMENT 'coming 邀约中 / active 已入驻',
  display_order INT          NOT NULL DEFAULT 0,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  meta          JSON         DEFAULT NULL,
  PRIMARY KEY (id)
) COMMENT='专家表：初期仅"张老师"真实卡+占位卡，真实入驻后再点亮';

-- ---------- 咨询动态（仅显活跃，不露金额） ----------
CREATE TABLE IF NOT EXISTS consult_dynamics (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_mask    VARCHAR(32)  NOT NULL COMMENT '脱敏展示名（如 王会计）',
  action_type  VARCHAR(16)  NOT NULL COMMENT 'consulting/answered',
  topic        VARCHAR(64)  DEFAULT NULL COMMENT '咨询主题（不露金额/付费与否）',
  is_paid      TINYINT      NOT NULL DEFAULT 0 COMMENT '内部标记，前端不展示',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_created (created_at)
) COMMENT='咨询动态流：制造社会证明，真实行为，不刷量';

-- =====================================================================
-- 索引与初始数据（定价配置：服务端固定价，呼应二维类别定价）
-- =====================================================================
INSERT INTO pricing_config (category, name, price_points, route_to, note) VALUES
  ('ai_free',   'AI问答(免费配额)', 0,  'app', '每日免费配额'),
  ('ai_deep',   'AI深度问答',        8,  'app', '带文号溯源长解读'),
  ('human_quick','人工快答(单点问题)', 30, 'app', '不论难易一口价，复杂转L3'),
  ('project',   '项目类(筹划/稽查/股权/注销)', 0, 'l3', '不按积分，转企微正式委托')
ON DUPLICATE KEY UPDATE price_points=VALUES(price_points), note=VALUES(note);

-- =====================================================================
-- 同行问（M1 用户互助社区）：发帖/回答/点赞/采纳
-- =====================================================================
CREATE TABLE IF NOT EXISTS community_post (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       BIGINT UNSIGNED NOT NULL,
  openid        VARCHAR(64)  NOT NULL,
  title         VARCHAR(120) NOT NULL COMMENT '标题（展示限60字）',
  content       TEXT         NOT NULL COMMENT '正文（限2000字）',
  scene_tag     VARCHAR(16)  NOT NULL DEFAULT '其他' COMMENT '发票/申报/风控预警/工商变更/资质办理/稽查应对/其他',
  is_anonymous  TINYINT      NOT NULL DEFAULT 0 COMMENT '匿名发帖：展示层脱敏为"匿名从业者"，运营可查真实身份',
  bounty_points INT          NOT NULL DEFAULT 0 COMMENT '悬赏积分（M2启用）',
  status        VARCHAR(12)  NOT NULL DEFAULT 'open' COMMENT 'open/accepted/removed(软删)',
  ai_answer     JSON         DEFAULT NULL COMMENT 'AI先答垫底 {answer, policy_refs}，发帖后异步生成',
  ai_mode       VARCHAR(32)  DEFAULT NULL,
  view_count    INT UNSIGNED NOT NULL DEFAULT 0,
  reply_count   INT UNSIGNED NOT NULL DEFAULT 0,
  pinned        TINYINT      NOT NULL DEFAULT 0 COMMENT '运营加精置顶',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_list (status, scene_tag, id),
  KEY idx_user (user_id)
) COMMENT='同行问帖子：从业者实务互助，AI先答垫底+人工实战补充';

CREATE TABLE IF NOT EXISTS post_reply (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  post_id       BIGINT UNSIGNED NOT NULL,
  user_id       BIGINT UNSIGNED NOT NULL,
  openid        VARCHAR(64)  NOT NULL,
  content       TEXT         NOT NULL COMMENT '回答正文（限1500字）',
  policy_refs   JSON         DEFAULT NULL COMMENT '引用政策文号数组（入库前已校验为现行有效）',
  like_count    INT UNSIGNED NOT NULL DEFAULT 0,
  is_accepted   TINYINT      NOT NULL DEFAULT 0 COMMENT '被发帖人采纳',
  status        VARCHAR(12)  NOT NULL DEFAULT 'open' COMMENT 'open/removed(软删)',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_post (post_id, id)
) COMMENT='同行问回答：自愿回答，被采纳答主+20积分';

CREATE TABLE IF NOT EXISTS post_like (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  reply_id      BIGINT UNSIGNED NOT NULL,
  user_id       BIGINT UNSIGNED NOT NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_reply_user (reply_id, user_id)
) COMMENT='回答点赞：UNIQUE约束防重复，幂等';

-- =====================================================================
-- 机构版：作业任务分派（工作流锁定核心）
-- 机构主/管理员下派日常作业（申报/风控/工商/其他）给成员，关联客户与征期场景。
-- =====================================================================
CREATE TABLE IF NOT EXISTS org_task (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id              BIGINT UNSIGNED NOT NULL,
  creator_member_id   BIGINT UNSIGNED NOT NULL,
  assignee_member_id  BIGINT UNSIGNED NOT NULL,
  title               VARCHAR(160) NOT NULL COMMENT '任务标题',
  scene_tag           VARCHAR(16)  NOT NULL DEFAULT '其他' COMMENT '申报/风控/工商/其他',
  due_date            DATE         DEFAULT NULL COMMENT '截止日（YYYY-MM-DD）',
  related_client_id   BIGINT UNSIGNED DEFAULT NULL COMMENT '关联客户（client_profile.id）',
  status              VARCHAR(12)  NOT NULL DEFAULT 'todo' COMMENT 'todo/doing/done',
  note                TEXT         COMMENT '备注/执行说明',
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_org (org_id, status, due_date)
) COMMENT='机构作业任务分派：把代账日常作业流固化进系统，形成工作流锁定';

-- =====================================================================
-- 微信订阅消息（征期提醒）：用户授权 + access_token 缓存
-- 约束：小程序订阅消息默认「一次性订阅」，授权后 7 天内可发 1 条。
-- =====================================================================
CREATE TABLE IF NOT EXISTS user_subscription (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  openid            VARCHAR(64)  NOT NULL,
  tmpl_id           VARCHAR(64)  NOT NULL COMMENT '订阅消息模板ID',
  granted_at        DATETIME     NOT NULL COMMENT '最近授权时间（7天发送窗口起点）',
  last_deadline_key VARCHAR(120) DEFAULT NULL COMMENT '上次已推送征期标识，防重复',
  status            VARCHAR(12)  NOT NULL DEFAULT 'active' COMMENT 'active/revoked',
  PRIMARY KEY (id),
  UNIQUE KEY uk_openid_tmpl (openid, tmpl_id)
) COMMENT='用户对征期提醒的订阅授权（一次性订阅，7天窗口）';

CREATE TABLE IF NOT EXISTS wx_token (
  id         TINYINT UNSIGNED NOT NULL,
  token      VARCHAR(255) DEFAULT NULL,
  expire_at  DATETIME     DEFAULT NULL,
  updated_at DATETIME     DEFAULT NULL,
  PRIMARY KEY (id)
) COMMENT='微信小程序 access_token 缓存';
