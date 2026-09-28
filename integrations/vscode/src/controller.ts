/**
 * One conversation with ABP's agent: starts the agent on demand, keeps the transcript the
 * chat view renders, and turns the agent's permission requests into questions a person
 * answers (in the chat view or a notification; the first answer wins). No VS Code API
 * here: the extension supplies the few things it needs through `ControllerDeps`.
 */
import { AcpClient, type ContentBlock, type PermissionOption, type PermissionRequest, type SessionUpdate } from "./acp";
import { isProblem, type AbpInstall, type DiscoveryProblem } from "./discovery";

export type Entry =
  | { kind: "user"; text: string }
  | { kind: "agent"; text: string }
  | { kind: "tool"; id: string; title: string; status: string }
  | { kind: "notice"; text: string; level: "info" | "error" }
  | { kind: "permission"; id: string; title: string; options: PermissionOption[]; answer?: string };

export type AgentState = "stopped" | "starting" | "idle" | "working";

export interface ControllerSettings {
  model: string;
  permissionMode: string;
}

export interface ControllerDeps {
  install: () => AbpInstall | DiscoveryProblem;
  settings: () => ControllerSettings;
  /** The folder the agent works in; undefined when no folder is open. */
  workspace: () => string | undefined;
  log: (line: string) => void;
  /** Called when the agent asks permission, so the extension can also show a notification. */
  onPermissionAsked?: (entry: Extract<Entry, { kind: "permission" }>) => void;
  clientVersion?: string;
}

export function agentArgs(settings: ControllerSettings): string[] {
  const args = ["--model", settings.model.trim() || "auto"];
  if (settings.permissionMode) args.push("--permission-mode", settings.permissionMode);
  return args;
}

export class AgentController {
  entries: Entry[] = [];
  state: AgentState = "stopped";
  /** The "provider/model" the agent reported for this conversation, once known. */
  model = "";
  private client?: AcpClient;
  private sessionId?: string;
  private sessionCwd?: string;
  private starting?: Promise<void>;
  private readonly listeners = new Set<() => void>();
  private readonly waiting = new Map<string, (optionId: string | null) => void>();

  constructor(private readonly deps: ControllerDeps) {}

  onDidChange(listener: () => void): { dispose(): void } {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  }

  /** Send a message (plus optional attached context) and wait for the turn to finish. */
  async send(text: string, context: ContentBlock[] = []): Promise<void> {
    const body = text.trim();
    if (!body) return;
    if (this.state === "working") {
      this.notice("The agent is still working on the last message. Stop it first, or wait.", "error");
      return;
    }
    this.entries.push({ kind: "user", text: body });
    this.changed();
    try {
      await this.ensureSession();
    } catch (error) {
      this.notice(message(error), "error");
      return;
    }
    this.setState("working");
    try {
      const result = await this.client!.prompt(this.sessionId!, [{ type: "text", text: body }, ...context]);
      if (result._meta?.abp?.model) this.model = result._meta.abp.model;
      if (result.stopReason === "cancelled") this.notice("Stopped.", "info");
      else if (result.stopReason === "max_turn_requests") this.notice("The agent reached its step limit for one message.", "info");
    } catch (error) {
      this.notice(message(error), "error");
    } finally {
      this.closeOpenTools();
      this.setState(this.client?.running ? "idle" : "stopped");
    }
  }

  cancel(): void {
    if (this.client && this.sessionId && this.state === "working") this.client.cancel(this.sessionId);
    for (const [id, resolve] of this.waiting) {
      this.recordAnswer(id, null);
      resolve(null);
    }
    this.waiting.clear();
  }

  /** Clear the transcript and start a fresh conversation (the agent process is kept). */
  async newSession(): Promise<void> {
    this.cancel();
    this.entries = [];
    this.model = "";
    this.sessionId = undefined;
    this.changed();
  }

  /** Stop the agent process; the next message starts it again with the current settings. */
  restart(): void {
    this.cancel();
    this.client?.dispose();
    this.client = undefined;
    this.sessionId = undefined;
    this.starting = undefined;
    this.model = "";
    this.setState("stopped");
  }

  answerPermission(id: string, optionId: string | null): void {
    const resolve = this.waiting.get(id);
    if (!resolve) return;
    this.waiting.delete(id);
    this.recordAnswer(id, optionId);
    resolve(optionId);
  }

  dispose(): void {
    this.restart();
    this.listeners.clear();
  }

  // ---- internals ----------------------------------------------------------------------
  private async ensureSession(): Promise<void> {
    const cwd = this.deps.workspace();
    if (!cwd) throw new Error("Open a folder first: the agent works inside your workspace folder.");
    if (!this.client?.running) {
      this.starting ??= this.startClient().finally(() => (this.starting = undefined));
      await this.starting;
    }
    if (!this.sessionId || this.sessionCwd !== cwd) {
      this.sessionId = await this.client!.newSession(cwd);
      this.sessionCwd = cwd;
    }
  }

  private async startClient(): Promise<void> {
    const found = this.deps.install();
    if (isProblem(found)) throw new Error(found.error);
    this.setState("starting");
    const args = agentArgs(this.deps.settings());
    this.deps.log(`starting ABP's agent from ${found.root} (${found.source}): ${found.python} -m abp_acp ${args.join(" ")}`);
    const client = new AcpClient({
      python: found.python,
      root: found.root,
      args,
      env: found.env,
      clientVersion: this.deps.clientVersion,
      onPermission: (request) => this.askPermission(request),
    });
    client.on("update", (sessionId: string, update: SessionUpdate) => {
      if (sessionId === this.sessionId) this.applyUpdate(update);
    });
    client.on("log", (line: string) => this.deps.log(line));
    client.on("exit", (code: number | null) => {
      if (this.client !== client) return;
      this.deps.log(`ABP's agent exited (code ${code})`);
      this.client = undefined;
      this.sessionId = undefined;
      this.setState("stopped");
    });
    this.client = client;
    try {
      const init = await client.start();
      this.deps.log(`connected to ${init.agentInfo?.title ?? "ABP agent"} ${init.agentInfo?.version ?? ""}`.trim());
    } catch (error) {
      client.dispose();
      if (this.client === client) this.client = undefined;
      this.setState("stopped");
      throw error;
    }
  }

  private askPermission(request: PermissionRequest): Promise<string | null> {
    const id = request.toolCall.toolCallId;
    const entry: Extract<Entry, { kind: "permission" }> = {
      kind: "permission",
      id,
      title: request.toolCall.title,
      options: request.options,
    };
    this.entries.push(entry);
    this.changed();
    const answer = new Promise<string | null>((resolve) => this.waiting.set(id, resolve));
    this.deps.onPermissionAsked?.(entry);
    return answer;
  }

  private recordAnswer(id: string, optionId: string | null): void {
    const entry = this.entries.find((e) => e.kind === "permission" && e.id === id);
    if (entry && entry.kind === "permission") entry.answer = optionId ?? "dismissed";
    this.changed();
  }

  private applyUpdate(update: SessionUpdate): void {
    if (update.sessionUpdate === "agent_message_chunk" && update.content?.type === "text") {
      const last = this.entries[this.entries.length - 1];
      if (last?.kind === "agent") last.text += update.content.text ?? "";
      else this.entries.push({ kind: "agent", text: update.content.text ?? "" });
    } else if (update.sessionUpdate === "tool_call" && update.toolCallId) {
      this.entries.push({ kind: "tool", id: update.toolCallId, title: update.title ?? "a tool", status: update.status ?? "in_progress" });
    } else if (update.sessionUpdate === "tool_call_update" && update.toolCallId) {
      for (const e of this.entries) if (e.kind === "tool" && e.id === update.toolCallId && update.status) e.status = update.status;
    } else {
      return;
    }
    this.changed();
  }

  private closeOpenTools(): void {
    for (const e of this.entries) if (e.kind === "tool" && e.status === "in_progress") e.status = "completed";
  }

  private notice(text: string, level: "info" | "error"): void {
    this.entries.push({ kind: "notice", text, level });
    this.changed();
  }

  private setState(state: AgentState): void {
    this.state = state;
    this.changed();
  }

  private changed(): void {
    for (const listener of this.listeners) listener();
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
