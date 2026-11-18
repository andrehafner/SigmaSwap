// SigmaLP landing page (browser only - the app itself runs inside the Ergo wallet).

function init() {
    const input = document.getElementById('appUrl');
    // host-agnostic: the dApp address is wherever this page is served from
    input.value = new URL('api.pl', window.location.href.split('?')[0]).href;
    document.getElementById('copyApp').addEventListener('click', async ev => {
        try {
            await navigator.clipboard.writeText(input.value);
            const old = ev.target.textContent;
            ev.target.textContent = 'Copied';
            setTimeout(() => { ev.target.textContent = old; }, 1500);
        } catch (e) {
            input.select();
        }
    });
}

document.addEventListener('DOMContentLoaded', init);
