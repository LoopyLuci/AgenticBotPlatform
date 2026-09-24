// The same URL classification as bot/browser_policy.py. Both are tested against tests/fixtures/url_vectors.json so they cannot drift.
import hosts from './sensitive-hosts.json';

export interface Policy {
  blocked_schemes: string[];
  sensitive_hosts: Record<string, string[]>;
  sensitive_path_regex: string;
  extra_sensitive: string[];
  sensitive_grants: string[];
  trusted_sites: string[];
  max_tabs: number;
  actions_per_minute: number;
  navigations_per_minute: number;
  max_session_minutes: number;
  capabilities: Record<'read' | 'interact' | 'navigate' | 'forms' | 'downloads' | 'uploads' | 'eval' | 'history', boolean>;
}

export interface Verdict { allowed: boolean; sensitive: boolean; category: string; reason: string }

export const DEFAULT_POLICY: Policy = {
  blocked_schemes: ['chrome', 'edge', 'brave', 'opera', 'vivaldi', 'about', 'chrome-extension', 'moz-extension', 'safari-web-extension', 'devtools',
    'view-source', 'file', 'javascript', 'data', 'blob', 'ftp'],
  sensitive_hosts: hosts.sensitive_hosts,
  sensitive_path_regex: '(^|/)(login|log-in|signin|sign-in|sso|oauth2?|authorize|2fa|mfa|verify|checkout|payment|payments|billing|wallet|' +
    'account/security|security/settings|password|reset-password|change-password)(/|$|\\?|\\.)',
  extra_sensitive: [], sensitive_grants: [], trusted_sites: [], max_tabs: 5, actions_per_minute: 60, navigations_per_minute: 20, max_session_minutes: 60,
  capabilities: { read: true, interact: true, navigate: true, forms: true, downloads: false, uploads: false, eval: false, history: false },
};

const hostMatches = (host: string, pattern: string): boolean => {
  const h = host.toLowerCase().replace(/\.$/, '');
  const p = pattern.toLowerCase();
  if (p.includes('/')) return false;
  return h === p || h.endsWith('.' + p);
};
const pathMatches = (host: string, path: string, pattern: string): boolean => {
  if (!pattern.includes('/')) return false;
  const i = pattern.indexOf('/');
  return hostMatches(host, pattern.slice(0, i)) && path.toLowerCase().startsWith('/' + pattern.slice(i + 1).toLowerCase());
};

export function classify(rawUrl: string, policy: Policy = DEFAULT_POLICY): Verdict {
  let u: URL;
  try { u = new URL(String(rawUrl).trim()); } catch { return { allowed: false, sensitive: true, category: 'invalid', reason: 'not a valid URL' }; }
  const scheme = u.protocol.replace(/:$/, '').toLowerCase();
  if (policy.blocked_schemes.includes(scheme)) return { allowed: false, sensitive: true, category: 'browser_internal', reason: `${scheme}: pages are never automated` };
  if (scheme !== 'http' && scheme !== 'https') return { allowed: false, sensitive: true, category: 'invalid', reason: `unsupported scheme ${scheme}` };
  const host = u.hostname.toLowerCase();
  if (!host) return { allowed: false, sensitive: true, category: 'invalid', reason: 'no host' };
  for (const p of policy.extra_sensitive) {
    if (hostMatches(host, p) || pathMatches(host, u.pathname || '/', p)) return { allowed: true, sensitive: true, category: 'user_blocklist', reason: `${host} is on your sensitive-site list` };
  }
  for (const [category, patterns] of Object.entries(policy.sensitive_hosts)) {
    for (const p of patterns) {
      if (hostMatches(host, p) || pathMatches(host, u.pathname || '/', p)) return { allowed: true, sensitive: true, category, reason: `${host} looks like ${category.replace(/_/g, ' ')}` };
    }
  }
  const trusted = policy.trusted_sites.some((t) => hostMatches(host, t.toLowerCase().replace(/^\*\./, '')));
  const re = new RegExp(policy.sensitive_path_regex, 'i');
  if (!trusted && re.test((u.pathname || '/') + (u.search || ''))) return { allowed: true, sensitive: true, category: 'sensitive_page', reason: 'this looks like a login, checkout or account-security page' };
  return { allowed: true, sensitive: false, category: '', reason: '' };
}

/** May the agent OPEN this URL? Sensitive pages only with a per-site grant; browser-internal never. */
export function navigationVerdict(rawUrl: string, policy: Policy): Verdict {
  const v = classify(rawUrl, policy);
  if (!v.allowed || !v.sensitive) return v;
  const host = new URL(rawUrl).hostname.toLowerCase();
  if (policy.sensitive_grants.some((g) => hostMatches(host, g))) return { ...v, reason: 'allowed by your per-site grant' };
  return { ...v, allowed: false, reason: `${v.reason}. The agent does not open sensitive pages unless you grant that site.` };
}
