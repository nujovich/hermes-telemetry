// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

// https://astro.build/config
export default defineConfig({
	site: 'https://nujovich.github.io',
	base: '/hermes-telemetry',
	integrations: [
		starlight({
			title: 'hermes-telemetry',
			logo: { src: './src/assets/logo.svg', alt: '' },
			customCss: ['./src/styles/theme.css'],
			components: { Footer: './src/components/Footer.astro' },
			plugins: [starlightLinksValidator()],
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/nujovich/hermes-telemetry' },
			],
			editLink: {
				baseUrl: 'https://github.com/nujovich/hermes-telemetry/edit/main/website/',
			},
			// Planned groups, added as their pages land: Guides, Concepts. Internals and Reference
			// are generated from ONBOARDING.md / CHANGELOG.md by scripts/sync-docs.mjs.
			sidebar: [
				{
					label: 'Getting started',
					items: [{ label: 'Introduction', slug: 'getting-started/introduction' }],
				},
				{
					label: 'Internals',
					collapsed: true,
					items: [{ autogenerate: { directory: 'internals' } }],
				},
				{
					label: 'Reference',
					items: [{ label: 'Changelog', slug: 'reference/changelog' }],
				},
			],
		}),
	],
});
