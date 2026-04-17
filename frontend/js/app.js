/* app.js — Main application logic: WebSocket, API calls, UI updates */

const BASE = '';  // relative to current origin+path since proxied
let ws = null;
let reconnectTimer = null;
let currentJobId = null;

// ---------- WebSocket ----------

function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Build WS URL: same host, same path prefix + /ws
    const pathBase = location.pathname.replace(/\/+$/, '');
    const wsUrl = `${proto}//${location.host}${pathBase}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        document.getElementById('statusDot').classList.add('online');
        document.getElementById('statusText').textContent = '已连接';
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };

    ws.onmessage = (evt) => {
        try {
            const event = JSON.parse(evt.data);
            handleEvent(event);
        } catch (e) { /* ignore */ }
    };

    ws.onclose = () => {
        document.getElementById('statusDot').classList.remove('online');
        document.getElementById('statusText').textContent = '已断开 — 重连中...';
        reconnectTimer = setTimeout(connectWS, 3000);
    };

    ws.onerror = () => ws.close();
}

// ---------- Event handler ----------

function handleEvent(evt) {
    // Log
    appendLog(evt);

    const type = evt.event_type || '';
    const data = evt.data || {};

    // Pipeline stage highlighting
    if (type.startsWith('discover')) updatePipelineStage('crawl');
    if (type.startsWith('download')) updatePipelineStage('download');
    if (type.startsWith('clean')) updatePipelineStage('clean');
    if (type === 'crawl_completed' || type === 'crawl_error') updatePipelineDone();

    // Update counts
    if (data.count !== undefined && type === 'discover_done') {
        updatePipelineCounts({ discovered: data.count });
        document.getElementById('statDiscovered').textContent = data.count;
    }
    if (type === 'download_done') {
        const cur = parseInt(document.getElementById('statDownloaded').textContent) || 0;
        const next = cur + 1;
        document.getElementById('statDownloaded').textContent = next;
        updatePipelineCounts({ downloaded: next });
    }
    if (type === 'clean_passed') {
        const cur = parseInt(document.getElementById('statCleaned').textContent) || 0;
        const next = cur + 1;
        document.getElementById('statCleaned').textContent = next;
        updatePipelineCounts({ cleaned: next });
    }
    if (type === 'clean_rejected' || type === 'download_failed') {
        const cur = parseInt(document.getElementById('statRejected').textContent) || 0;
        const next = cur + 1;
        document.getElementById('statRejected').textContent = next;
        updatePipelineCounts({ rejected: next });
    }

    // On completion, refresh data
    if (type === 'crawl_completed' || type === 'crawl_error' || type === 'crawl_stopped') {
        _resetButtons();
        setTimeout(() => {
            refreshGallery();
            refreshDirty();
            refreshStats();
        }, 500);
    }
}

// ---------- Log ----------

function appendLog(evt) {
    const container = document.getElementById('logContainer');
    if (!container) return;

    const line = document.createElement('div');
    line.className = 'log-line';

    const ts = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString('zh-CN') : '--:--:--';
    const stage = evt.stage || 'info';

    line.innerHTML = `
        <span class="log-time">${ts}</span>
        <span class="log-stage ${stage}">${stage}</span>
        <span class="log-msg">${escapeHtml(evt.message || '')}</span>
    `;
    container.appendChild(line);
    container.scrollTop = container.scrollHeight;
}

// ---------- API Calls ----------

async function apiFetch(path) {
    const pathBase = location.pathname.replace(/\/+$/, '');
    const resp = await fetch(`${pathBase}${path}`);
    if (!resp.ok) throw new Error(`API ${resp.status}`);
    return resp.json();
}

async function startCrawl() {
    const btnStart = document.getElementById('btnStart');
    const btnStop = document.getElementById('btnStop');
    btnStart.disabled = true;
    btnStart.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="12 6"><animateTransform attributeName="transform" type="rotate" from="0 8 8" to="360 8 8" dur="1s" repeatCount="indefinite"/></circle></svg> 运行中...';
    btnStop.style.display = 'flex';

    // Reset counters
    ['statDiscovered','statDownloaded','statCleaned','statRejected'].forEach(id => {
        document.getElementById(id).textContent = '0';
    });

    const body = {
        source: document.getElementById('crawlSource').value,
        query: document.getElementById('crawlQuery').value.trim(),
        limit: parseInt(document.getElementById('crawlLimit').value) || 6,
    };

    try {
        const resp = await fetch(`${location.pathname.replace(/\/+$/, '')}/api/crawl/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        currentJobId = data.job_id;
        appendLog({ stage: 'info', message: `任务已提交: ${data.job_id}`, timestamp: new Date().toISOString() });
    } catch (e) {
        appendLog({ stage: 'error', message: `启动失败: ${e.message}`, timestamp: new Date().toISOString() });
        _resetButtons();
    }
}

async function stopCrawl() {
    if (!currentJobId) return;
    try {
        await fetch(`${location.pathname.replace(/\/+$/, '')}/api/crawl/stop/${currentJobId}`, { method: 'POST' });
        appendLog({ stage: 'info', message: `已发送停止请求: ${currentJobId}`, timestamp: new Date().toISOString() });
    } catch (e) {
        appendLog({ stage: 'error', message: `停止失败: ${e.message}`, timestamp: new Date().toISOString() });
    }
}

function _resetButtons() {
    const btnStart = document.getElementById('btnStart');
    const btnStop = document.getElementById('btnStop');
    btnStart.disabled = false;
    btnStart.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><polygon points="4,2 14,8 4,14"/></svg> 启动管线';
    btnStop.style.display = 'none';
    currentJobId = null;
}

// ---------- Gallery ----------

async function refreshGallery() {
    try {
        const models = await apiFetch('/api/models');
        const grid = document.getElementById('galleryGrid');
        if (!models.length) {
            grid.innerHTML = '<p class="empty-hint">暂无模型，请先启动管线爬取</p>';
            return;
        }
        grid.innerHTML = '';
        models.forEach(m => {
            const card = document.createElement('div');
            card.className = 'model-card';
            card.onclick = () => openViewer(m);
            card.innerHTML = `
                <div class="model-card-icon">🧣</div>
                <div class="model-card-name" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</div>
                <div class="model-card-meta">
                    <div>🔺 面片: <span>${(m.face_count||0).toLocaleString()}</span></div>
                    <div>📐 顶点: <span>${(m.vertex_count||0).toLocaleString()}</span></div>
                    <div>📦 大小: <span>${((m.file_size||0)/1024).toFixed(1)} KB</span></div>
                    <div>🌐 来源: <span>${m.source||'—'}</span></div>
                    <div>💧 水密: <span>${m.is_watertight ? '✓' : '✗'}</span> · 🧩 流形: <span>${m.is_manifold ? '✓' : '✗'}</span></div>
                </div>
            `;
            grid.appendChild(card);
        });
    } catch (e) {
        console.error('Gallery refresh failed:', e);
    }
}

// ---------- Dirty data ----------

async function refreshDirty() {
    try {
        const dirtyList = await apiFetch('/api/dirty');
        const tbody = document.getElementById('dirtyBody');
        if (!dirtyList.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty-hint">暂无脏数据记录</td></tr>';
            return;
        }
        tbody.innerHTML = '';
        dirtyList.forEach(d => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${escapeHtml(d.name||'—')}</td>
                <td>${d.source||'—'}</td>
                <td><span class="reason-tag">${escapeHtml(d.reason_zh || d.reason || '—')}</span></td>
                <td>${escapeHtml(d.reason_detail||'—')}</td>
                <td>${d.detected_at ? new Date(d.detected_at).toLocaleString('zh-CN') : '—'}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Dirty refresh failed:', e);
    }
}

// ---------- Stats ----------

async function refreshStats() {
    try {
        const s = await apiFetch('/api/stats');
        document.getElementById('statDiscovered').textContent = s.total_discovered;
        document.getElementById('statDownloaded').textContent = s.total_downloaded;
        document.getElementById('statCleaned').textContent = s.total_cleaned;
        document.getElementById('statRejected').textContent = s.total_rejected;

        // Breakdown
        const bd = s.rejection_breakdown || {};
        const container = document.getElementById('breakdownContainer');
        const bars = document.getElementById('breakdownBars');
        if (Object.keys(bd).length) {
            container.style.display = 'block';
            const maxVal = Math.max(...Object.values(bd), 1);
            bars.innerHTML = '';
            for (const [reason, count] of Object.entries(bd)) {
                const pct = (count / maxVal) * 100;
                bars.innerHTML += `
                    <div class="breakdown-bar-row">
                        <span class="breakdown-bar-label">${escapeHtml(reason)}</span>
                        <div class="breakdown-bar"><div class="breakdown-bar-fill" style="width:${pct}%"></div></div>
                        <span class="breakdown-bar-count">${count}</span>
                    </div>
                `;
            }
        }
    } catch (e) {
        console.error('Stats refresh failed:', e);
    }
}

// ---------- Viewer ----------

function openViewer(modelInfo) {
    const modal = document.getElementById('viewerModal');
    modal.style.display = 'flex';
    document.getElementById('viewerTitle').textContent = modelInfo.name || '模型预览';

    const pathBase = location.pathname.replace(/\/+$/, '');
    const modelUrl = `${pathBase}/api/models/${modelInfo.id}/file`;
    // Call the Three.js loader from viewer.js (attached to window)
    if (window.loadModel) window.loadModel(modelUrl, modelInfo);
}

function closeViewer() {
    document.getElementById('viewerModal').style.display = 'none';
    if (window.destroyViewer) window.destroyViewer();
}
// Expose globally
window.startCrawl = startCrawl;
window.stopCrawl = stopCrawl;
window.closeViewer = closeViewer;

// ---------- Utilities ----------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ---------- Init ----------

document.addEventListener('DOMContentLoaded', () => {
    connectWS();
    refreshGallery();
    refreshDirty();
    refreshStats();
});
