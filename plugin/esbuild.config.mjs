import esbuild from "esbuild";
import { builtinModules } from "node:module";
import { copyFileSync } from "node:fs";

// The built plugin ships inside the Python package so `postcard plugin install` works without node.
const outdir = "../src/postcard/bridge";

await esbuild.build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "cjs",
  target: "es2020",
  platform: "browser",
  external: ["obsidian", "electron", ...builtinModules],
  outfile: `${outdir}/main.js`,
  logLevel: "info",
});
copyFileSync("manifest.json", `${outdir}/manifest.json`);
