// ==================== Global State ====================
let ws = null;
let currentLogs = {};
let activeProcesses = new Map(); // Map<process_id, {script, pid, started_at}>
let selectedLogProcess = '';
let autoScrollEnabled = true;
let reconnectAttempts = 0;
let currentZoom = 1.0;
let pendingAction = null; // For modal confirmation
const MAX_RECONNECT = 5;
const SCRIPTS = ['watchdog', 'fleet-live', 'autobuy', 'dashboard'];

// ==================== Zoom ====================
function changeZoom(delta) {
    currentZoom = Math.max(0.5, Math.min(1.5, currentZoom + delta));
    document.getElementById('zoom-wrapper').style.transform = `scale(${currentZoom})`;
    document.getElementById('zoom-level').textContent = `${Math.round(currentZoom * 100)}%`;
}

// ==================== WebSocket ====================
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        updateConnectionStatus(true);
        reconnectAttempts = 0;
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
    };
    
    ws.onclose = () => {
        updateConnectionStatus(false);
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            setTimeout(connectWebSocket, 2000 * reconnectAttempts);
        }
    };
}

function handleWebSocketMessage(data) {
    switch (data.type) {
        case 'connected':
            showToast('Connected to server', 'success');
            break;
        case 'log':
            addLogEntry(data.process_id, data.timestamp, data.message);
            break;
        case 'process_ended':
            showToast(`${data.process} stopped`, 'info');
            activeProcesses.delete(data.process_id);
            updateAllBotStatuses();
            break;
        case 'process_status':
            if (data.status.running) {
                activeProcesses.set(data.process_id, {
                    script: data.status.script,
                    pid: data.status.pid,
                    started_at: data.status.started_at
                });
                updateBotStatus(data.status.script, true);
                addLogProcessOption(data.process_id, data.status.script);
            }
            break;
    }
}

function updateConnectionStatus(connected) {
    const badge = document.getElementById('connection-status');
    badge.textContent = connected ? 'Connected' : 'Disconnected';
    badge.className = `status-badge ${connected ? 'connected' : 'disconnected'}`;
}

// ==================== API Functions ====================
async function apiCall(endpoint, options = {}) {
    try {
        const response = await fetch(endpoint, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (error) {
        showToast(error.message, 'error');
        throw error;
    }
}

async function startScript(scriptName) {
    try {
        const result = await apiCall(`/api/run/${scriptName}`, { method: 'POST' });
        if (result.status === 'started') {
            showToast(`${scriptName} started`, 'success');
            activeProcesses.set(result.process_id, { script: scriptName, pid: result.pid, started_at: new Date().toISOString() });
            updateBotStatus(scriptName, true);
            addLogProcessOption(result.process_id, scriptName);
        } else if (result.status === 'already_running') {
            showToast(`${scriptName} already running`, 'info');
        }
    } catch (error) {
        showToast(`Failed to start ${scriptName}`, 'error');
    }
}

async function stopScript(scriptName) {
    const processId = findProcessIdByScript(scriptName);
    if (!processId) {
        showToast(`${scriptName} not running`, 'error');
        return;
    }
    try {
        await apiCall(`/api/stop/${processId}`, { method: 'POST' });
        showToast(`${scriptName} stopped`, 'info');
        activeProcesses.delete(processId);
        updateBotStatus(scriptName, false);
    } catch (error) {
        showToast(`Failed to stop ${scriptName}`, 'error');
    }
}

async function killInstance(instanceId, gpu, dph) {
    // Show confirmation modal
    showConfirmModal({
        title: '⚠️ Destroy Server?',
        message: 'Are you sure you want to DESTROY this server?',
        details: [
            { label: 'Instance ID:', value: instanceId },
            { label: 'GPU:', value: gpu },
            { label: 'Cost:', value: `$${dph}/hr` }
        ],
        confirmText: 'DESTROY',
        onConfirm: async () => {
            try {
                await apiCall(`/api/instances/${instanceId}/destroy`, { method: 'POST' });
                showToast(`Instance ${instanceId} destroyed`, 'info');
                loadInstances();
            } catch (error) {
                showToast(`Failed to destroy instance`, 'error');
            }
        }
    });
}

async function removeFromBlacklist(machineId) {
    showConfirmModal({
        title: 'Remove from Blacklist?',
        message: `Remove machine ${machineId} from blacklist?`,
        details: [{ label: 'Machine ID:', value: machineId }],
        confirmText: 'Remove',
        onConfirm: async () => {
            try {
                await apiCall(`/api/blacklist/${machineId}`, { method: 'DELETE' });
                showToast('Removed from blacklist', 'success');
                loadBlacklist();
            } catch (error) {
                showToast('Failed to remove', 'error');
            }
        }
    });
}

// ==================== Data Loading ====================
async function loadInstances() {
    try {
        const data = await apiCall('/api/data/instances');
        
        // Check if backend returned an error
        if (data && data.error) {
            console.error('Backend error:', data);
            showToast(`Instances error: ${data.error}`, 'error');
            // Render empty table with error message
            const tbody = document.getElementById('dash-instances-tbody');
            if (tbody) {
                tbody.innerHTML = `<tr><td colspan="8" class="empty">$ ERROR: ${data.error} | stderr: ${data.stderr?.substring(0, 50) || 'unknown'}</td></tr>`;
            }
            return;
        }
        
        // Ensure data is an array
        const instances = Array.isArray(data) ? data : [];
        renderInstancesTable(instances);
        
        // Update stats for new terminal UI
        const running = instances.filter(i => i.actual_status === 'running');
        activeInstancesCount = running.length;
        
        // Update dashboard metrics
        const dashInstances = document.getElementById('dash-instances');
        const dashCostHour = document.getElementById('dash-cost-hour');
        const dashCostDay = document.getElementById('dash-cost-day');
        
        if (dashInstances) dashInstances.textContent = running.length;
        
        const totalCost = running.reduce((sum, i) => sum + (i.dph_total || 0), 0);
        if (dashCostHour) dashCostHour.textContent = `$${totalCost.toFixed(3)}`;
        if (dashCostDay) dashCostDay.textContent = `$${(totalCost * 24).toFixed(2)}`;
        
    } catch (error) {
        showToast(`Failed to load instances: ${error.message}`, 'error');
        console.error('loadInstances error:', error);
        // Show error in table
        const tbody = document.getElementById('dash-instances-tbody');
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="8" class="empty">$ CONNECTION_ERROR: ${error.message}</td></tr>`;
        }
    }
}

async function loadBlacklist() {
    try {
        const blacklist = await apiCall('/api/data/blacklist');
        renderBlacklistTable(blacklist);
        
        // Update dashboard blacklist count
        const dashBlacklist = document.getElementById('dash-blacklist');
        if (dashBlacklist) dashBlacklist.textContent = Object.keys(blacklist).length;
    } catch (error) {
        showToast(`Failed to load blacklist: ${error.message}`, 'error');
        console.error('loadBlacklist error:', error);
    }
}

async function loadProcessLogs(processId) {
    if (!processId) return;
    try {
        const result = await apiCall(`/api/logs/${processId}?lines=200`);
        currentLogs[processId] = result.logs || [];
        if (selectedLogProcess === processId) renderLogs();
    } catch (error) {
        console.error('Failed to load logs:', error);
    }
}

// ==================== UI Updates ====================
function findProcessIdByScript(scriptName) {
    for (const [pid, info] of activeProcesses) {
        if (info.script === scriptName) return pid;
    }
    return null;
}

function updateBotStatus(scriptName, running) {
    const dot = document.getElementById(`status-${scriptName}`);
    const text = document.getElementById(`text-${scriptName}`);
    const startBtn = document.getElementById(`btn-start-${scriptName}`);
    const stopBtn = document.getElementById(`btn-stop-${scriptName}`);
    
    if (!dot || !text || !startBtn || !stopBtn) return;
    
    dot.className = `status-dot ${running ? 'running' : ''}`;
    text.textContent = running ? 'Running' : 'Stopped';
    startBtn.disabled = running;
    stopBtn.disabled = !running;
}

function updateAllBotStatuses() {
    SCRIPTS.forEach(script => {
        const running = Array.from(activeProcesses.values()).some(p => p.script === script);
        updateBotStatus(script, running);
    });
}

function addLogProcessOption(processId, scriptName) {
    const select = document.getElementById('log-process-select');
    if (!select) return;
    const existing = select.querySelector(`option[value="${processId}"]`);
    if (existing) return;
    
    const option = document.createElement('option');
    option.value = processId;
    option.textContent = `${scriptName} (${processId.split('_')[1] || 'new'})`;
    select.appendChild(option);
}

// ==================== Rendering ====================
function addLogEntry(processId, timestamp, message) {
    if (!currentLogs[processId]) currentLogs[processId] = [];
    currentLogs[processId].push(`[${timestamp}] ${message}`);
    if (currentLogs[processId].length > 500) currentLogs[processId] = currentLogs[processId].slice(-500);
    
    if (selectedLogProcess === processId) {
        const container = document.getElementById('logs-container');
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.innerHTML = `<span class="log-timestamp">[${escapeHtml(timestamp)}]</span>${escapeHtml(message)}`;
        container.appendChild(entry);
        if (autoScrollEnabled) container.scrollTop = container.scrollHeight;
    }
}

function renderLogs() {
    const container = document.getElementById('logs-container');
    if (!container) return;
    const logs = currentLogs[selectedLogProcess] || [];
    
    if (logs.length === 0) {
        container.innerHTML = `
╔════════════════════════════════════════════════════════╗
║  NO_LOGS_AVAILABLE                                     ║
║  Select a process from dropdown to view live stream    ║
╚════════════════════════════════════════════════════════╝`;
        return;
    }
    
    container.innerHTML = logs.map(line => {
        const match = line.match(/^\[([^\]]+)\](.*)$/);
        if (match) {
            return `<div class="log-entry"><span class="timestamp">[${escapeHtml(match[1])}]</span>${escapeHtml(match[2])}</div>`;
        }
        return `<div class="log-entry">${escapeHtml(line)}</div>`;
    }).join('');
    
    if (autoScrollEnabled) container.scrollTop = container.scrollHeight;
}

// ==================== Modal ====================
function showConfirmModal({ title, message, details = [], confirmText, onConfirm }) {
    const modal = document.getElementById('confirm-modal');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-message').textContent = message;
    document.getElementById('modal-confirm-btn').textContent = confirmText;
    
    const detailsDiv = document.getElementById('modal-details');
    if (details.length > 0) {
        detailsDiv.innerHTML = details.map(d => `
            <div class="detail-row">
                <span class="detail-label">${d.label}</span>
                <span class="detail-value">${d.value}</span>
            </div>
        `).join('');
        detailsDiv.style.display = 'block';
    } else {
        detailsDiv.style.display = 'none';
    }
    
    pendingAction = onConfirm;
    modal.classList.add('active');
}

function closeModal() {
    document.getElementById('confirm-modal').classList.remove('active');
    pendingAction = null;
}

function confirmModalAction() {
    if (pendingAction) {
        pendingAction();
        closeModal();
    }
}

// ==================== Utilities ====================
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

let uptimeSeconds = 0;

function updateTime() {
    const el = document.getElementById('system-time');
    if (el) el.textContent = new Date().toLocaleTimeString();
    
    uptimeSeconds++;
    const uptimeEl = document.getElementById('uptime');
    if (uptimeEl) {
        const h = Math.floor(uptimeSeconds / 3600).toString().padStart(2, '0');
        const m = Math.floor((uptimeSeconds % 3600) / 60).toString().padStart(2, '0');
        const s = (uptimeSeconds % 60).toString().padStart(2, '0');
        uptimeEl.textContent = `${h}:${m}:${s}`;
    }
}

function clearLogs() {
    if (selectedLogProcess && currentLogs[selectedLogProcess]) {
        currentLogs[selectedLogProcess] = [];
        renderLogs();
        showToast('Logs cleared', 'info');
    }
}

function onLogSelectChange() {
    selectedLogProcess = document.getElementById('log-process-select').value;
    if (selectedLogProcess) {
        loadProcessLogs(selectedLogProcess);
        renderLogs();
    }
}

function onAutoScrollChange() {
    autoScrollEnabled = document.getElementById('auto-scroll').checked;
}

// ==================== Page Navigation ====================
let currentPage = 'dashboard';

function switchPage(page) {
    currentPage = page;
    
    // Update nav tabs
    document.querySelectorAll('.nav-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.dataset.page === page) tab.classList.add('active');
    });
    
    // Switch page content
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');
    
    // Load page-specific data
    if (page === 'dashboard') {
        loadInstances();
        refreshDashboard();
    } else if (page === 'market') {
        // Market data loaded on scan button
    } else if (page === 'autobot') {
        initAutobotTargets();
    } else if (page === 'logs') {
        // Logs already handled
    } else if (page === 'system') {
        loadBlacklist();
        updateAllBotStatuses();
    }
}

// ==================== Dashboard ====================
function refreshDashboard() {
    loadInstances();
    // Update stats
    document.getElementById('dash-instances').textContent = activeInstancesCount || '--';
    document.getElementById('dash-blacklist').textContent = blacklistCount || '--';
    // These would come from actual API data
    document.getElementById('dash-hashrate').textContent = '--';
    document.getElementById('dash-cost-hour').textContent = '--';
    document.getElementById('dash-cost-day').textContent = '--';
    document.getElementById('dash-balance').textContent = '--';
}

let activeInstancesCount = 0;
let blacklistCount = 0;

// ==================== Market ====================
function scanMarket() {
    const gpu = document.getElementById('market-gpu-filter').value;
    const sort = document.getElementById('market-sort').value;
    
    showToast('Scanning market...', 'info');
    
    // Simulate loading - would call actual API
    const tbody = document.getElementById('market-tbody');
    tbody.innerHTML = '<tr><td colspan="8" class="empty">$ scanning_market...</td></tr>';
    
    // TODO: Implement actual market scan via API
    setTimeout(() => {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">$ no_offers_found_matching_criteria</td></tr>';
    }, 1500);
}

// ==================== Autobot ====================
const GPU_TARGETS = [
    { id: 'rtx4090', name: 'RTX 4090', vram: '24GB' },
    { id: 'rtx3090', name: 'RTX 3090', vram: '24GB' },
    { id: 'rtx4080', name: 'RTX 4080', vram: '16GB' },
    { id: 'rtx4070', name: 'RTX 4070', vram: '12GB' },
    { id: 'a100', name: 'A100', vram: '40GB' },
    { id: 'h100', name: 'H100', vram: '80GB' },
    { id: 'mi300x', name: 'MI300X', vram: '192GB' }
];

function initAutobotTargets() {
    const container = document.getElementById('autobot-targets');
    if (!container || container.children.length > 0) return;
    
    container.innerHTML = GPU_TARGETS.map(gpu => `
        <div class="gpu-target">
            <input type="checkbox" id="target-${gpu.id}" value="${gpu.id}" checked>
            <label for="target-${gpu.id}">${gpu.name} <span class="unit">(${gpu.vram})</span></label>
        </div>
    `).join('');
}

function clearAutobotLog() {
    const log = document.getElementById('autobot-log');
    if (log) {
        log.innerHTML = '<div class="log-line"><span class="timestamp">[' + new Date().toLocaleTimeString() + ']</span> log_cleared</div>';
    }
}

// ==================== Updated Render Functions ====================
function renderInstancesTable(instances) {
    // Dashboard table
    const tbody1 = document.getElementById('dash-instances-tbody');
    if (tbody1) renderInstancesToTbody(tbody1, instances, true);
    
    activeInstancesCount = instances.filter(i => i.actual_status === 'running').length;
    
    // Update cost display
    const running = instances.filter(i => i.actual_status === 'running');
    const totalCost = running.reduce((sum, i) => sum + (i.dph_total || 0), 0);
    if (document.getElementById('dash-cost-hour')) {
        document.getElementById('dash-cost-hour').textContent = `$${totalCost.toFixed(3)}`;
        document.getElementById('dash-cost-day').textContent = `$${(totalCost * 24).toFixed(2)}`;
        document.getElementById('dash-instances').textContent = running.length;
    }
}

function renderInstancesToTbody(tbody, instances, showDestroyButton = false) {
    if (instances.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">$ no_active_instances</td></tr>';
        return;
    }
    
    tbody.innerHTML = instances.map(i => {
        const statusClass = i.actual_status === 'running' ? 'running' : 
                           i.actual_status === 'offline' ? 'offline' : 'loading';
        const uptime = i.start_date ? Math.floor((Date.now()/1000 - i.start_date)/60) : 0;
        const gpuStr = `${i.num_gpus}x ${i.gpu_name}`;
        
        const destroyBtn = showDestroyButton ? `
            <button class="key-btn stop" onclick="killInstance('${i.id}', '${gpuStr}', '${i.dph_total?.toFixed(3) || '0.000'}')">
                [DESTROY]
            </button>
        ` : '-';
        
        return `
            <tr>
                <td><code>${i.id}</code></td>
                <td>${i.machine_id || '-'}</td>
                <td>${gpuStr}</td>
                <td><span class="status-tag ${statusClass}">${i.actual_status.toUpperCase()}</span></td>
                <td>-</td>
                <td>$${i.dph_total?.toFixed(3) || '0.000'}</td>
                <td>${uptime}m</td>
                <td>${destroyBtn}</td>
            </tr>
        `;
    }).join('');
}

function renderBlacklistTable(blacklist) {
    const tbody = document.getElementById('system-blacklist-tbody');
    if (!tbody) return;
    
    const entries = Object.entries(blacklist);
    blacklistCount = entries.length;
    
    if (entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">$ database_empty</td></tr>';
        return;
    }
    
    tbody.innerHTML = entries.map(([mid, data]) => `
        <tr>
            <td><code>${mid}</code></td>
            <td>${data.instance_id || 'N/A'}</td>
            <td>${data.gpu || 'Unknown'}</td>
            <td>${data.reason || 'Unknown'}</td>
            <td>${data.time || 'N/A'}</td>
            <td>
                <button class="key-btn" onclick="removeFromBlacklist('${mid}')">[REMOVE]</button>
            </td>
        </tr>
    `).join('');
}

function updateBotStatus(scriptName, running) {
    const badge = document.getElementById(`badge-${scriptName}`);
    const startBtn = document.getElementById(`btn-start-${scriptName}`);
    const stopBtn = document.getElementById(`btn-stop-${scriptName}`);
    
    if (!badge || !startBtn || !stopBtn) return;
    
    if (running) {
        badge.textContent = '[RUNNING]';
        badge.className = 'status-badge running';
        startBtn.disabled = true;
        stopBtn.disabled = false;
    } else {
        badge.textContent = '[STOPPED]';
        badge.className = 'status-badge';
        startBtn.disabled = false;
        stopBtn.disabled = true;
    }
}

function updateConnectionStatus(connected) {
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    const wsStatus = document.getElementById('ws-status');
    
    if (dot) {
        dot.className = connected ? 'status-indicator connected' : 'status-indicator';
        dot.style.color = connected ? 'var(--accent-green)' : 'var(--accent-red)';
    }
    if (text) text.textContent = connected ? 'CONNECTED' : 'DISCONNECTED';
    if (wsStatus) {
        wsStatus.textContent = connected ? 'ONLINE' : 'OFFLINE';
        wsStatus.className = connected ? 'good' : 'bad';
    }
}

// ==================== Init ====================
document.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();
    loadInstances();
    updateAllBotStatuses();
    
    // Init dashboard
    refreshDashboard();
    
    setInterval(updateTime, 1000);
    setInterval(() => {
        if (currentPage === 'dashboard') {
            loadInstances();
        }
        if (selectedLogProcess && currentPage === 'logs') {
            loadProcessLogs(selectedLogProcess);
        }
    }, 5000);
    
    updateTime();
});
