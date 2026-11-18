import { build } from "esbuild";
import { cp, mkdtemp, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

const root = import.meta.dirname + "/..";
const dist = join(root, "dist");
const stage = await mkdtemp(join(root, ".frontend-build-"));
const shared = { absWorkingDir: root, bundle: true, target: "es2022", minify: true } as const;

async function inlineScript(entry: string): Promise<string> {
  const { outputFiles } = await build({
    ...shared,
    entryPoints: [entry],
    format: "iife",
    write: false,
  });
  return outputFiles[0].text;
}

try {
  const [frame, widget] = await Promise.all([
    inlineScript("frontend/widget-frame.ts"),
    inlineScript("frontend/widget-native.ts"),
    cp(join(root, "web"), join(stage, "web"), { recursive: true }),
  ]);
  await Promise.all([
    build({
      ...shared,
      entryPoints: ["frontend/app.ts", "frontend/widget-page.ts", "frontend/store.ts"],
      outdir: join(stage, "web"),
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
  ]);
  await rm(dist, { recursive: true, force: true });
  await rename(stage, dist);
} finally {
  await rm(stage, { recursive: true, force: true });
}
