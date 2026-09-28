/**
 * Runs inside VS Code (see runTest.ts). Drives the real extension through its commands
 * against the real ABP agent with a scripted model, and checks what lands in the workspace.
 */
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { AbpExtensionApi } from "../../src/extension";

type Test = [name: string, body: (api: AbpExtensionApi, workspace: string) => Promise<void>];

let scriptCount = 0;
async function useScript(steps: unknown[]): Promise<void> {
  const file = path.join(process.env.ABP_TEST_SCRATCH!, `script-${++scriptCount}.json`);
  fs.writeFileSync(file, JSON.stringify(steps));
  await vscode.workspace.getConfiguration("abp").update("model", `scripted:${file}`, vscode.ConfigurationTarget.Workspace);
}

async function waitFor<T>(check: () => T | undefined | false, ms = 30_000): Promise<T> {
  const until = Date.now() + ms;
  for (;;) {
    const value = check();
    if (value) return value;
    if (Date.now() > until) throw new Error("timed out waiting for a condition");
    await new Promise((r) => setTimeout(r, 50));
  }
}

const tests: Test[] = [
  ["the extension activates and registers its commands and view", async (api) => {
    assert.ok(api.controller, "activate() returns the API");
    const commands = await vscode.commands.getCommands(true);
    for (const id of ["abp.ask", "abp.askAboutSelection", "abp.fixProblems", "abp.stop", "abp.newSession", "abp.openDashboard", "abp.chat.focus"]) {
      assert.ok(commands.includes(id), `command ${id} is registered`);
    }
    await vscode.commands.executeCommand("abp.chat.focus");
  }],

  ["a question asked by command edits the workspace after permission is given", async (api, workspace) => {
    await useScript([{ call: "write_file", args: { path: "hello.txt", content: "hello from ABP" } }, { say: "I wrote **hello.txt**." }]);
    const asked: string[] = [];
    api.setPermissionNotifier((id, title) => {
      asked.push(title);
      api.controller.answerPermission(id, "allow_once");
    });
    await vscode.commands.executeCommand("abp.ask", "create hello.txt");
    assert.equal(fs.readFileSync(path.join(workspace, "hello.txt"), "utf8"), "hello from ABP");
    assert.match(asked[0] ?? "", /write_file/);
    const last = api.controller.entries.at(-1);
    assert.deepEqual(last, { kind: "agent", text: "I wrote **hello.txt**." });
    assert.equal(api.controller.model, "scripted");
    assert.equal(api.controller.state, "idle");
  }],

  ["a refused permission leaves the workspace untouched", async (api, workspace) => {
    await useScript([{ call: "write_file", args: { path: "refused.txt", content: "x" } }, { say: "ok" }]);
    await waitFor(() => api.controller.state === "stopped");        // the settings change restarted the agent
    api.setPermissionNotifier((id) => api.controller.answerPermission(id, "reject_once"));
    await vscode.commands.executeCommand("abp.ask", "create refused.txt");
    assert.equal(fs.existsSync(path.join(workspace, "refused.txt")), false);
    assert.ok(api.controller.entries.some((e) => e.kind === "permission" && e.answer === "reject_once"));
  }],

  ["fix problems sends VS Code's diagnostics for the file", async (api, workspace) => {
    await useScript([{ say: "fixed" }]);
    const file = vscode.Uri.file(path.join(workspace, "broken.py"));
    fs.writeFileSync(file.fsPath, "print(undefined_name)\n");
    const diagnostics = vscode.languages.createDiagnosticCollection("abp-test");
    diagnostics.set(file, [
      new vscode.Diagnostic(new vscode.Range(0, 6, 0, 20), '"undefined_name" is not defined', vscode.DiagnosticSeverity.Error),
      new vscode.Diagnostic(new vscode.Range(0, 0, 0, 1), "just a hint", vscode.DiagnosticSeverity.Hint),
    ]);
    try {
      await vscode.commands.executeCommand("abp.fixProblems", file);
    } finally {
      diagnostics.dispose();
    }
    const sent = [...api.controller.entries].reverse().find((e) => e.kind === "user");
    assert.ok(sent && sent.kind === "user");
    assert.match(sent.text, /broken\.py/);
    assert.match(sent.text, /line 1: error: "undefined_name" is not defined/);
    assert.doesNotMatch(sent.text, /just a hint/);
    assert.deepEqual(api.controller.entries.at(-1), { kind: "agent", text: "fixed" });
  }],

  ["a new conversation clears the transcript", async (api) => {
    await vscode.commands.executeCommand("abp.newSession");
    assert.equal(api.controller.entries.length, 0);
  }],
];

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension<AbpExtensionApi>("agenticbotplatform.abp-vscode");
  assert.ok(extension, "the extension is installed in this VS Code");
  const api = await extension.activate();
  const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  assert.ok(workspace, "a workspace folder is open");
  const failures: string[] = [];
  for (const [name, body] of tests) {
    const started = Date.now();
    try {
      await body(api, workspace);
      console.log(`  ok    ${name} (${Date.now() - started} ms)`);
    } catch (error) {
      failures.push(name);
      console.log(`  FAIL  ${name}\n${error instanceof Error ? error.stack : error}`);
    }
  }
  api.controller.dispose();
  console.log(`${tests.length - failures.length} passed, ${failures.length} failed`);
  if (failures.length) throw new Error(`${failures.length} VS Code test(s) failed: ${failures.join("; ")}`);
}
