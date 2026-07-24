(function () {
    'use strict';

    if (window.top !== window) return;

    const PROJECT_KEY = 'studio_active_project_id';
    const FEEDBACK_KEY = 'studio_feedback_v1';
    const MODULES = [
        { id: 'assistant', label: '助手', shortcut: 'Ctrl+1', icon: 'message-square', href: '/?page=agent' },
        { id: 'library', label: '素材库', shortcut: 'Ctrl+2', icon: 'library', href: '/?page=library' },
        { id: 'generate', label: 'AI 生图', shortcut: 'Ctrl+3', icon: 'wand-sparkles', href: '/?page=workbench' },
        { id: 'projects', label: '项目管理', shortcut: 'Ctrl+4', icon: 'folder-kanban', href: '/static/project-workbench.html' }
    ];

    let activeModule = 'assistant';
    let menuOpen = false;
    let shellRoot = null;
    let projectSelect = null;
    let toastTimer = null;

    function pageModule() {
        const path = location.pathname;
        if (path.endsWith('/project-workbench.html') || path.endsWith('/ppt-workbench.html')) return 'projects';
        if (path.endsWith('/smart-canvas.html') || path.endsWith('/canvas.html')) return 'generate';
        if (path.endsWith('/library.html')) return 'library';
        if (path === '/' || path.endsWith('/index.html')) {
            const requested = new URLSearchParams(location.search).get('page')
                || localStorage.getItem('studio_active_page')
                || 'agent';
            if (requested === 'library') return 'library';
            if (['workbench', 'zimage', 'enhance', 'klein', 'angle', 'online', 'gpt-chat', 'canvas', 'comfyui-settings'].includes(requested)) return 'generate';
        }
        return 'assistant';
    }

    function bodyModuleClass(moduleId) {
        const path = location.pathname;
        if (path.endsWith('/smart-canvas.html') || path.endsWith('/canvas.html')) return 'canvas';
        if (path.endsWith('/project-workbench.html') || path.endsWith('/ppt-workbench.html')) return 'projects';
        if (path.endsWith('/library.html')) return 'library';
        return 'index';
    }

    function icon(name) {
        return `<i data-lucide="${name}" aria-hidden="true"></i>`;
    }

    function moduleHref(item) {
        if (item.id !== 'projects') return item.href;
        const projectId = localStorage.getItem(PROJECT_KEY) || '';
        return projectId
            ? `${item.href}?project_id=${encodeURIComponent(projectId)}`
            : item.href;
    }

    function buildShell() {
        activeModule = pageModule();
        document.body.classList.add(`studio-shell-module-${bodyModuleClass(activeModule)}`);

        shellRoot = document.createElement('header');
        shellRoot.id = 'studio-global-shell';
        shellRoot.setAttribute('aria-label', '全局工作台导航');
        shellRoot.innerHTML = `
            <a class="studio-global-brand" href="/?page=agent" aria-label="Infinite Agent Work 首页">
                <img src="/static/logo.png" alt="">
                <span class="studio-global-brand-copy">
                    <strong>Infinite Agent Work</strong>
                    <small>AI DESIGN STUDIO</small>
                </span>
            </a>
            <nav class="studio-global-nav" aria-label="工作模块">
                ${MODULES.map(item => `
                    <a class="studio-global-tab${item.id === activeModule ? ' active' : ''}"
                       href="${moduleHref(item)}"
                       data-studio-module="${item.id}"
                       ${item.id === activeModule ? 'aria-current="page"' : ''}>
                        ${icon(item.icon)}
                        <span>${item.label}</span>
                        <small class="studio-global-shortcut">${item.shortcut}</small>
                    </a>
                `).join('')}
            </nav>
            <span class="studio-global-spacer"></span>
            <label class="studio-global-project" title="当前项目">
                <span class="studio-global-project-dot" aria-hidden="true"></span>
                <select id="studioGlobalProject" aria-label="当前项目">
                    <option value="">正在读取项目…</option>
                </select>
            </label>
            <div class="studio-global-actions">
                <button class="studio-global-action" type="button" data-studio-action="feedback" title="反馈问题" aria-label="反馈问题">
                    ${icon('message-circle-warning')}
                </button>
                <button class="studio-global-action" type="button" data-studio-action="theme" title="切换主题" aria-label="切换主题">
                    ${icon('sun-moon')}
                </button>
                <button class="studio-global-action studio-global-avatar" type="button" data-studio-action="account" title="用户菜单" aria-label="用户菜单" aria-expanded="false">
                    A
                </button>
            </div>
        `;
        document.body.prepend(shellRoot);
        projectSelect = shellRoot.querySelector('#studioGlobalProject');
        buildAccountMenu();
        buildFeedbackDialog();
        bindShell();
        refreshIcons();
        loadProjects();
        rememberCanvas();
    }

    function buildAccountMenu() {
        const menu = document.createElement('div');
        menu.className = 'studio-global-menu';
        menu.id = 'studioGlobalMenu';
        menu.innerHTML = `
            <div class="studio-global-menu-head">
                <strong>工作台用户</strong>
                <span>当前设备 · 本地优先</span>
            </div>
            <a href="/?page=api-settings">${icon('settings')}<span>API 与模型设置</span></a>
            <button type="button" data-menu-action="theme">${icon('sun-moon')}<span>切换明暗主题</span></button>
            <button type="button" data-menu-action="feedback">${icon('message-circle-warning')}<span>反馈问题</span></button>
        `;
        document.body.appendChild(menu);
    }

    function buildFeedbackDialog() {
        const backdrop = document.createElement('div');
        backdrop.className = 'studio-feedback-backdrop';
        backdrop.id = 'studioFeedbackBackdrop';
        backdrop.setAttribute('aria-hidden', 'true');
        backdrop.innerHTML = `
            <form class="studio-feedback-card" id="studioFeedbackForm">
                <h2>反馈问题</h2>
                <p>反馈会保存在当前设备，包含所在模块和项目，便于后续接入自动修复闭环。</p>
                <label>
                    反馈类型
                    <select name="type">
                        <option value="problem">功能问题</option>
                        <option value="suggestion">体验建议</option>
                        <option value="requirement">新需求</option>
                    </select>
                </label>
                <label>
                    问题描述
                    <textarea name="content" required maxlength="2000" placeholder="请描述你正在做什么、遇到了什么，以及期望结果…"></textarea>
                </label>
                <div class="studio-feedback-actions">
                    <button type="button" data-feedback-action="cancel">取消</button>
                    <button class="primary" type="submit">保存反馈</button>
                </div>
            </form>
        `;
        document.body.appendChild(backdrop);
    }

    function refreshIcons() {
        if (window.lucide?.createIcons) window.lucide.createIcons();
    }

    function setActive(moduleId) {
        activeModule = MODULES.some(item => item.id === moduleId) ? moduleId : 'assistant';
        shellRoot?.querySelectorAll('[data-studio-module]').forEach(tab => {
            const active = tab.dataset.studioModule === activeModule;
            tab.classList.toggle('active', active);
            if (active) tab.setAttribute('aria-current', 'page');
            else tab.removeAttribute('aria-current');
        });
    }

    function navigate(moduleId, href) {
        if ((location.pathname === '/' || location.pathname.endsWith('/index.html')) && typeof window.openStudioPage === 'function') {
            const page = moduleId === 'assistant' ? 'agent'
                : moduleId === 'library' ? 'library'
                    : moduleId === 'generate' ? 'workbench'
                        : '';
            if (page) {
                const frameUrl = page === 'library' ? '/static/library.html?v=20260720-unified-shell' : '';
                window.openStudioPage(page, frameUrl);
                history.replaceState(null, '', `/?page=${encodeURIComponent(page)}`);
                setActive(moduleId);
                return;
            }
        }
        location.href = href;
    }

    function bindShell() {
        shellRoot.querySelectorAll('[data-studio-module]').forEach(tab => {
            tab.addEventListener('click', event => {
                event.preventDefault();
                navigate(tab.dataset.studioModule, tab.href);
            });
        });

        shellRoot.querySelector('[data-studio-action="feedback"]')?.addEventListener('click', openFeedback);
        shellRoot.querySelector('[data-studio-action="theme"]')?.addEventListener('click', toggleTheme);
        shellRoot.querySelector('[data-studio-action="account"]')?.addEventListener('click', event => {
            event.stopPropagation();
            setMenuOpen(!menuOpen);
        });

        document.querySelector('[data-menu-action="theme"]')?.addEventListener('click', () => {
            toggleTheme();
            setMenuOpen(false);
        });
        document.querySelector('[data-menu-action="feedback"]')?.addEventListener('click', () => {
            setMenuOpen(false);
            openFeedback();
        });

        projectSelect?.addEventListener('change', handleProjectChange);
        document.getElementById('studioFeedbackBackdrop')?.addEventListener('click', event => {
            if (event.target.id === 'studioFeedbackBackdrop') closeFeedback();
        });
        document.querySelector('[data-feedback-action="cancel"]')?.addEventListener('click', closeFeedback);
        document.getElementById('studioFeedbackForm')?.addEventListener('submit', saveFeedback);
        document.addEventListener('click', () => setMenuOpen(false));
        document.addEventListener('keydown', handleKeyboard);

        window.addEventListener('studio-page-change', event => setActive(event.detail?.module || pageModule()));
        window.addEventListener('storage', event => {
            if (event.key === PROJECT_KEY && projectSelect) projectSelect.value = event.newValue || '';
        });
    }

    function handleKeyboard(event) {
        if (!event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.target?.matches?.('input, textarea, select, [contenteditable="true"]')) return;
        const index = Number(event.key) - 1;
        const item = MODULES[index];
        if (!item) return;
        event.preventDefault();
        navigate(item.id, new URL(moduleHref(item), location.origin).href);
    }

    function setMenuOpen(open) {
        menuOpen = Boolean(open);
        document.getElementById('studioGlobalMenu')?.classList.toggle('open', menuOpen);
        shellRoot?.querySelector('[data-studio-action="account"]')?.setAttribute('aria-expanded', menuOpen ? 'true' : 'false');
    }

    function openFeedback() {
        const backdrop = document.getElementById('studioFeedbackBackdrop');
        backdrop?.classList.add('open');
        backdrop?.setAttribute('aria-hidden', 'false');
        setTimeout(() => backdrop?.querySelector('textarea')?.focus(), 20);
    }

    function closeFeedback() {
        const backdrop = document.getElementById('studioFeedbackBackdrop');
        backdrop?.classList.remove('open');
        backdrop?.setAttribute('aria-hidden', 'true');
    }

    function saveFeedback(event) {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const content = String(form.get('content') || '').trim();
        if (!content) return;
        let records = [];
        try {
            records = JSON.parse(localStorage.getItem(FEEDBACK_KEY) || '[]');
            if (!Array.isArray(records)) records = [];
        } catch (_) {
            records = [];
        }
        records.unshift({
            id: crypto.randomUUID ? crypto.randomUUID() : `feedback-${Date.now()}`,
            type: String(form.get('type') || 'problem'),
            content,
            module: activeModule,
            project_id: localStorage.getItem(PROJECT_KEY) || '',
            path: `${location.pathname}${location.search}`,
            created_at: new Date().toISOString(),
            status: 'open'
        });
        localStorage.setItem(FEEDBACK_KEY, JSON.stringify(records.slice(0, 100)));
        event.currentTarget.reset();
        closeFeedback();
        showToast('反馈已保存，后续可接入自动处理闭环');
        window.dispatchEvent(new CustomEvent('studio-feedback-saved', { detail: records[0] }));
    }

    function toggleTheme() {
        const current = window.StudioTheme?.get?.() || localStorage.getItem('studio_theme') || 'dark';
        const next = current === 'dark' ? 'light' : 'dark';
        if (window.StudioTheme?.set) window.StudioTheme.set(next);
        else {
            localStorage.setItem('studio_theme', next);
            localStorage.setItem('canvas_theme', next);
            location.reload();
        }
        showToast(next === 'dark' ? '已切换为深色主题' : '已切换为浅色主题');
    }

    async function loadProjects() {
        if (!projectSelect) return;
        try {
            const response = await fetch('/api/projects', { cache: 'no-store' });
            if (!response.ok) throw new Error('project request failed');
            const projects = (await response.json()).projects || [];
            const requested = new URLSearchParams(location.search).get('project_id')
                || localStorage.getItem(PROJECT_KEY)
                || projects[0]?.id
                || '';
            projectSelect.innerHTML = projects.length
                ? projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.code ? `${project.code} · ${project.name}` : project.name)}</option>`).join('')
                : '<option value="">还没有项目</option>';
            if (requested && projects.some(project => String(project.id) === String(requested))) {
                projectSelect.value = requested;
                localStorage.setItem(PROJECT_KEY, requested);
            }
        } catch (_) {
            projectSelect.innerHTML = '<option value="">项目未连接</option>';
        }
    }

    function handleProjectChange(event) {
        const projectId = event.target.value || '';
        if (!projectId) return;
        localStorage.setItem(PROJECT_KEY, projectId);
        try {
            const channel = new BroadcastChannel('studio-project');
            channel.postMessage({ type: 'project-changed', project_id: projectId });
            channel.close();
        } catch (_) {}
        window.dispatchEvent(new CustomEvent('studio-project-change', { detail: { projectId } }));
        showToast('当前项目已切换');
        if (location.pathname.endsWith('/project-workbench.html')) {
            const url = new URL(location.href);
            url.searchParams.set('project_id', projectId);
            location.href = url.href;
        }
    }

    function rememberCanvas() {
        if (!location.pathname.endsWith('/smart-canvas.html')) return;
        const canvasId = new URLSearchParams(location.search).get('id');
        if (canvasId) localStorage.setItem('studio_last_canvas_id', canvasId);
    }

    function showToast(message) {
        let toast = document.getElementById('studioGlobalToast');
        if (!toast) {
            toast = document.createElement('div');
            toast.className = 'studio-global-toast';
            toast.id = 'studioGlobalToast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    window.StudioGlobalShell = {
        setActive,
        getActive: () => activeModule,
        openFeedback
    };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildShell, { once: true });
    else buildShell();
})();
