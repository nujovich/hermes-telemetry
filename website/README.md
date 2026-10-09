# hermes-telemetry docs site

Astro Starlight site published to https://nujovich.github.io/hermes-telemetry/ by
`.github/workflows/docs.yml` on every push to `main` that touches the docs.

```bash
npm ci
npm run dev      # http://localhost:4321/hermes-telemetry/
npm run build    # fails on broken internal links (starlight-links-validator)
```

Pages live in `src/content/docs/`; the sidebar is configured in `astro.config.mjs`.
