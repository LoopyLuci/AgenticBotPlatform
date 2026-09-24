// Every request, decision and result is recorded locally (a capped ring) and forwarded to ABP's audit log. Values typed into
// pages and anything secret are never recorded: callers pass a short description, never the payload.
import { getLocal, setLocal } from './storage';

export interface AuditEntry { at: number; action: string; detail: string }
const RING = 500;
let buffer: AuditEntry[] = [];
let outbox: AuditEntry[] = [];
let flush: ((entries: AuditEntry[]) => Promise<void>) | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;

export function setAuditSink(fn: ((entries: AuditEntry[]) => Promise<void>) | null): void { flush = fn; }

export function audit(action: string, detail = ''): void {
  const e: AuditEntry = { at: Date.now(), action: action.slice(0, 60), detail: detail.slice(0, 300) };
  buffer.push(e);
  outbox.push(e);
  if (buffer.length > RING) buffer = buffer.slice(-RING);
  if (outbox.length > 200) outbox = outbox.slice(-200);
  if (!timer) timer = setTimeout(() => { timer = null; void drain(); }, 1500);
}

async function drain(): Promise<void> {
  const local = buffer.slice(-RING);
  void setLocal('abp.audit', local).catch(() => undefined);
  if (!flush || !outbox.length) return;
  const batch = outbox;
  outbox = [];
  try { await flush(batch); } catch { outbox = [...batch, ...outbox].slice(-200); }      // not connected: keep for the next flush
}

export async function loadAudit(): Promise<AuditEntry[]> {
  if (!buffer.length) buffer = await getLocal<AuditEntry[]>('abp.audit', []);
  return buffer.slice();
}
export const auditNow = (): Promise<void> => drain();
