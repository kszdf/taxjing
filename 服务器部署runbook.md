# 税镜 · 部署到 124.222.33.233（Runbook）

> 目标域名：`tax.hgtcs.com` → 后端（本机 `127.0.0.1:8090`）
> 说明：`api.hgtcs.com` 已被「税智云·风险检测」占用**不可动**；故本产品改用 `tax.hgtcs.com`（现已空闲）。
> 原则：**只加不改**——只新增文件/端口，不动任何现有站点；`nginx reload` 不中断。

---

## 一、服务器现状（2026-09-11 只读体检，root@VM-0-8-ubuntu）

- **系统**：Ubuntu 22.04.5 LTS；**nginx 1.18** + **Certbot**；磁盘 24G 空闲；内存约 748M 可用。
- **现有 nginx 站点**（`/etc/nginx/sites-available/`）：
  | 站点 | 指向 |
  |---|---|
  | `report.hgtcs.com` | 简税盾，`root /opt/jianshuidun/...`，`/api/` → `127.0.0.1:8000` |
  | `sjk.hgtcs.com` | 静态，`root /opt/taxcheck` |
  | `zmgen.cn` / `www.zmgen.cn` | HGT 平台，`proxy_pass 127.0.0.1:8080`（frps） |
- **证书**：`report.hgtcs.com` 证书含 SAN `report.hgtcs.com + sjk.hgtcs.com`；另有 `zmgen.cn`。**无通配符证书**。
- **端口**：80/443（nginx）、**8080/7000/7500/8500/8385（frps，用户 ubuntu）**、8000（简税盾）、22。**`8090` 空闲 ✓**。
- **结论**：`hgtcs.com` 子域已在用 + Certbot 齐备 → 加 `tax.hgtcs.com` 是标准操作，**风险极低**。

---

## 二、部署步骤（**只加不改**）

### 步骤 0（**你做**）：加一条 DNS 记录
给 `tax.hgtcs.com` **新增**一条 A 记录 → **`124.222.33.233`**。
> ℹ️ `tax.hgtcs.com` 目前**无解析（空闲）**，新增即可，**不影响**任何现有子域（report / sjk / zmgen / api）。

### 步骤 1：上传代码（**我做**）
```bash
ssh root@124.222.33.233
mkdir -p /opt/suijing && cd /opt/suijing
# 上传：server.py、scripts/、db/、org.html、admin.html、tax-ai-prototype.html、（可选）miniprogram/
```

### 步骤 2：起后端（独立端口 8090，**我做**）
`systemd` 服务 `/etc/systemd/system/suijing.service`：
```ini
[Unit]
Description=Suijing Tax-AI Backend
After=network.target
[Service]
WorkingDirectory=/opt/suijing
Environment=PORT=8090
Environment=LLM_BASE_URL=https://api.deepseek.com/v1
Environment=LLM_MODEL=deepseek-chat
Environment=ADMIN_TOKEN=<强随机>
Environment=LLM_API_KEY=<DeepSeek Key>      # 仅 root 可读，勿入代码/git
ExecStart=/usr/bin/python3 /opt/suijing/server.py
Restart=always
[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload && systemctl enable --now suijing
curl -s http://127.0.0.1:8090/api/llm/status   # 自测
```

### 步骤 3：加 nginx 站点（**新文件**，**我做**）
`/etc/nginx/sites-available/tax.hgtcs.com.conf`：
```nginx
server {
    listen 80;
    server_name tax.hgtcs.com;
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
```bash
ln -s /etc/nginx/sites-available/tax.hgtcs.com.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx      # 平滑重载，不中断现有服务
```

### 步骤 4：HTTPS 证书（**我做**）
```bash
certbot --nginx -d tax.hgtcs.com -n --agree-tos -m <邮箱> --redirect
```
（Certbot 会自动改这个 conf、加 443 与证书、并配置自动续期。）

### 步骤 5（**你做**）：微信公众平台 → request 合法域名
加入 `https://tax.hgtcs.com`。

### 步骤 6（**我改代码**）：小程序 `config.js` 的 `API_BASE` → `https://tax.hgtcs.com`

---

## 三、红线与回滚

- **只新增**：新目录 `/opt/suijing`、新端口 `8090`、新 conf 文件；**不改**任何现有 conf，不动 frps/8000/8080。
- **平滑**：`systemctl reload nginx`（不 restart）；改 conf 前先 `cp -r /etc/nginx /etc/nginx.bak.$(date +%F)`。
- **回滚**：删掉 `sites-enabled/tax.hgtcs.com.conf` → `nginx -t && reload`；`systemctl disable --now suijing`。
- **密钥**：`LLM_API_KEY`/`ADMIN_TOKEN` 放 systemd `Environment`（root-only），**不写进代码、不进 git**。

---

## 四、待办
- [ ] 用户改 DNS：`tax.hgtcs.com` → `124.222.33.233`
- [ ] 用户授权后：上传代码 + 起服务 + 加 nginx + certbot
- [ ] 小程序 `config.js` 指向 `https://tax.hgtcs.com`
- [ ] 微信后台配 request 合法域名
