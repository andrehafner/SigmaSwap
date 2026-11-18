// SigmaSwap landing page (browser only - the swap UI itself runs inside the Ergo wallet).

let allPools = [];

async function apiCall(action, params = {}) {
    const formData = new FormData();
    formData.append('action', action);
    Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null) formData.append(k, v);
    });
    const response = await fetch('api.pl', { method: 'POST', body: formData });
    const data = await response.json();
    if (!data.success) throw new Error(data.error || 'Unknown error');
    return data;
}

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatUsd(value) {
    return '$' + Number(value).toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function renderPools() {
    const container = document.getElementById('pools');
    const term = document.getElementById('filter').value.trim().toLowerCase();
    const shown = allPools.filter(p => !term || (p.x + ' ' + p.y).toLowerCase().includes(term));
    document.getElementById('poolCount').textContent = allPools.length ? allPools.length + ' pools' : '';
    if (!shown.length) {
        container.innerHTML = '<p class="hint">No matching pairs.</p>';
        return;
    }
    // token names are untrusted on-chain text: always escaped
    container.innerHTML = shown.map(p => `
        <div class="pool">
            <span class="pair">
                <span class="logos">
                    <img src="${escapeHtml(p.xi)}" alt="" width="22" height="22" loading="lazy">
                    <img src="${escapeHtml(p.yi)}" alt="" width="22" height="22" loading="lazy">
                </span>
                <span>${escapeHtml(p.x)} / ${escapeHtml(p.y)}</span>
            </span>
            <span class="tvl" title="Liquidity in this pool">${escapeHtml(formatUsd(p.tvl))}</span>
        </div>`).join('');
}

async function loadPools() {
    const container = document.getElementById('pools');
    try {
        const data = await apiCall('pools');
        allPools = data.pools || [];
        renderPools();
    } catch (e) {
        container.innerHTML = '<p class="hint error">The pool list is unavailable right now. Please try again later.</p>';
    }
}

function setupListeners() {
    document.getElementById('filter').addEventListener('input', renderPools);
    document.getElementById('copyBtn').addEventListener('click', async () => {
        const input = document.getElementById('appUrl');
        const msg = document.getElementById('copyMsg');
        try {
            await navigator.clipboard.writeText(input.value);
            msg.textContent = 'Copied. Paste it into the dApps section of your Ergo wallet.';
        } catch (e) {
            input.select();
            msg.textContent = 'Press Ctrl/Cmd+C to copy.';
        }
    });
}

function init() {
    // host-agnostic: the dApp address is wherever this page is served from
    document.getElementById('appUrl').value = new URL('api.pl', window.location.href).href;
    setupListeners();
    loadPools();
}

document.addEventListener('DOMContentLoaded', init);
