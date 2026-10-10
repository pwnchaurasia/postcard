"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/main.ts
var main_exports = {};
__export(main_exports, {
  default: () => PostcardBridge
});
module.exports = __toCommonJS(main_exports);
var import_obsidian = require("obsidian");
var fs = __toESM(require("fs"));
var os = __toESM(require("os"));
var path = __toESM(require("path"));
var ASSET_TOKEN = "__POSTCARD_ASSET__/";
var CACHE_ROOT = path.join(os.homedir(), ".cache", "postcard") + path.sep;
var STRIP_SELECTORS = [
  ".markdown-embed-link",
  ".edit-block-button",
  ".heading-collapse-indicator",
  ".list-collapse-indicator",
  ".frontmatter",
  ".frontmatter-container",
  ".metadata-container",
  ".mod-header",
  ".mod-footer"
];
var PostcardBridge = class extends import_obsidian.Plugin {
  constructor() {
    super(...arguments);
    this.busy = Promise.resolve();
  }
  async onload() {
    this.registerObsidianProtocolHandler("postcard", (params) => {
      this.busy = this.busy.then(() => this.handle(params.req)).catch(() => {
      });
    });
  }
  async handle(reqPath) {
    if (!reqPath) return;
    const resolved = path.resolve(reqPath);
    if (!resolved.startsWith(CACHE_ROOT) || !fs.existsSync(resolved)) {
      new import_obsidian.Notice("Postcard: ignored a render request from outside the postcard cache.");
      return;
    }
    const req = JSON.parse(fs.readFileSync(resolved, "utf8"));
    const out = path.resolve(req.out);
    if (!out.startsWith(CACHE_ROOT)) return;
    fs.mkdirSync(path.join(out, "assets"), { recursive: true });
    writeJson(path.join(out, "ack.json"), { ok: true, obsidian: this.app.appVersion ?? null });
    try {
      await new Renderer(this.app, req, out).run();
      writeJson(path.join(out, "done.json"), { ok: true });
    } catch (e) {
      console.error("Postcard render failed", e);
      writeJson(path.join(out, "done.json"), { ok: false, error: String(e?.stack ?? e) });
    }
  }
};
var Renderer = class {
  constructor(app, req, out) {
    this.app = app;
    this.req = req;
    this.out = out;
    this.warnings = [];
    this.assetCount = 0;
    this.assetsBySource = /* @__PURE__ */ new Map();
  }
  async run() {
    const file = this.app.vault.getAbstractFileByPath(this.req.file);
    if (!(file instanceof import_obsidian.TFile)) throw new Error(`Note not found in vault: ${this.req.file}`);
    const raw = await this.app.vault.read(file);
    const body = raw.slice((0, import_obsidian.getFrontMatterInfo)(raw).contentStart);
    const host = document.body.createDiv();
    host.setAttr(
      "style",
      "position:fixed;inset:0;opacity:0;pointer-events:none;z-index:-1;overflow:auto;"
    );
    const view = host.createDiv({
      cls: "markdown-preview-view markdown-rendered is-readable-line-width allow-fold-headings show-indentation-guide"
    });
    const sizer = view.createDiv({ cls: "markdown-preview-sizer markdown-preview-section" });
    const component = new import_obsidian.Component();
    component.load();
    try {
      await import_obsidian.MarkdownRenderer.render(this.app, body, sizer, file.path, component);
      await settle(sizer, this.req.settle_ms, this.req.timeout_ms);
      if (sizer.querySelector("canvas")) await sleep(1500);
      this.freeze(sizer, file);
      await this.collectMedia(sizer);
      fs.writeFileSync(path.join(this.out, "content.html"), sizer.innerHTML);
      fs.writeFileSync(path.join(this.out, "styles.css"), await this.collectCss());
      writeJson(path.join(this.out, "meta.json"), {
        body_class: bodyClass(this.req.mode),
        body_style: document.body.getAttribute("style") ?? "",
        warnings: this.warnings
      });
    } finally {
      component.unload();
      host.remove();
    }
  }
  /** Turn the live, interactive DOM into something a static page can show. */
  freeze(root, file) {
    root.querySelectorAll(STRIP_SELECTORS.join(",")).forEach((el) => el.remove());
    root.querySelectorAll("[contenteditable]").forEach((el) => el.removeAttribute("contenteditable"));
    root.querySelectorAll("input[type=checkbox]").forEach((el) => {
      if (el.checked) el.setAttribute("checked", "");
      el.setAttribute("disabled", "");
    });
    const used = /* @__PURE__ */ new Set();
    root.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
      let id = slug(h.getAttribute("data-heading") ?? h.textContent ?? "");
      for (let n = 2; used.has(id); n++) id = `${slug(h.textContent ?? "")}-${n}`;
      used.add(id);
      h.id = id;
    });
    root.querySelectorAll("a.internal-link").forEach((a) => this.fixInternalLink(a, file));
    root.querySelectorAll("a.tag").forEach((a) => {
      const span = createSpan({ cls: a.className, text: a.textContent ?? "" });
      a.replaceWith(span);
    });
    root.querySelectorAll("a.external-link, a[href^='http']").forEach((a) => {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
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
  fixInternalLink(a, file) {
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
    const fm = dest ? this.app.metadataCache.getFileCache(dest)?.frontmatter : void 0;
    if (fm?.postcard_status === "live" && typeof fm.postcard_url === "string") {
      a.href = fm.postcard_url + anchor;
      return;
    }
    const span = createSpan({ cls: "internal-link postcard-unpublished-link", text: a.textContent ?? "" });
    a.replaceWith(span);
    this.warnings.push(`Linked note isn't published, shown as plain text: [[${linkpath}]]`);
  }
  /** Copy every image/video the page uses out of the vault (or blob/data URL) into assets/. */
  async collectMedia(root) {
    const els = root.querySelectorAll("img[src], video[src], audio[src], source[src]");
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
  async collectCss() {
    const internals = this.app;
    const skip = /* @__PURE__ */ new Set();
    if (internals.customCss?.styleEl) skip.add(internals.customCss.styleEl);
    internals.customCss?.extraStyleEls?.forEach((el) => skip.add(el));
    const skipText = new Set(this.activeThemeAndSnippetTexts());
    const parts = [];
    for (const sheet of Array.from(document.styleSheets)) {
      const owner = sheet.ownerNode;
      if (owner && (skip.has(owner) || skipText.has((owner.textContent ?? "").trim()))) continue;
      let rules;
      try {
        rules = sheet.cssRules;
      } catch {
        continue;
      }
      const text = Array.from(rules).map((r) => r.cssText).join("\n");
      parts.push(await this.inlineCssUrls(text, sheet.href ?? document.baseURI));
    }
    const extra = [this.req.theme_css, ...this.req.snippet_css].filter((p) => !!p);
    for (const file of extra) {
      const text = fs.readFileSync(file, "utf8");
      parts.push(await this.inlineCssUrls(text, "file://" + path.dirname(file) + "/"));
    }
    return parts.join("\n\n");
  }
  activeThemeAndSnippetTexts() {
    const internals = this.app;
    const base = internals.vault.adapter.getBasePath?.();
    if (!base) return [];
    const configDir = path.join(base, this.app.vault.configDir);
    const texts = [];
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
  async inlineCssUrls(css, base) {
    const re = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g;
    const urls = /* @__PURE__ */ new Set();
    for (const m of css.matchAll(re)) urls.add(m[2]);
    const replacements = /* @__PURE__ */ new Map();
    for (const u of urls) {
      if (/^(data:|#|https?:)/i.test(u)) continue;
      try {
        const abs = new URL(u, base).href;
        const { bytes, ext } = await readResource(abs);
        replacements.set(u, ASSET_TOKEN + this.writeAsset(bytes, ext, abs));
      } catch {
        replacements.set(u, "");
      }
    }
    return css.replace(re, (whole, _q, u) => {
      const r = replacements.get(u);
      if (r === void 0) return whole;
      return r ? `url("${r}")` : "none";
    });
  }
  writeAsset(bytes, ext, source) {
    if (source && this.assetsBySource.has(source)) return this.assetsBySource.get(source);
    const name = `${++this.assetCount}.${ext || "bin"}`;
    fs.writeFileSync(path.join(this.out, "assets", name), bytes);
    if (source) this.assetsBySource.set(source, name);
    return name;
  }
};
async function readResource(src) {
  if (src.startsWith("data:")) {
    const mime = src.slice(5, src.indexOf(";"));
    return { bytes: dataUrlBytes(src), ext: extFromMime(mime) };
  }
  const url = new URL(src);
  if (url.protocol === "file:" || url.protocol === "app:" && url.host !== "obsidian.md") {
    const file = decodeURIComponent(url.pathname);
    return { bytes: fs.readFileSync(file), ext: path.extname(file).slice(1).toLowerCase(), sourceFile: file };
  }
  const res = await fetch(src);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const ext = path.extname(url.pathname).slice(1).toLowerCase() || extFromMime(res.headers.get("content-type") ?? "");
  return { bytes: new Uint8Array(await res.arrayBuffer()), ext };
}
function bodyClass(mode) {
  const drop = /* @__PURE__ */ new Set(["theme-dark", "theme-light", "is-focused", "is-frameless", "is-hidden-frameless", "mod-macos", "mod-windows", "mod-linux", "is-translucent"]);
  const keep = Array.from(document.body.classList).filter((c) => !drop.has(c));
  return [...keep, `theme-${mode}`].join(" ");
}
function settle(el, quietMs, maxMs) {
  return new Promise((resolve2) => {
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
      resolve2();
    }
  });
}
var sleep = (ms) => new Promise((r) => window.setTimeout(r, ms));
function slug(text) {
  return text.trim().toLowerCase().replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") || "section";
}
function dataUrlBytes(url) {
  const comma = url.indexOf(",");
  const meta = url.slice(0, comma);
  const data = url.slice(comma + 1);
  return meta.endsWith(";base64") ? Buffer.from(data, "base64") : Buffer.from(decodeURIComponent(data), "utf8");
}
function extFromMime(mime) {
  const m = mime.split(";")[0].trim().toLowerCase();
  const map = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "font/woff2": "woff2",
    "font/woff": "woff",
    "font/ttf": "ttf",
    "video/mp4": "mp4"
  };
  return map[m] ?? "bin";
}
function writeJson(file, data) {
  fs.writeFileSync(file + ".tmp", JSON.stringify(data, null, 2));
  fs.renameSync(file + ".tmp", file);
}
