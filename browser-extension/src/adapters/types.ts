// A chat-website adapter is DATA, not code (DESIGN.md section 7.1): the engine in src/content/webengine.ts interprets it.
// A selector list is ranked, first match wins, and which entry matched is reported so drift shows up before it breaks:
//   "css selector"          any CSS selector
//   "aria:Send message"     an element whose aria-label contains the text (case-insensitive)
//   "text:button:Send"      a <button> (any tag) whose visible text contains the text
export type Sel = string[];

export interface ErrorRule { within?: Sel; match: string; code: 'E_RATE_LIMITED' | 'E_NOT_LOGGED_IN' | 'E_ADAPTER_BROKEN'; message: string }

export interface AdapterDef {
  id: string;
  name: string;
  version: number;
  /** hostnames (subdomains included); "host/path" restricts to a path prefix */
  hosts: string[];
  home: string;
  tos_note: string;
  vision?: boolean;
  models?: string[];
  login: { logged_out?: Sel; ready: Sel };
  new_chat?: { click?: Sel; navigate?: string };
  compose: { input: Sel; submit?: Sel; key?: 'Enter' | 'Ctrl+Enter'; mode?: 'paste' | 'type' };
  stream: { assistant: Sel; generating?: Sel; quiet_ms?: number; strip?: string[] };
  errors?: ErrorRule[];
  limits: { min_interval_ms: number; max_prompt_chars: number };
  selftest: { prompt: string; expect: string };
  /** development-only adapter (a mock chat page); never present in store builds */
  dev?: boolean;
}

export interface ProbeResult {
  ready: boolean; logged_out: boolean; url: string; title: string;
  matched: { input: number; submit: number; assistant: number; generating: number };
  error: { code: string; message: string } | null;
}
export interface ReadResult {
  count: number; text: string; generating: boolean; error: { code: string; message: string } | null; matched: { assistant: number; generating: number };
}
