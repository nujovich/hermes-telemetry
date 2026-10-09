import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
	plainHeadingText,
	splitOnboarding,
	demoteHeadings,
	buildAnchorMap,
	rewriteAnchors,
	rewriteRepoPaths,
	pageSlug,
	SITE_BASE,
	renderPage,
	buildInternalsPages,
	prepareChangelog,
} from './sync-docs.mjs';

const DOC = [
	'# Title — Notes',
	'',
	'> intro',
	'',
	'---',
	'',
	'## Table of Contents',
	'',
	'1. [Alpha](#alpha)',
	'2. [Beta Part](#beta-part)',
	'',
	'---',
	'',
	'## Alpha',
	'',
	'Text. See [beta](#beta-part) and [sub](#details).',
	'',
	'### Details',
	'',
	'```bash',
	'# not a heading',
	'## nor this',
	'```',
	'',
	'## Beta Part',
	'',
	'### Details',
	'',
	'Link to [other details](#details-1), [own title](#beta-part), [alpha](#alpha).',
	'',
].join('\n');

test('plainHeadingText strips inline markup', () => {
	assert.equal(plainHeadingText('The `foo` **bar** [link](http://x)'), 'The foo bar link');
});

test('pageSlug collapses punctuation runs', () => {
	assert.equal(pageSlug('Agent Intelligence (efficiency · smells · forecast)'), 'agent-intelligence-efficiency-smells-forecast');
	assert.equal(pageSlug('Free→Paid Model Transition Alert'), 'freepaid-model-transition-alert');
});

test('splitOnboarding separates preamble, drops the TOC, ignores fenced ##', () => {
	const { preamble, sections } = splitOnboarding(DOC);
	assert.deepEqual(sections.map((s) => s.title), ['Alpha', 'Beta Part']);
	assert.match(preamble, /^> intro/);
	assert.doesNotMatch(preamble, /^# /m);
	assert.doesNotMatch(preamble, /---\s*$/);
	assert.match(sections[0].body, /## nor this/);
});

test('demoteHeadings demotes headings but not fenced lines', () => {
	const out = demoteHeadings('### A\n\n#### B\n\n```\n### keep\n```\n');
	assert.equal(out, '## A\n\n### B\n\n```\n### keep\n```\n');
});

test('anchor map handles titles, duplicates with per-page counters', () => {
	const map = buildAnchorMap(DOC);
	assert.deepEqual(map.get('alpha'), { page: 'alpha', anchor: '' });
	assert.deepEqual(map.get('beta-part'), { page: 'beta-part', anchor: '' });
	// first "Details" is global `details`, second is global `details-1` but `details` on its own page
	assert.deepEqual(map.get('details'), { page: 'alpha', anchor: 'details' });
	assert.deepEqual(map.get('details-1'), { page: 'beta-part', anchor: 'details' });
});

test('rewriteAnchors resolves same-page, cross-page and title anchors', () => {
	const map = buildAnchorMap(DOC);
	const { sections } = splitOnboarding(DOC);
	const a = rewriteAnchors(sections[0].body, map, 'alpha');
	assert.match(a, /\]\(\/hermes-telemetry\/internals\/beta-part\/\)/);
	assert.match(a, /\]\(#details\)/);
	const b = rewriteAnchors(sections[1].body, map, 'beta-part');
	assert.match(b, /\]\(#details\)/); // details-1 -> own page anchor
	assert.match(b, /\]\(\/hermes-telemetry\/internals\/beta-part\/\)/);
	assert.match(b, /\]\(\/hermes-telemetry\/internals\/alpha\/\)/);
});

test('rewriteAnchors emits absolute links from the index page and skips code', () => {
	const map = buildAnchorMap(DOC);
	assert.equal(rewriteAnchors('[x](#alpha)', map, null), '[x](/hermes-telemetry/internals/alpha/)');
	assert.equal(rewriteAnchors('`[x](#nope)`', map, null), '`[x](#nope)`');
	assert.equal(rewriteAnchors('```\n[x](#nope)\n```', map, null), '```\n[x](#nope)\n```');
});

test('rewriteAnchors throws on an unknown anchor', () => {
	const map = buildAnchorMap(DOC);
	assert.throws(() => rewriteAnchors('[x](#nope)', map, 'alpha'), /#nope/);
});

test('rewriteRepoPaths rewrites relative paths only', () => {
	const out = rewriteRepoPaths(
		'[a](db.py) [b](./tests/x.py) [c](docs/) [d](https://e.com/x) [e](#frag) [f](mailto:a@b.c) [g](/abs)',
	);
	assert.equal(
		out,
		'[a](https://github.com/nujovich/hermes-telemetry/blob/main/db.py) ' +
			'[b](https://github.com/nujovich/hermes-telemetry/blob/main/tests/x.py) ' +
			'[c](https://github.com/nujovich/hermes-telemetry/tree/main/docs) ' +
			'[d](https://e.com/x) [e](#frag) [f](mailto:a@b.c) [g](/abs)',
	);
	assert.equal(rewriteRepoPaths('`[a](db.py)`'), '`[a](db.py)`');
});

test('renderPage emits frontmatter, aside and body', () => {
	const out = renderPage({
		title: 'A "quoted" title',
		order: 3,
		editPath: 'ONBOARDING.md',
		body: '## X\n',
	});
	assert.match(out, /^---\ntitle: "A \\"quoted\\" title"\n/);
	assert.match(out, /sidebar:\n {2}order: 3\n/);
	assert.match(out, /editUrl: "https:\/\/github.com\/nujovich\/hermes-telemetry\/edit\/main\/ONBOARDING.md"/);
	assert.match(out, /:::note\nGenerated from \[ONBOARDING.md\]\(https:\/\/github.com\/nujovich\/hermes-telemetry\/blob\/main\/ONBOARDING.md\)/);
	assert.match(out, /Edit the source file, not this page\.\n:::\n\n## X\n$/);
});

test('buildInternalsPages: index lists every page, sections demoted and linked', () => {
	const pages = buildInternalsPages(DOC);
	assert.deepEqual(pages.map((p) => p.file), ['index.md', 'alpha.md', 'beta-part.md']);
	assert.match(pages[0].content, /\[Alpha\]\(\/hermes-telemetry\/internals\/alpha\/\)/);
	assert.match(pages[0].content, /\[Beta Part\]\(\/hermes-telemetry\/internals\/beta-part\/\)/);
	assert.doesNotMatch(pages[1].content, /^## Alpha/m);
	assert.match(pages[1].content, /^## Details/m);
	assert.match(pages[1].content, /# not a heading/);
});

test('prepareChangelog drops the H1 and keeps version headings', () => {
	const out = prepareChangelog('# Changelog\n\nIntro.\n\n## [1.0.0] - 2026-01-01\n\n- x\n');
	assert.match(out, /title: "Changelog"/);
	assert.match(out, /^## \[1\.0\.0\] - 2026-01-01$/m);
	assert.doesNotMatch(out, /^# Changelog/m);
	assert.match(out, /edit\/main\/CHANGELOG.md/);
});

test('buildInternalsPages keeps rewritten anchors out of the repo-path rewrite', () => {
	const pages = buildInternalsPages(DOC);
	assert.match(pages[1].content, /\]\(\/hermes-telemetry\/internals\/beta-part\/\)/);
	assert.doesNotMatch(pages[1].content, /blob\/main\/\.\./);
});

test('duplicate page slugs throw, naming both headings', () => {
	const md = '# T\n\n## A b\n\nx\n\n## A-b\n\ny\n';
	assert.throws(() => buildInternalsPages(md), /"A b".*"A-b"|"A-b".*"A b"/);
});

test('SITE_BASE matches base in astro.config.mjs', () => {
	const cfg = readFileSync(new URL('../astro.config.mjs', import.meta.url), 'utf8');
	const m = cfg.match(/\bbase:\s*['"]([^'"]+)['"]/);
	assert.ok(m, 'base not found in astro.config.mjs');
	assert.equal(SITE_BASE, m[1]);
});

test('fences follow CommonMark: longer outer fence is not closed by an inner one', () => {
	const md = '````md\n```python\n## not a heading\n```\n## still inside\n````\n## Real\n';
	const inner = '````md\n```python\n### inner\n```\n### still inside\n````\n';
	assert.equal(demoteHeadings(`${inner}### Out\n`), `${inner}## Out\n`);
	const { sections } = splitOnboarding('# T\n\n' + md);
	assert.deepEqual(sections.map((s) => s.title), ['Real']);
});

test('a fence closer with an info string does not close the fence', () => {
	const { sections } = splitOnboarding('# T\n\n```\n```js\n## inside\n```\n## Out\n');
	assert.deepEqual(sections.map((s) => s.title), ['Out']);
});

test('heading closing hashes are stripped only when preceded by a space', () => {
	const { sections } = splitOnboarding('# T\n\n## C#\n\nx\n\n## D ##\n\ny\n');
	assert.deepEqual(sections.map((s) => s.title), ['C#', 'D']);
});

test('slug override gives the canonical telemetry home page a short slug', () => {
	const md =
		'# T\n\n## Canonical telemetry home (`HERMES_TELEMETRY_HOME`)\n\n### Sub\n\n## Other\n\nSee [s](#sub) and [t](#canonical-telemetry-home-hermes_telemetry_home).\n';
	const pages = buildInternalsPages(md);
	assert.deepEqual(pages.map((p) => p.file), ['index.md', 'telemetry-home.md', 'other.md']);
	assert.match(pages[2].content, /\]\(\/hermes-telemetry\/internals\/telemetry-home\/#sub\)/);
	assert.match(pages[2].content, /\]\(\/hermes-telemetry\/internals\/telemetry-home\/\)/);
	assert.match(pages[0].content, /\(\/hermes-telemetry\/internals\/telemetry-home\/\)/);
});
