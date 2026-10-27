# PT-AutoBrush 运行与维护

入口配置为您的域名（如 **https://pt.example.com**），运行目录 `/opt/pt-brush`。Nginx 转发到 `127.0.0.1:12300`；`12300/12301` 管理端口禁止公网直连，`12345` BT Peer 端口保留。

## 登录与密码

单管理员账号 `admin`。随机初始密码在服务器 `/root/pt-admin-login.json`（仅 root 可读），交付时另存至本地登录信息文件。密码使用 PBKDF2-SHA256（600,000 次迭代）存储。会话 Cookie 为 Secure、HttpOnly、SameSite=Strict，有效期 8 小时，退出会立即撤销。

修改密码（不进入命令历史）：

```bash
sudo /opt/pt-brush/venv/bin/python /opt/pt-brush/security_admin.py
sudo systemctl restart pt-brush
```

重启后旧会话失效。初始密码文件不会自动更新，修改后更新自己的密码管理器。日常配置接口隐藏下载器密码、Cookie 和 RSS 链接；管理员显式导出的配置包含秘密，请妥善保管。

## 部署与升级

`deploy/apply-secure.sh` 用于迁移已有安装，要求已有虚拟环境、qBittorrent 与域名证书。Nginx 和 systemd 模板位于 `deploy/`。现有环境不应重新执行旧版首次安装脚本。

代码更新：

```powershell
.\pack.ps1
scp deploy_update.sh user@your-vps-ip:/tmp/pt-deploy-update.sh
ssh user@your-vps-ip "sudo bash /tmp/pt-deploy-update.sh"
```

包包含后端、认证模块、完整前端，不包含配置和密码。升级保留安全配置，先备份、验证语法，再重启，启动或鉴权检查失败回滚旧代码。服务模板改变时，先检查差异，再重新执行安全部署脚本。

## 运行检查

报表数据保存在 `data/metrics.db`，后台约每分钟采集，无需保持页面打开。每日流量按北京时间以 qBittorrent 历史累计计数的差值计算，首次采集只建立基线，不把历史总量记入当天；跨日或中断期间按时间分摊并标记估算。切换下载器/分类会重建计数基线。新增任务和新做种只统计受管分类，首次观察到的已有任务不算新增，采样间隔内已删除的短暂任务可能漏记。日报保留 30 个北京时间日期（含当天），分钟快照仅保留最近 24 小时；报表支持近 7/30 天和近 1/6/24 小时快照。每分钟独立执行清理，即使下载器采集失败也会清除过期数据。`GET /api/reports` 需要管理员登录。现有自动备份同时保存报表数据库与代码。

“PT 账号状态”沿用 BTSCHOOL 的 Base URL 与 Cookie，与刷流引擎启停独立。后台每 2 小时读取 `index.php` 和 `mybonus.php`；点击账号状态标签或刷新按钮立即触发刷新，重复点击并发时合并在途请求。`GET /api/accounts` 读取缓存与每日历史，`POST /api/accounts/refresh` 启动异步刷新，接口沿用管理员登录与 CSRF 校验。账号页及魔力页分别记录更新时间，失败保留上次成功数据并提示；未发现新人考核提示时显示“站点当前未展示新人考核”，不推断已通过。

实时缓存在 `data/account_status.json`，每日快照在 `data/account_snapshots.db`，均不保存 Cookie。北京时间每天首个完整成功采集保存一份快照，当天重复刷新不覆盖；失败日期不生成虚假快照。快照含账号汇总、新人考核缺口/截止时间和每小时魔力值，保留最近 30 个日期（含当天）。更新 Cookie 后先重新验证账号，同一账号延续历史，不同账号隔离历史。自动备份包含每日快照数据库及 `account_status.py`。升级后超期日报及分钟快照会被清理。

受管刷流种子队列中的“考察中”和“已达标”标记使用已配置 BTSCHOOL 站点的 Base URL（`https://pt.btschool.club`）和 Cookie，与刷流引擎的启停独立。首次启动后台读取 `myhr.php?hrtype=A`（考察中）和 `myhr.php?hrtype=B`（已达标）的分页记录（每类最多 50 页），之后每 4 小时更新；配置或 Cookie 更改后，在下一次后台检查（约 1 分钟内）重新获取。首次读取 HR 种子的详情页 Hash，与下载器 Hash 精确匹配并缓存；已知 Hash 后不重复请求详情页，仅缺少 Hash 时尝试名称匹配，忽略大小写及空格、点号等分隔符；下载器提供 Tracker 时同时检查其是否属于 BTSCHOOL。其他 HR 状态忽略。现有 `GET /api/status` 仅使用缓存给种子附加 `hr_status` 及兼容字段 `hr_pending`，无独立页面或 HR 轮询接口。缓存保存在 `data/hr_status.json`，不包含 Cookie。失败时保留旧结果，等待下一轮刷新；Cookie 失效后请更新站点配置。

```bash
sudo systemctl status pt-brush nginx qbittorrent-nox
sudo journalctl -u pt-brush -n 50 --no-pager
sudo nginx -t
sudo ss -lntp | grep 12300
curl -I https://pt.example.com/login
curl -i https://pt.example.com/api/config  # 未登录返回 401
sudo systemctl list-timers pt-brush-backup.timer pt-brush-cert.timer
```

服务以专用 `pt-brush` 账号运行，只能写数据和配置，不能写代码或访问 `/root`。本机绑定地址由 systemd 强制，恢复旧配置不会重新开放端口。下载器由本机 API 控制，磁盘容量使用与下载目录相同的根分区。

qBittorrent WebUI 同样只绑定本机。需要直接操作时，可用 `ssh -L 12301:127.0.0.1:12301 user@your-vps-ip` 建立隧道，再访问本机 `http://127.0.0.1:12301`。

Nginx 提供 HTTPS、TLS 1.2/1.3、HSTS、CSP、防嵌入、请求大小限制与登录限流。写接口要求同源和 CSRF Token。登录和管理操作写入 journald；独立 Nginx 访问日志不记录查询参数。

## 备份与证书

每日 03:30 后备份，保留最近 14 个归档，目录 `/var/backups/pt-brush`（仅 root 可读）。包含代码、SQLite 一致性快照、配置和认证文件，不含下载内容。升级备份在 `/opt/pt-brush/backups`。同机备份不能防磁盘损坏，可另行复制到其他存储。

手动备份：`sudo systemctl start pt-brush-backup.service`。

恢复：先解压至临时目录，停服务并保存当前版本，再恢复应用文件、`data/*.db` 和 `/etc/pt-brush/security.json`。恢复数据库后移除旧的 `*.db-wal`、`*.db-shm`。数据与 config.json 归属 `pt-brush:pt-brush`，目录 700、文件 600；认证文件归属 `root:pt-brush`，权限 640。检查 Nginx 后重启并验证登录。备份不可放进公开网页目录。

Let's Encrypt 证书在 `/etc/nginx/ssl/pt.example.com`。每日 04:00 后检查续期，成功后自动检查并重载 Nginx，不改变其他域名任务。保留 HTTP 80 的 `/.well-known/acme-challenge/` 路径。

```bash
sudo systemctl start pt-brush-cert.service
sudo journalctl -u pt-brush-cert.service --no-pager
sudo openssl x509 -in /etc/nginx/ssl/pt.example.com/fullchain.pem -noout -dates
```

## Fail2ban

控制台 `pt-login` 与 SSH `sshd` 分别按 IP 统计：10 分钟内登录失败 5 次，封禁 172800 秒（48 小时）。控制台读取 Nginx 的真实来源 IP，只匹配登录接口返回 401 的 POST 请求；普通未登录 API 访问不计入。控制台封禁作用于 TCP 80/443，SSH 封禁作用于 TCP 22。

封禁数据库保留 7 天，服务重启后恢复有效封禁。配置在 `/etc/fail2ban/jail.d/pt-security.local`，过滤器在 `/etc/fail2ban/filter.d/pt-login.conf`。项目 `deploy/install-fail2ban.sh` 可安装和恢复这套配置，日常备份包含自定义配置文件。

```bash
sudo fail2ban-client status pt-login
sudo fail2ban-client status sshd
sudo fail2ban-client set pt-login unbanip <IP>
sudo fail2ban-client set sshd unbanip <IP>
sudo journalctl -u fail2ban -n 30 --no-pager
```

测试：`python -m unittest test_security test_responsiveness -v`（运行依赖加 `httpx`），`node --check app.js`。安全测试使用临时数据库，不连接真实站点和下载器。
