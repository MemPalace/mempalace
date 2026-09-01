import type { PluginInput } from '@opencode-ai/plugin';
import { wakeUp, mine, mineSync, isInitialized, initialize } from './mempalace-cli.js';
import { StateManager } from './state.js';
import { getWingFromPath, isEmptyWorkspace } from './utils.js';

export default async function mempalacePlugin(
  input: PluginInput,
  options?: { threshold?: number },
): Promise<any> {
  const dir = input.worktree || input.directory || process.cwd();
  const wing = getWingFromPath(dir);
  const threshold = options?.threshold || 15;
  const stateManager = new StateManager(threshold);

  let initializationDone = false;
  let isInitializing = false;

  const ensureInitialized = async (): Promise<'ready' | 'initializing' | 'empty'> => {
    if (await isEmptyWorkspace(dir)) {
      return 'empty';
    }

    if (initializationDone) return 'ready';
    if (isInitializing) return 'initializing';
    isInitializing = true;

    const initialized = await isInitialized(dir);
    if (initialized) {
      initializationDone = true;
      isInitializing = false;
      return 'ready';
    }

    initialize(dir)
      .then(() => {
        initializationDone = true;
      })
      .catch((e: any) => {
        console.warn('[MemPalace] Background initialization failed:', e.message);
      })
      .finally(() => {
        isInitializing = false;
      });

    return 'initializing';
  };

  const MAX_MEMORY_LENGTH = 4000;
  const CACHE_TTL = 60_000; // 1 minute
  let cachedMemory: string | null = null;
  let cacheTime = 0;

  async function getCachedMemory(targetWing: string): Promise<string | null> {
    if (cachedMemory && Date.now() - cacheTime < CACHE_TTL) {
      return cachedMemory;
    }
    cachedMemory = await wakeUp(targetWing);
    cacheTime = Date.now();
    return cachedMemory;
  }

  function invalidateCache() {
    cachedMemory = null;
    cacheTime = 0;
  }

  async function injectMemory(outputArray: string[], targetWing: string): Promise<void> {
    const state = await ensureInitialized();
    if (state === 'empty') {
      outputArray.push(
        '[MemPalace]: This environment has no memory yet. Please proceed with standard logic.',
      );
      return;
    }
    if (state === 'initializing') {
      outputArray.push(
        '[MemPalace]: The memory system is being built asynchronously in the background. The current response will not include historical memory context.',
      );
      return;
    }

    const memory = await getCachedMemory(targetWing);
    if (memory) {
      const truncatedMemory =
        memory.length > MAX_MEMORY_LENGTH
          ? memory.substring(0, MAX_MEMORY_LENGTH) + '\n...[Memory Truncated]'
          : memory;
      outputArray.push(truncatedMemory);
    }
  }

  let isFlushing = false;
  const flushDirtySessions = () => {
    if (isFlushing) return;
    isFlushing = true;
    const dirtySessions = stateManager.getDirtySessions();
    if (dirtySessions.length > 0) {
      mineSync(dir, 'convos', wing);
      for (const id of dirtySessions) {
        stateManager.resetCount(id);
      }
    }
    isFlushing = false;
  };

  const exitHandler = () => flushDirtySessions();
  const sigHandler = () => flushDirtySessions();

  process.on('exit', exitHandler);
  process.on('SIGINT', sigHandler);
  process.on('SIGTERM', sigHandler);

  return {
    event: async ({ event }: { event: any }) => {
      const sessionID = event.properties?.sessionID || event.properties?.info?.id;
      if (event.type === 'session.deleted' && sessionID) {
        stateManager.removeSession(sessionID);
      }

      if (
        event.type === 'session.idle' ||
        event.type === 'session.deleted' ||
        (event.type === 'session.status' && event.properties?.status?.type === 'idle')
      ) {
        if (sessionID && stateManager.hasPendingMessages(sessionID)) {
          if (!stateManager.acquireMiningLock(sessionID)) return;

          const state = await ensureInitialized();
          if (state !== 'ready') {
            stateManager.releaseMiningLock(sessionID);
            return;
          }

          setTimeout(() => {
            mine(dir, 'convos', wing)
              .then(invalidateCache)
              .catch((e: any) => console.warn('[MemPalace] idle mine failed:', e.message))
              .finally(() => {
                stateManager.releaseMiningLock(sessionID);
                stateManager.resetCount(sessionID);
              });
          }, 2000);
        }
      }
    },

    'experimental.session.compacting': async (
      _: any,
      output: { context: string[]; prompt?: string },
    ) => {
      await injectMemory(output.context, wing);
    },

    'experimental.chat.system.transform': async (_: any, output: { system: string[] }) => {
      await injectMemory(output.system, wing);
    },

    'chat.message': async ({ sessionID }: { sessionID: string }) => {
      if (stateManager.incrementAndCheck(sessionID)) {
        if (!stateManager.acquireMiningLock(sessionID)) return;

        const state = await ensureInitialized();
        if (state !== 'ready') {
          stateManager.releaseMiningLock(sessionID);
          return;
        }

        setTimeout(() => {
          mine(dir, 'convos', wing)
            .then(invalidateCache)
            .catch((e: any) => console.warn('[MemPalace] auto-mine failed:', e.message))
            .finally(() => {
              stateManager.releaseMiningLock(sessionID);
            });
        }, 2000);
      }
    },
  };
}
