import plugin from './index.js';
import * as cli from './mempalace-cli.js';
import * as utils from './utils.js';

jest.mock('./mempalace-cli.js');
jest.mock('./utils.js', () => ({
  getWingFromPath: jest.fn().mockReturnValue('wing_project'),
  isEmptyWorkspace: jest.fn().mockReturnValue(false),
}));

describe('opencode-plugin-mempalace', () => {
  let mockInput: any;

  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    mockInput = {
      directory: '/Users/test/project',
      worktree: '/Users/test/project',
      $: jest.fn(),
    };
    (cli.isInitialized as jest.Mock).mockResolvedValue(true);
    (utils.isEmptyWorkspace as jest.Mock).mockReturnValue(false);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('registers the expected hooks', async () => {
    const hooks = await plugin(mockInput);
    expect(hooks.event).toBeDefined();
    expect(hooks['experimental.session.compacting']).toBeDefined();
    expect(hooks['experimental.chat.system.transform']).toBeDefined();
    expect(hooks['chat.message']).toBeDefined();
  });

  it('initializes asynchronously if not initialized and returns initializing status', async () => {
    (cli.isInitialized as jest.Mock).mockResolvedValue(false);
    (cli.initialize as jest.Mock).mockResolvedValue(undefined);

    const hooks = await plugin(mockInput);
    const output: { system: string[] } = { system: [] };

    if (hooks['experimental.chat.system.transform']) {
      await hooks['experimental.chat.system.transform'](
        { sessionID: 'sess-1', model: {} as any },
        output,
      );
    }

    expect(cli.initialize).toHaveBeenCalledWith('/Users/test/project');
    expect(cli.wakeUp).not.toHaveBeenCalled();
    expect(output.system).toContain(
      '[MemPalace]: The memory system is being built asynchronously in the background. The current response will not include historical memory context.',
    );
  });

  it('returns empty workspace status if project is empty', async () => {
    (utils.isEmptyWorkspace as jest.Mock).mockReturnValue(true);

    const hooks = await plugin(mockInput);
    const output: { system: string[] } = { system: [] };

    if (hooks['experimental.chat.system.transform']) {
      await hooks['experimental.chat.system.transform'](
        { sessionID: 'sess-1', model: {} as any },
        output,
      );
    }

    expect(cli.initialize).not.toHaveBeenCalled();
    expect(cli.wakeUp).not.toHaveBeenCalled();
    expect(output.system).toContain(
      '[MemPalace]: This environment has no memory yet. Please proceed with standard logic.',
    );
  });

  it('adds wake-up context on experimental.session.compacting', async () => {
    (cli.wakeUp as jest.Mock).mockResolvedValue('MOCKED_WAKEUP_DATA');

    const hooks = await plugin(mockInput);
    const output: { context: string[] } = { context: [] };

    if (hooks['experimental.session.compacting']) {
      await hooks['experimental.session.compacting']({ sessionID: 'sess-1' }, output);
    }

    expect(cli.wakeUp).toHaveBeenCalledWith('wing_project');
    expect(output.context).toContain('MOCKED_WAKEUP_DATA');
  });

  it('adds wake-up context on experimental.chat.system.transform', async () => {
    (cli.wakeUp as jest.Mock).mockResolvedValue('MOCKED_WAKEUP_DATA');

    const hooks = await plugin(mockInput);
    const output: { system: string[] } = { system: [] };

    if (hooks['experimental.chat.system.transform']) {
      await hooks['experimental.chat.system.transform'](
        { sessionID: 'sess-1', model: {} as any },
        output,
      );
    }

    expect(cli.wakeUp).toHaveBeenCalledWith('wing_project');
    expect(output.system).toContain('MOCKED_WAKEUP_DATA');
  });

  it('triggers mine after reaching the message threshold', async () => {
    (cli.mine as jest.Mock).mockResolvedValue(undefined);

    const hooks = await plugin(mockInput, { threshold: 2 });

    if (hooks['chat.message']) {
      await hooks['chat.message']({ sessionID: 'sess-1' }, { message: {} as any, parts: [] });
      expect(cli.mine).not.toHaveBeenCalled();

      await hooks['chat.message']({ sessionID: 'sess-1' }, { message: {} as any, parts: [] });
      jest.advanceTimersByTime(2000);
      expect(cli.mine).toHaveBeenCalledWith('/Users/test/project', 'convos', 'wing_project');
    }
  });

  it('triggers mine on session idle when there are pending messages', async () => {
    (cli.mine as jest.Mock).mockResolvedValue(undefined);

    const hooks = await plugin(mockInput, { threshold: 15 });

    if (hooks['chat.message'] && hooks.event) {
      await hooks['chat.message']({ sessionID: 'sess-idle' }, { message: {} as any, parts: [] });
      expect(cli.mine).not.toHaveBeenCalled();

      await hooks.event({
        event: {
          type: 'session.idle',
          properties: { sessionID: 'sess-idle' },
        },
      });
      jest.advanceTimersByTime(2000);
      expect(cli.mine).toHaveBeenCalledWith('/Users/test/project', 'convos', 'wing_project');
    }
  });

  it('does not trigger mine on session idle when no pending messages', async () => {
    (cli.mine as jest.Mock).mockResolvedValue(undefined);

    const hooks = await plugin(mockInput, { threshold: 15 });

    if (hooks.event) {
      await hooks.event({
        event: {
          type: 'session.idle',
          properties: { sessionID: 'sess-clean' },
        },
      });
      expect(cli.mine).not.toHaveBeenCalled();
    }
  });
});
