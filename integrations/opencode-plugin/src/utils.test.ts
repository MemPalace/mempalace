import { getWingFromPath } from './utils.js';

describe('getWingFromPath', () => {
  it('extracts wing from standard project path', () => {
    expect(getWingFromPath('/Users/name/projects/my-app')).toBe('wing_my-app');
  });

  it('returns wing_general for root path', () => {
    expect(getWingFromPath('/')).toBe('wing_general');
  });

  it('replaces spaces and special chars with hyphens', () => {
    expect(getWingFromPath('/projects/My Awesome App!')).toBe('wing_my-awesome-app');
  });
});
