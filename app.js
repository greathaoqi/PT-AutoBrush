
    const nativeFetch = window.fetch.bind(window);
    let csrfToken = '';
    window.fetch = async function(resource, options = {}) {
      const headers = new Headers(options.headers || {});
      const method = (options.method || 'GET').toUpperCase();
      if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRF-Token', csrfToken);
      const response = await nativeFetch(resource, {...options, headers});
      if (response.status === 401 && location.protocol !== 'file:') location.replace('/login');
      return response;
    };
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    }
    async function logout() {
      const response = await fetch('/api/auth/logout', {method: 'POST'});
      if (response.ok) location.replace('/login');
    }
    function switchTab(tabId, el) {
      document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-pane').forEach(function(p) { p.classList.remove('active'); });
      var targetBtn = el || (window.event ? window.event.currentTarget : null) || document.querySelector('.tab-btn[data-tab="' + tabId + '"]');
      if (targetBtn) targetBtn.classList.add('active');
      var targetPane = document.getElementById('tab-' + tabId);
      if (targetPane) targetPane.classList.add('active');
    }
    window.switchTab = switchTab;
  

    let appConfig = null;
    let isConnected = false;
    let currentTorrents = [];
    let currentFilter = 'all';


    function showToast(msg, icon = '✅') {
      const toast = document.getElementById('toast');
      document.getElementById('toastMsg').textContent = msg;
      document.getElementById('toastIcon').textContent = icon;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2500);
    }

    let statusPolling = false;
    let logsPolling = false;
    let hasStatusData = false;
    let torrentSort = {key: null, direction: 1};
    let pagePeakUpload = 0;
    document.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
      const key = button.dataset.sort;
      torrentSort = {key, direction: torrentSort.key === key ? -torrentSort.direction : key === 'name' ? 1 : -1};
      document.querySelectorAll('[data-sort]').forEach(item => {
        const active = item.dataset.sort === key;
        item.classList.toggle('active', active);
        item.querySelector('span').textContent = active ? (torrentSort.direction === 1 ? '↑' : '↓') : '↕';
        item.closest('th').setAttribute('aria-sort', 'none');
      });
      button.closest('th').setAttribute('aria-sort', torrentSort.direction === 1 ? 'ascending' : 'descending');
      renderTorrents(currentTorrents);
    }));

    function renderLimitLabels(limits = {}) {
      const disk = limits.min_disk_free_gb ?? appConfig?.cull?.min_disk_free_gb;
      const speed = limits.single_upload_limit_mb ?? appConfig?.downloader?.single_upload_limit_mb;
      document.getElementById('diskMinThreshold').textContent = disk !== undefined ? `安全线 ${disk} GB` : '安全线未获取';
      document.getElementById('antiBanLimitLabel').textContent = speed !== undefined ? `单种限速 ${speed} MB/s` : '单种限速未获取';
    }
    let reportsBusy = false;
    let reportData = null;
    const GB = 1024 ** 3, MB = 1024 ** 2;
    const reportTime = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
    function formatTorrentTime(sec) {
      if (!sec || sec <= 0 || sec > 3000000000) return '—';
      const d = new Date(sec * 1000);
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const h = String(d.getHours()).padStart(2, '0');
      const min = String(d.getMinutes()).padStart(2, '0');
      return `${m}-${day} ${h}:${min}`;
    }
    function formatFullTime(sec) {
      if (!sec || sec <= 0 || sec > 3000000000) return '—';
      return new Date(sec * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
    }

    let accountsBusy = false;
    let checkinStatuses = {};

    async function fetchCheckinStatus() {
      try {
        const res = await fetch('/api/sites/checkin_status');
        if (res.ok) {
          const data = await res.json();
          checkinStatuses = data.status || {};
          if (appConfig?.sites) renderSites(appConfig.sites);
        }
      } catch (e) {
        console.debug('获取签到状态异常:', e);
      }
    }

    function renderAccounts(data) {
      const container = document.getElementById('accountCards');
      if (!data.sites?.length) {
        container.innerHTML = '<div class="card-desc">请先在“PT 站点与嗅探器”中添加 BTSCHOOL，填写 Base URL 和登录 Cookie。</div>';
        return;
      }
      container.innerHTML = data.sites.map(site => {
        const s = site.stats || {}, a = s.assessment;
        const ck = site.checkin || checkinStatuses[site.site_id] || {};
        const fields = [['用户名', s.username], ['魔力值', s.bonus], ['每小时魔力值', site.bonus_per_hour === undefined ? null : `${site.bonus_per_hour} 个/小时`], ['邀请', s.invites], ['分享率', s.ratio], ['H&R（站点原值）', s.hr], ['上传量', s.uploaded], ['下载量', s.downloaded], ['当前做种', s.seeding], ['当前下载', s.leeching], ['可连接', s.connectable], ['连接数', s.slots]];

        let checkinSection = '';
        const bonusTag = ck.bonus_earned ? `<span class="badge" style="background:rgba(234,179,8,0.2);color:#facc15;font-weight:600;margin-left:4px;">+${escapeHtml(ck.bonus_earned)} 魔力</span>` : '';
        if (ck.date) {
          const badgeClass = ck.status === 'success' || ck.status === 'already_checked' ? 'badge-2xfree' : 'badge-danger';
          const statusText = ck.status === 'success' ? '今日已签' : ck.status === 'already_checked' ? '今日已签' : '签到异常';
          checkinSection = `<div style="border-top:1px solid var(--border);padding-top:.7rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.4rem;">
            <div>
              <strong>今日签到：</strong>
              <span class="badge ${badgeClass}">${statusText}</span>
              ${bonusTag}
              <span style="font-size:0.8rem;margin-left:4px;color:var(--text-dim);">${escapeHtml(ck.message || '')}</span>
            </div>
            <button class="btn btn-outline btn-xs" data-action="site-checkin" data-site-id="${escapeHtml(site.site_id)}">重新签到</button>
          </div>`;
        } else {
          checkinSection = `<div style="border-top:1px solid var(--border);padding-top:.7rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.4rem;">
            <div>
              <strong>今日签到：</strong>
              <span style="color:var(--text-dim);font-size:0.8rem;">尚未签到或等待后台自动触发</span>
            </div>
            <button class="btn btn-primary btn-xs" data-action="site-checkin" data-site-id="${escapeHtml(site.site_id)}">立即签到</button>
          </div>`;
        }

        let historySection = '';
        if (ck.history && ck.history.length > 0) {
          const rows = [...ck.history].reverse().slice(0, 30).map(h => {
            const bStr = h.bonus_earned ? `<span style="color:#facc15;font-weight:600;">+${escapeHtml(h.bonus_earned)}</span>` : '<span style="color:var(--text-dim);">—</span>';
            const badge = h.status === 'success' || h.status === 'already_checked' ? '<span class="badge badge-2xfree">成功</span>' : '<span class="badge badge-danger">失败</span>';
            return `<tr><td>${escapeHtml(h.date)}</td><td>${reportTime(h.time)}</td><td>${badge}</td><td>${bStr}</td><td>${escapeHtml(h.message || '')}</td></tr>`;
          }).join('');
          historySection = `
          <details style="margin-top:0.6rem;"><summary style="font-size:0.8rem;cursor:pointer;color:var(--text-muted);">签到魔力历史记录 · 最近 ${Math.min(ck.history.length, 30)} 次</summary>
            <div style="overflow-x:auto;margin-top:0.4rem;">
              <table style="font-size:0.75rem;white-space:nowrap;width:100%;">
                <thead><tr><th>日期</th><th>签到时间</th><th>状态</th><th>获得魔力</th><th>说明</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </details>`;
        }

        return `<div class="site-card">
          <div class="site-header"><div class="site-title">${escapeHtml(site.site_name)}</div><span class="badge badge-site">${site.stale ? '待更新' : '已更新'}</span></div>
          <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.6rem;font-size:.85rem">${fields.map(([label, value]) => `<div>${label}<br><strong>${escapeHtml(value ?? '—')}</strong></div>`).join('')}</div>
          <div class="card-desc">账号更新时间：${site.updated_at ? reportTime(site.updated_at) : '尚未获取'}${site.stale && site.updated_at ? ' · 历史数据' : ''}</div>
          <div class="card-desc">魔力速率更新时间：${site.bonus_updated_at ? reportTime(site.bonus_updated_at) : '尚未获取'}${site.bonus_stale && site.bonus_updated_at ? ' · 历史数据' : ''}</div>
          ${site.error ? `<div style="color:#fbbf24">账号：${escapeHtml(site.error)}</div>` : ''}
          ${site.bonus_error ? `<div style="color:#fbbf24">魔力速率：${escapeHtml(site.bonus_error)}</div>` : ''}
          ${checkinSection}
          ${historySection}
          <div style="border-top:1px solid var(--border);padding-top:.7rem">
            <strong>${escapeHtml(a?.summary || '新人考核状态尚未获取')}</strong>
            ${a?.status === 'active' ? `<div class="card-desc">剩余时间：${escapeHtml(a.remaining ?? '未展示')}<br>截止时间：${escapeHtml(a.deadline ?? '未展示')}</div>${a.items.map(item => `<div>${escapeHtml(item.label)}：${escapeHtml(item.value)}</div>`).join('')}` : ''}
          </div>
          <div><a href="https://pt.btschool.club/index.php" target="_blank" rel="noopener noreferrer">站点账号</a> · <a href="https://pt.btschool.club/mybonus.php" target="_blank" rel="noopener noreferrer">魔力明细</a></div>
          <details><summary>每日快照（保留 30 天）· ${site.daily_snapshots?.length || 0} 条</summary>
            <div style="overflow-x:auto"><table style="font-size:.75rem;white-space:nowrap"><thead><tr><th>日期</th><th>采集时间</th><th>上传量</th><th>下载量</th><th>分享率</th><th>魔力值</th><th>魔力/小时</th><th>做种/下载</th><th>H&R</th><th>邀请</th><th>可连接/连接数</th><th>新人考核</th></tr></thead><tbody>${(site.daily_snapshots || []).map(row => {const r = row.stats || {}, a = r.assessment; return `<tr>${[row.day, reportTime(row.captured_at), r.uploaded, r.downloaded, r.ratio, r.bonus, row.bonus_per_hour, `${r.seeding ?? '—'}/${r.leeching ?? '—'}`, r.hr, r.invites, `${r.connectable ?? '—'}/${r.slots ?? '—'}`, [a?.summary, a?.remaining, a?.deadline, ...(a?.items || []).map(item => `${item.label}：${item.value}`)].filter(Boolean).join(' · ')].map(value => `<td>${escapeHtml(value ?? '—')}</td>`).join('')}</tr>`;}).join('')}</tbody></table></div>
            <div class="card-desc">北京时间每天首个完整成功采集保存一份，重复刷新不覆盖；采集失败的日期不补造数据。</div>
          </details>
        </div>`;
      }).join('');
    }
    async function loadAccounts(force = false) {
      if (accountsBusy) return;
      accountsBusy = true;
      const button = document.getElementById('accountsRefresh'), notice = document.getElementById('accountsNotice');
      button.disabled = true;
      try {
        if (!force) {
          notice.textContent = '正在读取站点账号缓存数据…';
          const data = await fetchPollingJSON('/api/accounts');
          renderAccounts(data);
          const firstSite = data.sites?.[0];
          const updateTime = firstSite?.updated_at ? reportTime(firstSite.updated_at) : '暂未同步';
          notice.textContent = `数据保持中 · 上次更新: ${updateTime} · 后台每 2 小时自动更新`;
        } else {
          notice.textContent = '正在强制获取站点最新数据…';
          renderAccounts(await fetchPollingJSON('/api/accounts'));
          const response = await fetch('/api/accounts/refresh', {method: 'POST'});
          if (!response.ok) throw new Error('refresh failed');
          let data = await response.json();
          renderAccounts(data);
          const started = Date.now();
          while (data.refreshing && Date.now() - started < 120000) {
            await new Promise(resolve => setTimeout(resolve, 1500));
            data = await fetchPollingJSON('/api/accounts');
            renderAccounts(data);
          }
          const firstSite = data.sites?.[0];
          const updateTime = firstSite?.updated_at ? reportTime(firstSite.updated_at) : '刚刚';
          notice.textContent = data.refreshing ? '站点仍在更新，稍后点击刷新查看结果。' : data.sites.some(s => s.error || s.bonus_error) ? '部分数据更新失败，已保留上次成功结果。' : `已完成强制刷新 (${updateTime}) · 每 2 小时后台自动更新`;
          showToast('账号数据已强制同步最新', '✅');
        }
      } catch (error) {
        notice.textContent = '账号状态暂时无法读取，请稍后刷新。已有数据保留。';
      } finally {
        accountsBusy = false;
        button.disabled = false;
      }
    }
    document.getElementById('accountsTabButton').addEventListener('click', function() {
      switchTab('accounts', this);
      loadAccounts(false);
    });
    document.getElementById('accountsRefresh').addEventListener('click', function() {
      loadAccounts(true);
    });

    function drawReportChart(id, rows, series, xLabel, details, gapSeconds = null) {
      const container = document.getElementById(id);
      if (!rows.some(row => series.some(line => row[line.key] !== null && row[line.key] !== undefined))) {
        container.innerHTML = '<div class="chart-empty">暂无采集数据，采集后自动显示</div>';
        return;
      }
      const w = Math.max(300, container.clientWidth), h = 250, left = 52, right = 16, top = 15, bottom = 36;
      const values = rows.flatMap(row => series.map(line => Number(row[line.key]) / (line.divisor || 1))).filter(Number.isFinite);
      const max = Math.max(1, ...values) * 1.15;
      const timed = rows.length > 1 && rows[0].ts !== undefined && rows.at(-1).ts > rows[0].ts;
      const coordinateX = index => left + (rows.length === 1 ? .5 : timed ? (rows[index].ts-rows[0].ts)/(rows.at(-1).ts-rows[0].ts) : index / (rows.length - 1)) * (w-left-right);
      const coordinateY = value => h-bottom - value / max * (h-top-bottom);
      let grid = '';
      for (let i = 0; i <= 4; i++) {
        const value = max*i/4, y = coordinateY(value);
        grid += `<line x1="${left}" x2="${w-right}" y1="${y}" y2="${y}" stroke="#334155" stroke-opacity=".55"/><text x="${left-10}" y="${y+4}" text-anchor="end" fill="#94a3b8" font-size="11">${value < 10 ? value.toFixed(1) : Math.round(value)}</text>`;
      }
      const ticks = new Set(w < 500 ? [0, rows.length-1] : [0, Math.floor((rows.length-1)/3), Math.floor(2*(rows.length-1)/3), rows.length-1]);
      ticks.forEach(index => { grid += `<text x="${coordinateX(index)}" y="${h-10}" text-anchor="${index === 0 ? 'start' : index === rows.length-1 ? 'end' : 'middle'}" fill="#94a3b8" font-size="11">${escapeHtml(xLabel(rows[index]))}</text>`; });
      const paths = series.map(line => {
        let path = '', connected = false, circles = '';
        rows.forEach((row, index) => {
          if (row[line.key] === null || row[line.key] === undefined) { connected = false; return; }
          if (gapSeconds && index && row.ts-rows[index-1].ts > gapSeconds) connected = false;
          const x = coordinateX(index), y = coordinateY(Number(row[line.key]) / (line.divisor || 1));
          path += `${connected ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
          connected = true;
          if (rows.length <= 90) circles += `<circle cx="${x}" cy="${y}" r="3" fill="${line.color}" stroke="#1e293b" stroke-width="1.5"/>`;
        });
        return `<path d="${path}" fill="none" stroke="${line.color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${circles}`;
      }).join('');
      container.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${escapeHtml(series.map(s => s.label).join('、'))}趋势图">${grid}${paths}<line class="chart-cursor" x1="0" x2="0" y1="${top}" y2="${h-bottom}" stroke="#94a3b8" stroke-dasharray="4 4" opacity="0"/></svg><div class="chart-tooltip" hidden></div><div class="chart-legend">${series.map(line => `<span><i class="chart-dot" style="background:${line.color}"></i>${escapeHtml(line.label)}</span>`).join('')}</div>`;
      const svg = container.querySelector('svg'), cursor = container.querySelector('.chart-cursor'), tooltip = container.querySelector('.chart-tooltip');
      svg.addEventListener('pointermove', event => {
        const box = svg.getBoundingClientRect(), x = (event.clientX-box.left)/box.width*w;
        const index = rows.reduce((closest, row, i) => Math.abs(coordinateX(i)-x) < Math.abs(coordinateX(closest)-x) ? i : closest, 0);
        cursor.setAttribute('x1', coordinateX(index)); cursor.setAttribute('x2', coordinateX(index)); cursor.setAttribute('opacity', '1');
        tooltip.textContent = details(rows[index]); tooltip.hidden = false;
      });
      svg.addEventListener('pointerleave', () => {cursor.setAttribute('opacity', '0'); tooltip.hidden = true;});
    }

    function renderReports(data) {
      const daily = data.daily || [], samples = data.samples || [];
      const sum = key => daily.reduce((value, row) => value + (Number(row[key]) || 0), 0);
      const hasData = Boolean(data.started_at);
      const kpis = [
        ['期间上传量', (sum('uploaded')/GB).toFixed(2), 'GB', '#34d399', '下载器累计流量增量'],
        ['期间下载量', (sum('downloaded')/GB).toFixed(2), 'GB', '#60a5fa', '按北京时间逐日统计'],
        ['期间新做种', sum('seeded'), '个', '#c084fc', `新增任务 ${sum('added')} 个`],
        ['快照上传峰值', ((data.peak_up || 0)/MB).toFixed(2), 'MB/s', '#fbbf24', `所选速率窗口 · 均值 ${((data.average_up || 0)/MB).toFixed(2)} MB/s`]
      ];
      document.getElementById('reportKpis').innerHTML = kpis.map(([label, value, unit, color, hint]) => `<div class="report-kpi"><div class="report-kpi-label">${label}</div><div class="report-kpi-value" style="color:${color}">${hasData ? value : '—'}<span class="report-kpi-unit">${unit}</span></div><div class="card-desc">${hint}</div></div>`).join('');
      document.getElementById('reportNotice').textContent = data.error || (hasData ? `每分钟采集 · 开始于 ${reportTime(data.started_at)} · 最近采集 ${reportTime(data.updated_at)}${data.stale ? ' · 数据暂未更新，当前展示历史记录' : ''}` : '采集尚未开始，首次采集后显示报表。历史数据从升级后开始积累。');
      drawReportChart('dailyTrafficChart', daily, [{key:'uploaded',label:'上传',color:'#34d399',divisor:GB},{key:'downloaded',label:'下载',color:'#60a5fa',divisor:GB}], r => r.day.slice(5), r => `${r.day}${r.estimated ? ' · 含估算' : ''}\n上传 ${r.uploaded === null ? '未采集' : (r.uploaded/GB).toFixed(2)+' GB'} · 下载 ${r.downloaded === null ? '未采集' : (r.downloaded/GB).toFixed(2)+' GB'}`);
      drawReportChart('seedingChart', daily, [{key:'added',label:'新增任务',color:'#60a5fa'},{key:'seeded',label:'新做种',color:'#c084fc'}], r => r.day.slice(5), r => `${r.day}\n新增任务 ${r.added ?? '未采集'} · 新做种 ${r.seeded ?? '未采集'}`);
      const timeLabel = r => new Date(r.ts*1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
      drawReportChart('speedChart', samples, [{key:'up',label:'上传快照 / 窗口均值',color:'#34d399',divisor:MB},{key:'peak_up',label:'窗口峰值',color:'#fbbf24',divisor:MB},{key:'down',label:'下载快照 / 窗口均值',color:'#60a5fa',divisor:MB}], timeLabel, r => `${reportTime(r.ts)}\n上传 ${(r.up/MB).toFixed(2)} MB/s · 上传峰值 ${(r.peak_up/MB).toFixed(2)} MB/s\n下载 ${(r.down/MB).toFixed(2)} MB/s`, Math.max(180, Number(data.hours)*3600/480*2));
      drawReportChart('queueChart', samples, [{key:'seeding',label:'已完成',color:'#c084fc'},{key:'downloading',label:'下载未完成',color:'#60a5fa'}], timeLabel, r => `${reportTime(r.ts)}\n已完成 ${r.seeding} 个 · 下载未完成 ${r.downloading} 个`, Math.max(180, Number(data.hours)*3600/480*2));
      document.getElementById('reportDailyBody').innerHTML = [...daily].reverse().map(row => `<tr><td>${escapeHtml(row.day)}${row.estimated ? ' *' : ''}</td><td style="color:#34d399">${row.uploaded === null ? '—' : (row.uploaded/GB).toFixed(2)+' GB'}</td><td style="color:#60a5fa">${row.downloaded === null ? '—' : (row.downloaded/GB).toFixed(2)+' GB'}</td><td>${row.added ?? '—'}</td><td>${row.seeded ?? '—'}</td></tr>`).join('');
    }

    async function fetchReports() {
      if (reportsBusy) return;
      reportsBusy = true;
      try {
        const days = document.getElementById('reportDays').value, hours = document.getElementById('reportHours').value;
        reportData = await fetchPollingJSON(`/api/reports?days=${days}&hours=${hours}`);
        renderReports(reportData);
      } catch (error) {
        document.getElementById('reportNotice').textContent = '报表暂时无法读取，请稍后刷新。' + (reportData ? '以下保留上次结果。' : '');
      } finally { reportsBusy = false; }
    }
    let reportResizeTimer;
    window.addEventListener('resize', () => {clearTimeout(reportResizeTimer); reportResizeTimer = setTimeout(() => {if (reportData && document.getElementById('tab-reports').classList.contains('active')) renderReports(reportData);}, 150);});
    document.getElementById('reportsTabButton').addEventListener('click', function() {switchTab('reports', this); fetchReports();});
    document.getElementById('reportDays').addEventListener('change', fetchReports);
    document.getElementById('reportHours').addEventListener('change', fetchReports);
    document.getElementById('reportRefresh').addEventListener('click', fetchReports);
    setInterval(() => {if (document.getElementById('tab-reports').classList.contains('active')) fetchReports();}, 60000);

    async function fetchPollingJSON(url) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
        return await res.json();
      } finally {
        clearTimeout(timer);
      }
    }

    // 首次加载保留刷新占位，连接失败后继续重试。
    async function init() {
      try {
        const sessionResponse = await fetch('/api/auth/session');
        if (sessionResponse.ok) csrfToken = (await sessionResponse.json()).csrf;
        const [statusResult, configResult, logsResult] = await Promise.allSettled([
          fetchPollingJSON('/api/status'),
          fetchPollingJSON('/api/config'),
          fetchPollingJSON('/api/logs')
        ]);
        if (statusResult.status === 'fulfilled') {
          isConnected = true;
          document.getElementById('modeBadge').textContent = '后端联机模式';
          document.getElementById('modeBadge').style.background = 'rgba(16, 185, 129, 0.15)';
          renderStatusData(statusResult.value);
          if (configResult.status === 'fulfilled') {
            appConfig = configResult.value;
            applyConfigToUI(appConfig);
            fetchCheckinStatus();
          }
          if (logsResult.status === 'fulfilled') {
            const logData = logsResult.value;
            if (logData && logData.logs) renderLogs(logData.logs);
          }
          setInterval(pollStatus, 4000);
          setInterval(fetchLogs, 5000);
          setInterval(fetchCheckinStatus, 30000);
          return;
        }
      } catch (e) {
        console.error(e);
      }

      isConnected = false;
      setInterval(pollStatus, 4000);
      setInterval(fetchLogs, 5000);
    }

    async function loadConfigFromBackend() {
      try {
        const res = await fetch('/api/config');
        if (res.ok) {
          appConfig = await res.json();
          applyConfigToUI(appConfig);
        }
      } catch (e) {
        console.error(e);
      }
    }

    function renderStatusData(data) {
      if (!data) return;
      if (data.status_ready === false) return;
      hasStatusData = true;
      renderLimitLabels(data.limits);
      // Engine running
      const pill = document.getElementById('globalStatusPill');
      const text = document.getElementById('globalStatusText');
      const btn = document.getElementById('btnTogglePause');
      btn.disabled = false;
      if (data.engine_running) {
        pill.style.background = 'var(--success-bg)';
        pill.style.color = 'var(--success)';
        text.textContent = '自动抓种中 (Running)';
        btn.textContent = '暂停抓种';
        btn.className = 'btn btn-danger btn-sm';
      } else {
        pill.style.background = 'var(--warning-bg)';
        pill.style.color = 'var(--warning)';
        text.textContent = '已暂停抓取 (Paused)';
        btn.textContent = '恢复抓种';
        btn.className = 'btn btn-primary btn-sm';
      }

      // Disk
      const disk = data.disk;
      if (disk) {
        document.getElementById('diskFreeVal').textContent = disk.free_gb;
        document.getElementById('diskTotalVal').textContent = disk.total_gb;
        document.getElementById('diskProgressFill').style.width = `${disk.used_pct}%`;
        document.getElementById('diskFooter').textContent = `已使用 ${disk.used_gb} GB (${disk.used_pct}%) · ${disk.is_low ? '⚠️ 空间紧缺熔断' : '空间正常'}`;
      }

      // Speeds
      const tr = data.transfer;
      if (tr) {
        document.getElementById('upSpeedVal').textContent = tr.up_speed_mb;
        document.getElementById('downSpeedFooter').textContent = `实时下载: ${tr.dl_speed_mb} MB/s`;
        document.getElementById('todayUpVal').textContent = tr.today_uploaded_gb;
        document.getElementById('todayDownFooter').textContent = `今日下载: ${tr.today_downloaded_gb} GB`;
        pagePeakUpload = Math.max(pagePeakUpload, Number(tr.up_speed_mb) || 0);
        document.getElementById('upSpeedProgress').style.width = `${pagePeakUpload ? Math.max(0, Number(tr.up_speed_mb) || 0) / pagePeakUpload * 100 : 0}%`;
        document.getElementById('upSpeedProgress').parentElement.title = `相对本次页面上传峰值 ${pagePeakUpload.toFixed(2)} MB/s`;
      }

      document.getElementById('seedingCountVal').textContent = data.seeding_count || 0;
      document.getElementById('downloadingCountVal').textContent = data.downloading_count || 0;
      document.getElementById('torrentCountBadge').textContent = data.torrents_count || 0;

      currentTorrents = data.torrents || [];
      renderTorrents(currentTorrents);
    }

    async function pollStatus() {
      if (statusPolling) return;
      statusPolling = true;
      try {
        const data = await fetchPollingJSON('/api/status');
        if (!isConnected) {
          isConnected = true;
          document.getElementById('modeBadge').textContent = '后端联机模式';
          await loadConfigFromBackend();
        }
        renderStatusData(data);
      } catch (e) {
        console.error(e);
      } finally {
        statusPolling = false;
      }
    }

    function renderTorrents(torrents) {
      const tbody = document.getElementById('torrentTableBody');
      if (!hasStatusData) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 2rem;">正在刷新…</td></tr>';
        return;
      }
      if (!torrents || torrents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 2rem;">当前分类下无活跃种子</td></tr>';
        return;
      }

      const filtered = torrents.filter(t => {
        if (currentFilter === 'downloading') return t.state && t.state.includes('DL');
        if (currentFilter === 'seeding') return t.state && t.state.includes('UP');
        return true;
      });
      if (torrentSort.key) filtered.sort((a, b) => {
        const key = torrentSort.key;
        let result = 0;
        if (key === 'name') {
          result = String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN', {numeric: true});
        } else if (key === 'score') {
          result = (Number(a.score) || 0) - (Number(b.score) || 0);
        } else if (key === 'added_on') {
          result = (Number(a.added_on) || 0) - (Number(b.added_on) || 0);
        } else if (key === 'completion_on') {
          const aComp = (Number(a.completion_on) > 0 && Number(a.completion_on) < 3000000000) ? Number(a.completion_on) : 0;
          const bComp = (Number(b.completion_on) > 0 && Number(b.completion_on) < 3000000000) ? Number(b.completion_on) : 0;
          result = aComp - bComp;
        } else {
          result = (Number(a[key]) || 0) - (Number(b[key]) || 0);
        }
        return result * torrentSort.direction || String(a.hash || '').localeCompare(String(b.hash || ''));
      });
      if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--text-dim);">当前筛选下无种子</td></tr>';
        return;
      }

      tbody.innerHTML = filtered.map(t => {
        const progress = Math.max(0, Math.min(100, Math.round((Number(t.progress) || 0) * 100)));
        const upMB = ((t.upspeed || 0) / (1024**2)).toFixed(1);
        const dlMB = ((t.dlspeed || 0) / (1024**2)).toFixed(1);
        const sizeGB = ((t.size || 0) / (1024**3)).toFixed(1);
        const upGB = ((t.uploaded || 0) / (1024**3)).toFixed(1);
        const ratio = (t.ratio || 0).toFixed(2);
        const isSeeding = progress >= 100;

        const addedSec = Number(t.added_on) || 0;
        const compSec = Number(t.completion_on) || 0;
        const addedShort = formatTorrentTime(addedSec);
        const addedFull = formatFullTime(addedSec);

        let compDisplay = '';
        let compFull = '';
        if (compSec > 0 && compSec < 3000000000) {
          compDisplay = formatTorrentTime(compSec);
          compFull = formatFullTime(compSec);
        } else if (progress >= 100) {
          compDisplay = '<span style="color:#34d399;">已完成</span>';
          compFull = '已完成下载';
        } else {
          compDisplay = `<span style="color:#60a5fa;font-size:0.72rem;">下载中 (${progress}%)</span>`;
          compFull = `下载中 (${progress}%)`;
        }

        const score = Number(t.score) || 0;
        let scoreColor = '#34d399';
        if (score >= 75) {
          scoreColor = '#34d399';
        } else if (score >= 45) {
          scoreColor = '#60a5fa';
        } else {
          scoreColor = '#f87171';
        }

        const isCompliant = Boolean(t.is_compliant);
        const compBadge = isCompliant
          ? '<span class="badge badge-hr-passed" style="font-size:0.68rem;padding:1px 5px;" title="该种子已完成所有考核指标，若评分过低允许被淘汰">已达标</span>'
          : '<span class="badge badge-hr" style="font-size:0.68rem;padding:1px 5px;" title="该种子仍在考核期或保种未满，享受保种保护，禁止淘汰">保种保护</span>';

        const scoreSummary = t.score_summary || `评分: ${score.toFixed(1)}分 · 状态: ${isCompliant ? '已达标' : '保种保护'}`;

        return `
          <tr>
            <td>
              <div class="torrent-title-block">
                <div class="torrent-title" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</div>
                <div class="torrent-meta">
                  <span class="badge ${isSeeding ? 'badge-2xfree' : 'badge-free'}">${isSeeding ? '做种' : '下载中'}</span>
                  ${t.hr_pending ? '<span class="badge badge-hr">考察中</span>' : ''}
                  ${t.hr_status === '已达标' ? '<span class="badge badge-hr-passed">已达标</span>' : ''}
                  ${!t.hr_pending && t.hr_status !== '已达标' ? '<span class="badge badge-site" style="opacity:0.65">无HR/未达标</span>' : ''}
                  <button class="btn btn-outline btn-xs" data-action="check-hr" data-hash="${escapeHtml(t.hash)}" data-name="${escapeHtml(t.name)}" title="重新检测该种子是否达标" style="padding:1px 5px;font-size:0.68rem;line-height:1.2;margin-left:2px;">🔄达标</button>
                  <span>做种 ${Number(t.num_seeds) || 0} / 同伴 ${Number(t.num_leechs) || 0}</span>
                </div>
              </div>
            </td>
            <td>
              <div style="display:flex;flex-direction:column;gap:3px;align-items:flex-start;" title="${escapeHtml(scoreSummary)}">
                <div style="font-weight:700;font-size:0.85rem;color:${scoreColor};display:flex;align-items:center;gap:3px;">
                  <span>★</span><span>${score.toFixed(1)}分</span>
                </div>
                <div>${compBadge}</div>
              </div>
            </td>
            <td>${sizeGB} GB</td>
            <td>
              <div style="width: 105px;">
                <div style="font-size: 0.72rem; display: flex; justify-content: space-between;">
                  <span>${progress}%</span><span style="color: ${isSeeding ? '#34d399' : '#60a5fa'};">${isSeeding ? '完成' : '拉取中'}</span>
                </div>
                <div class="progress-bar-wrap">
                  <div class="progress-bar-fill ${isSeeding ? 'success' : ''}" style="width: ${progress}%;"></div>
                </div>
              </div>
            </td>
            <td>
              <div title="添加时间: ${addedFull}" style="font-weight: 500; font-size: 0.8rem; white-space: nowrap;">${addedShort}</div>
              <div title="完成时间: ${compFull}" style="color: var(--text-dim); font-size: 0.72rem; white-space: nowrap; margin-top: 2px;">${compDisplay}</div>
            </td>
            <td>
              <div style="color: #34d399; font-weight: 600;">${upMB} MB/s ↑</div>
              <div style="color: var(--text-dim); font-size: 0.7rem;">${dlMB} MB/s ↓</div>
            </td>
            <td>
              <div style="font-weight: 600;">${upGB} GB</div>
              <div style="color: #a78bfa; font-size: 0.72rem;">Ratio: ${ratio}</div>
            </td>
            <td>
              <button class="btn btn-outline btn-sm" data-action="check-hr" data-hash="${escapeHtml(t.hash)}" data-name="${escapeHtml(t.name)}" title="刷新达标状态">查达标</button>
              <button class="btn btn-outline btn-sm" data-action="reannounce" data-hash="${escapeHtml(t.hash)}">强报</button>
              <button class="btn btn-danger btn-sm" data-action="delete" data-hash="${escapeHtml(t.hash)}" data-name="${escapeHtml(t.name)}">删种</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    function renderSites(sites) {
      document.getElementById('siteCountBadge').textContent = sites.length;
      const container = document.getElementById('siteCardsContainer');
      if (sites.length === 0) {
        container.innerHTML = '<div style="color: var(--text-dim); padding: 1rem;">暂未配置站点，点击右上角添加。</div>';
        return;
      }

      container.innerHTML = sites.map(s => {
        const ck = checkinStatuses[s.id] || {};
        const bonusTag = ck.bonus_earned ? `<span class="badge" style="background:rgba(234,179,8,0.2);color:#facc15;font-weight:600;margin-left:4px;">+${escapeHtml(ck.bonus_earned)} 魔力</span>` : '';
        const ckBadge = ck.status === 'success' || ck.status === 'already_checked'
          ? `<span class="badge badge-2xfree">今日已签到</span>`
          : ck.status === 'failed'
          ? `<span class="badge badge-danger">签到失败</span>`
          : `<span class="badge badge-site">待签到</span>`;
        const ckMsg = ck.message ? escapeHtml(ck.message) : '尚未签到';

        return `
        <div class="site-card">
          <div class="site-header">
            <div>
              <div class="site-title">${escapeHtml(s.name)}</div>
              <div class="site-url">${escapeHtml(s.base_url || '未配置 Base URL')}</div>
            </div>
            <span class="badge ${s.enabled ? 'badge-2xfree' : 'badge-site'}">${s.enabled ? '嗅探中' : '已停用'}</span>
          </div>
          <div style="font-size: 0.775rem; color: var(--text-muted); display: grid; grid-template-columns: 1fr 1fr; gap: 0.35rem;">
            <div>嗅探周期: <strong>${Number(s.interval_sec) || 60}s</strong></div>
            <div>H&R 规避: <strong style="color: #34d399;">${s.avoid_hnr ? '开启' : '关闭'}</strong></div>
            <div>自动签到: <strong style="color: ${s.auto_checkin !== false ? '#34d399' : '#94a3b8'};">${s.auto_checkin !== false ? '开启 (默认)' : '已关闭'}</strong></div>
            <div>Cookie 鉴权: <strong>${s.cookie ? '已配置' : '未提供'}</strong></div>
          </div>
          <div style="margin-top: 0.45rem; padding: 0.4rem 0.6rem; background: rgba(255,255,255,0.03); border-radius: 4px; display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem;">
            <div>
              ${ckBadge}
              ${bonusTag}
              <span style="margin-left: 6px; color: var(--text-dim);" title="${ckMsg}">${ckMsg}</span>
            </div>
            <button class="btn btn-outline btn-xs" data-action="site-checkin" data-site-id="${escapeHtml(s.id)}">立即签到</button>
          </div>
          <div style="display: flex; gap: 0.5rem; margin-top: 0.4rem;">
            <button class="btn ${s.enabled ? 'btn-danger' : 'btn-primary'} btn-sm" data-action="toggle-site" data-site="${escapeHtml(s.id)}">
              ${s.enabled ? '停用站点' : '启用站点'}
            </button>
          </div>
        </div>
      `;
      }).join('');
    }

    async function fetchLogs() {
      if (logsPolling) return;
      logsPolling = true;
      try {
        const data = await fetchPollingJSON('/api/logs');
        renderLogs(data.logs || []);
      } catch (e) {
        console.error(e);
      } finally {
        logsPolling = false;
      }
    }

    function renderLogs(logs) {
        const term = document.getElementById('logTerminal');
        term.innerHTML = logs.map(l => `
          <div class="log-line">
            <span class="log-time">[${escapeHtml(l.time)}]</span>
            <span class="log-tag ${['info', 'success', 'warning', 'error', 'danger'].includes(l.level) ? l.level : 'info'}">[${escapeHtml(l.tag)}]</span>
            <span>${escapeHtml(l.msg)}</span>
          </div>
        `).join('');
        term.scrollTop = term.scrollHeight;
    }

    async function toggleEnginePause() {
      if (!isConnected) {
        showToast('离线模式状态切换');
        return;
      }
      const res = await fetch('/api/engine/toggle', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        showToast(data.running ? '已恢复自动抓种' : '已暂停自动抓种');
        pollStatus();
      }
    }

    async function triggerManualCheck() {
      showToast('正在触发全站嗅探巡检...', '⚡');
      if (isConnected) {
        await fetch('/api/engine/sniff', { method: 'POST' });
        setTimeout(fetchLogs, 1000);
      }
    }

    async function emergencyCull() {
      if (confirm('确定要立即触发空间紧急清理吗？')) {
        if (isConnected) {
          const res = await fetch('/api/torrents/cull', { method: 'POST' });
          const data = await res.json();
          showToast(`紧急清理完成，释放 ${data.res.freed_gb} GB 空间！`, '🧹');
          pollStatus();
        } else {
          showToast('紧急清理完成，已释放 42 GB 空间！', '🧹');
        }
      }
    }

    async function reannounceTorrent(hash) {
      if (isConnected) {
        await fetch('/api/torrents/reannounce', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ hash })
        });
      }
      showToast('已发送 Tracker 强报指令');
    }

    async function deleteTorrent(hash, name) {
      if (confirm(`确定要彻底删除种子 [${name}] 并清除本地文件吗？`)) {
        if (isConnected) {
          await fetch(`/api/torrents/${hash}`, { method: 'DELETE' });
          pollStatus();
        }
        showToast(`已删除 [${name}]`);
      }
    }

    function applyConfigToUI(cfg) {
      renderLimitLabels({min_disk_free_gb: cfg.cull?.min_disk_free_gb, single_upload_limit_mb: cfg.downloader?.single_upload_limit_mb});
      if (!cfg) return;
      // 1. 选种与 Free 规则
      if (cfg.rules) {
        const r = cfg.rules;
        if (r.promotion_mode && document.getElementById('rulePromoMode')) document.getElementById('rulePromoMode').value = r.promotion_mode;
        if (r.min_free_remaining_min !== undefined && document.getElementById('ruleMinFreeRemaining')) document.getElementById('ruleMinFreeRemaining').value = r.min_free_remaining_min;
        if (r.min_size_gb !== undefined && document.getElementById('ruleMinSize')) document.getElementById('ruleMinSize').value = r.min_size_gb;
        if (r.max_size_gb !== undefined && document.getElementById('ruleMaxSize')) document.getElementById('ruleMaxSize').value = r.max_size_gb;
        if (r.max_seeders !== undefined && document.getElementById('ruleMaxSeeders')) document.getElementById('ruleMaxSeeders').value = r.max_seeders;
        if (r.min_leechers !== undefined && document.getElementById('ruleMinLeechers')) document.getElementById('ruleMinLeechers').value = r.min_leechers;
        if (r.max_age_min !== undefined && document.getElementById('ruleMaxAge')) document.getElementById('ruleMaxAge').value = r.max_age_min;
        if (r.avoid_hnr !== undefined && document.getElementById('ruleAvoidHnr')) document.getElementById('ruleAvoidHnr').checked = Boolean(r.avoid_hnr);
        if (r.exclude_keywords && document.getElementById('ruleExcludeKeywords')) {
          document.getElementById('ruleExcludeKeywords').value = Array.isArray(r.exclude_keywords) ? r.exclude_keywords.join(', ') : r.exclude_keywords;
        }
      }

      // 2. 自动删种与防爆盘规则
      if (cfg.cull) {
        const c = cfg.cull;
        if (c.min_disk_free_gb !== undefined && document.getElementById('cullMinDiskFree')) document.getElementById('cullMinDiskFree').value = c.min_disk_free_gb;
        if (c.emergency_disk_free_gb !== undefined && document.getElementById('cullEmergencyDiskFree')) document.getElementById('cullEmergencyDiskFree').value = c.emergency_disk_free_gb;
        if (c.max_disk_used_percent !== undefined && document.getElementById('cullMaxDiskUsedPercent')) document.getElementById('cullMaxDiskUsedPercent').value = c.max_disk_used_percent;
        if (c.pre_check_torrent_size !== undefined && document.getElementById('cullPreCheckSize')) document.getElementById('cullPreCheckSize').checked = Boolean(c.pre_check_torrent_size);
        if (c.emergency_pause_downloads !== undefined && document.getElementById('cullEmergencyPause')) document.getElementById('cullEmergencyPause').checked = Boolean(c.emergency_pause_downloads);
        if (c.auto_resume_downloads !== undefined && document.getElementById('cullAutoResume')) document.getElementById('cullAutoResume').checked = Boolean(c.auto_resume_downloads);
        if (c.target_ratio !== undefined && document.getElementById('cullTargetRatio')) document.getElementById('cullTargetRatio').value = c.target_ratio;
        if (c.max_seed_time_hours !== undefined && document.getElementById('cullMaxSeedHours')) document.getElementById('cullMaxSeedHours').value = c.max_seed_time_hours;
        if (c.hr_min_seed_hours !== undefined && document.getElementById('cullHrSeedHours')) document.getElementById('cullHrSeedHours').value = c.hr_min_seed_hours;
        if (c.hr_min_ratio !== undefined && document.getElementById('cullHrMinRatio')) document.getElementById('cullHrMinRatio').value = c.hr_min_ratio;
        if (c.enable_score_cull !== undefined && document.getElementById('cullEnableScoreCull')) document.getElementById('cullEnableScoreCull').checked = Boolean(c.enable_score_cull);
        if (c.cull_score_threshold !== undefined && document.getElementById('cullScoreThreshold')) document.getElementById('cullScoreThreshold').value = c.cull_score_threshold;
        if (c.score_grace_hours !== undefined && document.getElementById('cullScoreGraceHours')) document.getElementById('cullScoreGraceHours').value = c.score_grace_hours;
        if (c.kill_expired_free !== undefined && document.getElementById('cullKillExpiredFree')) document.getElementById('cullKillExpiredFree').checked = Boolean(c.kill_expired_free);
        if (c.delete_files !== undefined && document.getElementById('cullDeleteFiles')) document.getElementById('cullDeleteFiles').checked = Boolean(c.delete_files);
      }

      // 3. 下载器与防封配置
      if (cfg.downloader) {
        const d = cfg.downloader;
        if (d.url && document.getElementById('qbUrl')) document.getElementById('qbUrl').value = d.url;
        if (d.username && document.getElementById('qbUser')) document.getElementById('qbUser').value = d.username;
        if (d.category && document.getElementById('qbCategory')) document.getElementById('qbCategory').value = d.category;
        if (d.single_upload_limit_mb !== undefined && document.getElementById('qbUploadLimit')) document.getElementById('qbUploadLimit').value = d.single_upload_limit_mb;
        if (d.max_concurrent_downloads !== undefined && document.getElementById('qbMaxConcurrent')) document.getElementById('qbMaxConcurrent').value = d.max_concurrent_downloads;

        if (document.getElementById('uploadCategory')) document.getElementById('uploadCategory').value = d.category || 'pt-brush';
        if (document.getElementById('uploadLimitMb')) document.getElementById('uploadLimitMb').value = d.single_upload_limit_mb || 85;
      }

      // 4. 站点列表
      if (cfg.sites && Array.isArray(cfg.sites)) {
        renderSites(cfg.sites);
      }
    }

    async function saveAllSettings() {
      if (!appConfig) appConfig = {};
      appConfig.rules = {
        promotion_mode: document.getElementById('rulePromoMode').value,
        min_free_remaining_min: parseInt(document.getElementById('ruleMinFreeRemaining').value) || 60,
        min_size_gb: parseFloat(document.getElementById('ruleMinSize').value) || 2.0,
        max_size_gb: parseFloat(document.getElementById('ruleMaxSize').value) || 50.0,
        max_seeders: parseInt(document.getElementById('ruleMaxSeeders').value) || 10,
        min_leechers: parseInt(document.getElementById('ruleMinLeechers').value) || 2,
        max_age_min: parseInt(document.getElementById('ruleMaxAge').value) || 30,
        avoid_hnr: document.getElementById('ruleAvoidHnr').checked,
        exclude_keywords: document.getElementById('ruleExcludeKeywords').value.split(',').map(s => s.trim()).filter(Boolean)
      };

      appConfig.cull = {
        min_disk_free_gb: parseFloat(document.getElementById('cullMinDiskFree').value) || 15.0,
        emergency_disk_free_gb: parseFloat(document.getElementById('cullEmergencyDiskFree') ? document.getElementById('cullEmergencyDiskFree').value : 5.0) || 5.0,
        max_disk_used_percent: parseInt(document.getElementById('cullMaxDiskUsedPercent') ? document.getElementById('cullMaxDiskUsedPercent').value : 88) || 88,
        pre_check_torrent_size: document.getElementById('cullPreCheckSize') ? document.getElementById('cullPreCheckSize').checked : true,
        emergency_pause_downloads: document.getElementById('cullEmergencyPause') ? document.getElementById('cullEmergencyPause').checked : true,
        auto_resume_downloads: document.getElementById('cullAutoResume') ? document.getElementById('cullAutoResume').checked : true,
        kill_expired_free: document.getElementById('cullKillExpiredFree').checked,
        target_ratio: parseFloat(document.getElementById('cullTargetRatio').value) || 3.5,
        max_seed_time_hours: parseInt(document.getElementById('cullMaxSeedHours').value) || 24,
        hr_min_seed_hours: parseFloat(document.getElementById('cullHrSeedHours').value) || 20.0,
        hr_min_ratio: parseFloat(document.getElementById('cullHrMinRatio').value) || 1.0,
        enable_score_cull: document.getElementById('cullEnableScoreCull') ? document.getElementById('cullEnableScoreCull').checked : true,
        cull_score_threshold: parseFloat(document.getElementById('cullScoreThreshold')?.value || 35.0),
        score_grace_hours: parseFloat(document.getElementById('cullScoreGraceHours')?.value || 1.0),
        delete_files: document.getElementById('cullDeleteFiles').checked
      };

      appConfig.downloader = {
        url: document.getElementById('qbUrl').value,
        username: document.getElementById('qbUser').value,
        password: document.getElementById('qbPass').value || (appConfig.downloader ? appConfig.downloader.password : ""),
        category: document.getElementById('qbCategory').value,
        single_upload_limit_mb: parseInt(document.getElementById('qbUploadLimit').value) || 85,
        max_concurrent_downloads: parseInt(document.getElementById('qbMaxConcurrent').value) || 2
      };

      if (isConnected) {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(appConfig)
        });
        if (res.ok) showToast('配置已持久化存入 SQLite 并热生效！');
      } else {
        showToast('配置已更新（本地演示）');
      }
    }

    let selectedTorrentFiles = [];
    let currentUploadTab = 'file';

    function formatFileSize(bytes) {
      if (!bytes || bytes === 0) return '0 B';
      const k = 1024;
      const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function openUploadModal() {
      selectedTorrentFiles = [];
      renderSelectedFiles();
      const urlInput = document.getElementById('uploadUrlInput');
      if (urlInput) urlInput.value = '';
      if (appConfig && appConfig.downloader) {
        document.getElementById('uploadCategory').value = appConfig.downloader.category || 'pt-brush';
        document.getElementById('uploadLimitMb').value = appConfig.downloader.single_upload_limit_mb || 85;
      }
      switchUploadTab('file');
      document.getElementById('uploadTorrentModal').classList.add('open');
    }

    function closeUploadModal() {
      document.getElementById('uploadTorrentModal').classList.remove('open');
      selectedTorrentFiles = [];
      renderSelectedFiles();
      const fileInput = document.getElementById('torrentFileInput');
      if (fileInput) fileInput.value = '';
    }

    function switchUploadTab(tab) {
      currentUploadTab = tab;
      const fileBtn = document.getElementById('uploadTabFileBtn');
      const urlBtn = document.getElementById('uploadTabUrlBtn');
      const filePane = document.getElementById('uploadPaneFile');
      const urlPane = document.getElementById('uploadPaneUrl');

      if (tab === 'file') {
        fileBtn.className = 'btn btn-primary btn-sm';
        urlBtn.className = 'btn btn-outline btn-sm';
        filePane.style.display = 'block';
        urlPane.style.display = 'none';
      } else {
        urlBtn.className = 'btn btn-primary btn-sm';
        fileBtn.className = 'btn btn-outline btn-sm';
        filePane.style.display = 'none';
        urlPane.style.display = 'block';
      }
    }

    function handleTorrentFileSelect(files) {
      if (!files || files.length === 0) return;
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (!selectedTorrentFiles.some(f => f.name === file.name && f.size === file.size)) {
          selectedTorrentFiles.push(file);
        }
      }
      renderSelectedFiles();
    }

    function removeSelectedFile(index) {
      selectedTorrentFiles.splice(index, 1);
      renderSelectedFiles();
    }

    function renderSelectedFiles() {
      const container = document.getElementById('selectedFileList');
      if (!container) return;
      if (selectedTorrentFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
      }
      container.style.display = 'flex';
      container.innerHTML = selectedTorrentFiles.map((file, idx) => `
        <div class="upload-file-item">
          <div class="upload-file-info">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--primary); flex-shrink: 0;"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
            <span class="upload-file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
            <span class="upload-file-size">(${formatFileSize(file.size)})</span>
          </div>
          <button type="button" class="upload-file-remove" data-action="remove-file" data-index="${idx}" title="移除此文件">✕</button>
        </div>
      `).join('');
    }

    async function submitTorrentUpload() {
      const btn = document.getElementById('btnSubmitUpload');
      const category = document.getElementById('uploadCategory').value.trim() || 'pt-brush';
      const limitMb = parseInt(document.getElementById('uploadLimitMb').value) || 0;
      const startImmediately = document.getElementById('uploadStartImmediately').checked;
      const paused = !startImmediately;

      if (currentUploadTab === 'file') {
        if (selectedTorrentFiles.length === 0) {
          alert('请至少选择或拖入一个 .torrent 文件！');
          return;
        }

        const formData = new FormData();
        selectedTorrentFiles.forEach(file => {
          formData.append('files', file);
        });
        formData.append('category', category);
        formData.append('paused', paused.toString());
        formData.append('upload_limit_mb', limitMb.toString());

        const force = document.getElementById('uploadForce') ? document.getElementById('uploadForce').checked : false;
        formData.append('force', force.toString());

        btn.disabled = true;
        btn.textContent = '正在上传推送到 qB...';

        try {
          const res = await fetch('/api/torrents/upload', {
            method: 'POST',
            body: formData
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            showToast(`成功推送 ${data.success_count} 个种子到 qBittorrent`);
            closeUploadModal();
            pollStatus();
            fetchLogs();
          } else {
            alert('添加失败: ' + (data.detail || (data.results && data.results[0] && data.results[0].error) || '未知错误'));
          }
        } catch (e) {
          console.error(e);
          alert('请求异常: ' + e.message);
        } finally {
          btn.disabled = false;
          btn.textContent = '确认添加并推送';
        }
      } else {
        const urls = document.getElementById('uploadUrlInput').value.trim();
        if (!urls) {
          alert('请输入至少一个种子下载链接或磁力链！');
          return;
        }

        btn.disabled = true;
        btn.textContent = '正在解析推送到 qB...';

        try {
          const res = await fetch('/api/torrents/add_urls', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              urls: urls,
              category: category,
              paused: paused,
              upload_limit_mb: limitMb
            })
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            showToast(`成功添加 ${data.success_count} 个链接到 qBittorrent`);
            closeUploadModal();
            pollStatus();
            fetchLogs();
          } else {
            alert('添加失败: ' + (data.detail || '链接解析或推送失败'));
          }
        } catch (e) {
          console.error(e);
          alert('请求异常: ' + e.message);
        } finally {
          btn.disabled = false;
          btn.textContent = '确认添加并推送';
        }
      }
    }

    function exportConfigJson() {
      showToast('正在导出配置快照...', '📦');
      window.location.href = '/api/config/export';
    }

    function initDropzone() {
      const dropzone = document.getElementById('uploadDropzone');
      if (dropzone) {
        ['dragenter', 'dragover'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.add('dragover');
          });
        });
        ['dragleave', 'drop'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('dragover');
          });
        });
        dropzone.addEventListener('drop', (e) => {
          if (e.dataTransfer && e.dataTransfer.files) {
            handleTorrentFileSelect(e.dataTransfer.files);
          }
        });
      }

      // 全局窗口拖放支持：将 .torrent 拖入浏览器任何区域均可自动唤起弹窗
      window.addEventListener('dragover', (e) => { e.preventDefault(); }, false);
      window.addEventListener('drop', (e) => {
        if (e.target && (e.target.closest('#uploadTorrentModal') || e.target.closest('.upload-dropzone'))) return;
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
          const hasTorrent = Array.from(e.dataTransfer.files).some(f => f.name.toLowerCase().endsWith('.torrent'));
          if (hasTorrent) {
            e.preventDefault();
            openUploadModal();
            handleTorrentFileSelect(e.dataTransfer.files);
            showToast('已捕捉拖入的种子文件！', '📁');
          }
        }
      }, false);
    }

    function openAddSiteModal() { document.getElementById('addSiteModal').classList.add('open'); }
    function closeAddSiteModal() { document.getElementById('addSiteModal').classList.remove('open'); }

    async function submitAddSite() {
      const name = document.getElementById('modalSiteName').value.trim();
      const base_url = document.getElementById('modalSiteBaseUrl').value.trim();
      const rss_url = document.getElementById('modalSiteRss').value.trim();
      const cookie = document.getElementById('modalSiteCookie').value.trim();
      const interval_sec = parseInt(document.getElementById('modalSiteInterval').value) || 60;

      const auto_checkin = document.getElementById('modalSiteAutoCheckin') ? document.getElementById('modalSiteAutoCheckin').checked : true;

      if (!name || !rss_url) {
        alert('请至少填写站点名称与 RSS 订阅 URL');
        return;
      }

      const newSite = {
        id: 'site_' + Date.now(),
        name,
        type: 'nexusphp',
        base_url,
        rss_url,
        cookie,
        interval_sec,
        enabled: true,
        avoid_hnr: true,
        auto_checkin
      };

      if (!appConfig) appConfig = { sites: [] };
      if (!appConfig.sites) appConfig.sites = [];
      appConfig.sites.push(newSite);

      closeAddSiteModal();
      await saveAllSettings();
      renderSites(appConfig.sites);
      showToast(`站点 [${name}] 已成功添加并激活`);
    }

    async function toggleSite(siteId) {
      if (!appConfig || !appConfig.sites) return;
      const s = appConfig.sites.find(x => x.id === siteId);
      if (s) {
        s.enabled = !s.enabled;
        await saveAllSettings();
        renderSites(appConfig.sites);
        showToast(`站点 [${s.name}] 已${s.enabled ? '启用' : '停用'}`);
      }
    }

    async function checkTorrentHR(hash, btn) {
      if (!hash) return;
      const originalText = btn ? btn.textContent : '';
      if (btn) {
        btn.disabled = true;
        btn.textContent = '…';
      }
      showToast('正在核对站点最新达标状态…', '🔍');
      try {
        const res = await fetch(`/api/torrents/${encodeURIComponent(hash)}/check_hr`, { method: 'POST' });
        if (!res.ok) throw new Error('核查请求失败');
        const data = await res.json();

        const target = currentTorrents.find(t => String(t.hash || '').toLowerCase() === String(hash).toLowerCase());
        if (target) {
          target.hr_status = data.hr_status;
          target.hr_pending = Boolean(data.hr_pending);
        }

        const wrap = document.getElementById(`hr-wrap-${hash}`);
        if (wrap) {
          let badgeHtml = '';
          if (data.hr_status === '已达标') {
            badgeHtml = '<span class="badge badge-hr-passed">已达标</span>';
          } else if (data.hr_pending) {
            badgeHtml = '<span class="badge badge-hr">考察中</span>';
          } else {
            badgeHtml = '<span class="badge badge-site" style="opacity:0.65">无HR/未达标</span>';
          }
          wrap.innerHTML = `${badgeHtml} <button class="btn btn-outline btn-xs" data-action="check-hr" data-hash="${escapeHtml(hash)}" data-name="${escapeHtml(data.name || '')}" title="重新检测该种子是否达标" style="padding:1px 5px;font-size:0.68rem;line-height:1.2;margin-left:2px;">🔄达标</button>`;
        } else {
          renderTorrents(currentTorrents);
        }

        const icon = data.hr_status === '已达标' ? '🎉' : data.hr_pending ? '⏳' : 'ℹ️';
        showToast(data.message || '已更新达标状态', icon);
      } catch (err) {
        showToast('达标状态检测失败: ' + err.message, '⚠️');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = originalText;
        }
      }
    }

    async function checkinSite(siteId, btn) {
      if (!siteId) return;
      const originalText = btn ? btn.textContent : '';
      if (btn) {
        btn.disabled = true;
        btn.textContent = '签到中…';
      }
      showToast('正在向站点提交签到…', '⏳');
      try {
        const res = await fetch(`/api/sites/${encodeURIComponent(siteId)}/checkin`, { method: 'POST' });
        if (!res.ok) throw new Error('签到请求失败');
        const data = await res.json();
        const result = data.result || {};

        checkinStatuses[siteId] = result;
        const icon = result.status === 'success' ? '🎉' : result.status === 'already_checked' ? '✅' : '⚠️';
        showToast(result.message || '签到完成', icon);

        if (appConfig?.sites) renderSites(appConfig.sites);
        loadAccounts(false);
      } catch (err) {
        showToast('签到异常: ' + err.message, '⚠️');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = originalText;
        }
      }
    }

    function clearLogs() {
      document.getElementById('logTerminal').innerHTML = '';
      if (isConnected) fetch('/api/logs/clear', { method: 'POST' });
      showToast('日志已清屏');
    }

    function filterTorrents(filter) {
      currentFilter = filter;
      renderTorrents(currentTorrents);
      showToast(`已切换视图为: ${filter}`);
    }

    function loadMockData() {
      renderTorrents([
        {
          hash: "mock1",
          name: "Dune.Part.Two.2024.2160p.UHD.BluRay.x265.10bit.DTS-HD.MA.TrueHD.7.1.Atmos-FRDS",
          size: 28.4 * 1024**3,
          progress: 1.0,
          upspeed: 42.8 * 1024**2,
          dlspeed: 0,
          uploaded: 118.2 * 1024**3,
          ratio: 4.16,
          num_seeds: 18,
          num_leechs: 45,
          state: "uploading"
        },
        {
          hash: "mock2",
          name: "Furiosa.A.Mad.Max.Saga.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX",
          size: 18.6 * 1024**3,
          progress: 0.482,
          upspeed: 38.5 * 1024**2,
          dlspeed: 18.5 * 1024**2,
          uploaded: 22.4 * 1024**3,
          ratio: 2.50,
          num_seeds: 4,
          num_leechs: 82,
          state: "downloading"
        }
      ]);

      renderSites([
        { id: "s1", name: "KeepFRDS", base_url: "https://pt.keepfrds.com", enabled: true, interval_sec: 60, avoid_hnr: true, cookie: "c_secure=yes" }
      ]);

      const term = document.getElementById('logTerminal');
      term.innerHTML = `
        <div class="log-line"><span class="log-time">[18:34:02]</span><span class="log-tag info">[SYSTEM]</span><span>PT-AutoBrush 原型运行中，适配架构: el8uek.aarch64</span></div>
        <div class="log-line"><span class="log-time">[18:34:10]</span><span class="log-tag success">[SNIFFER]</span><span>[FRDS] RSS 拉取成功，发现 15 条候选种子，通过双重 Free 校验</span></div>
        <div class="log-line"><span class="log-time">[18:34:11]</span><span class="log-tag success">[HIT-PUSH]</span><span>[FRDS] 命中 2xFree: Dune.Part.Two.2024 (28.4GB) -> 自动推送到 qB 并限速 85MB/s</span></div>
      `;
    }

    // Launch
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      if (button.dataset.action === 'reannounce') reannounceTorrent(button.dataset.hash);
      if (button.dataset.action === 'delete') deleteTorrent(button.dataset.hash, button.dataset.name);
      if (button.dataset.action === 'toggle-site') toggleSite(button.dataset.site);
      if (button.dataset.action === 'remove-file') removeSelectedFile(Number(button.dataset.index));
      if (button.dataset.action === 'check-hr') checkTorrentHR(button.dataset.hash, button);
      if (button.dataset.action === 'site-checkin') checkinSite(button.dataset.siteId, button);
    });
    window.addEventListener('DOMContentLoaded', () => {
      init();
      initDropzone();
    });
  
document.addEventListener('DOMContentLoaded', () => {
document.querySelector('[data-handler="0"]').addEventListener('click', function(event) { openUploadModal(); });
document.querySelector('[data-handler="1"]').addEventListener('click', function(event) { triggerManualCheck(); });
document.querySelector('[data-handler="2"]').addEventListener('click', function(event) { toggleEnginePause(); });
document.querySelector('[data-handler="3"]').addEventListener('click', function(event) { switchTab('torrents', this); });
document.querySelector('[data-handler="4"]').addEventListener('click', function(event) { switchTab('sites', this); });
document.querySelector('[data-handler="5"]').addEventListener('click', function(event) { switchTab('rules', this); });
document.querySelector('[data-handler="6"]').addEventListener('click', function(event) { switchTab('cull', this); });
document.querySelector('[data-handler="7"]').addEventListener('click', function(event) { switchTab('downloader', this); });
document.querySelector('[data-handler="8"]').addEventListener('click', function(event) { switchTab('logs', this); });
document.querySelector('[data-handler="9"]').addEventListener('click', function(event) { openUploadModal(); });
document.querySelector('[data-handler="10"]').addEventListener('click', function(event) { filterTorrents('all'); });
document.querySelector('[data-handler="11"]').addEventListener('click', function(event) { filterTorrents('downloading'); });
document.querySelector('[data-handler="12"]').addEventListener('click', function(event) { filterTorrents('seeding'); });
document.querySelector('[data-handler="13"]').addEventListener('click', function(event) { emergencyCull(); });
document.querySelector('[data-handler="14"]').addEventListener('click', function(event) { openAddSiteModal(); });
document.querySelector('[data-handler="15"]').addEventListener('click', function(event) { saveAllSettings(); });
document.querySelector('[data-handler="16"]').addEventListener('click', function(event) { saveAllSettings(); });
document.querySelector('[data-handler="17"]').addEventListener('click', function(event) { exportConfigJson(); });
document.querySelector('[data-handler="18"]').addEventListener('click', function(event) { saveAllSettings(); });
document.querySelector('[data-handler="19"]').addEventListener('click', function(event) { clearLogs(); });
document.querySelector('[data-handler="20"]').addEventListener('click', function(event) { fetchLogs(); });
document.querySelector('[data-handler="21"]').addEventListener('click', function(event) { closeUploadModal(); });
document.querySelector('[data-handler="22"]').addEventListener('click', function(event) { switchUploadTab('file'); });
document.querySelector('[data-handler="23"]').addEventListener('click', function(event) { switchUploadTab('url'); });
document.querySelector('[data-handler="24"]').addEventListener('click', function(event) { document.getElementById('torrentFileInput').click(); });
document.querySelector('[data-handler="25"]').addEventListener('change', function(event) { handleTorrentFileSelect(this.files); });
document.querySelector('[data-handler="26"]').addEventListener('click', function(event) { closeUploadModal(); });
document.querySelector('[data-handler="27"]').addEventListener('click', function(event) { submitTorrentUpload(); });
document.querySelector('[data-handler="28"]').addEventListener('click', function(event) { closeAddSiteModal(); });
document.querySelector('[data-handler="29"]').addEventListener('click', function(event) { closeAddSiteModal(); });
document.querySelector('[data-handler="30"]').addEventListener('click', function(event) { submitAddSite(); });
document.getElementById('btnLogout').addEventListener('click', logout);
});
