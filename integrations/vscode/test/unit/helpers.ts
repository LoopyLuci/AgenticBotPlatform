import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { venvPython } from "../../src/discovery";

/** This checkout of ABP (integrations/vscode/test/unit -> the repository root). */
export const ABP_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
export const PYTHON = venvPython(ABP_ROOT, process.platform);
export const HAVE_PYTHON = fs.existsSync(PYTHON);

export function tempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/** A scripted model: steps the real agent replays instead of calling a model. */
export function script(dir: string, steps: unknown[]): string {
  const file = path.join(dir, "script.json");
  fs.writeFileSync(file, JSON.stringify(steps));
  return `scripted:${file}`;
}

export async function waitFor<T>(check: () => T | undefined | false, timeoutMs = 30_000): Promise<T> {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const value = check();
    if (value) return value;
    if (Date.now() > until) throw new Error("timed out waiting for a condition");
    await new Promise((r) => setTimeout(r, 50));
  }
}
