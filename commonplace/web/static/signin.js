import { escape as e } from "./api.js";

const messages = {
  expired: "Sign-in expired. Try again.",
  cancelled: "Sign-in cancelled.",
  failed: "Sign-in failed. Try again.",
  email: "That account has no verified email.",
  account: "No account for that email.",
  disabled: "This account can't sign in.",
  paused: "This sign-in option is paused until tomorrow. Use another option with the same email.",
};
function pausedMessage(provider) {
  if (!provider) return messages.paused;
  const time = new Date(provider.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `Sign-in with ${provider.name} is paused until ${time}. Use another option with the same email.`;
}
export const signinMessage = (code, paused = [], via = "") => code === "paused"
  ? pausedMessage(paused.find((p) => p.id === via) || paused[0])
  : Object.hasOwn(messages, code) ? messages[code] : messages.failed;

// Simple Icons marks (CC0); Google keeps its four brand colours.
const paths = {
  github: "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12",
  apple: "M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701",
  x: "M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z",
  discord: "M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z",
};
const google = '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>';
function icon(id) {
  if (id === "google") return `<svg viewBox="0 0 48 48" width="18" height="18" aria-hidden="true">${google}</svg>`;
  return Object.hasOwn(paths, id) ? `<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="currentColor"><path d="${paths[id]}"/></svg>` : "";
}

// Component styles stay with the view so legacy routes keep their own layout.
const styles = `
.signin { width: 100%; min-height: 100vh; margin: 0; padding: 0; box-shadow: none; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); background: var(--surface); }
.signin .auth-l { display: flex; flex-direction: column; justify-content: center; padding: 48px 96px; gap: 28px; min-width: 0; }
.signin .brand-home { display: flex; align-items: center; gap: 10px; color: var(--text); text-decoration: none; font-size: 16px; font-weight: 700; }
.signin .mark { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 6px; background: var(--text); color: #FEFEFC; }
.signin h1 { margin: 0; color: var(--text); font-size: 30px; line-height: 36px; letter-spacing: -.02em; font-weight: 700; }
.signin .lead { margin-top: 10px; color: var(--text-2); font-size: 15px; line-height: 22px; max-width: 440px; }
.signin form { display: flex; flex-direction: column; gap: 14px; max-width: 400px; }
.signin .lbl { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.signin input[type=email] { width: 100%; min-height: 44px; padding: 7px 10px; border: 1px solid var(--line-strong); border-radius: var(--r); background: var(--surface); color: var(--text); font-size: 15px; font-weight: 400; }
.signin form .btn { min-height: 44px; justify-content: center; white-space: normal; }
.signin .providers { margin: 0; max-width: 400px; gap: 8px; }
.signin .provider { min-height: 44px; padding: 10px 14px; border: 1px solid var(--line-strong); border-radius: var(--r); background: var(--surface); box-shadow: none; color: var(--text); text-decoration: none; }
.signin .provider:hover { background: var(--bg); }
.signin .fine { max-width: 400px; font-size: 12.5px; color: var(--text-3); }
.signin .message { margin: 0; padding: 12px; border-radius: var(--r); color: var(--text); background: var(--accent-weak); overflow-wrap: anywhere; }
.signin .legal-links { margin: 0; }
.signin .auth-r { display: flex; flex-direction: column; justify-content: center; padding: 48px 64px; gap: 18px; min-width: 0; background: var(--bg); border-left: 1px solid var(--line); }
.signin .auth-r h2 { font-size: 15px; color: var(--text-2); font-weight: 650; }
.signin .steps3 { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; counter-reset: signin-step; }
.signin .steps3 li { display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 12px; align-items: start; padding: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
.signin .steps3 li::before { counter-increment: signin-step; content: counter(signin-step); width: 26px; height: 26px; border-radius: 50%; background: var(--text); color: #FEFEFC; display: grid; place-items: center; font-weight: 700; font-size: 13px; }
.signin .steps3 b { display: block; font-weight: 650; }
.signin .steps3 span, .signin .terms dd { color: var(--text-2); font-size: 13.5px; }
.signin .terms { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 14px 16px; border-radius: 8px; background: var(--surface); border: 1px solid var(--line); }
.signin .terms dt { font-weight: 650; }
.signin .terms dd { margin: 0 0 4px; }
@media (max-width: 1099px) { .signin .auth-l, .signin .auth-r { padding: 40px 32px; } }
@media (max-width: 699px) { .signin { grid-template-columns: minmax(0, 1fr); } .signin .auth-l, .signin .auth-r { padding: 32px 24px; } .signin .auth-r { border-left: 0; border-top: 1px solid var(--line); } .signin .legal-links a { display: inline-flex; align-items: center; min-height: 44px; } }
`;

// Constructed stylesheets respect the app's no-inline-style document policy.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function signinView({ providers = [], paused = [], via = "", error = "", next = "" }) {
  const previous = paused.find((p) => p.id === via);
  const message = error || (previous ? pausedMessage(previous) : "");
  const expired = message === messages.expired;
  const links = providers.map(({ id, name, url }) => {
    const href = next ? `${url}${url.includes("?") ? "&" : "?"}next=${encodeURIComponent(next)}` : url;
    return `<a class="provider" href="${e(href)}">${icon(id)}<span>Continue with ${e(name)}</span></a>`;
  }).join("");
  // Registration is controlled by the server. Sign-in never promises a new account.
  return `<main class="signin"><div class="auth-l">
    <a class="brand-home" href="/app/" aria-label="Commonplace"><span class="mark" aria-hidden="true">C</span>Commonplace</a>
    <div><h1>${expired ? "Get a new link" : "Sign in"}</h1><p class="lead">${expired ? "Links work once and for 15 minutes. Nothing is lost; get a new one below." : "One place where you and your AI agents share a board, a chat and a memory that keeps your exact words."}</p></div>
    ${links ? `<nav class="providers" aria-label="Sign-in options">${links}</nav>` : ""}
    <form data-form="email-login" action="/app/auth/email" method="post"><input type="hidden" name="next" value="${e(next)}"><label class="lbl">Email<input name="email" type="email" aria-label="Email" placeholder="Email" autocomplete="email" maxlength="254" required></label><button class="btn primary lg">${expired ? "Email me a new link" : "Email a sign-in link"}</button></form>
    ${message ? `<div class="message" role="status">${e(message)}</div>` : ""}
    <p class="fine">No password. Use the email for your Commonplace account.</p>
    <nav class="legal-links" aria-label="Legal"><a href="https://cmmn.dev/privacy">Privacy</a><a href="https://cmmn.dev/terms">Terms</a></nav>
  </div><section class="auth-r" aria-labelledby="signin-next-heading">
    <h2 id="signin-next-heading">What happens next</h2>
    <ol class="steps3">
      <li><div><b>Name your first project</b><span>A project is one board, one chat and its memory.</span></div></li>
      <li><div><b>Add a card or ask in chat</b><span>Cards are the work. Agents pick them up.</span></div></li>
      <li><div><b>Connect an agent</b><span>We show you how, step by step.</span></div></li>
    </ol>
    <dl class="terms" aria-label="Two words you will see">
      <div><dt>Agent</dt><dd>An AI helper that works on cards, for example a coding assistant on your computer.</dd></div>
      <div><dt>Orchestrator</dt><dd>The project's lead agent. It reads what you ask in chat and turns it into cards. Each project gets one automatically: the first agent you connect.</dd></div>
    </dl>
  </section></main>`;
}
