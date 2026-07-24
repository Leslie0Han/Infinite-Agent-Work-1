# Design QA — Smart Canvas recording recreation

- Source visual truth: `/Users/leslie/Desktop/录屏2026-07-14 17.06.06.mov`
- Reference state: frame at 00:09:50, Generate style panel open
- Implementation screenshot: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-style-panel-final.png`
- Side-by-side comparison: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-design-qa-comparison.png`
- Viewport: 1280 x 720 desktop, dark theme

## Visible fidelity

- Top project bar and Share control, dotted dark canvas, selected-object toolbar, four selection handles, dimensions, right task panel, candidate rail, and bottom action dock are present in the same visual hierarchy as the recording. Plan, upgrade, and third-party branding surfaces are intentionally omitted.
- The nine Generate style cards use real crops from the recording instead of CSS-filter approximations.
- The right panel, contextual toolbar, and bottom dock stay fixed while the infinite canvas pans and zooms.
- Typography, borders, radii, shadows, pink/purple accent, and compact control density were checked in the combined comparison image.

## Core interaction verification

- Candidate session: single click previews without adding a node; plus promotes to an independent node; double-click and drag share the same promotion route.
- Selection chrome: four handles and native image dimensions render for the active image.
- Render, Swap Material, Edit, Populate, Generate style, Enhance, Video gate, Download, layer, and delete actions route from the selected-object toolbar.
- Swap Material includes area, optional material reference, description formula, design-adherence guidance, and Generate.
- Material Catalogue can coexist with the action/Agent surface; a material card opens detail actions for use as reference, add to canvas, and sample request.
- Agent material workflow was exercised through reference choice, area choice, semantic scope, and handoff to the Swap Material panel.
- Project History persists independently from visible nodes, groups by date, and restores results without plan or unlock promotions.
- Download exports directly without a commercial-rights upsell; Enhance is a deliberate one-click action without an unnecessary prompt.
- Failed generations keep a retryable candidate session and history entry instead of silently disappearing.

## Verification evidence

- `tests/check_smart_canvas_materials.py`: passed.
- Inline application JavaScript syntax: passed through the regression check.
- Backend round-trip for nodes, logs, material/selection/cutout metadata, and `generationHistory`: passed.
- Browser console after the final interaction pass: no application errors.
- Local service: running on port 3000; browser reload and asset requests passed.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the implementation uses a slightly wider right task panel than the reference at 1280 px so nine style labels remain readable; interaction hierarchy and content density remain faithful.

final result: passed

---

# Design QA — PPT v3 full-image and text workbench

- Audit date: 2026-07-24
- Live route: `/static/ppt-workbench.html?project_id=project_590db9b73b224e1699b51088199e5abd`
- State: real 15-page source template, image-review mode, slide 12 text mode, and QA/export-history mode
- Browser viewport: 847 × 871 in the Codex in-app browser

## Visible workflow verification

- The global studio shell remains intact. The local PPT toolbar exposes only the three task modes: `图片 / 文字 / 校验`.
- The left outline exposes all 15 source pages and supports update, pending, completed, protected, and all-page filters.
- Image mode selects PPT objects directly on the slide, distinguishes exact matches, suspected updates, semantic candidates, and protected layout images, and keeps current-project assets ahead of the shared library.
- Text mode displays selectable native text overlays even when LibreOffice cannot render a template's Windows CJK font. Slide 12 visibly exposes `洋房`, `核心筒`, `使用率`, and the table labels.
- QA mode reports blocking issues, warnings, exact no-op matches, pending update confirmations, font substitutions, and versioned export history.

## Functional evidence

- Real-template scan: 15 slides, 26 slide-local image objects, 7 protected layout images, 119 native text shapes, and 255 non-empty table cells.
- Exact-match handling: three project assets are marked `无需替换`; a no-op export leaves every slide XML and slide relationship part byte-identical.
- Migration safety: the old `image43` assignment is no longer reused on slides 13–15; historical exports remain available.
- Rotation safety: 90/270-degree frames normalize only when replacement and original media orientations differ, preventing both uncorrected rotation and double rotation.
- Real no-op export: 15 pages, valid ZIP package, LibreOffice-openable, 50,397,998 bytes, and registered project task/output lineage.
- Browser console: no errors or warnings from the PPT workbench.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: at the narrow 847px browser width, the inspector stacks below the canvas instead of remaining in a third column. No horizontal overflow occurs and the complete workflow remains usable.
- P3: LibreOffice still omits some Chinese glyphs from raster previews despite font mapping. Text mode deliberately overlays the extracted native strings, while exported PPTX files preserve the original template fonts and XML.

final result: passed

---

# Design QA — Architectural PPT workbench

- Source visual truth: `/private/tmp/rom-ai-ppt-studio-0716-audit/01-template.png`
- Implementation screenshot: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-ppt-workbench-v1-edit.png`
- Side-by-side comparison: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-ppt-workbench-source-vs-implementation.png`
- Viewport: 1280 × 720 desktop; source and implementation are both 1280 × 720 at 1× density
- State: real 15-slide source template loaded; template mode; six required architectural image slots visible

## Full-view comparison evidence

- Both views use the same editor hierarchy: mode switch and file actions above; page/slot outline at left; live 16:9 slide in the center; task or template inspector at right.
- The implementation intentionally keeps the existing 48px Infinite Agent Work global shell above the local editor. The colleague reference has no shared product shell.
- The implementation narrows the source's generic 10-page narrative to the requested six replacement pages: one 彩总, one 鸟瞰, and four 户型 pages.

## Focused region comparison evidence

- Left outline: 240px in the implementation versus approximately 242px in the source; page number, thumbnail, title, and required/ready state remain readable.
- Center canvas: the inherited source slide preview is centered on a measured grid stage and replacement images are overlaid using the original PPT object frame and rotation.
- Right inspector: the implementation uses 390px to fit project-scoped template metadata, semantic slot guidance, project assets, quality findings, and export history without truncating core controls.

## Primary interactions tested

- Imported the supplied 68.8MB, 15-slide PPTX and generated all 15 page previews.
- Uploaded 彩总、鸟瞰、户型 replacement images; each upload became a current-project asset.
- Reused the same project-library asset for multiple independent PPT slots.
- Quality mode blocked missing images, then passed at 100/100 after all six slots were assigned.
- Export produced a distinct editable PPTX, recorded a succeeded project generation task, and created seven upstream lineage edges.
- Reload restored the active job, slot assignments, quality state, and export history.
- Browser console check: no application errors.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the implementation preserves the product's global shell, so the local PPT editor has 48px less vertical space than the standalone reference.
- P3: source-template slide 12 already contains an object beyond the slide canvas; the same warning exists in the untouched source deck and is not introduced by replacement.

## Comparison history

- Initial browser capture found a P1 layout issue: the page had scrolled by the global-shell height and hid the local mode toolbar.
- Fix: replaced the app's collapsing top margin with body top padding; post-fix evidence shows the global shell, local mode toolbar, and three-pane editor simultaneously.
- First exported deck exposed a P1 template-object issue on slide 14: its inherited image object rotates media by 270 degrees.
- Fix: template analysis now records object rotation and pre-rotates replacement media before writing it into the inherited relationship. The post-fix slide 14 render is upright and the quality gate passes at 100/100.

final result: passed

---

# Design QA — PPT quick workflow and full-deck preservation

- Audit date: 2026-07-24
- Flow: open an existing project PPT job → confirm all source pages → inspect a rotated template slot → export the complete deck
- Full-page screenshot: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-ppt-quick-workflow-all-pages.png`
- Rotated-slot screenshot: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-ppt-quick-workflow-page14.png`
- Final deck: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/assets/ppt_workbench/project_590db9b73b224e1699b51088199e5abd/pptjob_8c6c9440628c43ecaacc8e5a88d86295/exports/浙江保利杭州运河中地块立项方案-0720(1)-建筑强排-20260724-111343-d4f2d2.pptx`

## Flow steps

1. Import and orientation: healthy. The left pane shows all 15 source pages, not only the six replaceable pages. Non-target pages say “完整保留”; target pages carry a compact “替” marker.
2. Image replacement: healthy. The current status is 6/6, the toolbar offers “下一项”, and the right panel explains the automatic orientation handling.
3. Rotated template object: healthy. Slide 14 has an inherited 270-degree picture frame; the browser preview now uses its displayed bounding box without applying a second visual rotation. The replacement appears upright.
4. Validation and export: healthy. The primary action explicitly says “导出完整 15 页”. The new export contains 15 slide XML files and six independent replacement media files.
5. Persistence and traceability: healthy. The export appears first as “最新可用版本”, while prior outputs are visually demoted to history. The generation task, output asset, and lineage remain project-scoped.

## Highest-impact fixes

- Page-count clarity: replaced the six-task-only outline with the complete 15-page outline.
- Rotation clarity: removed the duplicate CSS rotation and calculated the visual bounding box for 90/270-degree template frames.
- Export isolation: each slot now receives a new media part and only its slide relationship is updated. Original media and unrelated pages remain untouched, including when a template reuses the same media asset.
- Safety gate: export fails if the output slide count differs from the source slide count.
- Faster onboarding: loaded jobs now expose a three-step state and a “下一项” action.

## QA evidence

- Source, prior export, and corrected export all contain 15 slides.
- Corrected PPTX archive integrity: passed.
- Corrected export adds exactly six `ppt/media/iaw_*` media parts.
- Full 15-slide montage inspected; slides 2, 7, and 12–15 inspected individually at full size.
- All six replacement images are upright in the corrected render.
- Browser console errors: none.
- All `tests/check_*.py`: passed.
- `slides_test.py` still flags slide 12 overflow; the same overflow exists in the untouched source deck and is not introduced by this workflow.

## Accessibility and evidence limits

- Page rows and main actions have distinct accessible names; keyboard focus styling remains visible.
- Screenshot review cannot prove complete screen-reader or keyboard-only compliance.
- Visual QA uses LibreOffice rendering. A final PowerPoint-native spot check is still useful for template-specific rendering differences, but the package structure, page count, media relationships, and rendered orientation are verified.

final result: passed

---

# Design QA — Contextual left drawers

- Source visual truth:
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/reports/shenjianghai-ai-system-audit/app-ui-analysis-20260720/frames/02-skills-gallery-1018.jpg`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/reports/shenjianghai-ai-system-audit/app-ui-analysis-20260720/08-current-left-drawer-audit.png`
- Implementation screenshots:
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-context-drawer-agent.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-context-drawer-ai-tools.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-context-drawer-library-full.png`
- Side-by-side comparison: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-context-drawer-comparison.jpg`
- Viewport: 1280 × 720 desktop, dark theme
- State: assistant home, AI image tool index, online image tool, and resource library

## Full-view comparison evidence

- The comparison image places the previous icon-only rail on the left and the implemented assistant context drawer on the right.
- The duplicate Agent, image-tool, knowledge-base, and resource-library module icons are removed from the left side.
- The top bar remains the only global module navigation, while the left side now exposes project conversation context.

## Focused region comparison evidence

- The assistant drawer was inspected at full resolution: title, new-chat action, search input, recent conversation titles, times, and collapse control are readable.
- The AI image state was separately captured with seven labeled tool choices.
- The resource library was captured at full width with the outer drawer hidden; its existing source, category, tag, and search controls remain available.

## Required fidelity surfaces

- Fonts and typography: existing Inter/system typography is preserved. Drawer title, section label, action label, history titles, descriptions, and timestamps establish a readable hierarchy.
- Spacing and layout rhythm: the contextual drawer is 252px wide, flush with the content surface, and uses one divider instead of the previous floating rail. Collapsed width is 64px.
- Colors and visual tokens: current dark tokens and green brand signal are retained; the drawer no longer introduces another global active-module treatment.
- Image quality and asset fidelity: no new raster assets were needed. Existing bundled Lucide icons provide the standard UI symbols.
- Copy and content: left content now describes the current module only. No duplicate `图像工具 / 知识库 / 资源库` global links remain.

## Primary interactions tested

- Assistant conversation search filtered four records down to the one matching `测试成功`.
- AI image top navigation opened the labeled tool drawer.
- Online API image generation opened inside the workbench while the AI tool drawer remained visible and marked the active tool.
- Resource library opened full width and the outer drawer measured `display:none`, width `0`.
- DOM accessibility snapshot exposes named regions, labeled buttons, a named searchbox, and descriptive AI tool buttons.
- Browser console: no application errors. The resource library still emits its pre-existing Tailwind CDN production warning.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the AI image landing state is intentionally sparse because the tool choices moved from the center into the contextual drawer; the next product iteration can use the center for recent canvases and generation tasks.
- P3: the resource library should eventually replace its horizontal category density with a project/category tree, but the current full-width state is clearer than retaining a duplicate outer drawer.

## Comparison history

- The initial state had a P1 navigation-architecture issue: the left icon rail duplicated the global top modules and exposed unnamed icon buttons.
- Fix: replaced it with named, module-specific drawers; removed duplicate global entries; added conversation search; made the library full width.
- Post-fix evidence: the final DOM snapshot exposes readable control names, and the comparison image shows the duplicated rail removed.

final result: passed

---

# Design QA — Unified studio shell

- Source visual truth:
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/reports/shenjianghai-ai-system-audit/app-ui-analysis-20260720/frames/02-skills-gallery-1018.jpg`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/reports/shenjianghai-ai-system-audit/app-ui-analysis-20260720/frames/05b-ai-canvas-stable-1212.jpg`
- Implementation screenshots:
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-agent.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-library.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-generate.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-projects.png`
  - `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-canvas.png`
- Side-by-side comparison: `/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/.codex-unified-shell-comparison.jpg`
- Viewport: 1280 × 720 desktop, dark theme
- State: assistant home and populated smart canvas, with the shared project selected

## Full-view comparison evidence

- The reference and implementation are placed together in the comparison image: reference assistant/canvas on the left, implemented assistant/canvas on the right.
- Both use a persistent black global bar, horizontally ordered work domains, a light active tab, project context at the right, and a module-local work surface below.
- The implementation intentionally keeps the existing Infinite Agent Work dark product language instead of copying the reference app's white content surface.

## Focused region comparison evidence

- Top navigation was checked at full pixel visibility in the individual 1280 × 720 captures.
- The smart-canvas boundary was measured in the browser: global bar bottom `48px`, canvas top `48px`, canvas height `672px`, viewport height `720px`. No canvas or local toolbar is hidden below the shared shell.
- Separate screenshots verify assistant, library, AI image workbench, project workbench, and smart-canvas states; no additional crop was needed because the critical shell controls remain readable at this viewport.

## Required fidelity surfaces

- Fonts and typography: existing Inter/system stack is preserved. The implementation uses larger navigation text than the recording to retain readability; weight, truncation, and small shortcut hierarchy are consistent across modules.
- Spacing and layout rhythm: the 48px fixed bar, active-tab dimensions, project selector, and 32px utility targets align consistently. The four existing pages begin below the bar without overlap.
- Colors and visual tokens: black global layer, off-white active tab, muted inactive text, green project status, and orange-red active underline match the reference hierarchy while using the current product's dark palette.
- Image quality and asset fidelity: the existing `/static/logo.png` is used directly. UI controls use the project's bundled Lucide library; no placeholder, CSS-drawn, emoji, or inline-SVG replacement was introduced.
- Copy and content: the requested domains are exactly `助手 / 素材库 / AI 生图 / 项目管理`, with `Ctrl+1–4`; no work-time entry is present.

## Primary interactions tested

- Click navigation: assistant → library → AI image workbench → project management.
- Keyboard navigation: `Ctrl+1` returned from project management to assistant.
- Global project selector loaded real `/api/projects` results and preserved `studio_active_project_id`.
- Feedback opened as an accessible form and cancelled without writing a test record.
- Smart canvas loaded with the AI image tab active and retained its populated canvas state.
- Browser console: no application errors. One pre-existing Tailwind CDN production warning was observed from the resource-library page.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the reference has denser, smaller global controls; this implementation keeps slightly larger targets for readability and accessibility.
- P3: the resource library still loads Tailwind from its existing CDN integration; this is outside the shared-shell change and should be bundled in a later production-hardening pass.

## Comparison history

- First comparison found no actionable P0/P1/P2 mismatch. The visible differences are deliberate product constraints: removal of the work-time module and preservation of the existing Infinite Agent Work dark content surfaces.

final result: passed
