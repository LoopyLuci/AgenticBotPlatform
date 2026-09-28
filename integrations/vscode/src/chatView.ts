import * as crypto from "node:crypto";
import * as vscode from "vscode";
import type { AgentController } from "./controller";

/** Messages the webview sends; anything else is ignored. */
type FromView =
  | { type: "ready" }
  | { type: "send"; text: string }
  | { type: "cancel" }
  | { type: "newSession" }
  | { type: "openDashboard" }
  | { type: "permission"; id: string; optionId: string | null };

export class ChatViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = "abp.chat";
  private view?: vscode.WebviewView;

  constructor(private readonly extensionUri: vscode.Uri, private readonly controller: AgentController) {
    controller.onDidChange(() => this.post());
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((msg: FromView) => this.receive(msg));
    view.onDidChangeVisibility(() => view.visible && this.post());
  }

  /** Bring the view into sight (used by commands that send a message from the editor). */
  async reveal(): Promise<void> {
    await vscode.commands.executeCommand(`${ChatViewProvider.viewId}.focus`);
  }

  private receive(msg: FromView): void {
    switch (msg?.type) {
      case "ready":
        this.post();
        break;
      case "send":
        if (typeof msg.text === "string") void this.controller.send(msg.text);
        break;
      case "cancel":
        this.controller.cancel();
        break;
      case "newSession":
        void this.controller.newSession();
        break;
      case "openDashboard":
        void vscode.commands.executeCommand("abp.openDashboard");
        break;
      case "permission":
        if (typeof msg.id === "string") this.controller.answerPermission(msg.id, typeof msg.optionId === "string" ? msg.optionId : null);
        break;
    }
  }

  private post(): void {
    if (!this.view) return;
    const { entries, state, model } = this.controller;
    void this.view.webview.postMessage({ type: "state", entries, state, model });
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      `img-src ${webview.cspSource} data:`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${style}">
<title>ABP agent</title>
</head>
<body>
<div id="status" role="status" aria-live="polite"></div>
<main id="log" aria-label="Conversation" aria-live="polite"></main>
<form id="composer">
  <label for="input" class="visually-hidden">Message to the agent</label>
  <textarea id="input" rows="3" placeholder="Ask the agent to do something in this workspace (Enter sends, Shift+Enter adds a line)"></textarea>
  <div class="actions">
    <button type="button" id="stop" class="secondary" hidden>Stop</button>
    <button type="submit" id="send">Send</button>
  </div>
</form>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}
