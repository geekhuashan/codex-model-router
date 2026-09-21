"""Self-contained read-only dashboard; no CDN, credentials or conversation content."""
HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex · 请求来源</title><style>
body{font:15px system-ui;background:#101820;color:#e6edf3;margin:32px}h1{font-size:25px}
p{color:#aab7c4}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:12px;border-bottom:1px solid #30404e}th{color:#8dd8d2}#state{color:#8dd8d2}small{color:#aab7c4}.scroll{overflow:auto}
</style><h1>Codex 请求来源</h1><p id="state">正在连接本机路由…</p>
<p>显示请求实际转发的来源和上游主机。HTTP 200 不等于生成完成。Token 为上游报告值，不代表余额或账单。</p>
<div class="scroll"><table><thead><tr><th>时间</th><th>来源</th><th>模型</th><th>上游</th><th>操作</th><th>HTTP</th><th>生成结果</th><th>输入 / 输出 / 缓存 Token</th></tr></thead><tbody id="rows"></tbody></table></div>
<p><small>每 3 秒刷新；仅显示本进程最近 100 次请求。重启前的记录保存在本机 activity.log。下游账号池具体用了哪个账号，由网关决定，本页无法确认。</small></p>
<script>
const labels={in_progress:'进行中',completed:'已完成',compacted:'已压缩',incomplete:'不完整',failed:'生成失败',unconfirmed:'未确认完成',http_error:'HTTP 错误',connection_error:'连接中断',cancelled:'请求取消'};
async function refresh(){try{const r=await fetch('health',{cache:'no-store'});if(!r.ok)throw Error();const d=await r.json();
document.getElementById('state').textContent='路由在线 · 累计 '+d.requests+' 次请求 · 当前 '+d.monitor.active.length+' 次进行中';
const rows=document.getElementById('rows');rows.replaceChildren();
for(const e of [...d.monitor.active,...d.monitor.recent]){const tr=document.createElement('tr');const u=e.usage||{};
for(const value of [e.time,e.source,e.model,e.upstream_host,e.operation,e.status??'—',labels[e.outcome]||e.outcome,[u.input_tokens??'?',u.output_tokens??'?',u.cached_tokens??'?'].join(' / ')]){const td=document.createElement('td');td.textContent=value;tr.append(td)}rows.append(tr)}
}catch(e){document.getElementById('state').textContent='路由连接失败，请检查服务是否运行'}setTimeout(refresh,3000)}refresh();
</script></html>'''
