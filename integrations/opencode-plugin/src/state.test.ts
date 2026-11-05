import { StateManager } from './state.js';

describe('StateManager', () => {
  it('increments message count and triggers when threshold reached', () => {
    const state = new StateManager(3);
    expect(state.incrementAndCheck('session-1')).toBe(false);
    expect(state.incrementAndCheck('session-1')).toBe(false);
    expect(state.incrementAndCheck('session-1')).toBe(true);
    expect(state.incrementAndCheck('session-1')).toBe(false);
  });

  it('tracks pending messages correctly', () => {
    const state = new StateManager(3);
    expect(state.hasPendingMessages('session-1')).toBe(false);
    state.incrementAndCheck('session-1');
    expect(state.hasPendingMessages('session-1')).toBe(true);
    state.incrementAndCheck('session-1');
    expect(state.hasPendingMessages('session-1')).toBe(true);
    state.incrementAndCheck('session-1');
    expect(state.hasPendingMessages('session-1')).toBe(false);
  });

  it('resets count correctly', () => {
    const state = new StateManager(3);
    state.incrementAndCheck('session-1');
    expect(state.hasPendingMessages('session-1')).toBe(true);
    state.resetCount('session-1');
    expect(state.hasPendingMessages('session-1')).toBe(false);
  });

  it('returns dirty sessions ignoring locked ones', () => {
    const state = new StateManager(3);
    state.incrementAndCheck('session-1');
    state.incrementAndCheck('session-2');

    expect(state.getDirtySessions()).toEqual(['session-1', 'session-2']);

    state.acquireMiningLock('session-1');
    expect(state.getDirtySessions()).toEqual(['session-2']);

    state.releaseMiningLock('session-1');
    expect(state.getDirtySessions()).toEqual(['session-1', 'session-2']);
  });
});
