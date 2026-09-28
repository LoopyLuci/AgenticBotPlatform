import * as path from "node:path";
import * as vscode from "vscode";
import type { ContentBlock } from "./acp";
import { ChatViewProvider } from "./chatView";
import { AgentController, type ControllerSettings } from "./controller";
import { dashboardUrl, findAbp, isProblem } from "./discovery";

/** What `activate` returns: used by the integration tests, and by any extension that builds on this one. */
export interface AbpExtensionApi {
  controller: AgentController;
  /** Replace how permission questions are shown (the tests answer them directly). */
  setPermissionNotifier(notify: ((id: string, title: string) => void) | undefined): void;
}

const MAX_CONTEXT_CHARS = 100_000;

export function activate(context: vscode.ExtensionContext): AbpExtensionApi {
  const output = vscode.window.createOutputChannel("ABP agent", { log: true });
  const config = () => vscode.workspace.getConfiguration("abp");
  const install = () =>
    findAbp({
      abpPath: config().get<string>("abpPath"),
      pythonPath: config().get<string>("pythonPath"),
      workspaceFolders: (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath),
      env: process.env,
      platform: process.platform,
    });
  const settings = (): ControllerSettings => ({
    model: config().get<string>("model") || "auto",
    permissionMode: config().get<string>("permissionMode") || "",
  });

  let notifier: ((id: string, title: string) => void) | undefined = (id, title) => void notifyPermission(id, title);
  const controller = new AgentController({
    install,
    settings,
    workspace: () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
    log: (line) => output.info(line),
    clientVersion: String(context.extension.packageJSON.version ?? "0"),
    onPermissionAsked: (entry) => notifier?.(entry.id, entry.title),
  });

  async function notifyPermission(id: string, title: string): Promise<void> {
    const entry = controller.entries.find((e) => e.kind === "permission" && e.id === id);
    if (!entry || entry.kind !== "permission") return;
    const byName = new Map(entry.options.map((o) => [o.name, o.optionId]));
    const picked = await vscode.window.showWarningMessage(`ABP's agent asks: ${title}`, ...byName.keys());
    // Dismissing the notification leaves the question open in the chat view; only a click answers it.
    if (picked) controller.answerPermission(id, byName.get(picked) ?? null);
  }

  const chat = new ChatViewProvider(context.extensionUri, controller);
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "abp.chat.focus";
  const updateStatus = () => {
    const icon = { stopped: "$(hubot)", starting: "$(loading~spin)", idle: "$(hubot)", working: "$(loading~spin)" }[controller.state];
    status.text = `${icon} ABP`;
    status.tooltip = `ABP agent: ${controller.state}${controller.model ? ` (${controller.model})` : ""}. Click to open the chat.`;
    status.show();
  };
  updateStatus();

  const send = async (text: string, extra: ContentBlock[] = []) => {
    await chat.reveal();
    await controller.send(text, extra);
  };

  context.subscriptions.push(
    output,
    status,
    { dispose: () => controller.dispose() },
    controller.onDidChange(updateStatus),
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewId, chat, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.commands.registerCommand("abp.ask", async (text?: string) => {
      const question = typeof text === "string" ? text : await vscode.window.showInputBox({ prompt: "Ask ABP's agent", ignoreFocusOut: true });
      if (question) await send(question);
    }),
    vscode.commands.registerCommand("abp.askAboutSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.selection.isEmpty) {
        void vscode.window.showInformationMessage("Select some code first.");
        return;
      }
      const question = await vscode.window.showInputBox({
        prompt: "What should the agent do with the selection?",
        value: "Explain this code",
        ignoreFocusOut: true,
      });
      if (!question) return;
      const { start, end } = editor.selection;
      const where = `${relative(editor.document.uri)}, lines ${start.line + 1}-${end.line + 1}`;
      await send(`${question}\n\n(The selection is in ${where}.)`, [
        resource(editor.document.uri, editor.document.getText(editor.selection), editor.document.languageId),
      ]);
    }),
    vscode.commands.registerCommand("abp.fixProblems", async (uri?: vscode.Uri) => {
      const target = uri instanceof vscode.Uri ? uri : vscode.window.activeTextEditor?.document.uri;
      if (!target) {
        void vscode.window.showInformationMessage("Open the file with problems first.");
        return;
      }
      const problems = vscode.languages
        .getDiagnostics(target)
        .filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning);
      if (!problems.length) {
        void vscode.window.showInformationMessage("VS Code reports no errors or warnings in this file.");
        return;
      }
      const lines = problems.map((d) => {
        const kind = d.severity === vscode.DiagnosticSeverity.Error ? "error" : "warning";
        const source = d.source ? ` [${d.source}${d.code !== undefined ? ` ${typeof d.code === "object" ? d.code.value : d.code}` : ""}]` : "";
        return `- line ${d.range.start.line + 1}: ${kind}${source}: ${d.message}`;
      });
      await send(`Fix these problems that VS Code reports in ${relative(target)}, then check your change:\n${lines.join("\n")}`);
    }),
    vscode.commands.registerCommand("abp.stop", () => controller.cancel()),
    vscode.commands.registerCommand("abp.newSession", () => controller.newSession()),
    vscode.commands.registerCommand("abp.restartAgent", () => {
      controller.restart();
      void vscode.window.showInformationMessage("ABP's agent will restart with your current settings on your next message.");
    }),
    vscode.commands.registerCommand("abp.showLog", () => output.show()),
    vscode.commands.registerCommand("abp.openDashboard", async () => {
      const found = install();
      const url = dashboardUrl(isProblem(found) ? undefined : found.root, process.env);
      if (!(await reachable(url))) {
        void vscode.window.showWarningMessage("The ABP dashboard is not running. Start the ABP desktop app, then try again.");
        return;
      }
      await vscode.env.openExternal(vscode.Uri.parse(url));
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("abp") && controller.state !== "stopped") {
        if (controller.state === "working") {
          void vscode.window.showInformationMessage("ABP's new settings apply after the current message finishes. Run \"ABP: Restart the agent\" then.");
        } else {
          controller.restart();
        }
      }
    }),
  );

  return {
    controller,
    setPermissionNotifier(notify) {
      notifier = notify;
    },
  };
}

export function deactivate(): void {
  /* the controller is disposed through context.subscriptions */
}

function relative(uri: vscode.Uri): string {
  return vscode.workspace.asRelativePath(uri, false) || path.basename(uri.fsPath);
}

function resource(uri: vscode.Uri, text: string, languageId: string): ContentBlock {
  const body = text.length > MAX_CONTEXT_CHARS ? `${text.slice(0, MAX_CONTEXT_CHARS)}\n[... cut: the selection is longer than ${MAX_CONTEXT_CHARS} characters]` : text;
  return { type: "resource", resource: { uri: uri.toString(), text: body, mimeType: `text/x-${languageId}` } };
}

async function reachable(url: string): Promise<boolean> {
  try {
    const response = await fetch(new URL("healthz", url), { signal: AbortSignal.timeout(2500) });
    return response.ok;
  } catch {
    return false;
  }
}
