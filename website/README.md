# hermes-telemetry docs site

Astro Starlight site published to https://nujovich.github.io/hermes-telemetry/ by
`.github/workflows/docs.yml` on every push to `main` that touches the docs.

```bash
npm ci
npm run dev      # http://localhost:4321/hermes-telemetry/
npm run build    # fails on broken internal links (starlight-links-validator)
```

Pages live in `src/content/docs/`; the sidebar is configured in `astro.config.mjs`.

The **Internals** and **Reference** (Changelog) sections are generated at build time by
`scripts/sync-docs.mjs` (run by `predev` / `prebuild`) from `ONBOARDING.md` and `CHANGELOG.md`
at the repo root. The output under `src/content/docs/internals/` and `src/content/docs/reference/`
is gitignored and rewritten on every run: edit `ONBOARDING.md` / `CHANGELOG.md`, never the
generated pages. `npm test` runs the generator's unit tests.
