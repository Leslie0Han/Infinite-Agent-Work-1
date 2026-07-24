# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Before a Sites handoff, run `npm run build` and verify both `dist/server/index.js` and `dist/.openai/hosting.json`. Treat the release as complete only after the final deployment reports `succeeded` and the canonical public URL opens with the expected title; an earlier successful version is not evidence for the latest one.
