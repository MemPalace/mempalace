import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import { Response, SidecarApp } from 'sidecar_sdk';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BRIDGE_SCRIPT = path.join(__dirname, 'bridge.py');
const PLUGIN_ROOT = path.resolve(__dirname, '..', '..');

const indexHtmlPath = path.join(__dirname, 'index.html');
const stylesCssPath = path.join(__dirname, 'styles.css');
const appJsPath = path.join(__dirname, 'app.js');
const logoPath = path.join(PLUGIN_ROOT, 'assets', 'logo.svg');
const logoSvg = fs.existsSync(logoPath) ? fs.readFileSync(logoPath, 'utf-8') : '';

const isWindows = process.platform === 'win32';

/**
 * Check whether a candidate Python interpreter can import `mempalace`.
 */
function canImportMempalace(pythonBin, extraEnv = {}) {
  if (!pythonBin) return false;
  try {
    const res = spawnSync(
      pythonBin,
      ['-c', 'import mempalace.version; print(mempalace.version.__version__)'],
      {
        encoding: 'utf-8',
        timeout: 4000,
        windowsHide: true,
        env: { ...process.env, ...extraEnv, PYTHONUTF8: '1' },
      }
    );
    return res.status === 0 && Boolean((res.stdout || '').trim());
  } catch {
    return false;
  }
}

/**
 * Inspect console scripts (`mempalace-mcp`, `mempalace`) on PATH to locate the
 * isolated virtual environment's Python interpreter (uv tool / pipx / venv).
 */
function candidatesFromConsoleScripts() {
  const found = [];
  const locator = isWindows ? 'where.exe' : 'which';
  for (const scriptName of ['mempalace-mcp', 'mempalace']) {
    try {
      const res = spawnSync(locator, [scriptName], {
        encoding: 'utf-8',
        timeout: 2000,
        windowsHide: true,
      });
      if (res.status !== 0 || !res.stdout) continue;
      const lines = res.stdout
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
      for (const scriptPath of lines) {
        const scriptDir = path.dirname(scriptPath);
        // Sibling python in Scripts/ or bin/
        const siblingPy = path.join(scriptDir, isWindows ? 'python.exe' : 'python');
        if (fs.existsSync(siblingPy)) {
          found.push(siblingPy);
        }
        // Parse POSIX text shebang or Windows pip/uv .exe embedded shebang (#!...python.exe)
        try {
          const stat = fs.statSync(scriptPath);
          if (stat.size > 0 && stat.size <= 1024 * 1024) {
            const raw = fs.readFileSync(scriptPath).toString('latin1');
            const m = raw.match(/#!"?([^\r\n"]*?python[0-9.]*(?:\.exe)?)"?/i);
            if (m && m[1]) {
              const candidate = m[1].trim();
              if (fs.existsSync(candidate)) {
                found.push(candidate);
              }
            }
          }
        } catch {
          // Ignore unreadable script wrapper
        }
      }
    } catch {
      // Ignore locator error
    }
  }
  return found;
}

/**
 * Common uv tool and local dev checkout locations.
 */
function commonInstallCandidates() {
  const home = os.homedir();
  const candidates = [];

  if (isWindows) {
    const appData = process.env.APPDATA || path.join(home, 'AppData', 'Roaming');
    const localAppData = process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local');
    candidates.push(
      path.join(appData, 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe'),
      path.join(localAppData, 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe'),
      path.join(home, '.local', 'share', 'uv', 'tools', 'mempalace', 'Scripts', 'python.exe')
    );
  } else {
    candidates.push(
      path.join(home, '.local', 'share', 'uv', 'tools', 'mempalace', 'bin', 'python'),
      path.join(home, '.local', 'pipx', 'venvs', 'mempalace', 'bin', 'python')
    );
  }

  // If running from inside the MemPalace repo or workspace checkout:
  const possibleRoots = [
    path.resolve(PLUGIN_ROOT, '..'),
    path.resolve(PLUGIN_ROOT, '..', '..'),
    process.cwd(),
  ];
  for (const root of possibleRoots) {
    const venvPy = isWindows
      ? path.join(root, '.venv', 'Scripts', 'python.exe')
      : path.join(root, '.venv', 'bin', 'python');
    if (fs.existsSync(venvPy)) {
      candidates.push(venvPy);
    }
  }

  return candidates;
}

function resolvePythonRuntime() {
  const explicit = (process.env.MEMPAL_PYTHON || process.env.MEMPALACE_PYTHON || '').trim();
  if (explicit && canImportMempalace(explicit)) {
    return { pythonBin: explicit, extraEnv: {} };
  }

  const candidates = [
    ...candidatesFromConsoleScripts(),
    ...commonInstallCandidates(),
    ...(isWindows ? ['python', 'python3'] : ['python3', 'python']),
  ];

  const seen = new Set();
  const repoParentCandidates = [
    path.resolve(PLUGIN_ROOT, '..', '..'),
    path.resolve(PLUGIN_ROOT, '..'),
  ];
  const repoParent =
    repoParentCandidates.find((dir) => fs.existsSync(path.join(dir, 'mempalace', '__init__.py'))) ||
    null;
  const hasLocalRepo = Boolean(repoParent);

  for (const candidate of candidates) {
    if (!candidate || seen.has(candidate)) continue;
    seen.add(candidate);
    if (canImportMempalace(candidate)) {
      return { pythonBin: candidate, extraEnv: {} };
    }
    if (hasLocalRepo && canImportMempalace(candidate, { MEMPALACE_REPO_ROOT: repoParent, PYTHONPATH: repoParent })) {
      return {
        pythonBin: candidate,
        extraEnv: { MEMPALACE_REPO_ROOT: repoParent, PYTHONPATH: repoParent },
      };
    }
  }

  return {
    pythonBin: null,
    extraEnv: {},
    error:
      'Could not find a Python interpreter with `mempalace` installed. Install via `uv tool install mempalace` or `pip install mempalace`, or set `MEMPAL_PYTHON`.',
  };
}

class PalaceBridge {
  constructor() {
    this.child = null;
    this.ready = false;
    this.version = null;
    this.lastError = null;
    this.pythonBin = null;
    this.extraEnv = {};
    this.reqSeq = 1;
    this.pending = new Map();
    this.startingPromise = null;
    this.stderrTail = [];
  }

  async ensureStarted(forceRestart = false) {
    if (forceRestart && this.child) {
      try {
        this.child.kill();
      } catch {
        // ignore
      }
      this.child = null;
      this.ready = false;
      this.startingPromise = null;
    }

    if (this.child && this.ready) {
      return { ok: true, pythonBin: this.pythonBin, version: this.version };
    }
    if (this.startingPromise) {
      return this.startingPromise;
    }

    this.startingPromise = new Promise((resolve) => {
      const runtime = resolvePythonRuntime();
      if (!runtime.pythonBin) {
        this.lastError = runtime.error;
        this.startingPromise = null;
        resolve({ ok: false, error: this.lastError });
        return;
      }

      this.pythonBin = runtime.pythonBin;
      this.extraEnv = runtime.extraEnv || {};
      this.stderrTail = [];

      const child = spawn(this.pythonBin, ['-u', BRIDGE_SCRIPT], {
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
        env: {
          ...process.env,
          ...this.extraEnv,
          PYTHONUTF8: '1',
          PYTHONIOENCODING: 'utf-8',
        },
      });
      this.child = child;

      const startupTimer = setTimeout(() => {
        if (!this.ready) {
          this.lastError = `MemPalace bridge timed out during startup (${this.stderrTail.join(' | ') || 'no stderr'})`;
          this.startingPromise = null;
          resolve({ ok: false, error: this.lastError });
        }
      }, 15000);

      const rl = readline.createInterface({ input: child.stdout });
      rl.on('line', (rawLine) => {
        const line = rawLine.replace(/^\uFEFF/, '').trim();
        if (!line.startsWith('{')) return;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          return;
        }

        if (msg.type === 'ready') {
          clearTimeout(startupTimer);
          this.ready = true;
          this.version = msg.version || null;
          this.lastError = null;
          this.startingPromise = null;
          resolve({ ok: true, pythonBin: this.pythonBin, version: this.version });
          return;
        }

        if (msg.id !== undefined && this.pending.has(msg.id)) {
          const entry = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          clearTimeout(entry.timer);
          entry.resolve(msg);
        }
      });

      const errRl = readline.createInterface({ input: child.stderr });
      errRl.on('line', (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        this.stderrTail.push(trimmed);
        if (this.stderrTail.length > 12) this.stderrTail.shift();
      });

      child.on('error', (err) => {
        clearTimeout(startupTimer);
        this.ready = false;
        this.child = null;
        this.lastError = err.message;
        this.startingPromise = null;
        for (const [, entry] of this.pending) {
          clearTimeout(entry.timer);
          entry.resolve({ ok: false, error: err.message });
        }
        this.pending.clear();
        resolve({ ok: false, error: this.lastError });
      });

      child.on('exit', (code) => {
        clearTimeout(startupTimer);
        const wasReady = this.ready;
        this.ready = false;
        this.child = null;
        this.startingPromise = null;
        const errMsg = `Bridge process exited (code ${code}): ${this.stderrTail.slice(-3).join(' | ')}`;
        this.lastError = errMsg;
        for (const [, entry] of this.pending) {
          clearTimeout(entry.timer);
          entry.resolve({ ok: false, error: errMsg });
        }
        this.pending.clear();
        if (!wasReady) {
          resolve({ ok: false, error: errMsg });
        }
      });
    });

    return this.startingPromise;
  }

  async request(action, params = {}, timeoutMs = 20000) {
    const startRes = await this.ensureStarted(Boolean(params && params.restartBridge));
    if (!startRes.ok || !this.child) {
      return {
        ok: false,
        setupRequired: true,
        error: startRes.error || this.lastError || 'MemPalace bridge is unavailable.',
        pythonBin: this.pythonBin,
      };
    }

    const id = this.reqSeq++;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({
          ok: false,
          error: `Request '${action}' timed out after ${timeoutMs}ms`,
        });
      }, timeoutMs);

      this.pending.set(id, { resolve, timer });
      try {
        this.child.stdin.write(JSON.stringify({ id, action, params }) + '\n');
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ ok: false, error: err.message });
      }
    });
  }
}

const bridge = new PalaceBridge();
const app = new SidecarApp();

app.page('/', () => fs.readFileSync(indexHtmlPath, 'utf-8'));

app.api(
  '/styles.css',
  () =>
    new Response(fs.readFileSync(stylesCssPath, 'utf-8'), {
      status: 200,
      headers: {
        'Content-Type': 'text/css; charset=utf-8',
        'Cache-Control': 'no-store',
      },
    }),
  'GET'
);

app.api(
  '/app.js',
  () =>
    new Response(fs.readFileSync(appJsPath, 'utf-8'), {
      status: 200,
      headers: {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Cache-Control': 'no-store',
      },
    }),
  'GET'
);

app.api(
  '/logo.svg',
  () =>
    new Response(logoSvg, {
      status: 200,
      headers: { 'Content-Type': 'image/svg+xml; charset=utf-8' },
    }),
  'GET'
);

function registerApi(route, handler) {
  app.api(route, handler, 'POST');
  app.api(route, handler, 'GET');
}

registerApi('/api/overview', async (data = {}) => {
  return await bridge.request('overview', {
    workspace_path: data.workspacePath || data.workspace_path || '',
    force_refresh: Boolean(
      data.forceRefresh ||
        data.force_refresh ||
        data.reconnect === '1' ||
        data.reconnect === 'true' ||
        data.reconnect === true
    ),
    reconnect: Boolean(
      data.reconnect === '1' || data.reconnect === 'true' || data.reconnect === true
    ),
    restartBridge: Boolean(
      data.restartBridge ||
        data.restart === '1' ||
        data.restart === 'true' ||
        data.restart === true
    ),
  });
});

registerApi('/api/search', async (data = {}) => {
  const strat = data.strategy || data.candidate_strategy || 'union';
  return await bridge.request('search', {
    query: data.query || '',
    wing: data.wing || '',
    room: data.room || '',
    limit: Number(data.limit) || 25,
    strategy: strat,
    candidate_strategy: strat,
  });
});

registerApi('/api/drawers', async (data = {}) => {
  return await bridge.request('drawers', {
    wing: data.wing || '',
    room: data.room || '',
    since: data.since || '',
    before: data.before || '',
    sort: data.sort || 'recent',
    limit: Number(data.limit) || 30,
    offset: Number(data.offset) || 0,
  });
});

registerApi('/api/drawer', async (data = {}) => {
  return await bridge.request('drawer', {
    drawer_id: data.drawer_id || '',
  });
});

registerApi('/api/tunnels', async (data = {}) => {
  return await bridge.request('tunnels', {
    wing: data.wing || '',
    wing_a: data.wing_a || '',
    wing_b: data.wing_b || '',
    start_room: data.start_room || '',
    max_hops: Number(data.max_hops) || 2,
    limit: Number(data.limit) || 60,
  });
});

registerApi('/api/kg', async (data = {}) => {
  return await bridge.request('kg', {
    entity: data.entity || '',
    as_of: data.as_of || '',
    direction: data.direction || 'both',
    limit: Number(data.limit) || 80,
    offset: Number(data.offset) || 0,
  });
});

registerApi('/api/events', async (data = {}) => {
  return await bridge.request('events', {
    stream: data.stream || '',
    room: data.room || '',
    topic: data.topic || '',
    type: data.type || '',
    to_agent: data.to_agent || '',
    writer: data.writer || '',
    correlation_id: data.correlation_id || '',
    status: data.status || '',
    since_event_id: data.since_event_id || '',
    limit: Number(data.limit) || 50,
    preview: Boolean(data.preview === '1' || data.preview === 'true' || data.preview === true),
  });
});

registerApi('/api/artifact', async (data = {}) => {
  return await bridge.request('artifact', {
    sha256: data.sha256 || '',
  });
});

registerApi('/api/diary', async (data = {}) => {
  return await bridge.request('diary', {
    agent_name: data.agent_name || '',
    wing: data.wing || '',
    last_n: Number(data.last_n) || 25,
  });
});

// Warm the Python bridge in the background on startup
bridge.ensureStarted().catch(() => {});

app.run();
