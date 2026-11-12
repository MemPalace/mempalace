#!/usr/bin/env node
/**
 * Shared cross-platform helpers for MemPalace Antigravity lifecycle hooks:
 *   - scripts/save-hook.mjs  (Stop event)
 *   - scripts/wake-hook.mjs  (PreInvocation event, gated to invocationNum === 1)
 *
 * Zero external npm dependencies; runs identically on Windows, macOS, and Linux
 * under the Node.js runtime bundled with Google Antigravity.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const isWindows = process.platform === 'win32';

/**
 * Resolve home directory, honoring `HOME` / `USERPROFILE` overrides for hermetic tests.
 */
export function resolveHome() {
  const envHome = (process.env.HOME || '').trim();
  if (envHome) return envHome;
  const userProfile = (process.env.USERPROFILE || '').trim();
  if (userProfile) return userProfile;
  return os.homedir();
}

/**
 * Expand a leading `~`, `~/`, or `~\` against `home`.
 */
export function expandHome(inputPath, home = resolveHome()) {
  const raw = String(inputPath || '');
  if (raw === '~') return home;
  if (raw.startsWith('~/') || raw.startsWith('~\\')) {
    return path.join(home, raw.slice(2));
  }
  return raw;
}

/**
 * Resolve the shared hook state directory (`~/.mempalace/hook_state` by default).
 */
export function resolveStateDir(home = resolveHome()) {
  const override = (process.env.MEMPAL_STATE_DIR || '').trim();
  if (override) return override;
  return path.join(home, '.mempalace', 'hook_state');
}

export function ensureStateDir(stateDir) {
  try {
    fs.mkdirSync(stateDir, { recursive: true });
  } catch {
    // Fail-open
  }
}

/**
 * Resolve MemPalace config directory matching `mempalace.paths.config_dir` & `common.sh`.
 */
export function resolveConfigDir(home = resolveHome()) {
  const cfgEnv = (process.env.MEMPALACE_CONFIG_DIR || '').trim();
  if (cfgEnv) {
    return expandHome(cfgEnv, home);
  }

  const legacy = path.join(home, '.mempalace');
  const legacyMarkers = [
    path.join(legacy, 'config.json'),
    path.join(legacy, 'people_map.json'),
    path.join(legacy, 'palace', 'chroma.sqlite3'),
    path.join(legacy, 'palace', 'palace.sqlite3'),
  ];
  if (legacyMarkers.some((p) => fs.existsSync(p))) {
    return legacy;
  }

  const xdgRaw = expandHome((process.env.XDG_CONFIG_HOME || '').trim(), home);
  if (xdgRaw && (xdgRaw.startsWith('/') || /^[A-Za-z]:[\\/]/.test(xdgRaw))) {
    return path.join(xdgRaw, 'mempalace');
  }

  return path.join(home, '.config', 'mempalace');
}

function isDir(dirPath) {
  try {
    return fs.statSync(dirPath).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Kill switch check. Evaluated BEFORE creating state directories or logs so
 * removing `~/.mempalace` ("palace nuke") leaves zero disk footprint.
 */
export function isKillSwitchTripped(home = resolveHome()) {
  const cfgDir = resolveConfigDir(home);
  const legacyDir = path.join(home, '.mempalace');

  if (!isDir(legacyDir) && !isDir(cfgDir)) {
    return true;
  }

  const disableHook = (process.env.MEMPAL_DISABLE_HOOK || '').trim().toLowerCase();
  if (['1', 'true', 'yes'].includes(disableHook)) {
    return true;
  }

  const autoSaveEnv = (process.env.MEMPALACE_HOOKS_AUTO_SAVE || '').trim().toLowerCase();
  if (['false', '0', 'no'].includes(autoSaveEnv)) {
    return true;
  }

  const cfgFile = path.join(cfgDir, 'config.json');
  try {
    if (fs.existsSync(cfgFile)) {
      const cfg = JSON.parse(fs.readFileSync(cfgFile, 'utf-8'));
      const autoSave = cfg?.hooks?.auto_save;
      if (autoSave === false || String(autoSave).toLowerCase() === 'false') {
        return true;
      }
    }
  } catch {
    // Malformed config fails open (hook stays enabled unless disabled)
  }

  return false;
}

/**
 * Append a structured ISO-8601 UTC log line to `antigravity_hook.log`.
 */
export function hookLog(stateDir, event, convId, message) {
  try {
    ensureStateDir(stateDir);
    const ts = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
    const line = `[${ts}] [event=${event || '?'}] [conv=${convId || 'unknown'}] ${message || ''}\n`;
    fs.appendFileSync(path.join(stateDir, 'antigravity_hook.log'), line, 'utf-8');
  } catch {
    // Fail-open
  }
}

/**
 * Fast, zero-subprocess Python interpreter discovery:
 *   1. $MEMPAL_PYTHON / $MEMPALACE_PYTHON override
 *   2. Shebang / sibling interpreter of `mempalace-mcp` or `mempalace` on PATH
 *   3. Standard `uv tool` / `pipx` isolated venv locations
 *   4. First `python3` / `python` on PATH
 */
export function resolvePythonBin(home = resolveHome()) {
  const explicit = (process.env.MEMPAL_PYTHON || process.env.MEMPALACE_PYTHON || '').trim();
  if (explicit && fs.existsSync(explicit)) {
    return explicit;
  }

  const pathDirs = (process.env.PATH || '')
    .split(path.delimiter)
    .map((d) => d.trim())
    .filter(Boolean);

  const scriptNames = isWindows
    ? ['mempalace-mcp.exe', 'mempalace-mcp', 'mempalace.exe', 'mempalace']
    : ['mempalace-mcp', 'mempalace'];

  for (const dir of pathDirs) {
    for (const name of scriptNames) {
      const fullPath = path.join(dir, name);
      try {
        const stat = fs.statSync(fullPath);
        if (!stat.isFile()) continue;

        // Parse shebang first (works on POSIX scripts & Windows pip/uv .exe wrappers)
        if (stat.size > 0 && stat.size <= 1024 * 1024) {
          const raw = fs.readFileSync(fullPath).toString('latin1');
          const match = raw.match(/#!"?([^\r\n"]*?python[0-9.]*(?:\.exe)?)"?/i);
          if (match && match[1]) {
            const interp = match[1].trim();
            const base = path.basename(interp).toLowerCase();
            if (base.startsWith('python') && fs.existsSync(interp)) {
              return interp;
            }
          }
        }

        // Check sibling python in isolated venv bin/ or Scripts/
        const sibling = path.join(dir, isWindows ? 'python.exe' : 'python');
        if (fs.existsSync(sibling)) {
          return sibling;
        }
      } catch {
        // Ignore unreadable candidate
      }
    }
  }

  // Standard uv tool / pipx install paths when ~/.local/bin isn't in GUI PATH
  const fallbackVenvs = isWindows
    ? [
        path.join(process.env.APPDATA || path.join(home, 'AppData', 'Roaming'), 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe'),
        path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe'),
        path.join(home, '.local', 'share', 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe'),
      ]
    : [
        path.join(home, '.local', 'share', 'uv', 'tools', 'mempalace', 'bin', 'python'),
        path.join(home, '.local', 'pipx', 'venvs', 'mempalace', 'bin', 'python'),
      ];

  for (const candidate of fallbackVenvs) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  const pyNames = isWindows
    ? ['python.exe', 'python3.exe', 'python', 'python3']
    : ['python3', 'python'];
  for (const dir of pathDirs) {
    for (const pyName of pyNames) {
      const candidate = path.join(dir, pyName);
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }

  return isWindows ? 'python' : 'python3';
}

function sanitizeControlAndSurrogates(val) {
  const str = String(val ?? '');
  let out = '';
  for (let i = 0; i < str.length; i += 1) {
    const code = str.charCodeAt(i);
    if (code < 32 || code === 127) continue;
    // Valid surrogate pair (astral Unicode plane, e.g. emoji / CJK extensions)
    if (code >= 0xd800 && code <= 0xdbff && i + 1 < str.length) {
      const low = str.charCodeAt(i + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        out += str[i] + str[i + 1];
        i += 1;
        continue;
      }
      continue;
    }
    // Strip lone UTF-16 surrogates
    if (code >= 0xd800 && code <= 0xdfff) continue;
    out += str[i];
  }
  return out;
}

function sanitizeId(val) {
  return String(val ?? '').replace(/[^a-zA-Z0-9._-]/g, '');
}

function sanitizeNonNegInt(val, fallback = 0) {
  if (typeof val === 'boolean' || val === null || val === undefined || val === '') {
    return fallback;
  }
  const n = Number.parseInt(String(val), 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

function normalizeFilePath(rawPath, home) {
  let cleaned = sanitizeControlAndSurrogates(rawPath);
  if (!cleaned) return '';
  if (cleaned.startsWith('file://')) {
    try {
      cleaned = fileURLToPath(cleaned);
    } catch {
      // Keep cleaned value if malformed file:// URL
    }
  }
  return expandHome(cleaned, home);
}

/**
 * Parse Antigravity camelCase hook stdin JSON safely.
 */
export function parseHookStdin(rawInput, home = resolveHome()) {
  const data = JSON.parse(rawInput);
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Hook stdin payload must be a JSON object');
  }

  const wpArr = Array.isArray(data.workspacePaths) ? data.workspacePaths : [];
  const firstWorkspace = wpArr.length > 0 ? normalizeFilePath(wpArr[0], home) : '';
  const fullyIdleRaw = data.fullyIdle;
  const fullyIdle =
    fullyIdleRaw === true ||
    ['true', '1', 'yes'].includes(String(fullyIdleRaw ?? '').toLowerCase());

  return {
    conversationId: sanitizeId(data.conversationId) || 'unknown',
    transcriptPath: normalizeFilePath(data.transcriptPath, home),
    workspacePath: firstWorkspace,
    artifactDirectoryPath: normalizeFilePath(data.artifactDirectoryPath, home),
    executionNum: sanitizeNonNegInt(data.executionNum, 0),
    terminationReason: String(data.terminationReason ?? '')
      .toLowerCase()
      .replace(/[^a-z_]/g, ''),
    fullyIdle,
    invocationNum: sanitizeNonNegInt(data.invocationNum, 0),
    initialNumSteps: sanitizeNonNegInt(data.initialNumSteps, 0),
  };
}

/**
 * Exact parity with `mempalace.config.normalize_wing_name` + `common.sh:mempal_infer_wing`.
 */
export function inferWing(workspacePath, fallback = 'sessions') {
  let ws = String(workspacePath || '');
  if (!ws) return fallback;

  // Strip trailing slashes and backslashes
  ws = ws.replace(/[\\/]+$/g, '');
  if (!ws) return fallback;

  const parts = ws.split(/[\\/]/);
  const leaf = parts[parts.length - 1] || '';
  if (!leaf || /^[A-Za-z]:$/.test(leaf)) {
    return fallback;
  }

  let slug = leaf.toLowerCase().replace(/[ -]/g, '_');
  slug = slug.replace(/[^\p{L}\p{N}_.']+/gu, '_');
  slug = slug.replace(/\.{2,}/g, '.');
  slug = Array.from(slug).slice(0, 120).join('').replace(/^[_.']+|[_.']+$/g, '');
  return slug || fallback;
}

/**
 * Validate transcript path (`.json` or `.jsonl`, no `..` traversal segment).
 */
export function isValidTranscriptPath(transcriptPath) {
  if (!transcriptPath) return false;
  const lower = transcriptPath.toLowerCase();
  if (!lower.endsWith('.json') && !lower.endsWith('.jsonl')) {
    return false;
  }
  const normalized = transcriptPath.replace(/\\/g, '/');
  if (`/${normalized}/`.includes('/../')) {
    return false;
  }
  return true;
}

/**
 * Read `MEMPAL_SAVE_INTERVAL` (default 15, floored to >= 1 -> 15 on invalid/zero).
 */
export function resolveSaveInterval() {
  const raw = String(process.env.MEMPAL_SAVE_INTERVAL ?? '15').trim();
  if (!/^\d+$/.test(raw)) return 15;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 1 ? parsed : 15;
}

/**
 * Read `MEMPAL_STATE_TTL_DAYS` (default 30, digits only).
 */
export function resolveStateTtlDays() {
  const raw = String(process.env.MEMPAL_STATE_TTL_DAYS ?? '30').trim();
  if (!/^\d+$/.test(raw)) return 30;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 30;
}

/**
 * Atomic single-file counter write via same-directory temp file + rename.
 */
export function writeCounterAtomic(filePath, value) {
  const body = String(value);
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.writeFileSync(tmpPath, body, 'utf-8');
    fs.renameSync(tmpPath, filePath);
  } catch {
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      // Ignore temp cleanup error
    }
    try {
      fs.writeFileSync(filePath, body, 'utf-8');
    } catch {
      // Fail-open
    }
  }
}

/**
 * Opportunistic GC of stale per-conversation state files older than TTL days
 * (throttled to at most once every 24 hours).
 */
export function gcStaleState(stateDir) {
  try {
    if (!isDir(stateDir)) return;
    const marker = path.join(stateDir, 'antigravity_last_sweep');
    const nowMs = Date.now();
    try {
      const st = fs.statSync(marker);
      if (nowMs - st.mtimeMs < 86_400_000) {
        return;
      }
    } catch {
      // Marker missing -> run sweep
    }

    try {
      fs.writeFileSync(marker, '', 'utf-8');
    } catch {
      return;
    }

    const ttlDays = resolveStateTtlDays();
    const cutoffMs = nowMs - (ttlDays + 1) * 86_400_000;
    const entries = fs.readdirSync(stateDir, { withFileTypes: true });
    for (const entry of entries) {
      const name = entry.name;
      const isCountOrPending =
        entry.isFile() &&
        (name.startsWith('antigravity_save_count_') || name.startsWith('antigravity_pending_'));
      const isWokeDir = entry.isDirectory() && name.startsWith('antigravity_woke_');
      if (!isCountOrPending && !isWokeDir) continue;

      const full = path.join(stateDir, name);
      try {
        const st = fs.statSync(full);
        if (st.mtimeMs < cutoffMs) {
          fs.rmSync(full, { recursive: true, force: true });
        }
      } catch {
        // Ignore individual entry error
      }
    }
  } catch {
    // Fail-open
  }
}

/**
 * Read all UTF-8 text from stdin.
 */
export function readStdinSync() {
  try {
    return fs.readFileSync(0, 'utf-8');
  } catch {
    return '';
  }
}

export function emitStopPass() {
  process.stdout.write('{}\n');
}
