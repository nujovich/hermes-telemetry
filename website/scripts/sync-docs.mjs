// Generates the Internals and Reference sections of the docs site from the
// repo's markdown (ONBOARDING.md, CHANGELOG.md). Run by `predev` / `prebuild`.
// The output directories are build artifacts: gitignored, wiped on every run.
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import GithubSlugger from 'github-slugger';

const REPO = 'https://github.com/nujovich/hermes-telemetry';
// Must match `base` in astro.config.mjs. Cross-page links are emitted as absolute
// paths because starlight-links-validator rejects relative links by default.
export const SITE_BASE = '/hermes-telemetry';
const TOC_TITLE = 'Table of Contents';

// ---------------------------------------------------------------- helpers

/**
 * CommonMark fence tracking. Returns the next fence state ({char, len} or null)
 * and whether `line` is itself a fence line. A closing fence must use the same
 * character, be at least as long as the opener, and carry no info string.
 */
function stepFence(line, fence) {
	const m = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
	if (!m) return { fence, isFence: false };
	const [, run, rest] = m;
	if (!fence) {
		if (run[0] === '`' && rest.includes('`')) return { fence, isFence: false };
		return { fence: { char: run[0], len: run.length }, isFence: true };
	}
	if (run[0] === fence.char && run.length >= fence.len && rest.trim() === '') {
		return { fence: null, isFence: true };
	}
	return { fence, isFence: false };
}

/** Apply `fn` to every line outside fenced code blocks. */
function mapOutsideFences(text, fn) {
	let fence = null;
	return text
		.split('\n')
		.map((line) => {
			const step = stepFence(line, fence);
			fence = step.fence;
			return step.isFence || fence ? line : fn(line);
		})
		.join('\n');
}

/** Apply `fn` to the text of a line outside inline code spans. */
function mapOutsideInlineCode(line, fn) {
	return line
		.split(/(`[^`]*`)/)
		.map((part, i) => (i % 2 ? part : fn(part)))
		.join('');
}

function mapProse(text, fn) {
	return mapOutsideFences(text, (line) => mapOutsideInlineCode(line, fn));
}

/** Headings (outside fences) in document order. */
function collectHeadings(md) {
	const out = [];
	mapOutsideFences(md, (line) => {
		const m = line.match(/^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/);
		if (m) out.push({ level: m[1].length, raw: m[2], text: plainHeadingText(m[2]) });
		return line;
	});
	return out;
}

export function plainHeadingText(raw) {
	return raw
		.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/`/g, '')
		.replace(/\*+/g, '')
		.trim();
}

// Explicit page-slug overrides, keyed by plain heading text. Anchors inside the
// page are unaffected; only the page URL changes.
const SLUG_OVERRIDES = new Map([
	['Canonical telemetry home (HERMES_TELEMETRY_HOME)', 'telemetry-home'],
]);

/** File/URL slug of a page: override, else github-slugger with dash runs collapsed. */
export function pageSlug(title) {
	const plain = plainHeadingText(title);
	if (SLUG_OVERRIDES.has(plain)) return SLUG_OVERRIDES.get(plain);
	return new GithubSlugger()
		.slug(plainHeadingText(title))
		.replace(/-+/g, '-')
		.replace(/^-|-$/g, '');
}

// ------------------------------------------------------------------ split

export function splitOnboarding(md) {
	const preambleLines = [];
	const sections = [];
	let current = null;
	let fence = null;
	for (const line of md.split('\n')) {
		const step = stepFence(line, fence);
		fence = step.fence;
		const h = !fence && !step.isFence ? line.match(/^## +(.*?)(?:[ \t]+#+)?[ \t]*$/) : null;
		if (h) {
			current = { heading: h[1], title: plainHeadingText(h[1]), lines: [] };
			sections.push(current);
		} else if (current) {
			current.lines.push(line);
		} else {
			preambleLines.push(line);
		}
	}
	const trim = (lines) =>
		lines
			.join('\n')
			.replace(/^\s*\n/, '')
			.replace(/\n\s*(---\s*\n?\s*)*$/, '')
			.replace(/^\s+|\s+$/g, '');
	const preamble = trim(preambleLines.filter((l, i, a) => !(i === a.findIndex((x) => /^# /.test(x)) && /^# /.test(l))));
	return {
		preamble: preamble.replace(/^\s+/, ''),
		sections: sections
			.filter((s) => s.title !== TOC_TITLE)
			.map((s) => ({ heading: s.heading, title: s.title, body: trim(s.lines) })),
	};
}

export function demoteHeadings(body) {
	return mapOutsideFences(body, (line) => line.replace(/^(#{3,6})(\s)/, (_, h, s) => h.slice(1) + s));
}

// ----------------------------------------------------------------- anchors

/**
 * Map every anchor of the ORIGINAL document (global slugger, as GitHub
 * numbers duplicates) to {page, anchor} in the generated site. `page` is
 * null for the index page; `anchor` is '' when the heading became the page title.
 */
export function buildAnchorMap(md) {
	const orig = new GithubSlugger();
	const map = new Map();
	let page = null; // null = index (preamble)
	let pageSlugger = new GithubSlugger();
	let dropped = false;
	for (const h of collectHeadings(md)) {
		const origSlug = orig.slug(h.text);
		if (h.level === 1) continue;
		if (h.level === 2) {
			dropped = h.text === TOC_TITLE;
			if (dropped) continue;
			page = pageSlug(h.text);
			pageSlugger = new GithubSlugger();
			map.set(origSlug, { page, anchor: '' });
			continue;
		}
		if (dropped) continue;
		map.set(origSlug, { page, anchor: pageSlugger.slug(h.text) });
	}
	return map;
}

/** Rewrite `](#frag)` links to the generated page that owns the heading. */
export function rewriteAnchors(text, map, currentPage) {
	return mapProse(text, (s) =>
		s.replace(/\]\(#([^)\s]*)((?:\s+"[^"]*")?)\)/g, (_, frag, title) => {
			const target = map.get(frag);
			if (!target) throw new Error(`Unresolved anchor #${frag} (page: ${currentPage ?? 'index'})`);
			if (target.page === currentPage && target.anchor) return `](#${target.anchor}${title})`;
			const dir = target.page === null ? '' : `${target.page}/`;
			const prefix = `${SITE_BASE}/internals/`;
			const hash = target.anchor ? `#${target.anchor}` : '';
			return `](${prefix}${dir}${hash}${title})`;
		}),
	);
}

/** Rewrite relative repo paths to GitHub URLs; leave URLs and anchors alone. */
export function rewriteRepoPaths(text) {
	return mapProse(text, (s) =>
		s.replace(/\]\(([^)\s]+)((?:\s+"[^"]*")?)\)/g, (whole, href, title) => {
			if (/^([a-z][a-z0-9+.-]*:|#|\/)/i.test(href)) return whole;
			const path = href.replace(/^(\.\/)+/, '');
			const dir = path.endsWith('/');
			return `](${REPO}/${dir ? 'tree' : 'blob'}/main/${dir ? path.slice(0, -1) : path}${title})`;
		}),
	);
}

// ------------------------------------------------------------------ render

export function renderPage({ title, order, editPath, body, description }) {
	const fm = [
		'---',
		`title: ${JSON.stringify(title)}`,
		...(description ? [`description: ${JSON.stringify(description)}`] : []),
		'sidebar:',
		`  order: ${order}`,
		`editUrl: ${JSON.stringify(`${REPO}/edit/main/${editPath}`)}`,
		'---',
	].join('\n');
	const authority = editPath === 'ONBOARDING.md' ? ' — the design authority for this project.' : '.';
	const note =
		`:::note\nGenerated from [${editPath}](${REPO}/blob/main/${editPath})${authority} ` +
		'Edit the source file, not this page.\n:::';
	return `${fm}\n\n${note}\n\n${body.replace(/\s+$/, '')}\n`;
}

/** ONBOARDING.md -> [{file, content}] for src/content/docs/internals/. */
export function buildInternalsPages(md) {
	const { preamble, sections } = splitOnboarding(md);
	const map = buildAnchorMap(md);
	// Repo paths first: anchor rewriting emits relative `../page/` links that must not be re-read as repo paths.
	const fix = (text, page) => rewriteAnchors(rewriteRepoPaths(text), map, page);

	const slugs = sections.map((s) => pageSlug(s.title));
	const seen = new Map();
	sections.forEach((s, i) => {
		if (seen.has(slugs[i])) {
			throw new Error(
				`Duplicate page slug "${slugs[i]}": "${seen.get(slugs[i])}" and "${s.title}" both map to it`,
			);
		}
		seen.set(slugs[i], s.title);
	});
	const list = sections.map((s, i) => `- [${s.title}](${SITE_BASE}/internals/${slugs[i]}/)`).join('\n');
	const indexBody = `${fix(preamble, null)}\n\n## Pages\n\n${list}`;

	const pages = [
		{
			file: 'index.md',
			content: renderPage({
				title: 'Internals overview',
				description:
					'Design and implementation notes for hermes-telemetry contributors, generated from ONBOARDING.md.',
				order: 0,
				editPath: 'ONBOARDING.md',
				body: indexBody,
			}),
		},
	];
	sections.forEach((s, i) => {
		pages.push({
			file: `${slugs[i]}.md`,
			content: renderPage({
				title: s.title,
				order: i + 1,
				editPath: 'ONBOARDING.md',
				body: fix(demoteHeadings(s.body), slugs[i]),
			}),
		});
	});
	return pages;
}

/** CHANGELOG.md -> content of reference/changelog.md. */
export function prepareChangelog(md) {
	const body = md.replace(/^# .*\n+/, '');
	return renderPage({
		title: 'Changelog',
		order: 1,
		editPath: 'CHANGELOG.md',
		body: rewriteRepoPaths(body),
	});
}

// -------------------------------------------------------------------- main

function main() {
	const siteRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
	const repoRoot = join(siteRoot, '..');
	const docs = join(siteRoot, 'src', 'content', 'docs');

	const internals = buildInternalsPages(readFileSync(join(repoRoot, 'ONBOARDING.md'), 'utf8'));
	const changelog = prepareChangelog(readFileSync(join(repoRoot, 'CHANGELOG.md'), 'utf8'));

	for (const dir of ['internals', 'reference']) rmSync(join(docs, dir), { recursive: true, force: true });
	mkdirSync(join(docs, 'internals'), { recursive: true });
	mkdirSync(join(docs, 'reference'), { recursive: true });
	for (const p of internals) writeFileSync(join(docs, 'internals', p.file), p.content);
	writeFileSync(join(docs, 'reference', 'changelog.md'), changelog);
	console.log(`sync-docs: wrote ${internals.length} internals pages + changelog`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
