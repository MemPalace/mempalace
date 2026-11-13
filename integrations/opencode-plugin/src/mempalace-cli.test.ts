import { wakeUp, mine, isInitialized, initialize } from './mempalace-cli.js';
import execa from 'execa';

jest.mock('execa');

describe('mempalace-cli', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('calls wake-up with the correct wing', async () => {
    (execa as unknown as jest.Mock).mockResolvedValue({
      stdout: 'L0 context\nL1 context',
    });
    const result = await wakeUp('test_wing');
    expect(execa).toHaveBeenCalledWith('mempalace', ['wake-up', '--wing', 'test_wing'], {
      timeout: 10000,
    });
    expect(result).toBe('L0 context\nL1 context');
  });

  it('calls mine with the correct arguments', async () => {
    (execa as unknown as jest.Mock).mockResolvedValue({
      stdout: 'mined successfully',
    });
    await mine('/test/dir', 'convos', 'test_wing');
    expect(execa).toHaveBeenCalledWith(
      'mempalace',
      ['mine', '/test/dir', '--mode', 'convos', '--wing', 'test_wing'],
      { timeout: 60000 },
    );
  });

  it('handles missing mempalace gracefully', async () => {
    (execa as unknown as jest.Mock).mockRejectedValue(
      new Error("ENOENT: no such file or directory, exec 'mempalace'"),
    );
    const result = await wakeUp('test_wing');
    expect(result).toBeNull();
  });

  it('checks if palace is initialized', async () => {
    (execa as unknown as jest.Mock).mockResolvedValue({ stdout: 'Palace OK' });
    const result = await isInitialized('/test/dir');
    expect(execa).toHaveBeenCalledWith(
      'mempalace',
      ['status', '--palace', '/test/dir/.mempalace/palace'],
      { timeout: 5000 },
    );
    expect(result).toBe(true);
  });

  it('initializes non-interactively with newline input', async () => {
    (execa as unknown as jest.Mock).mockResolvedValue({ stdout: 'Init OK' });
    await initialize('/test/dir');
    expect(execa).toHaveBeenCalledWith('mempalace', ['init', '--yes', '/test/dir'], {
      input: '\n',
      timeout: 30000,
    });
  });
});
