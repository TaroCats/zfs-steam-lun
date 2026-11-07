/* ZFS Steam LUN 控制台 */
'use strict';

const S = {
  view: 'overview',
  overview: null,
  scan: null,
  pools: [],
  logs: [],
  sessions: [],
  busy: false,
};

const TITLES = {
  overview: '概览', masters: '母版卷', clients: '客户端',
  sessions: 'iSCSI 会话', logs: '操作日志', settings: '设置',
};

/* ---------------------------------------------------------------- 工具 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
}

function pct(part, total) {
  if (!total) return 0;
  return Math.min(100, Math.round((part / total) * 1000) / 10);
}

function toast(msg, type = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3600);
  setTimeout(() => el.remove(), 4000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: {
      'Content-Type': 'application/json',
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || ('请求失败 (' + res.status + ')');
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ---------------------------------------------------------------- 弹窗 */
function modal({ title, body, wide = false, footer = '', onMount }) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal ${wide ? 'wide' : ''}" role="dialog">
      <div class="modal-head"><span>${esc(title)}</span><span class="x">×</span></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;
  $('#modal-root').appendChild(mask);
  const close = () => mask.remove();
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
  $('.x', mask).addEventListener('click', close);
  if (onMount) onMount(mask, close);
  return { el: mask, close };
}

function confirmDlg({ title, message, danger = false, confirmText = '确认', requireText = '' }) {
  return new Promise((resolve) => {
    const { el, close } = modal({
      title,
      body: `<div style="font-size:13px;line-height:1.8;color:var(--muted)">${message}</div>
        ${requireText ? `<div class="field" style="margin-top:14px;margin-bottom:0">
          <label>请输入 <span class="mono" style="color:var(--text)">${esc(requireText)}</span> 以确认</label>
          <input id="cfm-text" autocomplete="off" /></div>` : ''}`,
      footer: `<button class="btn" data-act="no">取消</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-act="yes" ${requireText ? 'disabled' : ''}>${esc(confirmText)}</button>`,
      onMount(root) {
        const yes = root.querySelector('[data-act=yes]');
        const input = root.querySelector('#cfm-text');
        if (input) {
          input.addEventListener('input', () => { yes.disabled = input.value.trim() !== requireText; });
          setTimeout(() => input.focus(), 50);
        }
        root.querySelector('[data-act=no]').onclick = () => { close(); resolve(false); };
        yes.onclick = () => { close(); resolve(true); };
      },
    });
    el.addEventListener('click', (e) => { if (e.target === el) resolve(false); });
  });
}

function busyBtn(btn, on, text = '处理中') {
  if (!btn) return;
  if (on) {
    btn.dataset.old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> ${esc(text)}`;
  } else {
    btn.disabled = false;
    if (btn.dataset.old) btn.innerHTML = btn.dataset.old;
  }
}

async function guard(fn, btn, text) {
  if (S.busy) return;
  S.busy = true;
  busyBtn(btn, true, text);
  try { await fn(); }
  catch (e) { toast(e.message || String(e), 'err'); }
  finally { S.busy = false; busyBtn(btn, false); }
}

/* ---------------------------------------------------------------- 数据 */
async function loadData(force = false) {
  const jobs = [
    api('/api/overview'),
    api('/api/scan'),
    api('/api/pools'),
    api('/api/logs?limit=80'),
    api('/api/sessions'),
  ];
  const [ov, sc, pools, logs, sessions] = await Promise.all(jobs);
  S.overview = ov; S.scan = sc; S.pools = pools; S.logs = logs; S.sessions = sessions;
  render();
}

/* ---------------------------------------------------------------- 路由 */
function go(view) {
  if (!TITLES[view]) view = 'overview';
  S.view = view;
  location.hash = '#' + view;
  $$('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.view === view));
  $('#page-title').textContent = TITLES[view];
  render();
}

function render() {
  if (!S.overview) return;
  const v = S.view;
  $('#foot-ver').textContent = 'v' + (S.overview.version || '1.0.0');
  $('#top-meta').textContent = S.overview.pool
    ? `${S.overview.pool.name} · ${fmtBytes(S.overview.pool.free)} 可用`
    : '未配置存储池';
  const el = $('#view');
  if (v === 'overview') el.innerHTML = viewOverview();
  else if (v === 'masters') el.innerHTML = viewMasters();
  else if (v === 'clients') el.innerHTML = viewClients();
  else if (v === 'sessions') el.innerHTML = viewSessions();
  else if (v === 'logs') el.innerHTML = viewLogs();
  else if (v === 'settings') { el.innerHTML = viewSettings(); bindSettings(); }
  bindCommon();
}

/* ---------------------------------------------------------------- 概览 */
function viewOverview() {
  const o = S.overview, sc = S.scan || { masters: [], clients: [] };
  const pool = o.pool;
  const usedPct = pool ? pct(pool.alloc, pool.size) : 0;
  const barCls = usedPct > 90 ? 'bad' : usedPct > 75 ? 'warn' : '';

  const warnings = [];
  if (!o.settings.root_dataset) {
    warnings.push(`<div class="banner warn">尚未指定根数据集。请到 <a href="#settings">设置</a> 选择存储池并创建数据集，之后才能创建母版卷。</div>`);
  }
  if (!o.configfs) {
    warnings.push(`<div class="banner bad">容器未看到 <span class="mono">/sys/kernel/config/target/iscsi</span>，LIO 不可用。请确认 compose 已挂载 <span class="mono">/sys/kernel/config</span> 且以 privileged 运行。</div>`);
  }
  if (!o.zfs_ok) {
    warnings.push(`<div class="banner bad">容器内无法执行 <span class="mono">zfs</span> 命令，请检查镜像依赖与 <span class="mono">/dev/zfs</span> 挂载。</div>`);
  }
  if (pool && pool.health !== 'ONLINE') {
    warnings.push(`<div class="banner bad">存储池 <span class="mono">${esc(pool.name)}</span> 健康状态为 <b>${esc(pool.health)}</b>，请先处理池故障。</div>`);
  }
  if (sc.orphan_targets && sc.orphan_targets.length) {
    warnings.push(`<div class="banner warn">检测到 ${sc.orphan_targets.length} 个指向不存在卷的 iSCSI 映射，可在“客户端”页重建或清理。</div>`);
  }

  const recent = (S.logs || []).slice(0, 8).map((l) => `
    <tr><td class="mono" style="white-space:nowrap">${esc(l.ts)}</td>
    <td>${esc(l.action)}</td><td class="mono">${esc(l.target || '-')}</td>
    <td><span class="pill ${l.result === 'ok' ? 'ok' : 'bad'}">${esc(l.result)}</span></td></tr>`).join('');

  return `
    ${warnings.join('')}
    <div class="grid c4">
      <div class="card stat">
        <div class="label">池容量</div>
        <div class="value">${pool ? fmtBytes(pool.alloc) : '—'}<small>/ ${pool ? fmtBytes(pool.size) : '—'}</small></div>
        <div class="bar ${barCls}" style="margin-top:12px"><i style="width:${usedPct}%"></i></div>
        <div class="sub">已用 ${usedPct}% · 可用 ${pool ? fmtBytes(pool.free) : '—'}</div>
      </div>
      <div class="card stat">
        <div class="label">母版卷</div>
        <div class="value">${sc.masters.length}<small>个</small></div>
        <div class="sub">快照总数 ${sc.masters.reduce((a, m) => a + (m.snapshots || 0), 0)}</div>
      </div>
      <div class="card stat">
        <div class="label">客户端克隆</div>
        <div class="value">${sc.clients.length}<small>台</small></div>
        <div class="sub">占用 ${fmtBytes(sc.clients.reduce((a, c) => a + (c.refer || c.used || 0), 0))}</div>
      </div>
      <div class="card stat">
        <div class="label">在线会话</div>
        <div class="value">${o.online}<small>个</small></div>
        <div class="sub">${o.online > 0 ? '有客户端正在使用 LUN' : '当前无连接'}</div>
      </div>
    </div>

    <div class="grid c2" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><h3>推荐工作流</h3></div>
        <div class="card-body">
          <ol class="steps">
            <li>在 <b>母版</b> 页创建母版卷，挂给一台电脑，装好 Steam 库并<b>安全弹出/断开</b>。</li>
            <li>点 <b>打快照</b>，得到一个干净基线（NTFS 必须已卸载）。</li>
            <li>在 <b>客户端</b> 页为每台电脑克隆一份，独立 LUN、独立盘符。</li>
            <li>母版更新后，可对单个客户端 <b>重建</b>，不影响其他机器。</li>
          </ol>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>存储池</h3><div class="spacer"></div>
          <span class="pill ${pool && pool.health === 'ONLINE' ? 'ok' : 'bad'}"><i class="dot"></i>${esc(pool ? pool.health : '未配置')}</span>
        </div>
        <div class="card-body tight">
          ${S.pools.length ? `<table><thead><tr><th>池</th><th>容量</th><th>已用</th><th>碎片</th><th>状态</th></tr></thead><tbody>
            ${S.pools.map((p) => `<tr>
              <td class="name-cell mono">${esc(p.name)}</td>
              <td>${fmtBytes(p.size)}</td>
              <td>${fmtBytes(p.alloc)} <span class="mono">(${pct(p.alloc, p.size)}%)</span></td>
              <td class="mono">${esc(p.frag)}</td>
              <td><span class="pill ${p.health === 'ONLINE' ? 'ok' : 'bad'}"><i class="dot"></i>${esc(p.health)}</span></td>
            </tr>`).join('')}
          </tbody></table>` : `<div class="empty">未检测到 ZFS 池</div>`}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h3>最近操作</h3><div class="spacer"></div><a href="#logs" style="font-size:12px">全部</a></div>
      <div class="card-body tight">
        ${recent ? `<table><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>结果</th></tr></thead><tbody>${recent}</tbody></table>`
                 : `<div class="empty">还没有操作记录</div>`}
      </div>
    </div>`;
}

/* ---------------------------------------------------------------- 母版 */
function viewMasters() {
  const list = (S.scan && S.scan.masters) || [];
  const rows = list.map((m) => {
    const raw = m.name.replace(/^.*\/masters\//, '');
    return `<tr>
      <td>
        <div class="name-cell">${esc(m.short)}</div>
        <div class="mono" style="margin-top:2px">${esc(m.name)}</div>
      </td>
      <td>${fmtBytes(m.volsize)}<div class="mono" style="margin-top:2px">已用 ${fmtBytes(m.used)} · ${esc(m.compress)}</div></td>
      <td>${m.snapshots} <span class="mono">快照</span><div class="mono" style="margin-top:2px">${m.clones.length} 克隆</div></td>
      <td><span class="pill ${m.online ? 'warn' : 'off'}"><i class="dot"></i>${m.online ? '已挂载' : '未挂载'}</span></td>
      <td>
        <div class="row-actions">
          <button class="btn sm" data-m="snap" data-name="${esc(raw)}">快照</button>
          <button class="btn sm" data-m="mount" data-name="${esc(raw)}">挂载</button>
          ${m.online ? `<button class="btn sm" data-m="unmount" data-name="${esc(raw)}">断开</button>` : ''}
          <button class="btn sm ghost" data-m="snaps" data-name="${esc(raw)}">快照列表</button>
          <button class="btn sm danger" data-m="del" data-name="${esc(raw)}">删除</button>
        </div>
      </td>
    </tr>`;
  }).join('');

  return `
    <div class="card">
      <div class="card-head">
        <h3>母版卷</h3>
        <span class="mono" style="color:var(--muted-2)">${esc((S.overview.settings.root_dataset || '-') + '/masters')}</span>
        <div class="spacer"></div>
        <button class="btn primary sm" id="new-master">新建母版</button>
      </div>
      <div class="card-body tight">
        ${list.length ? `<table>
          <thead><tr><th>名称</th><th>容量 / 属性</th><th>派生</th><th>状态</th><th style="text-align:right">操作</th></tr></thead>
          <tbody>${rows}</tbody></table>`
          : `<div class="empty"><b>还没有母版卷</b><br />母版是一份「装了 Steam 库」的原始磁盘，所有客户端都从它的快照克隆。</div>`}
      </div>
    </div>
    <div class="banner info" style="margin-top:16px">
      修改母版内容（装/更新游戏）前，先在客户端断开 iSCSI；改完后重新打快照。已存在的克隆<b>不会</b>自动同步母版变化。
    </div>`;
}

/* ---------------------------------------------------------------- 客户端 */
function viewClients() {
  const list = (S.scan && S.scan.clients) || [];
  const rows = list.map((c) => `<tr>
    <td>
      <div class="name-cell">${esc(c.short)}</div>
      ${c.note ? `<div class="mono" style="margin-top:2px">${esc(c.note)}</div>` : ''}
    </td>
    <td class="mono">${esc(c.master_name || '-')}</td>
    <td>${fmtBytes(c.volsize)}<div class="mono" style="margin-top:2px">已用 ${fmtBytes(c.refer || c.used)}</div></td>
    <td class="mono" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(c.target)}">${esc(c.target || '未映射')}</td>
    <td><span class="pill ${c.online ? 'ok' : 'off'}"><i class="dot"></i>${c.online ? '在线' : '离线'}</span>
      ${c.chap ? '<span class="pill info" style="margin-left:4px">CHAP</span>' : ''}</td>
    <td>
      <div class="row-actions">
        <button class="btn sm primary" data-c="guide" data-name="${esc(c.short)}">连接</button>
        ${c.target ? '' : `<button class="btn sm" data-c="attach" data-name="${esc(c.short)}">重新映射</button>`}
        <button class="btn sm" data-c="resize" data-name="${esc(c.short)}" data-size="${c.volsize}">扩容</button>
        <button class="btn sm ghost" data-c="reclone" data-name="${esc(c.short)}">重建</button>
        <button class="btn sm danger" data-c="del" data-name="${esc(c.short)}">删除</button>
      </div>
    </td>
  </tr>`).join('');

  return `
    <div class="card">
      <div class="card-head">
        <h3>客户端</h3>
        <span class="mono" style="color:var(--muted-2)">${esc((S.overview.settings.root_dataset || '-') + '/clients')}</span>
        <div class="spacer"></div>
        <button class="btn primary sm" id="new-client">新增客户端</button>
      </div>
      <div class="card-body tight">
        ${list.length ? `<table>
          <thead><tr><th>名称</th><th>母版</th><th>容量</th><th>目标 IQN</th><th>状态</th><th style="text-align:right">操作</th></tr></thead>
          <tbody>${rows}</tbody></table>`
          : `<div class="empty"><b>还没有客户端</b><br />每个客户端 = 一份独立克隆卷 + 一个独立 iSCSI 目标，互不影响。</div>`}
      </div>
    </div>
    <div class="banner warn" style="margin-top:16px">
      同一份 NTFS 卷<b>严禁</b>被两台电脑同时挂载。克隆出来的每份卷序列号相同但数据独立，请只在自己的机器上挂载。
    </div>`;
}

/* ---------------------------------------------------------------- 会话 */
function viewSessions() {
  const rows = (S.sessions || []).map((s) => `<tr>
    <td class="mono">${esc(s.session)}</td>
    <td class="mono">${esc(s.target)}</td>
    <td class="mono">${esc(s.tpgt || '-')}</td>
    <td><span class="pill ${s.online ? 'ok' : 'off'}"><i class="dot"></i>${esc(s.state || '未知')}</span></td>
  </tr>`).join('');
  return `<div class="card">
    <div class="card-head"><h3>iSCSI 会话</h3><div class="spacer"></div>
      <span class="mono" style="color:var(--muted-2)">来自 /sys/class/iscsi_session</span></div>
    <div class="card-body tight">
      ${rows ? `<table><thead><tr><th>会话</th><th>目标</th><th>TPGT</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table>`
             : `<div class="empty"><b>当前没有 iSCSI 会话</b><br />客户端连上后会在这里出现。</div>`}
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 日志 */
function viewLogs() {
  const rows = (S.logs || []).map((l) => `<tr>
    <td class="mono" style="white-space:nowrap">${esc(l.ts)}</td>
    <td>${esc(l.action)}</td>
    <td class="mono">${esc(l.target || '-')}</td>
    <td class="mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(l.detail || '')}">${esc(l.detail || '-')}</td>
    <td><span class="pill ${l.result === 'ok' ? 'ok' : 'bad'}">${esc(l.result)}</span></td>
  </tr>`).join('');
  return `<div class="card">
    <div class="card-head"><h3>操作日志</h3><div class="spacer"></div>
      <button class="btn sm ghost" id="refresh-logs">刷新</button></div>
    <div class="card-body tight">
      ${rows ? `<table><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>详情</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table>`
             : `<div class="empty">暂无记录</div>`}
    </div></div>`;
}

/* ---------------------------------------------------------------- 设置 */
function viewSettings() {
  const st = S.overview.settings;
  const root = st.root_dataset || '';
  const poolPart = root.includes('/') ? root.split('/')[0] : '';
  const dsPart = root.includes('/') ? root.split('/').slice(1).join('/') : '';
  return `
  <div class="grid c2">
    <div class="card">
      <div class="card-head"><h3>存储位置</h3></div>
      <div class="card-body">
        <div class="field">
          <label>存储池</label>
          <select id="s-pool">
            <option value="">— 选择 —</option>
            ${S.pools.map((p) => `<option value="${esc(p.name)}" ${p.name === poolPart ? 'selected' : ''}>${esc(p.name)} (${fmtBytes(p.free)} 可用)</option>`).join('')}
          </select>
        </div>
        <div class="field">
          <label>数据集名称</label>
          <input id="s-ds" value="${esc(dsPart || 'steamzfs')}" placeholder="steamzfs" />
          <div class="hint">母版与克隆都放在 <span class="mono">&lt;池&gt;/&lt;数据集&gt;/masters|clients</span> 下。切换前需先清空旧数据集下的卷。</div>
        </div>
        <button class="btn primary" id="save-storage">保存存储设置</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>iSCSI 参数</h3></div>
      <div class="card-body">
        <div class="field">
          <label>IQN 前缀</label>
          <input id="s-iqn" value="${esc(st.iqn_prefix)}" placeholder="iqn.2026-11.com.fylun" />
          <div class="hint">最终目标名：<span class="mono">前缀:实例-客户端名</span></div>
        </div>
        <div class="field-row">
          <div class="field"><label>实例名</label><input id="s-inst" value="${esc(st.instance)}" /></div>
          <div class="field"><label>Portal 端口</label><input id="s-port" type="number" value="${esc(st.portal_port)}" /></div>
        </div>
        <div class="field">
          <label>监听地址</label>
          <input id="s-portal" value="${esc(st.portal)}" />
          <div class="hint">0.0.0.0 表示所有网卡；多网卡可按需限定。</div>
        </div>
        <button class="btn primary" id="save-iscsi">保存 iSCSI 设置</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h3>运行环境诊断</h3><div class="spacer"></div>
      <button class="btn sm ghost" id="run-diag">运行检测</button></div>
    <div class="card-body" id="diag-box">
      <div class="hint">检查容器是否具备操作宿主机 ZFS 与 LIO 的权限。</div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h3>维护</h3></div>
    <div class="card-body" style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn" id="do-saveconfig">持久化 target 配置</button>
      <span class="hint" style="margin:0;align-self:center">持久化会把当前 LUN 拓扑写入 /etc/target/saveconfig.json，供宿主重启后恢复。</span>
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 事件绑定 */
function bindCommon() {
  $$('[data-m]').forEach((b) => b.onclick = () => masterAction(b.dataset.m, b.dataset.name, b));
  $$('[data-c]').forEach((b) => b.onclick = () => clientAction(b.dataset.c, b.dataset.name, b));
  const nm = $('#new-master'); if (nm) nm.onclick = dlgNewMaster;
  const nc = $('#new-client'); if (nc) nc.onclick = dlgNewClient;
  const rl = $('#refresh-logs'); if (rl) rl.onclick = () => guard(async () => { await loadData(); }, rl, '刷新');
}

function bindSettings() {
  const saveStorage = $('#save-storage');
  if (saveStorage) saveStorage.onclick = () => guard(async () => {
    const pool = $('#s-pool').value;
    const ds = $('#s-ds').value.trim() || 'steamzfs';
    if (!pool) throw new Error('请选择存储池');
    await api('/api/settings', { method: 'POST', body: { root_dataset: `${pool}/${ds}` } });
    toast('存储设置已保存');
    await loadData();
  }, saveStorage, '保存');

  const saveIscsi = $('#save-iscsi');
  if (saveIscsi) saveIscsi.onclick = () => guard(async () => {
    await api('/api/settings', {
      method: 'POST',
      body: {
        iqn_prefix: $('#s-iqn').value.trim(),
        instance: $('#s-inst').value.trim(),
        portal: $('#s-portal').value.trim(),
        portal_port: Number($('#s-port').value),
      },
    });
    toast('iSCSI 参数已保存');
    await loadData();
  }, saveIscsi, '保存');

  const diag = $('#run-diag');
  if (diag) diag.onclick = () => guard(async () => {
    const d = await api('/api/diagnostics');
    $('#diag-box').innerHTML = `
      <div class="kv">
        <dt>zfs 命令</dt><dd>${d.zfs ? '<span class="pill ok">可用</span>' : '<span class="pill bad">不可用</span>'} <span class="mono">${esc((d.details.zfs || []).join(' / '))}</span></dd>
        <dt>/dev/zfs</dt><dd>${d.dev_zfs ? '<span class="pill ok">存在</span>' : '<span class="pill bad">缺失</span>'}</dd>
        <dt>configfs</dt><dd>${d.configfs ? '<span class="pill ok">已挂载</span>' : '<span class="pill bad">未挂载</span>'}</dd>
        <dt>targetcli</dt><dd>${d.targetcli ? '<span class="pill ok">可用</span>' : '<span class="pill bad">不可用</span>'}</dd>
        <dt>存储池</dt><dd class="mono">${esc((d.details.pools || []).join(' , ') || '-')}</dd>
      </div>`;
  }, diag, '检测');

  const sc = $('#do-saveconfig');
  if (sc) sc.onclick = () => guard(async () => {
    await api('/api/saveconfig', { method: 'POST' });
    toast('当前 target 配置已持久化');
  }, sc, '写入');
}

/* ---------------------------------------------------------------- 母版操作 */
function dlgNewMaster() {
  if (!S.overview.settings.root_dataset) { toast('请先在「设置」里指定存储池与数据集', 'err'); go('settings'); return; }
  const { close } = modal({
    title: '新建母版卷',
    body: `
      <div class="field"><label>名称（小写字母/数字/短横线）</label>
        <input id="m-name" placeholder="steam-lib" autocomplete="off" />
        <div class="hint">最终路径：母版数据集/<b>名称</b></div></div>
      <div class="field-row">
        <div class="field"><label>容量 (GB)</label><input id="m-size" type="number" value="500" min="1" /></div>
        <div class="field"><label>volblocksize</label>
          <select id="m-bs">
            <option>16K</option><option>8K</option><option>32K</option><option>64K</option><option>128K</option>
          </select></div>
      </div>
      <div class="field"><label>压缩</label>
        <select id="m-comp"><option>lz4</option><option>zstd</option><option>zstd-3</option><option>off</option></select>
        <div class="hint">Steam 库多为已压缩资源，lz4 开销最低；机械盘建议 16K 块。</div></div>
      <div class="field"><label>备注（可选）</label><input id="m-note" placeholder="例如：主 Steam 库 500G" /></div>`,
    footer: `<button class="btn" data-act="cancel">取消</button>
      <button class="btn primary" data-act="ok">创建</button>`,
    onMount(root) {
      root.querySelector('[data-act=cancel]').onclick = close;
      const ok = root.querySelector('[data-act=ok]');
      ok.onclick = () => guard(async () => {
        const name = root.querySelector('#m-name').value.trim();
        const size = Number(root.querySelector('#m-size').value);
        if (!name) throw new Error('请填写名称');
        if (!size || size < 1) throw new Error('容量至少 1 GB');
        busyBtn(ok, true, '创建中');
        try {
          await api('/api/masters', {
            method: 'POST',
            body: {
              name, size_gb: size,
              volblocksize: root.querySelector('#m-bs').value,
              compression: root.querySelector('#m-comp').value,
              note: root.querySelector('#m-note').value.trim(),
            },
          });
          toast('母版已创建');
          close();
          await loadData();
        } finally { busyBtn(ok, false); }
      }, ok, '创建中');
    },
  });
}

async function masterAction(act, name, btn) {
  if (act === 'snap') {
    const { close } = modal({
      title: `为 ${name} 打快照`,
      body: `<div class="banner warn" style="margin-bottom:14px">打快照前请确认该卷已在 Windows 中「安全弹出/断开」，否则文件系统可能不一致。</div>
        <div class="field"><label>快照名（留空自动按时间生成）</label><input id="s-name" placeholder="baseline-20261107" /></div>`,
      footer: `<button class="btn" data-act="c">取消</button><button class="btn primary" data-act="o">创建快照</button>`,
      onMount(root) {
        root.querySelector('[data-act=c]').onclick = close;
        const ok = root.querySelector('[data-act=o]');
        ok.onclick = () => guard(async () => {
          busyBtn(ok, true);
          try {
            const r = await api(`/api/masters/${encodeURIComponent(name)}/snapshot`, {
              method: 'POST', body: { name: root.querySelector('#s-name').value.trim() },
            });
            toast('快照已创建：' + r.snapshot);
            close(); await loadData();
          } finally { busyBtn(ok, false); }
        }, ok);
      },
    });
    return;
  }
  if (act === 'mount') {
    const chap = await api('/api/chap');
    const { close } = modal({
      title: `挂载母版 ${name}`,
      body: `<div class="hint" style="margin-bottom:14px">临时把这台母版作为 LUN 挂给一台电脑，用于安装/更新 Steam 库。用完请断开。</div>
        <div class="field"><label>客户端 Initiator IQN（可留空，稍后补 ACL）</label>
          <input id="mt-ini" placeholder="iqn.1991-05.com.microsoft:my-pc" /></div>
        <div class="field-row">
          <div class="field"><label>CHAP 用户名（可选）</label><input id="mt-user" placeholder="steam" /></div>
          <div class="field"><label>CHAP 密码</label><input id="mt-pass" value="${esc(chap.password)}" /></div>
        </div>`,
      footer: `<button class="btn" data-act="c">取消</button><button class="btn primary" data-act="o">创建映射</button>`,
      onMount(root) {
        root.querySelector('[data-act=c]').onclick = close;
        const ok = root.querySelector('[data-act=o]');
        ok.onclick = () => guard(async () => {
          busyBtn(ok, true);
          try {
            const r = await api(`/api/masters/${encodeURIComponent(name)}/mount`, {
              method: 'POST', body: {
                initiator: root.querySelector('#mt-ini').value.trim(),
                chap_user: root.querySelector('#mt-user').value.trim(),
                chap_pass: root.querySelector('#mt-user').value.trim() ? root.querySelector('#mt-pass').value : '',
              },
            });
            close(); await loadData();
            guideDlg({ name: '母版 ' + name, target: r.target, initiator: root.querySelector('#mt-ini').value.trim(),
              chap_user: root.querySelector('#mt-user').value.trim(), chap_pass: root.querySelector('#mt-user').value.trim() ? root.querySelector('#mt-pass').value : '' });
          } finally { busyBtn(ok, false); }
        }, ok);
      },
    });
    return;
  }
  if (act === 'unmount') {
    const ok = await confirmDlg({ title: `断开母版 ${name}`, message: '将移除该母版的 iSCSI 映射（不影响卷数据）。请确认客户端已断开连接。', confirmText: '断开映射' });
    if (!ok) return;
    await guard(async () => {
      await api(`/api/masters/${encodeURIComponent(name)}/unmount`, { method: 'POST' });
      toast('已移除母版映射'); await loadData();
    }, btn);
    return;
  }
  if (act === 'snaps') {
    await guard(async () => {
      const snaps = await api(`/api/masters/${encodeURIComponent(name)}/snapshots`);
      modal({
        title: `${name} 的快照`,
        wide: true,
        body: snaps.length ? `<table><thead><tr><th>快照</th><th>占用</th><th>引用</th><th>时间</th><th></th></tr></thead><tbody>
          ${snaps.map((s) => `<tr>
            <td class="mono">${esc(s.name)}</td>
            <td>${fmtBytes(s.used)}</td><td>${fmtBytes(s.refer)}</td>
            <td class="mono">${new Date(s.creation * 1000).toLocaleString()}</td>
            <td style="text-align:right"><button class="btn sm danger" data-snap="${esc(s.name)}">删除</button></td>
          </tr>`).join('')}</tbody></table>`
          : `<div class="empty">还没有快照</div>`,
        onMount(root, close) {
          root.querySelectorAll('[data-snap]').forEach((b) => b.onclick = () => guard(async () => {
            const ok = await confirmDlg({ title: '删除快照', message: `确认删除快照 <b>${esc(b.dataset.snap)}</b>？被克隆引用的快照无法删除。`, danger: true, confirmText: '删除' });
            if (!ok) return;
            await api(`/api/masters/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(b.dataset.snap)}`, { method: 'DELETE' });
            toast('快照已删除'); close(); await loadData();
          }, b));
        },
      });
    }, btn);
    return;
  }
  if (act === 'del') {
    const ok = await confirmDlg({
      title: '删除母版',
      message: `将永久删除母版 <b>${esc(name)}</b> 及其全部快照。<br />若仍有客户端克隆依赖，需要先删除那些客户端。`,
      danger: true, confirmText: '永久删除', requireText: name,
    });
    if (!ok) return;
    await guard(async () => {
      await api(`/api/masters/${encodeURIComponent(name)}`, { method: 'DELETE' });
      toast('母版已删除'); await loadData();
    }, btn, '删除中');
  }
}

/* ---------------------------------------------------------------- 客户端操作 */
function dlgNewClient() {
  const masters = (S.scan && S.scan.masters) || [];
  if (!S.overview.settings.root_dataset) { toast('请先在「设置」里指定存储池与数据集', 'err'); go('settings'); return; }
  if (!masters.length) { toast('请先创建母版卷', 'err'); go('masters'); return; }
  const chap = { password: '' };
  api('/api/chap').then((r) => { chap.password = r.password; const el = $('#c-pass'); if (el && !el.value) el.value = r.password; }).catch(() => {});
  const { close } = modal({
    title: '新增客户端',
    wide: true,
    body: `
      <div class="field-row">
        <div class="field"><label>客户端名称</label><input id="c-name" placeholder="pc-study" autocomplete="off" />
          <div class="hint">仅小写字母/数字/短横线，会成为目标 IQN 后缀。</div></div>
        <div class="field"><label>来源母版</label>
          <select id="c-master">${masters.map((m) => `<option value="${esc(m.short)}">${esc(m.short)} (${fmtBytes(m.volsize)}, ${m.snapshots} 快照)</option>`).join('')}</select></div>
      </div>
      <div class="field"><label>客户端 Initiator IQN</label>
        <input id="c-ini" placeholder="iqn.1991-05.com.microsoft:desktop-abc" />
        <div class="hint">Windows 上运行 <span class="mono">iscsicpl</span> →「配置」页可看到本机 IQN；留空表示不限制来源（不建议）。</div></div>
      <div class="field-row">
        <div class="field"><label>CHAP 用户名（可选）</label><input id="c-user" placeholder="steam" /></div>
        <div class="field"><label>CHAP 密码</label><input id="c-pass" value="" /></div>
      </div>
      <div class="field"><label>备注（可选）</label><input id="c-note" placeholder="书房台式机" /></div>
      <div class="banner info" style="margin:0">创建时会自动从母版<b>最新快照</b>克隆；若还没有快照，会先打一个 baseline 快照。</div>`,
    footer: `<button class="btn" data-act="c">取消</button><button class="btn primary" data-act="o">创建并映射</button>`,
    onMount(root) {
      root.querySelector('[data-act=c]').onclick = close;
      const ok = root.querySelector('[data-act=o]');
      const nameEl = root.querySelector('#c-name');
      nameEl.addEventListener('input', () => {
        const ini = root.querySelector('#c-ini');
        if (!ini.dataset.touched) {
          ini.value = nameEl.value.trim() ? 'iqn.1991-05.com.microsoft:' + nameEl.value.trim() : '';
        }
      });
      root.querySelector('#c-ini').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });
      ok.onclick = () => guard(async () => {
        const name = nameEl.value.trim();
        if (!name) throw new Error('请填写客户端名称');
        busyBtn(ok, true, '克隆中');
        try {
          const user = root.querySelector('#c-user').value.trim();
          const r = await api('/api/clients', {
            method: 'POST', body: {
              name,
              master: root.querySelector('#c-master').value,
              initiator: root.querySelector('#c-ini').value.trim(),
              chap_user: user,
              chap_pass: user ? root.querySelector('#c-pass').value : '',
              note: root.querySelector('#c-note').value.trim(),
            },
          });
          toast('客户端已创建');
          close(); await loadData();
          guideDlg({ name, target: r.target, initiator: root.querySelector('#c-ini').value.trim(),
            chap_user: user, chap_pass: user ? root.querySelector('#c-pass').value : '' });
        } finally { busyBtn(ok, false); }
      }, ok, '克隆中');
    },
  });
}

async function clientAction(act, name, btn) {
  if (act === 'guide') {
    const rec = await api(`/api/clients/${encodeURIComponent(name)}/secret`);
    const c = ((S.scan || {}).clients || []).find((x) => x.short === name) || {};
    guideDlg({ name, target: rec.target || c.target, initiator: rec.initiator || c.initiator || '',
      chap_user: rec.chap_user, chap_pass: rec.chap_pass, size: c.volsize });
    return;
  }
  if (act === 'attach') {
    const rec = await api(`/api/clients/${encodeURIComponent(name)}/secret`);
    await guard(async () => {
      const r = await api(`/api/clients/${encodeURIComponent(name)}/attach`, {
        method: 'POST', body: {
          initiator: rec.initiator || '', chap_user: rec.chap_user || '', chap_pass: rec.chap_pass || '',
        },
      });
      toast('已重新映射到 ' + r.target);
      await loadData();
    }, btn, '映射中');
    return;
  }
  if (act === 'resize') {
    const cur = Number(btn.dataset.size || 0) / 1024 ** 3;
    const { close } = modal({
      title: `扩容 ${name}`,
      body: `<div class="field"><label>新容量 (GB)</label><input id="r-size" type="number" value="${Math.round(cur) + 50}" min="1" /></div>
        <div class="hint">超过 2TB 的 NTFS 需要 GPT 分区；扩容后需在 Windows 磁盘管理里「扩展卷」才能生效。</div>`,
      footer: `<button class="btn" data-act="c">取消</button><button class="btn primary" data-act="o">扩容</button>`,
      onMount(root) {
        root.querySelector('[data-act=c]').onclick = close;
        const ok = root.querySelector('[data-act=o]');
        ok.onclick = () => guard(async () => {
          busyBtn(ok, true);
          try {
            await api(`/api/clients/${encodeURIComponent(name)}/resize`, { method: 'POST', body: { size_gb: Number(root.querySelector('#r-size').value) } });
            toast('容量已调整'); close(); await loadData();
          } finally { busyBtn(ok, false); }
        }, ok);
      },
    });
    return;
  }
  if (act === 'reclone') {
    const ok = await confirmDlg({
      title: `重建 ${name}`,
      message: `将销毁该客户端现有克隆卷，并从母版最新快照重新克隆。<br /><b style="color:var(--bad)">该客户端里的存档/游戏改动会全部丢失</b>，其他客户端不受影响。`,
      danger: true, confirmText: '销毁并重建', requireText: name,
    });
    if (!ok) return;
    await guard(async () => {
      const r = await api(`/api/clients/${encodeURIComponent(name)}/reclone`, { method: 'POST', body: {} });
      toast('已重建，来源 ' + r.origin); await loadData();
    }, btn, '重建中');
    return;
  }
  if (act === 'del') {
    const ok = await confirmDlg({
      title: `删除客户端 ${name}`,
      message: '将移除 iSCSI 映射并永久删除该克隆卷（母版与其他客户端不受影响）。',
      danger: true, confirmText: '永久删除', requireText: name,
    });
    if (!ok) return;
    await guard(async () => {
      await api(`/api/clients/${encodeURIComponent(name)}?purge=true`, { method: 'DELETE' });
      toast('客户端已删除'); await loadData();
    }, btn, '删除中');
  }
}

/* ---------------------------------------------------------------- 连接指引 */
function guideDlg({ name, target, initiator, chap_user, chap_pass, size }) {
  const ip = location.hostname;
  const portal = `${ip}:${(S.overview.settings.portal_port || 3260)}`;
  modal({
    title: `连接指引 · ${name}`,
    wide: true,
    body: `
      <div class="kv" style="margin-bottom:16px">
        <dt>目标门户</dt><dd>${esc(portal)}</dd>
        <dt>目标 IQN</dt><dd>${esc(target || '（尚未映射）')}</dd>
        <dt>发起程序</dt><dd>${esc(initiator || '（未限制，任意发起程序可连）')}</dd>
        <dt>CHAP</dt><dd>${chap_user ? esc(chap_user) + ' / ' + esc(chap_pass) : '未启用'}</dd>
        ${size ? `<dt>卷容量</dt><dd>${fmtBytes(size)}</dd>` : ''}
      </div>
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">Windows 端步骤</div>
      <ol class="steps">
        <li>Win+R 输入 <b>iscsicpl</b> 打开「iSCSI 发起程序」，首次会提示启动服务，选「是」。</li>
        <li>「目标」输入 <b class="mono">${esc(portal)}</b>，点「快速连接」。</li>
        <li>「发现」→「发现门户」→ 添加 <b class="mono">${esc(ip)}</b> 端口 <b>${esc(String(S.overview.settings.portal_port || 3260))}</b>。</li>
        ${chap_user ? `<li>「目标」→「连接」→「高级」→ 勾选 <b>启用 CHAP 登录</b>，用户名 <b class="mono">${esc(chap_user)}</b>，密码 <b class="mono">${esc(chap_pass)}</b>。</li>` : ''}
        <li>连接成功后打开「磁盘管理」，对新磁盘<b>联机 → 初始化（GPT）→ 新建简单卷</b>，盘符建议固定。</li>
        <li>在 Steam「设置 → 存储」中添加该盘符的 SteamLibrary 目录。</li>
      </ol>
      <div class="banner warn" style="margin-top:16px;margin-bottom:0">
        关机前请先在 Steam 退出、并在「iSCSI 发起程序」里<b>断开</b>该目标，避免下次克隆出现脏文件系统。
      </div>`,
  });
}

/* ---------------------------------------------------------------- 启动 */
function showApp() {
  $('#shell').hidden = false;
  const v = (location.hash || '#overview').slice(1);
  go(TITLES[v] ? v : 'overview');
  loadData().catch((e) => toast(e.message, 'err'));
}

$('#refresh').onclick = (e) => guard(async () => { await loadData(); toast('已刷新'); }, e.currentTarget, '刷新');

$$('.nav-item').forEach((n) => n.addEventListener('click', () => go(n.dataset.view)));
window.addEventListener('hashchange', () => {
  const v = location.hash.slice(1);
  if (TITLES[v] && v !== S.view) go(v);
});

/* 无登录：脚本加载即进入主界面 */
showApp();
