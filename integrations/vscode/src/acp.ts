/**
 * A client for the Agent Client Protocol server ABP ships (`python -m abp_acp`).
 *
 * ACP is JSON-RPC 2.0, one message per line, over the agent's standard input and output.
 * This client starts the agent, opens sessions, sends prompts, streams `session/update`
 * notifications to listeners, and answers the agent's `session/request_permission`
 * requests through a callback. It deliberately has no dependency on the VS Code API, so it
 * is tested against the real ABP program with plain Node.
 */
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { EventEmitter } from "node:events";
import * as readline from "node:readline";

export const PROTOCOL_VERSION = 1;

export type ContentBlock =
  | { type: "text"; text: string }
  | { type: "resource"; resource: { uri: string; text: string; mimeType?: string } };

export interface SessionUpdate {
  sessionUpdate: string;
  content?: { type: string; text?: string };
  toolCallId?: string;
  title?: string;
  kind?: string;
  status?: string;
}

export interface PermissionOption {
  optionId: string;
  name: string;
  kind: string;
}

export interface PermissionRequest {
  sessionId: string;
  toolCall: { toolCallId: string; title: string; rawInput?: unknown };
  options: PermissionOption[];
}

/** The chosen optionId, or null when the question was dismissed (the agent treats that as a refusal). */
export type PermissionAnswer = string | null;

export interface PromptResult {
  stopReason: "end_turn" | "cancelled" | "max_turn_requests" | string;
  _meta?: { abp?: { model?: string; tokens?: number } };
}

export interface InitializeResult {
  protocolVersion: number;
  agentInfo?: { name?: string; title?: string; version?: string };
  agentCapabilities?: Record<string, unknown>;
}

export class RpcError extends Error {
  constructor(public readonly code: number, message: string) {
    super(message);
    this.name = "RpcError";
  }
}

export interface AcpClientOptions {
  python: string;
  /** The folder the agent program starts in: ABP's root, so `abp_acp` is importable. */
  root: string;
  args: string[];
  env?: NodeJS.ProcessEnv;
  onPermission: (request: PermissionRequest) => Promise<PermissionAnswer>;
  clientVersion?: string;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

/**
 * Events: "update" (sessionId, SessionUpdate), "log" (line of the agent's standard error),
 * "exit" (code, lastErrorLines).
 */
export class AcpClient extends EventEmitter {
  private proc?: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly stderrTail: string[] = [];
  private exited = false;

  constructor(private readonly options: AcpClientOptions) {
    super();
  }

  get running(): boolean {
    return !!this.proc && !this.exited;
  }

  /** Start the agent program and complete the protocol handshake. */
  async start(): Promise<InitializeResult> {
    const { python, root, args, env } = this.options;
    const proc = spawn(python, ["-m", "abp_acp", ...args], {
      cwd: root,
      env: { ...process.env, ...env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.proc = proc;
    readline.createInterface({ input: proc.stdout }).on("line", (line) => this.onLine(line));
    readline.createInterface({ input: proc.stderr }).on("line", (line) => {
      this.stderrTail.push(line);
      if (this.stderrTail.length > 40) this.stderrTail.shift();
      this.emit("log", line);
    });
    const failed = (error: Error) => this.onExit(null, error);
    proc.on("error", failed);
    proc.on("exit", (code) => this.onExit(code));
    return (await this.request("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: "abp-vscode", title: "ABP for VS Code", version: this.options.clientVersion ?? "0" },
    })) as InitializeResult;
  }

  async newSession(cwd: string): Promise<string> {
    const result = (await this.request("session/new", { cwd, mcpServers: [] })) as { sessionId: string };
    return result.sessionId;
  }

  prompt(sessionId: string, prompt: ContentBlock[]): Promise<PromptResult> {
    return this.request("session/prompt", { sessionId, prompt }) as Promise<PromptResult>;
  }

  cancel(sessionId: string): void {
    this.send({ method: "session/cancel", params: { sessionId } });
  }

  /** Stop the agent and everything it started (on Windows, the whole process tree). */
  dispose(): void {
    const proc = this.proc;
    if (!proc || this.exited) return;
    try {
      proc.stdin.end();
    } catch {
      /* already closed */
    }
    if (process.platform === "win32" && proc.pid) {
      spawnSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { windowsHide: true });
    } else {
      proc.kill("SIGTERM");
    }
  }

  request(method: string, params: unknown): Promise<unknown> {
    if (!this.proc || this.exited) return Promise.reject(new Error(this.stoppedMessage()));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.send({ id, method, params });
    });
  }

  private send(message: Record<string, unknown>): void {
    if (!this.proc || this.exited) return;
    this.proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...message }) + "\n");
  }

  private onLine(line: string): void {
    if (!line.trim()) return;
    let message: { id?: number | string | null; method?: string; params?: any; result?: unknown; error?: { code?: number; message?: string } };
    try {
      message = JSON.parse(line);
    } catch {
      this.emit("log", `(not a protocol message) ${line}`);
      return;
    }
    if (message.method !== undefined) {
      if (message.id === undefined || message.id === null) {
        if (message.method === "session/update") this.emit("update", message.params?.sessionId, message.params?.update);
        return;
      }
      void this.answerRequest(message.id, message.method, message.params);
      return;
    }
    const waiting = typeof message.id === "number" ? this.pending.get(message.id) : undefined;
    if (!waiting) return;
    this.pending.delete(message.id as number);
    if (message.error) waiting.reject(new RpcError(message.error.code ?? -32603, message.error.message ?? "request failed"));
    else waiting.resolve(message.result);
  }

  private async answerRequest(id: number | string, method: string, params: any): Promise<void> {
    if (method !== "session/request_permission") {
      this.send({ id, error: { code: -32601, message: `method not found: ${method}` } });
      return;
    }
    let answer: PermissionAnswer = null;
    try {
      answer = await this.options.onPermission(params as PermissionRequest);
    } catch (error) {
      this.emit("log", `permission prompt failed: ${String(error)}`);
    }
    const outcome = answer ? { outcome: "selected", optionId: answer } : { outcome: "cancelled" };
    this.send({ id, result: { outcome } });
  }

  private onExit(code: number | null, error?: Error): void {
    if (this.exited) return;
    this.exited = true;
    const why = error ? new Error(`could not start ABP's agent: ${error.message}`) : new Error(this.stoppedMessage(code));
    for (const waiting of this.pending.values()) waiting.reject(why);
    this.pending.clear();
    this.emit("exit", code, this.stderrTail.slice(-10));
  }

  private stoppedMessage(code?: number | null): string {
    const tail = this.stderrTail.filter((l) => l.trim()).slice(-3).join("\n");
    const status = code === undefined ? "is not running" : `stopped${code === null ? "" : ` (exit code ${code})`}`;
    return `ABP's agent ${status}${tail ? `:\n${tail}` : ""}`;
  }
}
