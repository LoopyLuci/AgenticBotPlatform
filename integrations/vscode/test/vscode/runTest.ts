/**
 * Runs test/vscode/suite.ts inside a real VS Code: the one installed on this machine if
 * there is one (VSCODE_EXECUTABLE, or the default install path), otherwise a downloaded
 * stable build. It uses a throwaway profile and extensions folder, and a temporary
 * workspace, so your own VS Code setup is never touched. ABP_CODE_ROOT points the extension
 * at this checkout (and ABP_INSTALL_POINTER away from your real ~/.abp/install.json); the
 * agent runs a scripted model, so no key is needed.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

function installedVsCode(): string | undefined {
  const candidates = [
    process.env.VSCODE_EXECUTABLE,
    process.platform === "win32" && process.env.LOCALAPPDATA
      ? path.join(process.env.LOCALAPPDATA, "Programs", "Microsoft VS Code", "Code.exe")
      : undefined,
    process.platform === "win32" && process.env.ProgramFiles ? path.join(process.env.ProgramFiles, "Microsoft VS Code", "Code.exe") : undefined,
    process.platform === "darwin" ? "/Applications/Visual Studio Code.app/Contents/MacOS/Electron" : undefined,
    process.platform === "linux" ? "/usr/share/code/code" : undefined,
  ];
  return candidates.find((p): p is string => !!p && fs.existsSync(p));
}

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "..", "..");
  const abpRoot = path.resolve(extensionDevelopmentPath, "..", "..");
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "abp-vscode-test-"));
  const workspace = path.join(scratch, "workspace");
  fs.mkdirSync(path.join(workspace, ".vscode"), { recursive: true });
  const executable = installedVsCode();
  console.log(executable ? `using the installed VS Code at ${executable}` : "no installed VS Code found; downloading a stable build");
  try {
    await runTests({
      ...(executable ? { vscodeExecutablePath: executable } : { version: "stable" }),
      extensionDevelopmentPath,
      extensionTestsPath: path.join(__dirname, "suite.js"),
      extensionTestsEnv: {
        ABP_CODE_ROOT: abpRoot,
        ABP_INSTALL_POINTER: path.join(scratch, "no-install.json"),
        ABP_TEST_SCRATCH: scratch,
      },
      launchArgs: [
        workspace,
        "--disable-extensions",
        "--disable-workspace-trust",
        "--skip-welcome",
        "--skip-release-notes",
        "--user-data-dir", path.join(scratch, "user-data"),
        "--extensions-dir", path.join(scratch, "extensions"),
      ],
    });
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
