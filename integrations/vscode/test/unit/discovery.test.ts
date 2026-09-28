import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, it } from "vitest";
import { dashboardUrl, findAbp, installLocations, isProblem, pointerPath, readEnvFile, readPointer, venvPython } from "../../src/discovery";
import { tempDir } from "./helpers";

function layout(files: string[]): (p: string) => boolean {
  const set = new Set(files.map((f) => path.normalize(f)));
  return (p) => set.has(path.normalize(p));
}

const root = path.join(path.sep, "abp");
const acp = path.join(root, "abp_acp", "__main__.py");
const py = venvPython(root, process.platform);

describe("findAbp", () => {
  it("prefers the setting, then ABP_CODE_ROOT, then the workspace, then the install folder", () => {
    const other = path.join(path.sep, "other");
    const exists = layout([acp, py, path.join(other, "abp_acp", "__main__.py"), venvPython(other, process.platform)]);
    const base = { workspaceFolders: [root], platform: process.platform, exists, home: path.sep, pointer: null };
    expect(findAbp({ ...base, env: {} })).toMatchObject({ root, source: "the open workspace", python: py });
    expect(findAbp({ ...base, env: { ABP_CODE_ROOT: other } })).toMatchObject({ root: other, source: "ABP_CODE_ROOT" });
    expect(findAbp({ ...base, env: {}, abpPath: other })).toMatchObject({ root: other, source: "the abp.abpPath setting" });
  });

  it("finds the ABP last started, with its own python and state folder", () => {
    const other = path.join(path.sep, "installed");
    const ownPython = path.join(path.sep, "py", "python");
    const exists = layout([path.join(other, "abp_acp", "__main__.py"), ownPython]);
    const pointer = { code_root: other, state_root: path.join(path.sep, "state"), python: ownPython };
    const found = findAbp({ workspaceFolders: [], env: {}, platform: process.platform, exists, home: path.sep, pointer });
    expect(found).toEqual({ root: other, python: ownPython, source: "the ABP you last started", env: { ABP_HOME: pointer.state_root } });
    const sameState = findAbp({ workspaceFolders: [], env: {}, platform: process.platform, exists, home: path.sep, pointer: { ...pointer, state_root: other } });
    expect(sameState).not.toHaveProperty("env");
  });

  it("reads the pointer file ABP writes", () => {
    const dir = tempDir("abp-pointer-");
    const file = path.join(dir, "install.json");
    fs.writeFileSync(file, JSON.stringify({ code_root: "/x", python: "/x/py" }));
    expect(readPointer(file)).toEqual({ code_root: "/x", python: "/x/py" });
    expect(readPointer(path.join(dir, "missing.json"))).toBeNull();
    expect(pointerPath({ ABP_INSTALL_POINTER: file }, "/home/u")).toBe(file);
    expect(pointerPath({}, "/home/u")).toBe(path.join("/home/u", ".abp", "install.json"));
  });

  it("uses the python setting as given", () => {
    const found = findAbp({ workspaceFolders: [root], env: {}, platform: process.platform, exists: layout([acp]), pythonPath: "/usr/bin/python3" });
    expect(found).toMatchObject({ root, python: "/usr/bin/python3" });
  });

  it("explains a missing venv python instead of failing later", () => {
    const found = findAbp({ workspaceFolders: [root], env: {}, platform: process.platform, exists: layout([acp]) });
    expect(isProblem(found) && found.error).toMatch(/Python .* is missing/);
  });

  it("recognises an installed ABP from before editor support", () => {
    const found = findAbp({ workspaceFolders: [root], env: {}, platform: process.platform, exists: layout([path.join(root, "bot", "main.py")]) });
    expect(isProblem(found) && found.error).toMatch(/older than editor support/);
  });

  it("does not look past a wrong abp.abpPath setting", () => {
    const found = findAbp({ abpPath: path.join(path.sep, "nowhere"), workspaceFolders: [root], env: {}, platform: process.platform, exists: layout([acp, py]) });
    expect(isProblem(found) && found.error).toMatch(/abp\.abpPath is set to/);
  });

  it("says where to get ABP when there is none", () => {
    const found = findAbp({ workspaceFolders: [], env: {}, platform: process.platform, exists: () => false });
    expect(isProblem(found) && found.error).toMatch(/Install the ABP desktop app/);
  });

  it("knows the desktop app's install folders", () => {
    expect(installLocations("win32", { LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" }, "C:\\Users\\u")[0]).toBe(
      "C:\\Users\\u\\AppData\\Local\\AgenticBotPlatform",
    );
    expect(installLocations("darwin", {}, "/Users/u")[0]).toBe("/Applications/AgenticBotPlatform.app/Contents/Resources");
    expect(installLocations("linux", {}, "/home/u")).toContain("/usr/lib/AgenticBotPlatform");
  });
});

describe("the dashboard address", () => {
  it("comes from DASHBOARD_PORT in the environment or ABP's .env, else 8787", () => {
    const dir = tempDir("abp-env-");
    fs.writeFileSync(path.join(dir, ".env"), "# comment\nDASHBOARD_TOKEN=unused\nDASHBOARD_PORT=\"9123\"\n");
    expect(readEnvFile(path.join(dir, ".env"))).toEqual({ DASHBOARD_TOKEN: "unused", DASHBOARD_PORT: "9123" });
    expect(dashboardUrl(dir, {})).toBe("http://127.0.0.1:9123/");
    expect(dashboardUrl(dir, { DASHBOARD_PORT: "9000" })).toBe("http://127.0.0.1:9000/");
    expect(dashboardUrl(undefined, {})).toBe("http://127.0.0.1:8787/");
    expect(dashboardUrl(undefined, { DASHBOARD_PORT: "not-a-port" })).toBe("http://127.0.0.1:8787/");
  });
});
