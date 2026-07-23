# BUILD-PLAN — Card #190

**Card:** https://github.com/nujovich/hermes-radar/issues/190
**Title:** Antigravity session artifacts surfaced in trace Artifacts tab
**Decision:** Option A (full Artifacts tab with markdown rendering + modal preview)

## Milestones

- [ ] Milestone 1 — Add session artifact scanner endpoint in plugin_api.py and serve.py: scan `~/.hermes/sessions/<id>/` for generated files, return file listing with name, size, type, and modification time
- [ ] Milestone 2 — Add "Artifacts" tab to dashboard HTML: list files with type icons, render markdown files inline, link downloadable files
- [ ] Milestone 3 — Add modal viewer for full-screen artifact preview (markdown rendering, image display, code blocks)