// Wire protocol v1 (docs/browser-extension/DESIGN.md section 4). JSON-RPC 2.0-shaped frames over a WebSocket.
export const PROTOCOL = 1;

export type ErrorCode =
  | 'E_AUTH' | 'E_PROTOCOL' | 'E_METHOD' | 'E_PARAMS' | 'E_TIMEOUT' | 'E_CANCELLED' | 'E_NO_TAB' | 'E_STALE_REF' | 'E_NOT_ALLOWED'
  | 'E_NEEDS_APPROVAL' | 'E_SENSITIVE_SITE' | 'E_PAGE_CHANGED' | 'E_DEBUGGER_UNAVAILABLE' | 'E_NOT_INTERACTABLE' | 'E_BLOCKED_BY_PAGE'
  | 'E_TOO_LARGE' | 'E_BUSY' | 'E_ADAPTER_BROKEN' | 'E_NOT_LOGGED_IN' | 'E_RATE_LIMITED' | 'E_MODEL_UNAVAILABLE' | 'E_OOM' | 'E_INTERNAL'
  | 'E_NOT_CONNECTED' | 'E_INTEGRITY';

export interface Ctx {
  session?: string;
  trace?: string;
  deadline_ms?: number;
  idem?: string;
  approval?: { id: string; by: 'user' | 'policy' };
}

export interface RequestFrame { v: 1; id?: string; method: string; params?: unknown; ctx?: Ctx }
export interface ResultFrame { v: 1; id: string; result: unknown }
export interface ErrorFrame { v: 1; id?: string; error: WireError }
export type Frame = RequestFrame | ResultFrame | ErrorFrame;

export interface WireError { code: ErrorCode; message: string; data?: unknown; retryable?: boolean; hint?: string }

export class BridgeError extends Error {
  readonly code: ErrorCode;
  readonly retryable: boolean;
  readonly data: unknown;
  readonly hint: string;
  constructor(code: ErrorCode, message = '', opts: { retryable?: boolean; data?: unknown; hint?: string } = {}) {
    super(message ? `${code}: ${message}` : code);
    this.code = code;
    this.retryable = opts.retryable ?? false;
    this.data = opts.data;
    this.hint = opts.hint ?? '';
    this.name = 'BridgeError';
  }
  toWire(): WireError {
    return { code: this.code, message: this.message.replace(/^E_[A-Z_]+: /, ''), data: this.data ?? {}, retryable: this.retryable, hint: this.hint };
  }
}

export function toBridgeError(e: unknown): BridgeError {
  if (e instanceof BridgeError) return e;
  const msg = e instanceof Error ? e.message : String(e);
  return new BridgeError('E_INTERNAL', msg.slice(0, 300));
}

export const newId = (): string => crypto.randomUUID().replace(/-/g, '');

export const isRequest = (m: unknown): m is RequestFrame => typeof m === 'object' && m !== null && typeof (m as RequestFrame).method === 'string';
export const isResponse = (m: unknown): m is ResultFrame | ErrorFrame =>
  typeof m === 'object' && m !== null && !('method' in (m as object)) && ('result' in (m as object) || 'error' in (m as object));

export const okFrame = (id: string, result: unknown): ResultFrame => ({ v: 1, id, result: result ?? {} });
export const errFrame = (id: string | undefined, e: BridgeError): ErrorFrame => ({ v: 1, id, error: e.toWire() });

export const MAX_FRAME_BYTES = 1_048_576;
export const DEFAULT_DEADLINE_MS = 30_000;
