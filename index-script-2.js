
        function generateUUID() {
            if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                try { return crypto.randomUUID(); } catch (e) { }
            }
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
                var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        }
        const CID = localStorage.getItem("client_id") || generateUUID();
        localStorage.setItem("client_id", CID);
        const ACTIVE_PAGE_KEY = 'studio_active_page';
        const DEFAULT_PAGE_ID = 'zimage';
        const PAGE_IDS = ['zimage','enhance','klein','angle','online','gpt-chat','canvas','api-settings','comfyui-settings'];

        function switchUI(el, id, options = {}) {
            if(!PAGE_IDS.includes(id)) id = DEFAULT_PAGE_ID;
            document.querySelectorAll('.nav-item,.side-pill').forEach(n => n.classList.remove('active'));
            if(el) el.classList.add('active');
            document.querySelectorAll('iframe').forEach(f => f.classList.remove('active'));
            const target = document.getElementById('frame-' + id);
            target.classList.add('active');
            if (!target.src) target.src = target.dataset.src;
            if(!options.skipRemember) localStorage.setItem(ACTIVE_PAGE_KEY, id);
            // sync theme to newly activated iframe
            syncThemeToFrame(target);
            syncLanguageToFrame(target);
            // 鍒囨崲鍒扮敾甯冩椂閫氱煡鍒锋柊宸ヤ綔娴佸垪琛紙闃叉鍦?comfyui-settings 淇敼鍚庣敾甯冩湭鍙婃椂鏇存柊锛?            if (id === 'canvas' && target.src) {
                try { target.contentWindow?.postMessage({ type: 'canvas-focus' }, '*'); } catch(e) {}
            }
        }

        function restoreActivePage() {
            const id = PAGE_IDS.includes(localStorage.getItem(ACTIVE_PAGE_KEY)) ? localStorage.getItem(ACTIVE_PAGE_KEY) : DEFAULT_PAGE_ID;
            const trigger = document.querySelector(`[onclick*="'${id}'"],[onclick*='"${id}"']`);
            switchUI(trigger, id, { skipRemember:true });
            document.documentElement.classList.remove('studio-route-booting');
        }
        document.addEventListener('DOMContentLoaded', restoreActivePage, { once:true });

        async function syncStatus() {
            try {
                const res = await fetch(`/api/queue_status?client_id=${CID}`);
                const data = await res.json();
                const monitor = document.getElementById('nano-monitor');
                const queueVal = document.getElementById('queue-val');
                const logoDot = document.getElementById('logo-dot');
                const total = data.total || 0;
                const pos = data.position || 0;
                if (pos > 0) {
                    monitor.classList.add('is-busy');
                    queueVal.innerText = `${pos}/${total}`;
                    logoDot.style.backgroundColor = '#3b82f6';
                } else {
                    monitor.classList.remove('is-busy');
                    queueVal.innerText = total > 0 ? total : '0';
                    logoDot.style.backgroundColor = 'var(--text)';
                }
            } catch (e) { }
        }

        const host = window.location.host;
        if (host) {
            const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
            const ws = new WebSocket(`${protocol}://${host}/ws/stats?client_id=${CID}`);
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'stats') {
                    document.getElementById('online-val').innerText = data.online_count;
                } else if (data.type === 'cloud_status') {
                    const iframe = document.querySelector('iframe.active');
                    if (iframe && iframe.contentWindow) {
                        iframe.contentWindow.postMessage(data, '*');
                    }
                } else if (data.type === 'canvas_updated') {
                    const iframe = document.querySelector('iframe.active');
                    if (iframe && iframe.contentWindow) {
                        iframe.contentWindow.postMessage(data, '*');
                    }
                }
            };
            setInterval(syncStatus, 2000);
        }

        // --- 澶滈棿妯″紡 ---

        function syncThemeToFrame(iframe) {
            const theme = (window.StudioTheme || {get: () => 'light'}).get();
            try {
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.postMessage({ type: 'studio-theme', theme }, '*');
                }
            } catch (e) {}
        }

        function broadcastTheme(theme) {
            if (window.StudioTheme) {
                window.StudioTheme.set(theme);
            }
            document.querySelectorAll('iframe').forEach(f => syncThemeToFrame(f));
            updateThemeIcon(theme);
        }

        function updateThemeIcon(theme) {
            const moon = document.getElementById('icon-moon');
            const sun = document.getElementById('icon-sun');
            if (theme === 'dark') {
                moon.style.display = 'none';
                sun.style.display = 'block';
            } else {
                moon.style.display = 'block';
                sun.style.display = 'none';
            }
        }

        function toggleTheme() {
            const current = window.StudioTheme ? window.StudioTheme.get() : 'light';
            broadcastTheme(current === 'dark' ? 'light' : 'dark');
        }

        function toggleLanguage() {
            if(!window.StudioI18n) return;
            window.StudioI18n.toggle();
            document.querySelectorAll('iframe').forEach(frame => syncLanguageToFrame(frame));
        }

        function syncLanguageToFrame(frame) {
            if(!window.StudioI18n) return;
            try {
                frame.contentWindow?.postMessage({ type:'studio-lang', lang:window.StudioI18n.lang() }, '*');
            } catch(e) {}
        }

        function broadcastLanguage() {
            document.querySelectorAll('iframe').forEach(frame => {
                try {
                    frame.contentWindow?.postMessage({ type:'studio-lang', lang:window.StudioI18n.lang() }, '*');
                } catch(e) {}
            });
        }

        // listen for theme changes triggered by theme.js
        window.addEventListener('studio-theme-change', (e) => {
            updateThemeIcon(e.detail.theme);
        });

        // init icon state on load
        window.addEventListener('DOMContentLoaded', () => {
            const theme = window.StudioTheme ? window.StudioTheme.get() : 'light';
            updateThemeIcon(theme);
            if(window.StudioI18n) window.StudioI18n.apply();
            broadcastLanguage();
        });

        // sync theme when iframe loads
        document.querySelectorAll('iframe').forEach(f => {
            f.addEventListener('load', () => {
                syncThemeToFrame(f);
                syncLanguageToFrame(f);
            });
        });
    