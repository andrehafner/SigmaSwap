// SigmaMint browser page. Minting happens inside the Ergo wallet (Mosaik). This
// page exists for the one thing Mosaik cannot do: pick a file. It uploads the NFT
// media and hands back the IPFS link to paste into the wallet.

let config = { uploads: 0, max_mb: 10 };

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

const el = id => document.getElementById(id);

async function uploadFile(file) {
    el('result').hidden = true;
    el('preview').hidden = true;
    el('preview').innerHTML = '';
    if (!file) return;
    const msg = el('uploadMsg');
    if (file.size > config.max_mb * 1024 * 1024) {
        msg.textContent = `That file is too large (limit ${config.max_mb} MB).`;
        el('file').value = '';
        return;
    }
    msg.textContent = 'Uploading…';
    el('file').disabled = true;
    try {
        const res = await apiCall('upload', { file });
        msg.textContent = 'Uploaded.';
        // preview from the user's own file: nothing is fetched
        const url = URL.createObjectURL(file);
        el('preview').innerHTML =
              res.media === 'picture' ? `<img src="${escapeHtml(url)}" alt="Preview">`
            : res.media === 'audio'   ? `<audio controls src="${escapeHtml(url)}"></audio>`
            :                           `<video controls src="${escapeHtml(url)}"></video>`;
        el('preview').hidden = false;
        el('link').value = 'ipfs://' + res.cid;
        el('result').hidden = false;
    } catch (e) {
        msg.textContent = e.message;
        el('file').value = '';
    } finally {
        el('file').disabled = false;
    }
}

async function copyFrom(inputId, button) {
    const input = el(inputId);
    try {
        await navigator.clipboard.writeText(input.value);
        const old = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = old; }, 1500);
    } catch (e) {
        input.select();
    }
}

function setupListeners() {
    el('file').addEventListener('change', ev => uploadFile(ev.target.files[0]));
    el('copyLink').addEventListener('click', ev => copyFrom('link', ev.target));
    el('copyApp').addEventListener('click', ev => copyFrom('appUrl', ev.target));
}

async function init() {
    // host-agnostic: the dApp address is wherever this page is served from
    el('appUrl').value = new URL('api.pl', window.location.href).href;
    setupListeners();
    try {
        config = await apiCall('config');
    } catch (e) { /* uploads stay hidden */ }
    el('uploadCard').hidden = !config.uploads;
}

document.addEventListener('DOMContentLoaded', init);
