// Bundles the extension into dist/extension.js (CommonJS, as VS Code loads it), and with
// --tests the integration-test runner and suite into out/test/.
import * as esbuild from "esbuild";

const watch = process.argv.includes("--watch");
const tests = process.argv.includes("--tests");
const common = { bundle: true, platform: "node", target: "node20", format: "cjs", sourcemap: true, external: ["vscode"], logLevel: "info" };

const builds = tests
  ? [{ ...common, entryPoints: ["test/vscode/runTest.ts", "test/vscode/suite.ts"], outdir: "out/test", external: ["vscode", "@vscode/test-electron"] }]
  : [{ ...common, entryPoints: ["src/extension.ts"], outfile: "dist/extension.js", minify: !watch }];

for (const options of builds) {
  if (watch) await (await esbuild.context(options)).watch();
  else await esbuild.build(options);
}
