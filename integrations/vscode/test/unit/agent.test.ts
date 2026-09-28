/**
 * The ACP client and the controller against the REAL `python -m abp_acp` from this
 * checkout, with a scripted model (no key, no cost, deterministic). Skipped only when the
 * checkout has no .venv to run it with.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { AcpClient, RpcError, type PermissionRequest, type SessionUpdate } from "../../src/acp";
import { AgentController, agentArgs, type Entry } from "../../src/controller";
import { ABP_ROOT, HAVE_PYTHON, PYTHON, script, tempDir, waitFor } from "./helpers";

const live = describe.skipIf(!HAVE_PYTHON);
const cleanup: Array<() => void> = [];
afterEach(() => {
  while (cleanup.length) cleanup.pop()!();
});

function client(model: string, answer: (req: PermissionRequest) => string | null): AcpClient {
  const c = new AcpClient({ python: PYTHON, root: ABP_ROOT, args: ["--model", model], onPermission: async (r) => answer(r) });
  cleanup.push(() => c.dispose());
  return c;
}

live("AcpClient against the real agent", () => {
  it("initializes, streams a reply, reports tool activity and the model", async () => {
    const dir = tempDir("abp-acp-");
    fs.writeFileSync(path.join(dir, "config.json"), '{"port": 8123}');
    const c = client(script(dir, [{ call: "read_file", args: { path: "config.json" } }, { say: "The port is **8123**." }]), () => null);
    const init = await c.start();
    expect(init.protocolVersion).toBe(1);
    expect(init.agentInfo?.name).toBe("abp");
    const updates: SessionUpdate[] = [];
    c.on("update", (_sid: string, u: SessionUpdate) => updates.push(u));
    const sid = await c.newSession(dir);
    const result = await c.prompt(sid, [{ type: "text", text: "what port?" }]);
    expect(result.stopReason).toBe("end_turn");
    expect(result._meta?.abp?.model).toBe("scripted");
    const text = updates.filter((u) => u.sessionUpdate === "agent_message_chunk").map((u) => u.content?.text).join("");
    expect(text).toBe("The port is **8123**.");
    expect(updates.some((u) => u.sessionUpdate === "tool_call" && /read_file/.test(u.title ?? ""))).toBe(true);
  }, 60_000);

  it("asks permission before a write and obeys the answer", async () => {
    const dir = tempDir("abp-acp-");
    const steps = [{ call: "write_file", args: { path: "out.txt", content: "written" } }, { say: "done" }];
    const asked: PermissionRequest[] = [];
    const allow = client(script(dir, steps), (r) => (asked.push(r), "allow_once"));
    await allow.start();
    await allow.prompt(await allow.newSession(dir), [{ type: "text", text: "write it" }]);
    expect(fs.readFileSync(path.join(dir, "out.txt"), "utf8")).toBe("written");
    expect(asked[0]?.options.map((o) => o.optionId)).toEqual(["allow_once", "allow_always", "reject_once"]);

    const other = tempDir("abp-acp-");
    const deny = client(script(other, steps), () => null);
    await deny.start();
    await deny.prompt(await deny.newSession(other), [{ type: "text", text: "write it" }]);
    expect(fs.existsSync(path.join(other, "out.txt"))).toBe(false);
  }, 60_000);

  it("turns protocol errors into RpcError and a dead agent into a clear message", async () => {
    const dir = tempDir("abp-acp-");
    const c = client(script(dir, [{ say: "x" }]), () => null);
    await c.start();
    await expect(c.newSession("relative/path")).rejects.toBeInstanceOf(RpcError);
    c.dispose();
    await waitFor(() => !c.running);
    await expect(c.newSession(dir)).rejects.toThrow(/ABP's agent/);
  }, 60_000);

  it("reports why the agent could not start", async () => {
    const c = new AcpClient({ python: PYTHON, root: ABP_ROOT, args: ["--model", "scripted:/no/such/script.json"], onPermission: async () => null });
    cleanup.push(() => c.dispose());
    await expect(c.start()).rejects.toThrow(/could not read the script/);
  }, 60_000);
});

function controllerFor(workspace: string, model: string) {
  const logs: string[] = [];
  const asked: string[] = [];
  const c = new AgentController({
    install: () => ({ root: ABP_ROOT, python: PYTHON, source: "test" }),
    settings: () => ({ model, permissionMode: "" }),
    workspace: () => workspace,
    log: (l) => logs.push(l),
    onPermissionAsked: (e) => asked.push(e.id),
  });
  cleanup.push(() => c.dispose());
  return { c, logs, asked };
}

live("AgentController against the real agent", () => {
  it("keeps a transcript of the conversation and its tool activity", async () => {
    const dir = tempDir("abp-ctl-");
    fs.writeFileSync(path.join(dir, "a.txt"), "alpha");
    const { c } = controllerFor(dir, script(dir, [{ call: "read_file", args: { path: "a.txt" } }, { say: "It says alpha." }]));
    const states: string[] = [c.state];
    c.onDidChange(() => states.at(-1) !== c.state && states.push(c.state));
    await c.send("what is in a.txt?");
    const kinds = c.entries.map((e) => e.kind);
    expect(kinds[0]).toBe("user");
    expect(kinds).toContain("tool");
    expect(c.entries.at(-1)).toEqual({ kind: "agent", text: "It says alpha." });
    expect(c.entries.every((e) => e.kind !== "tool" || e.status === "completed")).toBe(true);
    expect(states).toEqual(["stopped", "starting", "working", "idle"]);
    expect(c.model).toBe("scripted");
  }, 60_000);

  it("waits for a person to answer a permission question, from either place", async () => {
    const dir = tempDir("abp-ctl-");
    const steps = [{ call: "write_file", args: { path: "b.txt", content: "yes" } }, { say: "written" }];
    const { c, asked } = controllerFor(dir, script(dir, steps));
    const turn = c.send("write b.txt");
    const question = await waitFor(() => c.entries.find((e): e is Extract<Entry, { kind: "permission" }> => e.kind === "permission"));
    expect(asked).toEqual([question.id]);
    expect(fs.existsSync(path.join(dir, "b.txt"))).toBe(false);
    c.answerPermission(question.id, "allow_once");
    c.answerPermission(question.id, "reject_once");          // a second answer (the other place) is ignored
    await turn;
    expect(fs.readFileSync(path.join(dir, "b.txt"), "utf8")).toBe("yes");
    expect(question.answer).toBe("allow_once");
  }, 60_000);

  it("stopping refuses an open question and ends the turn", async () => {
    const dir = tempDir("abp-ctl-");
    const { c } = controllerFor(dir, script(dir, [{ call: "write_file", args: { path: "c.txt", content: "no" } }, { say: "x" }]));
    const turn = c.send("write c.txt");
    await waitFor(() => c.entries.some((e) => e.kind === "permission"));
    c.cancel();
    await turn;
    expect(fs.existsSync(path.join(dir, "c.txt"))).toBe(false);
    expect(c.entries.some((e) => e.kind === "permission" && e.answer === "dismissed")).toBe(true);
    expect(c.state).toBe("idle");
  }, 60_000);

  it("explains a missing install or folder in the transcript", async () => {
    const noFolder = new AgentController({
      install: () => ({ root: ABP_ROOT, python: PYTHON, source: "test" }),
      settings: () => ({ model: "auto", permissionMode: "" }),
      workspace: () => undefined,
      log: () => undefined,
    });
    await noFolder.send("hi");
    expect(noFolder.entries.at(-1)).toMatchObject({ kind: "notice", level: "error", text: expect.stringMatching(/Open a folder/) });
    const noAbp = new AgentController({
      install: () => ({ error: "ABP was not found.", tried: [] }),
      settings: () => ({ model: "auto", permissionMode: "" }),
      workspace: () => ABP_ROOT,
      log: () => undefined,
    });
    await noAbp.send("hi");
    expect(noAbp.entries.at(-1)).toMatchObject({ kind: "notice", text: "ABP was not found." });
    expect(noAbp.state).toBe("stopped");
  });
});

describe("agentArgs", () => {
  it("defaults to auto (never a fixed provider) and passes the permission mode only when set", () => {
    expect(agentArgs({ model: "", permissionMode: "" })).toEqual(["--model", "auto"]);
    expect(agentArgs({ model: "openrouter/x", permissionMode: "plan" })).toEqual(["--model", "openrouter/x", "--permission-mode", "plan"]);
  });
});
