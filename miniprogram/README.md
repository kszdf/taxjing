# 慧根堂·数字财税助手 — 微信小程序（原生框架）

本目录是一套**原生微信小程序**源码，封装了财税AI智库的全部能力：11 模块首页、AI 深度问答（RAG+大模型）、政策库、我的积分、企微客服引导、转人工快答（30积分工单）。后端为同仓库的 `server.py`（零依赖 Python）。

## 目录结构
```
miniprogram/
  project.config.json     # appid 默认 touristappid（测试号）
  sitemap.json
  app.js / app.json / app.wxss
  config.js               # API_BASE 配置（⚠️ 上线前必须改成你的 HTTPS 域名）
  utils/request.js        # 统一请求封装（自动拼 BASE + JSON + 失败 toast）
  utils/store.js          # 本地 openid 读写
  pages/
    index/        首页（11 模块宫格 + 积分卡）
    ask/          AI 问答
    policy/       政策库列表
    policyDetail/ 政策详情（失效条款标红）
    points/       我的积分 + 获取积分引导
    kefu/         联系客服（企微法币充值流程说明）
    human/        转人工快答（提交 + 我的工单）
    profile/      个人中心
```

## 本地联调（开发期）
1. 启动后端：在仓库根目录运行 `python server.py`（建议带 DeepSeek key 环境变量 `LLM_API_KEY` 等，详见根目录方案文档）。后端监听 `http://localhost:8080`。
2. 打开微信开发者工具 → 导入项目 → 选择本 `miniprogram` 目录。
3. AppID：开发阶段可选「测试号」（对应 `project.config.json` 中的 `touristappid`）；正式发布需填你自己的小程序 AppID 并替换 `project.config.json` 的 appid。
4. 开发时关闭域名校验：右上角「详情」→「本地设置」→ 勾选 **不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书**。
5. 修改 `config.js` 的 `API_BASE`：
   - 开发者工具「模拟器」访问本机后端：`http://localhost:8080`
   - 真机预览/调试访问本机后端：需用电脑**局域网 IP**，如 `http://192.168.x.x:8080`（localhost 在真机上指向手机自身）。

## 生产发布（上线前必做）
1. 后端必须部署到 **HTTPS** 公网域名（微信要求 request 合法域名必须为 HTTPS）。
2. 微信公众平台 → 开发 → 开发设置 → 服务器域名 → **request 合法域名** 加入你的后端域名（不含 http/https 前缀，如 `https://api.example.com`）。
3. 把 `config.js` 的 `API_BASE` 改为 `https://你的域名`。
4. 替换 `project.config.json` 的 `appid` 为正式 AppID，上传代码并提交审核发布。
5. 真实企微客服：配置环境变量 `WX_CORPID`（企业微信 corpid），并在小程序内调用 `wx.openCustomerServiceChat` 等官方客服能力（本 demo 的 kefu 页为引导页，未直连企微会话）。
6. 运营后台 `admin.html` 的 `ADMIN_TOKEN` 必须改为强随机值（环境变量注入，勿写死在代码）。

## 设计铁律（与方案一致）
- **不碰法币**：小程序内购积分走企微私域，后端仅做「企微转账 → 运营后台确认 → 积分到账」，平台不经资金池（规避二清/支付牌照）。
- **零错误三铁律**：RAG 只检索现行有效（active）条款；答案强制引用真实文号；免责声明由系统层注入，不依赖模型自觉。
- **可升级不推倒**：后端建表全 `IF NOT EXISTS`，用户资产独立持久。

## 已知限制 / 待补
- RAG 现为标准库关键词检索，可平滑升级为向量检索（接口不变）。
- 联网抓取到的政策多为 `pending_review` 且无正文，RAG 暂仅覆盖 seed 三份 + 未来复核生效项；需把复核后条款写进 `policy_clause` 并载入裁决引擎。
- 引用校验护栏已在 `/api/ask` 生效（检出答案中未在检索范围内的文号并追加警示），建议后续加「答案文号 ⊆ policy_refs」硬校验。
- 企微客服为引导页，未直连真实会话（需 corpid + 微信官方客服接口）。
