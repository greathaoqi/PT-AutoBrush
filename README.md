# PT-AutoBrush 自动化刷流服务 (ARM64 / aarch64 专用版)

服务默认运行在 `/opt/pt-brush`，使用管理员登录与 Nginx HTTPS 反向代理，后端仅监听本机 127.0.0.1。更新、密码修改、备份和续期见 [OPERATIONS.md](OPERATIONS.md)。现有系统使用 `pack.ps1` 生成安全更新包，下方原始安装说明仅适用于依赖引导。

专为 **Oracle Linux 8 UEK (`5.4.17-2136.352.5.el8uek.aarch64`)** 及主流 Linux ARM64 VPS 量身打造的轻量级 PT 自动刷流、免费种子嗅探与空间自愈服务。

---

## 🌟 核心特性

- **PT 账号状态**：BTSCHOOL 账号汇总、新人考核及每小时魔力值，后台每 2 小时刷新，点击查看立即刷新。北京时间每天首个完整成功采集保存一份账号快照，保留 30 天。
- **数据报表与排序**：表头支持名称、大小、进度、速率、上传量及分享率排序。报表提供每日上传下载、新增任务、新做种及速率/队列快照，后台每分钟采集，SQLite 持久保存。日报保留 30 天，分钟快照仅保留 1 天，支持近 7/30 天日报及近 1/6/24 小时快照。

- **HR 状态标记**：在当前受管刷流种子队列中，为匹配 BTSCHOOL 记录的种子显示“考察中”或“已达标”徽标。读取 `myhr.php?hrtype=A` 和 `myhr.php?hrtype=B`，使用 Hash 精确匹配，沿用站点 Cookie，每 4 小时后台更新，缓存跨重启保存。
- **100% 免费种子安全保障**：仅抓取 `2xFree` 与 `Free` 种子；集成 **Free 剩余时长安全阈值**（如少于 60 分钟坚决不下），杜绝下载中途变回普通收费种而扣真实下载量。
- **针对 VPS 空间特性的防爆盘循环 (Auto-Cull)**：设置磁盘安全红线（默认 15GB），低于水位时自动熔断推新；智能淘汰“达标退役种”与“超时退役种”，物理清理磁盘空间。
- **防封号限速控制 (Anti-Ban)**：单种上传限速（默认 85 MB/s），严防触发国内各大 PT 站点关于“盒子单种百兆超速”的自动封禁脚本。
- **极轻量原生架构**：免 Docker，内存占用低于 45MB，基于 **Python 3 + FastAPI** 与社区官方 **`qbittorrent-nox` aarch64 静态编译二进制**。
- **现代化单文件 WebUI**：无需任何编译工具链，开箱即用，支持暗色护眼模式、即时嗅探 (Sniff)、在线 Dry-Run 规则模拟评估。

---

## 📂 文件结构

```text
├── install.sh      # 针对 el8uek.aarch64 的全自动一键安装与 Systemd 注册脚本
├── server.py       # 后端守护进程 (FastAPI + NexusPHP 嗅探 + qB 控制器)
├── index.html      # 现代化单文件 WebUI 前端 (支持联机与离线双模式)
├── config.json     # 核心配置文件 (站点、选种规则、删种策略、下载器设置)
├── PRD.md          # 详细产品需求文档
└── README.md       # 本项目说明
```

---

## 🚀 部署指引 (在您的 VPS 上运行)

### 步骤 1：将代码上传至 VPS
您可以将当前目录打包上传至 VPS（例如 `/root/pt`），或在 VPS 上直接拉取：
```bash
# 进入目录
cd /root/pt
chmod +x install.sh
```

### 步骤 2：执行全自动部署脚本
```bash
sudo bash install.sh
```
脚本将全自动完成：
1. 验证 `aarch64` 硬件架构；
2. 自动拉取最新的 `aarch64` 静态版 `qbittorrent-nox` 二进制；
3. 创建并配置 `qbittorrent-nox.service` systemd 开机自启服务；
4. 部署 Python 独立虚拟环境并安装核心轻量依赖；
5. 创建并配置 `pt-brush.service` systemd 守护服务；
6. 自动检测并放行防火墙端口 (`12300`, `12301`, `12345`)。

---

## 🌐 端口规划 (严格适配 12300 - 12400 范围)

| 端口号 | 协议 | 服务用途 | 说明 |
| :---: | :---: | :--- | :--- |
| **`12300`** | TCP | **PT-AutoBrush WebUI** | 刷流控制台主界面 (`http://<VPS-IP>:12300`) |
| **`12301`** | TCP | **qBittorrent WebUI** | 下载器管理控制台 (`http://<VPS-IP>:12301`) |
| **`12345`** | TCP/UDP | **BT Peer 传入监听端口** | **做种抢上传核心**，供外部 Peer 连入抢流量 |
| `12346-12400`| - | 预留备用区间 | 预留给后续扩展站点或独立下载器实例 |

---

## 🖥️ 访问与配置

部署完成后，在浏览器中访问：
- **刷流控制台 WebUI**：`http://<您的-VPS-IP>:12300`
- **qBittorrent WebUI**：`http://<您的-VPS-IP>:12301` (默认账号: `admin`，本地 127.0.0.1 免密通信)
- **BT Peer 监听端口**：`12345` (已自动预设进 qB，公网可连接)

### 添加 NexusPHP 站点配置步骤：
1. 打开 WebUI -> 切换到 **“PT 站点与嗅探器”** 选项卡；
2. 点击 **“+ 添加新站点”**：
   - **站点名称**：如 `KeepFRDS`、`Hares`、`PTer`；
   - **Base URL**：如 `https://pt.keepfrds.com`；
   - **RSS 完整链接**：从站点“下载/RSS”页面生成的订阅链接（建议选仅输出 `dl` 下载模式的链接）；
   - **Cookie**：填入浏览器登录后的完整 Cookie（系统将自动用于 `details.php?id=XXX` 的二次免费状态与 H&R 核验）；
3. 保存后点击 **“测试抓取 (Dry Run)”**，即可实时查看模拟判定结果；
4. 确认无误后点击启用，系统即刻进入全自动无人值守抢传状态！

---

## 🛠️ Systemd 服务运维常用命令

```bash
# 查看 PT 刷流服务状态
systemctl status pt-brush

# 实时滚动查看运行日志
journalctl -u pt-brush -f

# 重启 / 停止刷流服务
systemctl restart pt-brush
systemctl stop pt-brush

# 查看 qBittorrent 状态
systemctl status qbittorrent-nox
```
