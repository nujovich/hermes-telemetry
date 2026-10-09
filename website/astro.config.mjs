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
			// Planned groups, added as their pages land: Guides, Concepts, Internals, Reference.
			sidebar: [
				{
					label: 'Getting started',
					items: [{ label: 'Introduction', slug: 'getting-started/introduction' }],
				},
			],
		}),
	],
});
