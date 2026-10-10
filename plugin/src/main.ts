/**
 * Postcard Bridge — renders one note with Obsidian's own engine (theme, snippets,
 * plugins) and writes a frozen snapshot that the `postcard` CLI turns into a page.
 *
 * Protocol (driven by the CLI):
 *   1. CLI writes  ~/.cache/postcard/<job>/request.json
 *   2. CLI opens   obsidian://postcard?vault=<name>&req=<path to request.json>
 *   3. We write    ack.json (got it) … content.html, styles.css, assets/*, meta.json … done.json (last)
 */
import { App, Component, MarkdownRenderer, Notice, Plugin, TFile, getFrontMatterInfo } from "obsidian";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

interface RenderRequest {
	version: number;
	file: string; // vault-relative path of the note
	out: string; // absolute output dir (inside the postcard cache dir)
	theme_css: string | null; // absolute path to the theme.css to publish with, null = Obsidian default theme
	snippet_css: string[]; // absolute paths of CSS snippets to include
	mode: "dark" | "light";
	settle_ms: number;
	timeout_ms: number;
}

const ASSET_TOKEN = "__POSTCARD_ASSET__/";
const CACHE_ROOT = path.join(os.homedir(), ".cache", "postcard") + path.sep;

// Elements that make sense inside Obsidian but not on a static page.
const STRIP_SELECTORS = [
	".markdown-embed-link",
	".edit-block-button",
	".heading-collapse-indicator",
	".list-collapse-indicator",
	".frontmatter",
	".frontmatter-container",
	".metadata-container",
	".mod-header",
	".mod-footer",
];

export default class PostcardBridge extends Plugin {
	private busy: Promise<void> = Promise.resolve();

	async onload() {
		this.registerObsidianProtocolHandler("postcard", (params) => {
			// Render jobs run one at a time; they share the off-screen container.
			this.busy = this.busy.then(() => this.handle(params.req)).catch(() => {});
		});
	}

	private async handle(reqPath: string | undefined) {
		if (!reqPath) return;
		const resolved = path.resolve(reqPath);
		// Only accept jobs the CLI created in its own cache dir. A web page can open
		// obsidian:// links, but it can't put a request file there.
		if (!resolved.startsWith(CACHE_ROOT) || !fs.existsSync(resolved)) {
			new Notice("Postcard: ignored a render request from outside the postcard cache.");
			return;
		}
		const req = JSON.parse(fs.readFileSync(resolved, "utf8")) as RenderRequest;
		const out = path.resolve(req.out);
		if (!out.startsWith(CACHE_ROOT)) return;

		fs.mkdirSync(path.join(out, "assets"), { recursive: true });
		writeJson(path.join(out, "ack.json"), { ok: true, obsidian: (this.app as AppInternals).appVersion ?? null });
		try {
			await new Renderer(this.app, req, out).run();
			writeJson(path.join(out, "done.json"), { ok: true });
		} catch (e) {
			console.error("Postcard render failed", e);
			writeJson(path.join(out, "done.json"), { ok: false, error: String((e as Error)?.stack ?? e) });
		}
	}
}

interface AppInternals {
	appVersion?: string;
	customCss?: {
		styleEl?: HTMLStyleElement;
		extraStyleEls?: HTMLStyleElement[];
		theme?: string;
		enabledSnippets?: Set<string>;
	};
	vault: { getConfig?: (key: string) => unknown; adapter: { getBasePath?: () => string } };
}

class Renderer {
	private warnings: string[] = [];
	private assetCount = 0;
	private assetsBySource = new Map<string, string>();

	constructor(private app: App, private req: RenderRequest, private out: string) {}

	async run() {
		const file = this.app.vault.getAbstractFileByPath(this.req.file);
		if (!(file instanceof TFile)) throw new Error(`Note not found in vault: ${this.req.file}`);

		const raw = await this.app.vault.read(file);
		const body = raw.slice(getFrontMatterInfo(raw).contentStart);

		// Render inside the viewport (invisible) so lazy, size-aware plugins (charts, excalidraw) draw.
		const host = document.body.createDiv();
		host.setAttr(
			"style",
			"position:fixed;inset:0;opacity:0;pointer-events:none;z-index:-1;overflow:auto;",
		);
		const view = host.createDiv({
			cls: "markdown-preview-view markdown-rendered is-readable-line-width allow-fold-headings show-indentation-guide",
		});
		const sizer = view.createDiv({ cls: "markdown-preview-sizer markdown-preview-section" });
		const component = new Component();
		component.load();
		try {
			await MarkdownRenderer.render(this.app, body, sizer, file.path, component);
			await settle(sizer, this.req.settle_ms, this.req.timeout_ms);
			if (sizer.querySelector("canvas")) await sleep(1500); // let chart animations finish

			this.freeze(sizer, file);
			await this.collectMedia(sizer);
			fs.writeFileSync(path.join(this.out, "content.html"), sizer.innerHTML);
			fs.writeFileSync(path.join(this.out, "styles.css"), await this.collectCss());
			writeJson(path.join(this.out, "meta.json"), {
				body_class: bodyClass(this.req.mode),
				body_style: document.body.getAttribute("style") ?? "",
				warnings: this.warnings,
			});
		} finally {
			component.unload();
			host.remove();
		}
	}

	/** Turn the live, interactive DOM into something a static page can show. */
	private freeze(root: HTMLElement, file: TFile) {
		root.querySelectorAll(STRIP_SELECTORS.join(",")).forEach((el) => el.remove());
		root.querySelectorAll("[contenteditable]").forEach((el) => el.removeAttribute("contenteditable"));
		root.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((el) => {
			if (el.checked) el.setAttribute("checked", "");
			el.setAttribute("disabled", "");
		});

		// Stable ids on headings so [[#Heading]] and the TOC can link to them.
		const used = new Set<string>();
		root.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
			let id = slug(h.getAttribute("data-heading") ?? h.textContent ?? "");
			for (let n = 2; used.has(id); n++) id = `${slug(h.textContent ?? "")}-${n}`;
			used.add(id);
			h.id = id;
		});

		root.querySelectorAll<HTMLAnchorElement>("a.internal-link").forEach((a) => this.fixInternalLink(a, file));
		root.querySelectorAll<HTMLAnchorElement>("a.tag").forEach((a) => {
			const span = createSpan({ cls: a.className, text: a.textContent ?? "" });
			a.replaceWith(span);
		});
		root.querySelectorAll<HTMLAnchorElement>("a.external-link, a[href^='http']").forEach((a) => {
			a.target = "_blank";
			a.rel = "noopener noreferrer";
		});

		// Charts and other canvases: snapshot to PNG.
		root.querySelectorAll("canvas").forEach((canvas) => {
			try {
				const name = this.writeAsset(dataUrlBytes(canvas.toDataURL("image/png")), "png", null);
				const img = createEl("img", { attr: { src: ASSET_TOKEN + name } });
				img.setAttr("style", `width:${canvas.clientWidth || canvas.width}px;max-width:100%;height:auto;`);
				canvas.replaceWith(img);
			} catch (e) {
				this.warnings.push(`Could not snapshot a chart/canvas: ${e}`);
			}
		});

		const pdfs = root.querySelectorAll("iframe, .pdf-embed");
		if (pdfs.length) this.warnings.push(`${pdfs.length} embedded PDF/iframe(s) won't render on the page.`);
	}

	private fixInternalLink(a: HTMLAnchorElement, file: TFile) {
		const target = a.getAttribute("data-href") ?? a.getAttribute("href") ?? "";
		const hash = target.indexOf("#");
		const linkpath = hash === -1 ? target : target.slice(0, hash);
		const sub = hash === -1 ? "" : target.slice(hash + 1);
		const anchor = sub && !sub.startsWith("^") ? "#" + slug(sub.split("#").pop() ?? "") : "";
		a.removeAttribute("data-href");
		a.removeAttribute("target");

		const dest = linkpath ? this.app.metadataCache.getFirstLinkpathDest(linkpath, file.path) : file;
		if (dest && dest.path === file.path) {
			a.href = anchor || "#";
			return;
		}
		const fm = dest ? this.app.metadataCache.getFileCache(dest)?.frontmatter : undefined;
		if (fm?.postcard_status === "live" && typeof fm.postcard_url === "string") {
			a.href = fm.postcard_url + anchor;
			return;
		}
		// Not published: keep the text, drop the link, so private note names don't become URLs.
		const span = createSpan({ cls: "internal-link postcard-unpublished-link", text: a.textContent ?? "" });
		a.replaceWith(span);
		this.warnings.push(`Linked note isn't published, shown as plain text: [[${linkpath}]]`);
	}

	/** Copy every image/video the page uses out of the vault (or blob/data URL) into assets/. */
	private async collectMedia(root: HTMLElement) {
		const els = root.querySelectorAll<HTMLElement>("img[src], video[src], audio[src], source[src]");
		for (const el of Array.from(els)) {
			const src = el.getAttribute("src") ?? "";
			if (src.startsWith(ASSET_TOKEN)) continue;
			el.removeAttribute("srcset");
			if (/^https?:/i.test(src)) {
				this.warnings.push(`External image stays hot-linked (viewers' browsers will fetch it): ${src}`);
				continue;
			}
			try {
				const { bytes, ext, sourceFile } = await readResource(src);
				el.setAttribute("src", ASSET_TOKEN + this.writeAsset(bytes, ext, sourceFile ?? src));
			} catch (e) {
				this.warnings.push(`Could not copy media ${src.slice(0, 120)}: ${e}`);
			}
		}
	}

	/**
	 * Obsidian's own CSS + plugin styles + Style Settings, minus the active theme and snippets,
	 * plus the theme and snippets the CLI asked for. url(...) assets are copied out too.
	 */
	private async collectCss(): Promise<string> {
		const internals = this.app as unknown as AppInternals;
		const skip = new Set<Element>();
		if (internals.customCss?.styleEl) skip.add(internals.customCss.styleEl);
		internals.customCss?.extraStyleEls?.forEach((el) => skip.add(el));
		const skipText = new Set(this.activeThemeAndSnippetTexts());

		const parts: string[] = [];
		for (const sheet of Array.from(document.styleSheets)) {
			const owner = sheet.ownerNode as Element | null;
			if (owner && (skip.has(owner) || skipText.has((owner.textContent ?? "").trim()))) continue;
			let rules: CSSRuleList;
			try {
				rules = sheet.cssRules;
			} catch {
				continue; // cross-origin sheet
			}
			const text = Array.from(rules)
				.map((r) => r.cssText)
				.join("\n");
			parts.push(await this.inlineCssUrls(text, sheet.href ?? document.baseURI));
		}

		const extra = [this.req.theme_css, ...this.req.snippet_css].filter((p): p is string => !!p);
		for (const file of extra) {
			const text = fs.readFileSync(file, "utf8");
			parts.push(await this.inlineCssUrls(text, "file://" + path.dirname(file) + "/"));
		}
		return parts.join("\n\n");
	}

	private activeThemeAndSnippetTexts(): string[] {
		const internals = this.app as unknown as AppInternals;
		const base = internals.vault.adapter.getBasePath?.();
		if (!base) return [];
		const configDir = path.join(base, this.app.vault.configDir);
		const texts: string[] = [];
		const theme = internals.vault.getConfig?.("cssTheme");
		if (typeof theme === "string" && theme) {
			const p = path.join(configDir, "themes", theme, "theme.css");
			if (fs.existsSync(p)) texts.push(fs.readFileSync(p, "utf8").trim());
		}
		const snippets = internals.vault.getConfig?.("enabledCssSnippets");
		if (Array.isArray(snippets)) {
			for (const s of snippets) {
				const p = path.join(configDir, "snippets", `${s}.css`);
				if (fs.existsSync(p)) texts.push(fs.readFileSync(p, "utf8").trim());
			}
		}
		return texts;
	}

	private async inlineCssUrls(css: string, base: string): Promise<string> {
		const re = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g;
		const urls = new Set<string>();
		for (const m of css.matchAll(re)) urls.add(m[2]);
		const replacements = new Map<string, string>();
		for (const u of urls) {
			if (/^(data:|#|https?:)/i.test(u)) continue;
			try {
				const abs = new URL(u, base).href;
				const { bytes, ext } = await readResource(abs);
				replacements.set(u, ASSET_TOKEN + this.writeAsset(bytes, ext, abs));
			} catch {
				replacements.set(u, ""); // unreachable app resource: drop it rather than leak app:// URLs
			}
		}
		return css.replace(re, (whole, _q, u: string) => {
			const r = replacements.get(u);
			if (r === undefined) return whole;
			return r ? `url("${r}")` : "none";
		});
	}

	private writeAsset(bytes: Uint8Array, ext: string, source: string | null): string {
		if (source && this.assetsBySource.has(source)) return this.assetsBySource.get(source)!;
		const name = `${++this.assetCount}.${ext || "bin"}`;
		fs.writeFileSync(path.join(this.out, "assets", name), bytes);
		if (source) this.assetsBySource.set(source, name);
		return name;
	}
}

async function readResource(src: string): Promise<{ bytes: Uint8Array; ext: string; sourceFile?: string }> {
	if (src.startsWith("data:")) {
		const mime = src.slice(5, src.indexOf(";"));
		return { bytes: dataUrlBytes(src), ext: extFromMime(mime) };
	}
	const url = new URL(src);
	if (url.protocol === "file:" || (url.protocol === "app:" && url.host !== "obsidian.md")) {
		// app://<id>/Users/.../photo.jpg?123 is a vault file on disk.
		const file = decodeURIComponent(url.pathname);
		return { bytes: fs.readFileSync(file), ext: path.extname(file).slice(1).toLowerCase(), sourceFile: file };
	}
	const res = await fetch(src); // app://obsidian.md/... bundled resources, blob: URLs
	if (!res.ok) throw new Error(`HTTP ${res.status}`);
	const ext = path.extname(url.pathname).slice(1).toLowerCase() || extFromMime(res.headers.get("content-type") ?? "");
	return { bytes: new Uint8Array(await res.arrayBuffer()), ext };
}

function bodyClass(mode: "dark" | "light"): string {
	const drop = new Set(["theme-dark", "theme-light", "is-focused", "is-frameless", "is-hidden-frameless", "mod-macos", "mod-windows", "mod-linux", "is-translucent"]);
	const keep = Array.from(document.body.classList).filter((c) => !drop.has(c));
	return [...keep, `theme-${mode}`].join(" ");
}

function settle(el: HTMLElement, quietMs: number, maxMs: number): Promise<void> {
	return new Promise((resolve) => {
		let quiet = window.setTimeout(done, quietMs);
		const hard = window.setTimeout(done, maxMs);
		const obs = new MutationObserver(() => {
			window.clearTimeout(quiet);
			quiet = window.setTimeout(done, quietMs);
		});
		obs.observe(el, { subtree: true, childList: true, attributes: true, characterData: true });
		function done() {
			obs.disconnect();
			window.clearTimeout(quiet);
			window.clearTimeout(hard);
			resolve();
		}
	});
}

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

function slug(text: string): string {
	return (
		text
			.trim()
			.toLowerCase()
			.replace(/[^\p{L}\p{N}\s-]/gu, "")
			.replace(/\s+/g, "-")
			.replace(/-+/g, "-")
			.replace(/^-|-$/g, "") || "section"
	);
}

function dataUrlBytes(url: string): Uint8Array {
	const comma = url.indexOf(",");
	const meta = url.slice(0, comma);
	const data = url.slice(comma + 1);
	return meta.endsWith(";base64") ? Buffer.from(data, "base64") : Buffer.from(decodeURIComponent(data), "utf8");
}

function extFromMime(mime: string): string {
	const m = mime.split(";")[0].trim().toLowerCase();
	const map: Record<string, string> = {
		"image/png": "png",
		"image/jpeg": "jpg",
		"image/gif": "gif",
		"image/webp": "webp",
		"image/svg+xml": "svg",
		"font/woff2": "woff2",
		"font/woff": "woff",
		"font/ttf": "ttf",
		"video/mp4": "mp4",
	};
	return map[m] ?? "bin";
}

function writeJson(file: string, data: unknown) {
	// Write-then-rename so the CLI never reads a half-written file.
	fs.writeFileSync(file + ".tmp", JSON.stringify(data, null, 2));
	fs.renameSync(file + ".tmp", file);
}
