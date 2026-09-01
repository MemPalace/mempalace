import fs from 'fs/promises';
import path from 'path';

export function getWingFromPath(workspacePath: string): string {
  if (!workspacePath || workspacePath === '/') {
    return 'wing_general';
  }

  const baseName = path.basename(workspacePath);
  const sanitized = baseName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
  return `wing_${sanitized}`;
}

export async function isEmptyWorkspace(dir: string): Promise<boolean> {
  try {
    const files = await fs.readdir(dir);
    const ignored = new Set([
      '.git',
      '.mempalace',
      '.opencode',
      '.DS_Store',
      'node_modules',
      '.cursor',
      '.vscode',
      '.idea',
    ]);
    const meaningfulFiles = files.filter((f) => !ignored.has(f));
    return meaningfulFiles.length === 0;
  } catch (error) {
    return true;
  }
}
