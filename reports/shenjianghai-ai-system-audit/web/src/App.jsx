import { useEffect, useMemo, useState } from "react";

const navItems = [
  ["overview", "总览"],
  ["timeline", "视频复盘"],
  ["canvas", "智能画布"],
  ["modules", "功能系统"],
  ["architecture", "后端架构"],
  ["database", "数据库"],
  ["flywheel", "数据飞轮"],
  ["roadmap", "建设路线"],
  ["conclusion", "最终判断"],
];

const canvasSteps = [
  {
    id: "01",
    label: "进入画布",
    title: "项目上下文中的无限画布",
    image: "/assets/05-canvas-overview.jpg",
    description:
      "顶部承载项目、协作、历史与导出；左侧是共享展廊；中央是无限画布；底部 Dock 放置全局绘制工具。画布不是独立文件，而是项目视图。",
    points: ["项目级保存", "多人在线状态", "共享案例入口", "对象与关系共存"],
  },
  {
    id: "02",
    label: "选中对象",
    title: "对象附近出现能力，而非永久堆在侧栏",
    image: "/assets/06-canvas-object-actions.jpg",
    description:
      "选中图片后显示尺寸、悬浮工具栏、Prompt 与当前生成参数。快速编辑、关系链、加入聊天、下载和分享都围绕当前对象展开。",
    points: ["对象级工具栏", "隐式输入上下文", "画布级默认模型", "比例与数量可调"],
  },
  {
    id: "03",
    label: "参考与生成",
    title: "参考图来源被统一成资产",
    image: "/assets/08-canvas-reference-picker.jpg",
    description:
      "参考图可以从共享展廊、项目素材、历史生成或当前画布选择。用户关闭选择器后，应返回原任务并保留已填写的 Prompt 与参数。",
    points: ["来源统一", "上下文不丢失", "权限过滤", "参考权重可追踪"],
  },
  {
    id: "04",
    label: "多结果分支",
    title: "候选结果不覆盖源图",
    image: "/assets/07-canvas-generation-branch.jpg",
    description:
      "一次生成多个候选，结果在源图附近形成分支。生成过程不锁死画布，任何输出都可以继续作为下一次编辑的输入。",
    points: ["异步生成", "部分成功", "源图不破坏", "结果继续分支"],
  },
  {
    id: "05",
    label: "血缘详情",
    title: "源图、选区、Prompt 与输出完整可追溯",
    image: "/assets/09-canvas-result-detail.jpg",
    description:
      "结果详情把源图、局部重绘区域、中间步骤、参考素材和最终结果放在同一条链路中。画布连线可以隐藏，但数据库血缘不能丢失。",
    points: ["选区与遮罩", "Prompt 版本", "参数快照", "加载回画布"],
  },
];

const modules = [
  {
    number: "01",
    name: "AI 助手",
    summary: "让系统替人执行任务",
    details: "项目会话、技能展廊、执行计划、工具调用、人工确认、失败恢复与产物回写。",
    status: "已展示",
  },
  {
    number: "02",
    name: "素材库",
    summary: "让项目内容可检索、可引用",
    details: "项目库与永久库、业务分类、标签、OCR、图像描述、全文与向量混合检索。",
    status: "已展示",
  },
  {
    number: "03",
    name: "AI 渲染",
    summary: "让图像探索变成可追溯任务",
    details: "多模型路由、参考图、局部编辑、多结果、成本记录、输出入库与生成血缘。",
    status: "已展示",
  },
  {
    number: "04",
    name: "项目管理",
    summary: "让工作结构与进度进入上下文",
    details: "卡片墙、甘特、人员负载、月历。不同视图应投影同一套任务数据。",
    status: "能力清单",
  },
  {
    number: "05",
    name: "工时",
    summary: "让时间归属具体项目与任务",
    details: "开始、停止、补录、归集和统计；不应把应用打开时长直接当作有效工时。",
    status: "能力清单",
  },
  {
    number: "06",
    name: "桌面自动化",
    summary: "把 Rhino 与 InDesign 接入执行链",
    details: "Agent 计划经本机桥接或插件执行，回传日志、截图、文件版本和验证结果。",
    status: "已演示",
  },
];

const architectureLayers = [
  {
    label: "体验层",
    items: ["Web 工作台", "智能画布", "素材库", "Agent 会话", "桌面桥接"],
  },
  {
    label: "业务层",
    items: ["项目上下文", "画布版本", "素材治理", "Agent 运行时", "评审反馈"],
  },
  {
    label: "智能层",
    items: ["模型网关", "生成编排", "混合检索", "技能执行", "质量门"],
  },
  {
    label: "数据层",
    items: ["PostgreSQL", "pgvector", "Redis / Queue", "NAS / 对象存储", "事件日志"],
  },
];

const schemas = [
  {
    group: "项目与人员",
    tables: [
      ["organizations", "组织设置与数据边界", "id, name, slug, settings_json"],
      ["users", "用户身份与状态", "id, email, display_name, status"],
      ["projects", "全系统一级上下文", "id, organization_id, name, code, status"],
      ["tasks", "卡片、甘特、月历的统一数据", "id, project_id, phase_id, assignee, due_at"],
      ["time_entries", "项目与任务工时", "id, project_id, task_id, user_id, duration"],
    ],
  },
  {
    group: "素材与检索",
    tables: [
      ["blob_objects", "NAS/S3 文件本体引用", "id, storage_key, sha256, byte_size"],
      ["assets", "业务素材身份", "id, project_id, type, category, visibility"],
      ["asset_versions", "文件版本、预览与尺寸", "id, asset_id, blob_id, width, height"],
      ["asset_text_chunks", "OCR 与文档分块", "id, asset_version_id, text, region_json"],
      ["asset_embeddings", "向量召回", "id, asset_id, model, embedding"],
    ],
  },
  {
    group: "画布与血缘",
    tables: [
      ["canvases", "项目内画布", "id, project_id, name, current_snapshot_id"],
      ["canvas_snapshots", "快速恢复与历史版本", "id, canvas_id, version, scene_json"],
      ["canvas_nodes", "图片、文本、图形与任务节点", "id, canvas_id, node_type, asset_id, x, y"],
      ["canvas_edges", "可视关系边", "id, source_node_id, target_node_id, edge_type"],
      ["canvas_events", "协作与操作历史", "id, actor_id, operation_id, event_type, payload"],
      ["lineage_edges", "不依赖画布显示的永久血缘", "from_asset_id, to_asset_id, task_id"],
    ],
  },
  {
    group: "模型与生成",
    tables: [
      ["ai_providers", "API 平台与能力", "id, name, base_url, capability_json"],
      ["provider_credentials", "密钥系统引用", "id, provider_id, secret_ref, status"],
      ["ai_models", "平台下的可选模型", "id, provider_id, model_id, capabilities"],
      ["canvas_model_policies", "画布级默认 API 与模型", "canvas_id, task_type, provider_id, model_id"],
      ["generation_tasks", "异步生成任务快照", "id, canvas_id, model_id, prompt, status, cost"],
      ["generation_inputs", "源图、参考图、遮罩", "task_id, asset_id, input_role, region_json"],
      ["generation_outputs", "独立候选结果", "task_id, asset_id, output_index, status"],
    ],
  },
  {
    group: "Agent 与评审",
    tables: [
      ["agent_threads", "项目会话", "id, project_id, title, created_by"],
      ["agent_runs", "计划与运行检查点", "id, thread_id, status, plan_json, context_snapshot"],
      ["skills", "技能身份与权限声明", "id, organization_id, name, permission_manifest"],
      ["tool_calls", "逐步工具调用", "id, run_id, tool_name, input, output, status"],
      ["reviews", "接受、拒绝与总结", "id, output_id, reviewer_id, decision, summary"],
      ["review_scores", "结构化评价量表", "review_id, criterion_id, score, passed"],
      ["preference_events", "收藏、采用、分享和复用信号", "asset_id, event_type, context_json"],
    ],
  },
];

const roadmap = [
  {
    phase: "00",
    title: "统一地基",
    items: ["组织、项目与权限", "对象存储", "模型网关", "审计日志"],
    acceptance: "同一个 project_id 贯穿素材、画布与模型调用。",
  },
  {
    phase: "01",
    title: "画布生产闭环",
    items: ["对象编辑", "画布级 API", "异步多结果", "局部编辑", "血缘与历史"],
    acceptance: "素材进入画布后能生成、挑选、二次编辑、回存并追溯。",
  },
  {
    phase: "02",
    title: "素材与共享",
    items: ["业务分类", "混合检索", "共享展廊", "团队偏好信号"],
    acceptance: "用户能从项目和组织知识中快速找到可信参考。",
  },
  {
    phase: "03",
    title: "Agent 与技能",
    items: ["项目会话", "计划确认", "内部技能", "取消与重试"],
    acceptance: "Agent 的每一步都可解释、可授权、可恢复。",
  },
  {
    phase: "04",
    title: "专业自动化",
    items: ["评审量表", "几何门", "Rhino", "InDesign", "运营分析"],
    acceptance: "桌面任务具有版本、回滚、日志和结果验证。",
  },
];

const evidenceModes = {
  all: "全部",
  observed: "视频明确展示",
  inferred: "合理推断",
  recommended: "落地建议",
};

function Evidence({ type = "observed", children }) {
  return <span className={`evidence evidence-${type}`}>{children}</span>;
}

function Figure({ src, alt, caption, onOpen, wide = false }) {
  return (
    <figure className={wide ? "figure figure-wide" : "figure"}>
      <button className="figure-button" onClick={() => onOpen({ src, alt, caption })}>
        <img src={src} alt={alt} loading="lazy" />
        <span className="figure-action">查看高清证据</span>
      </button>
      <figcaption>{caption}</figcaption>
    </figure>
  );
}

function Section({ id, eyebrow, title, intro, children }) {
  return (
    <section id={id} className="report-section" data-section={id}>
      <div className="section-heading">
        <span className="section-eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        {intro && <p>{intro}</p>}
      </div>
      {children}
    </section>
  );
}

export function App() {
  const [activeSection, setActiveSection] = useState("overview");
  const [canvasStep, setCanvasStep] = useState(0);
  const [evidenceMode, setEvidenceMode] = useState("all");
  const [expandedSchema, setExpandedSchema] = useState(0);
  const [lightbox, setLightbox] = useState(null);
  const [progress, setProgress] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    const updateProgress = () => {
      const scrollable = document.documentElement.scrollHeight - window.innerHeight;
      setProgress(scrollable > 0 ? (window.scrollY / scrollable) * 100 : 0);
    };
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible) setActiveSection(visible.target.id);
      },
      { rootMargin: "-20% 0px -65% 0px", threshold: [0.05, 0.2, 0.5] },
    );
    document.querySelectorAll("[data-section]").forEach((section) => observer.observe(section));
    window.addEventListener("scroll", updateProgress, { passive: true });
    updateProgress();
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", updateProgress);
    };
  }, []);

  useEffect(() => {
    if (!lightbox) return;
    const close = (event) => event.key === "Escape" && setLightbox(null);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [lightbox]);

  const currentCanvas = useMemo(() => canvasSteps[canvasStep], [canvasStep]);

  const jumpTo = (id) => {
    if (id === "overview") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    } else {
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    setMenuOpen(false);
  };

  return (
    <div className={`site-shell evidence-mode-${evidenceMode}`}>
      <div className="reading-progress" style={{ width: `${progress}%` }} />

      <header className="topbar">
        <button className="brand" onClick={() => jumpTo("overview")} aria-label="返回报告开头">
          <span className="brand-mark">SA</span>
          <span>
            <strong>AI 设计系统复盘</strong>
            <small>高清影像研究报告</small>
          </span>
        </button>
        <div className="topbar-meta">
          <span>93 分钟</span>
          <span>17 张证据</span>
          <span>5 个系统视图</span>
        </div>
        <button
          className="menu-button"
          onClick={() => setMenuOpen((value) => !value)}
          aria-expanded={menuOpen}
        >
          目录
        </button>
      </header>

      <aside className={`sidebar ${menuOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-label">章节导航</div>
        <nav>
          {navItems.map(([id, label], index) => (
            <button
              key={id}
              className={activeSection === id ? "active" : ""}
              onClick={() => jumpTo(id)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <strong>证据边界</strong>
          <p>界面事实与技术推断分开标注。口述但未出现在画面中的内容不作为确定事实。</p>
        </div>
      </aside>

      <main className="report">
        <section id="overview" className="hero" data-section="overview">
          <div className="hero-copy">
            <div className="kicker">SYSTEM REVIEW · 2026</div>
            <h1>
              <span className="hero-line">从智能画布，</span>
              <span className="hero-line">到设计事务所的</span>
              <span className="hero-line hero-line-accent">项目操作系统</span>
            </h1>
            <p className="hero-lead">
              对《申江海工作室 AI 介入设计工作分享》高清原片的 UI、交互、功能、后端和数据库复盘。
            </p>
            <div className="hero-actions">
              <button className="primary-action" onClick={() => jumpTo("canvas")}>
                查看画布逻辑
              </button>
              <button className="secondary-action" onClick={() => jumpTo("architecture")}>
                查看技术架构
              </button>
            </div>
          </div>
          <div className="hero-visual">
            <img src="/assets/01-unified-workbench.jpg" alt="统一工作台五个视图展示" />
            <div className="hero-caption">
              <span>视频证据 01</span>
              统一工作台：五个视图，一个入口
            </div>
          </div>
          <div className="thesis-card">
            <span className="thesis-index">核心判断</span>
            <p>
              它的壁垒不是某个生图按钮，而是让项目、素材、画布、Agent 与评审共享同一套对象身份和数据血缘。
            </p>
          </div>
        </section>

        <div className="content-column">
          <div className="evidence-toolbar" aria-label="证据类型筛选">
            <span>阅读视角</span>
            <div>
              {Object.entries(evidenceModes).map(([key, label]) => (
                <button
                  key={key}
                  className={evidenceMode === key ? "active" : ""}
                  onClick={() => setEvidenceMode(key)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <Section
            id="timeline"
            eyebrow="01 · 视频复盘"
            title="93 分钟里，他们展示了什么"
            intro="视频不是从功能菜单开始，而是先解释为什么要把散落的 AI、项目文件和桌面软件收进同一个工作台。"
          >
            <div className="timeline">
              {[
                ["00:00–10:00", "统一系统", "五个业务视图，不按模型或技术模块分导航。"],
                ["10:00–18:30", "Agent 与运营", "技能、工具调用、成功失败、耗时和成本同时可见。"],
                ["18:30–27:30", "建设路线", "先完成素材、飞轮和助手，再进入专业上下文。"],
                ["27:30–35:30", "智能画布", "对象级生成、参考、多结果、局部编辑与血缘。"],
                ["35:30–42:00", "素材库", "项目资产、永久资产、分类、标签、检索与 Agent 共屏。"],
                ["42:00–53:00", "评审与 Rhino", "结构化步骤、几何门、量表评审和桌面执行。"],
                ["53:00–64:00", "InDesign", "项目数据、素材元数据与规则驱动自动排版。"],
                ["64:00–93:04", "上下文与飞轮", "本地 NAS、模型网关、数据主权和下一阶段路线。"],
              ].map(([time, title, text], index) => (
                <article className="timeline-row" key={time}>
                  <div className="timeline-time">{time}</div>
                  <div className="timeline-node">{String(index + 1).padStart(2, "0")}</div>
                  <div>
                    <h3>{title}</h3>
                    <p>{text}</p>
                  </div>
                </article>
              ))}
            </div>
            <div className="evidence-grid">
              <Figure
                src="/assets/02-workbench-agent-models.jpg"
                alt="Agent 与技能展廊"
                caption="Agent 会话、技能和逐步执行日志"
                onOpen={setLightbox}
              />
              <Figure
                src="/assets/03-asset-analytics.jpg"
                alt="团队和模型运营数据"
                caption="调用、成功率、耗时与成本进入运营视图"
                onOpen={setLightbox}
              />
            </div>
          </Section>

          <Section
            id="canvas"
            eyebrow="02 · 智能画布"
            title="以“选中对象”为中心的生成工作流"
            intro="画布不是一个大号 Prompt 表单。对象先成为上下文，参数和能力才贴近对象出现；生成结果保留源图与分支关系。"
          >
            <div className="canvas-demo">
              <div className="canvas-tabs" role="tablist" aria-label="画布操作步骤">
                {canvasSteps.map((step, index) => (
                  <button
                    key={step.id}
                    role="tab"
                    aria-selected={canvasStep === index}
                    className={canvasStep === index ? "active" : ""}
                    onClick={() => setCanvasStep(index)}
                  >
                    <span>{step.id}</span>
                    {step.label}
                  </button>
                ))}
              </div>
              <div className="canvas-stage">
                <button
                  className="canvas-image"
                  onClick={() =>
                    setLightbox({
                      src: currentCanvas.image,
                      alt: currentCanvas.title,
                      caption: currentCanvas.label,
                    })
                  }
                >
                  <img src={currentCanvas.image} alt={currentCanvas.title} />
                </button>
                <article className="canvas-explanation">
                  <Evidence type="observed">视频明确展示</Evidence>
                  <span className="canvas-step-label">{currentCanvas.id} / 05</span>
                  <h3>{currentCanvas.title}</h3>
                  <p>{currentCanvas.description}</p>
                  <ul>
                    {currentCanvas.points.map((point) => (
                      <li key={point}>{point}</li>
                    ))}
                  </ul>
                </article>
              </div>
            </div>

            <div className="principle-grid">
              <article>
                <span>对象身份</span>
                <h3>结果不是一张临时图片</h3>
                <p>每个结果拥有资产 ID、任务 ID、模型参数和来源，可以继续编辑、分享、评审和进入正式交付。</p>
              </article>
              <article>
                <span>状态管理</span>
                <h3>生成任务不锁死画布</h3>
                <p>任务进入队列后，用户仍可缩放、移动和检查其他内容；多个输出应允许部分成功。</p>
              </article>
              <article>
                <span>数据血缘</span>
                <h3>连线可隐藏，关系不能消失</h3>
                <p>视觉连线只是显示层，源资产、遮罩、参考、Prompt 和输出关系必须独立持久化。</p>
              </article>
            </div>
          </Section>

          <Section
            id="modules"
            eyebrow="03 · 功能系统"
            title="五个视图，共用同一个项目数据底座"
            intro="用户看见的是助手、素材、画布、项目和工时；系统真正沉淀的是项目结构、资产语义、操作轨迹、执行链和评审结果。"
          >
            <Figure
              src="/assets/14-shared-context-five-functions.jpg"
              alt="五个功能一个数据底座"
              caption="视频中的系统总结：五个功能共同生产同一个东西"
              onOpen={setLightbox}
              wide
            />
            <div className="module-grid">
              {modules.map((module) => (
                <article key={module.number}>
                  <div className="module-head">
                    <span>{module.number}</span>
                    <Evidence type={module.status === "能力清单" ? "inferred" : "observed"}>
                      {module.status}
                    </Evidence>
                  </div>
                  <h3>{module.name}</h3>
                  <strong>{module.summary}</strong>
                  <p>{module.details}</p>
                </article>
              ))}
            </div>
            <div className="split-feature">
              <Figure
                src="/assets/10-asset-library.jpg"
                alt="项目素材库"
                caption="素材分类、标签和右侧项目 Agent"
                onOpen={setLightbox}
              />
              <div className="feature-copy">
                <Evidence type="observed">视频明确展示</Evidence>
                <h3>素材库的核心不是存储，而是资产语义化</h3>
                <p>
                  项目素材按效果图、分析图、图纸、模型、材料、现场记录等业务类别组织。文件一旦拥有稳定身份，就能被检索、引用、生成、评审和排版。
                </p>
                <dl>
                  <div>
                    <dt>257,000</dt>
                    <dd>页面展示的文本向量规模</dd>
                  </div>
                  <div>
                    <dt>3 通道</dt>
                    <dd>全文、向量与结构化过滤</dd>
                  </div>
                </dl>
              </div>
            </div>
          </Section>

          <Section
            id="architecture"
            eyebrow="04 · 后端架构"
            title="先做模块化单体，再拆高负载服务"
            intro="第一阶段不需要把系统切成十几个微服务。更重要的是稳定的对象身份、异步任务、事件和权限边界。"
          >
            <div className="architecture-board">
              {architectureLayers.map((layer, index) => (
                <div className="architecture-row" key={layer.label}>
                  <div className="architecture-label">
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <strong>{layer.label}</strong>
                  </div>
                  <div className="architecture-items">
                    {layer.items.map((item) => (
                      <span key={item}>{item}</span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="architecture-notes">
              <article>
                <Evidence type="recommended">落地建议</Evidence>
                <h3>生成编排</h3>
                <p>统一输入、参数校验、队列、回调、失败分类、输出入库、成本和画布回写。</p>
              </article>
              <article>
                <Evidence type="recommended">落地建议</Evidence>
                <h3>模型网关</h3>
                <p>供应商、模型、凭证、能力和价格分离；画布只选择可用模型，不直接保存 API Key。</p>
              </article>
              <article>
                <Evidence type="inferred">合理推断</Evidence>
                <h3>桌面桥接</h3>
                <p>浏览器 Agent 通过本机服务、插件或 CLI 驱动 Rhino 与 InDesign，并回传可审计结果。</p>
              </article>
            </div>
            <div className="evidence-grid">
              <Figure
                src="/assets/12-rhino-agent-control.jpg"
                alt="Rhino 自动化"
                caption="Rhino 分层建模和执行日志"
                onOpen={setLightbox}
              />
              <Figure
                src="/assets/13-indesign-automation.jpg"
                alt="InDesign 自动排版"
                caption="规则驱动的多页 InDesign 文档"
                onOpen={setLightbox}
              />
            </div>
          </Section>

          <Section
            id="database"
            eyebrow="05 · 数据库"
            title="数据库不是文件清单，而是产品的长期记忆"
            intro="推荐 PostgreSQL 作为主库、pgvector 存向量、NAS 或 S3 兼容存储保存文件本体。点击分组查看核心表。"
          >
            <div className="schema-layout">
              <div className="schema-tabs" role="tablist" aria-label="数据库分组">
                {schemas.map((schema, index) => (
                  <button
                    key={schema.group}
                    className={expandedSchema === index ? "active" : ""}
                    onClick={() => setExpandedSchema(index)}
                  >
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    {schema.group}
                  </button>
                ))}
              </div>
              <div className="schema-table">
                <div className="schema-table-head">
                  <span>数据表</span>
                  <span>职责</span>
                  <span>关键字段</span>
                </div>
                {schemas[expandedSchema].tables.map(([name, purpose, fields]) => (
                  <div className="schema-table-row" key={name}>
                    <code>{name}</code>
                    <strong>{purpose}</strong>
                    <span>{fields}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="database-rule">
              <span>关键设计规则</span>
              <p>
                画布快照用于快速恢复，规范化节点表用于业务查询；视觉连线存在于
                <code> canvas_edges </code>，永久生成关系存在于
                <code> lineage_edges </code>。
              </p>
            </div>
          </Section>

          <Section
            id="flywheel"
            eyebrow="06 · 数据飞轮"
            title="不是把聊天记录全部塞进向量库"
            intro="真正有价值的是人的选择：用了什么、留下什么、拒绝什么、又把什么带进了正式交付。"
          >
            <div className="flywheel-layout">
              <div className="flywheel-sequence">
                {[
                  "检索素材",
                  "放入画布",
                  "选择参考",
                  "生成候选",
                  "保留结果",
                  "继续编辑",
                  "结构化评审",
                  "进入交付",
                ].map((item, index) => (
                  <div key={item}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <strong>{item}</strong>
                  </div>
                ))}
              </div>
              <Figure
                src="/assets/16-data-flywheel.jpg"
                alt="数据主权和数据飞轮"
                caption="本地 NAS、模型网关、账户管控与数据主权"
                onOpen={setLightbox}
              />
            </div>
            <div className="callout">
              <Evidence type="recommended">落地建议</Evidence>
              <p>
                第一阶段不要急着训练个人数据。先用收藏、采用、分享、复用和评审信号改善检索排序、案例推荐、默认模型路由和 Prompt 模板。
              </p>
            </div>
          </Section>

          <Section
            id="roadmap"
            eyebrow="07 · 建设路线"
            title="从一条可用闭环开始，而不是一次复刻整个系统"
            intro="专业上下文越深，错误成本越高。先把项目、资产、画布和任务做稳，再进入规范、材料和造价。"
          >
            <div className="roadmap">
              {roadmap.map((phase) => (
                <article key={phase.phase}>
                  <div className="roadmap-number">{phase.phase}</div>
                  <div className="roadmap-body">
                    <h3>{phase.title}</h3>
                    <ul>
                      {phase.items.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <p>
                      <strong>验收：</strong>
                      {phase.acceptance}
                    </p>
                  </div>
                </article>
              ))}
            </div>
            <div className="evidence-grid">
              <Figure
                src="/assets/15-context-layers.jpg"
                alt="五层上下文"
                caption="从 APP 使用到材料供应链的上下文深度"
                onOpen={setLightbox}
              />
              <Figure
                src="/assets/17-next-roadmap.jpg"
                alt="下一批路线"
                caption="材料、前期分析、规范和项目动态上下文"
                onOpen={setLightbox}
              />
            </div>
          </Section>

          <section id="conclusion" className="conclusion" data-section="conclusion">
            <span className="section-eyebrow">08 · 最终判断</span>
            <h2>只复制 UI，会得到一个“看起来像”的画布。</h2>
            <p>
              把对象身份、任务状态、数据血缘、项目上下文和反馈回流建立起来，才会得到真正可持续使用的设计生产系统。
            </p>
            <div className="conclusion-flow">
              <span>项目素材</span>
              <span>智能画布</span>
              <span>异步生成</span>
              <span>完整血缘</span>
              <span>Agent / 交付</span>
            </div>
            <button className="primary-action" onClick={() => jumpTo("overview")}>
              返回报告开头
            </button>
          </section>
        </div>
      </main>

      {lightbox && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label={lightbox.alt}>
          <button className="lightbox-backdrop" onClick={() => setLightbox(null)} aria-label="关闭大图" />
          <div className="lightbox-panel">
            <div className="lightbox-head">
              <div>
                <strong>{lightbox.caption}</strong>
                <span>1920 × 1080 原始视频截图</span>
              </div>
              <button onClick={() => setLightbox(null)}>关闭</button>
            </div>
            <img src={lightbox.src} alt={lightbox.alt} />
          </div>
        </div>
      )}
    </div>
  );
}
