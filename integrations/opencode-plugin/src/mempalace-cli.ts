import execa from 'execa';
import path from 'path';

const COMMANDS = [
  { cmd: 'mempalace', prefix: [] as string[] },
  { cmd: 'python3', prefix: ['-m', 'mempalace'] },
  { cmd: 'python', prefix: ['-m', 'mempalace'] },
] as const;

const TIMEOUTS = {
  status: 5_000,
  wakeUp: 10_000,
  init: 30_000,
  mine: 60_000,
} as const;

async function executeMempalace(args: string[], timeoutMs: number): Promise<any> {
  const options = { timeout: timeoutMs };
  let lastError;

  for (const { cmd, prefix } of COMMANDS) {
    try {
      return await execa(cmd, [...prefix, ...args], options);
    } catch (error: any) {
      lastError = error;
      if (error.timedOut) break;
    }
  }
  throw lastError;
}

export async function isInitialized(dir: string): Promise<boolean> {
  try {
    const palacePath = path.join(dir, '.mempalace', 'palace');
    await executeMempalace(['status', '--palace', palacePath], TIMEOUTS.status);
    return true;
  } catch (error) {
    return false;
  }
}

export async function initialize(dir: string): Promise<void> {
  try {
    const options = { timeout: TIMEOUTS.init, input: '\n' };
    let lastError;
    for (const { cmd, prefix } of COMMANDS) {
      try {
        await execa(cmd, [...prefix, 'init', '--yes', dir], options);
        return;
      } catch (error: any) {
        lastError = error;
        if (error.timedOut) break;
      }
    }
    throw lastError;
  } catch (error: any) {
    console.warn(`[MemPalace] Failed to initialize in ${dir}:`, error.message);
  }
}

export async function wakeUp(wing: string): Promise<string | null> {
  try {
    const { stdout } = await executeMempalace(['wake-up', '--wing', wing], TIMEOUTS.wakeUp);
    return stdout;
  } catch (error: any) {
    console.warn(`[MemPalace] Failed to wake up:`, error.message);
    return null;
  }
}

export async function mine(dir: string, mode: string, wing: string): Promise<void> {
  await executeMempalace(['mine', dir, '--mode', mode, '--wing', wing], TIMEOUTS.mine);
}

export function mineSync(dir: string, mode: string, wing: string): void {
  const options = { timeout: TIMEOUTS.mine };

  for (const { cmd, prefix } of COMMANDS) {
    try {
      execa.sync(cmd, [...prefix, 'mine', dir, '--mode', mode, '--wing', wing], options);
      return;
    } catch (error: any) {
      if (error.timedOut) break;
      console.error(`[MemPalace] Emergency save failed for ${cmd}:`, error.message);
    }
  }
}
