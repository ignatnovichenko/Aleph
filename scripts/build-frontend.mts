import { build, transform } from "esbuild";
import { createHash } from "node:crypto";
import { cp, mkdtemp, readdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { brotliCompressSync, constants, gzipSync } from "node:zlib";

const root = import.meta.dirname + "/..";
const dist = join(root, "dist");
const stage = await mkdtemp(join(root, ".frontend-build-"));
const web = join(stage, "web");
const shared = { absWorkingDir: root, bundle: true, target: "es2022", minify: true, charset: "utf8" } as const;

async function inlineScript(entry: string): Promise<string> {
  const { outputFiles } = await build({
    ...shared,
    entryPoints: [entry],
    format: "iife",
    write: false,
  });
  return outputFiles[0].text;
}

async function versioned(name: string, source: Uint8Array) {
  const hash = createHash("sha256").update(source).digest("hex").slice(0, 12);
  const extension = extname(name);
  const filename = `${name.slice(0, -extension.length)}-${hash}${extension}`;
  await writeFile(join(web, filename), source);
  return "./" + filename;
}

async function buildMainPage() {
  const assets = new Map<string, string>();
  for (const name of await readdir(web)) {
    if (/\.(?:woff2|webp)$/.test(name)) assets.set("./" + name, await versioned(name, await readFile(join(web, name))));
  }
  const mark = assets.get("./aleph.webp");
  if (!mark) throw new Error("Missing Aleph Mini App icon");
  const [startup, app, styleSources] = await Promise.all([
    inlineScript("frontend/startup.ts"),
    build({
      ...shared,
      entryPoints: ["frontend/app.ts"],
      format: "esm",
      define: { __ALEPH_MARK_URL__: JSON.stringify(mark) },
      write: false,
    }),
    Promise.all(["fonts.css", "tokens.css", "styles.css"].map(name => readFile(join(web, name), "utf8"))),
  ]);
  let styles = styleSources.join("\n");
  for (const [name, url] of assets) styles = styles.replaceAll(name, url);
  const css = await transform(styles, { loader: "css", minify: true });
  const js = await versioned("app.js", app.outputFiles[0].contents);
  const path = join(web, "index.html");
  const template = await readFile(path, "utf8");
  await writeFile(path, template
    .replaceAll("./aleph.webp", mark)
    .replace("./app.js", js)
    .replace("<!-- Startup -->", () => `<script>${startup}</script>`)
    .replace("<!-- App styles -->", () => `<style>${css.code}</style>`));
}

async function compressWebFiles() {
  for (const name of await readdir(web)) {
    if (!/\.(?:html|js|css)$/.test(name)) continue;
    const path = join(web, name);
    const contents = await readFile(path);
    if (contents.length < 512) continue;
    await Promise.all([
      writeFile(path + ".br", brotliCompressSync(contents, {params: {[constants.BROTLI_PARAM_QUALITY]: 9}})),
      writeFile(path + ".gz", gzipSync(contents, {level: 9})),
    ]);
  }
}

try {
  const [frame, widget] = await Promise.all([
    inlineScript("frontend/widget-frame.ts"),
    inlineScript("frontend/widget-native.ts"),
    cp(join(root, "web"), web, { recursive: true }),
  ]);
  await Promise.all([
    build({
      ...shared,
      entryPoints: ["frontend/widget-page.ts", "frontend/store.ts"],
      outdir: web,
      format: "esm",
      splitting: true,
      define: { __WIDGET_FRAME_SOURCE__: JSON.stringify(frame) },
    }),
    build({
      ...shared,
      entryPoints: ["frontend/privacy.ts"],
      outfile: join(stage, "web/privacy.js"),
      format: "iife",
    }),
    writeFile(join(stage, "widget.js"), widget),
    buildMainPage(),
  ]);
  await compressWebFiles();
  await rm(dist, { recursive: true, force: true });
  await rename(stage, dist);
} finally {
  await rm(stage, { recursive: true, force: true });
}
