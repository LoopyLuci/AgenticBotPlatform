/**
 * Finds the ABP installation (or checkout) whose Python runs the agent, so nobody has to
 * configure a path. In order:
 *   1. the `abp.abpPath` setting;
 *   2. `ABP_CODE_ROOT`;
 *   3. an open workspace folder that is itself an ABP checkout;
 *   4. `~/.abp/install.json`, which every ABP server writes when it starts (so the ABP you
 *      last ran is found, checkout or installed copy, with its own Python and state folder);
 *   5. where the desktop app installs.
 * Pure Node (no VS Code API) so it is unit-tested directly.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface AbpInstall {
  root: string;
  python: string;
  /** Where the root came from, for the log and error messages. */
  source: string;
  /** Extra environment for the agent: ABP_HOME when that ABP keeps its state outside its code folder. */
  env?: Record<string, string>;
}

/** What an ABP server records about itself in ~/.abp/install.json (bot/editor_integrations.py). */
export interface InstallPointer {
  code_root?: string;
  state_root?: string;
  python?: string;
}

export interface DiscoveryProblem {
  error: string;
  tried: string[];
}

export interface DiscoveryInput {
  abpPath?: string;
  pythonPath?: string;
  workspaceFolders: string[];
  env: NodeJS.ProcessEnv;
  platform: NodeJS.Platform;
  home?: string;
  exists?: (p: string) => boolean;
  /** The parsed ~/.abp/install.json; read from disk when not given. */
  pointer?: InstallPointer | null;
}

export function pointerPath(env: NodeJS.ProcessEnv, home: string): string {
  return env.ABP_INSTALL_POINTER?.trim() || path.join(home, ".abp", "install.json");
}

export function readPointer(file: string): InstallPointer | null {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return data && typeof data === "object" ? (data as InstallPointer) : null;
  } catch {
    return null;
  }
}

const ACP_ENTRY = path.join("abp_acp", "__main__.py");

/** Where the desktop app puts its files on each platform (Tauri's resource directory). */
export function installLocations(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, home: string): string[] {
  const out: string[] = [];
  if (platform === "win32") {
    if (env.LOCALAPPDATA) out.push(path.win32.join(env.LOCALAPPDATA, "AgenticBotPlatform"));
    if (env.ProgramFiles) out.push(path.win32.join(env.ProgramFiles, "AgenticBotPlatform"));
  } else if (platform === "darwin") {
    out.push("/Applications/AgenticBotPlatform.app/Contents/Resources");
    out.push(path.posix.join(home, "Applications/AgenticBotPlatform.app/Contents/Resources"));
  } else {
    out.push("/usr/lib/AgenticBotPlatform", "/usr/lib/agentic-bot-platform", "/opt/AgenticBotPlatform");
  }
  return out;
}

export function venvPython(root: string, platform: NodeJS.Platform): string {
  return platform === "win32"
    ? path.win32.join(root, ".venv", "Scripts", "python.exe")
    : path.posix.join(root, ".venv", "bin", "python");
}

export function findAbp(input: DiscoveryInput): AbpInstall | DiscoveryProblem {
  const exists = input.exists ?? ((p: string) => fs.existsSync(p));
  const home = input.home ?? os.homedir();
  const pointer = input.pointer !== undefined ? input.pointer : readPointer(pointerPath(input.env, home));
  const candidates: Array<[string, string]> = [];
  if (input.abpPath?.trim()) candidates.push([input.abpPath.trim(), "the abp.abpPath setting"]);
  if (input.env.ABP_CODE_ROOT?.trim()) candidates.push([input.env.ABP_CODE_ROOT.trim(), "ABP_CODE_ROOT"]);
  for (const folder of input.workspaceFolders) candidates.push([folder, "the open workspace"]);
  if (pointer?.code_root) candidates.push([pointer.code_root, "the ABP you last started"]);
  for (const place of installLocations(input.platform, input.env, home)) candidates.push([place, "the desktop app's install folder"]);

  const tried: string[] = [];
  let outdated: string | undefined;
  for (const [root, source] of candidates) {
    tried.push(root);
    if (exists(path.join(root, ACP_ENTRY))) {
      const fromPointer = pointer?.code_root === root ? pointer : undefined;
      const python = input.pythonPath?.trim() || (fromPointer?.python && exists(fromPointer.python) ? fromPointer.python : venvPython(root, input.platform));
      if (!input.pythonPath?.trim() && !exists(python)) {
        return {
          error: `ABP was found at ${root} (${source}) but its Python (${python}) is missing. ` +
            "Start the ABP desktop app once so it can repair it, or set abp.pythonPath.",
          tried,
        };
      }
      const state = fromPointer?.state_root;
      const env = state && path.resolve(state) !== path.resolve(root) ? { ABP_HOME: state } : undefined;
      return env ? { root, python, source, env } : { root, python, source };
    }
    // An installed app from before editor support has bot/ but not abp_acp/.
    if (!outdated && exists(path.join(root, "bot", "main.py"))) outdated = root;
    if (source === "the abp.abpPath setting") {
      return { error: `abp.abpPath is set to ${root}, but no ABP with editor support is there (abp_acp is missing).`, tried };
    }
  }
  if (outdated) {
    return {
      error: `The ABP at ${outdated} is older than editor support. Update ABP, or set abp.abpPath to a newer copy.`,
      tried,
    };
  }
  return {
    error: "ABP was not found. Install the ABP desktop app, or set abp.abpPath to the folder it is in.",
    tried,
  };
}

export function isProblem(result: AbpInstall | DiscoveryProblem): result is DiscoveryProblem {
  return (result as DiscoveryProblem).error !== undefined;
}

/** KEY=value pairs from an ABP `.env` file (only what the extension needs: the dashboard's port). */
export function readEnvFile(file: string): Record<string, string> {
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return {};
  }
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.endsWith(value[0]!)) value = value.slice(1, -1);
    out[line.slice(0, eq).trim()] = value;
  }
  return out;
}

/** The local dashboard's address. The page itself receives its token from the server on a local load. */
export function dashboardUrl(root: string | undefined, env: NodeJS.ProcessEnv): string {
  const fileEnv = root ? readEnvFile(path.join(root, ".env")) : {};
  const port = Number(env.DASHBOARD_PORT || fileEnv.DASHBOARD_PORT || 8787);
  return `http://127.0.0.1:${Number.isInteger(port) && port > 0 && port < 65536 ? port : 8787}/`;
}
