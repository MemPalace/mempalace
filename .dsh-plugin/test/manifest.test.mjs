import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
// A Windows checkout may carry CRLF line endings; the assertions read LF.
const patch = readFileSync(path.join(root, 'cordis.patch.yml'), 'utf8').replace(/\r\n/g, '\n')
const rows = [...patch.matchAll(/- id: (\S+)\n\s+name: '([^']+)'/g)].map(([, id, name]) => ({ id, name }))

test('the bundle manifest points at its patch layer', () => {
  assert.equal(pkg.dsh.bundle.patch, './cordis.patch.yml')
  assert.ok(pkg.files.includes('cordis.patch.yml'))
})

test('the patch inserts exactly the three mempalace rows', () => {
  assert.deepEqual(
    rows.map((row) => row.id),
    ['mempalace-recall', 'mempalace-autosave', 'mempalace-mcp'],
  )
})

test("every row naming this package resolves through the package's own exports", async () => {
  for (const { id, name } of rows.filter((row) => row.name.startsWith(`${pkg.name}/`))) {
    const subpath = `./${name.slice(pkg.name.length + 1)}`
    const target = pkg.exports[subpath]
    assert.ok(target, `${id}: package.json exports ${subpath}`)

    const plugin = await import(pathToFileURL(path.join(root, target)).href)
    assert.equal(typeof plugin.apply, 'function', `${id} exports apply`)
    assert.equal(typeof plugin.name, 'string', `${id} exports name`)
    assert.ok(Array.isArray(plugin.inject) && plugin.inject.includes('subprocess'), `${id} injects subprocess`)
  }
})

test('the MCP row serves the palace tools under the name the recall prompt teaches', async () => {
  assert.match(patch, /name: '@deepseek-ai\/dsh-mcp-client'\n\s+config:\n\s+serverName: mempalace\n\s+transport: stdio\n\s+command: mempalace-light-mcp\n/)
  const { DEFAULT_SEARCH_TOOL } = await import(pathToFileURL(path.join(root, 'lib/palace.js')).href)
  assert.equal(DEFAULT_SEARCH_TOOL, 'mcp__mempalace__palace_query')
})
