import assert from 'node:assert/strict'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, test } from 'node:test'

import { projectWing, yamlWing } from '../lib/palace.js'

let root
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'mempalace-dsh-'))
})
afterEach(async () => {
  await rm(root, { recursive: true, force: true })
})

test('yamlWing reads quoted and plain scalars the way YAML does', () => {
  assert.equal(yamlWing('wing: "my # project"\n'), 'my # project')
  assert.equal(yamlWing("wing: 'research''s' # the lab\n"), "research's")
  assert.equal(yamlWing('wing: "say \\"hi\\""\n'), 'say "hi"')
  assert.equal(yamlWing('wing: plain_name # a comment\n'), 'plain_name')
  assert.equal(yamlWing('wing: plain#not-a-comment\n'), 'plain#not-a-comment')
  assert.equal(yamlWing('rooms: []\r\nwing: crlf_wing\r\n'), 'crlf_wing')
})

test('yamlWing tells a missing wing apart from one it cannot read exactly', () => {
  assert.equal(yamlWing('rooms:\n  - wing: nested\n'), undefined)
  assert.equal(yamlWing('wing:\n'), undefined)
  assert.equal(yamlWing("wing: ''\n"), undefined)
  assert.equal(yamlWing('wing: "left open\n'), null)
  assert.equal(yamlWing('wing: "unknown \\q escape"\n'), null)
  assert.equal(yamlWing("wing: 'closed' then text\n"), null)
  assert.equal(yamlWing('wing: &anchor name\n'), null)
})

test('an unreadable configured wing wakes the whole palace instead of guessing one', async () => {
  const dir = path.join(root, 'Guessable-Name')
  await mkdir(dir)
  await writeFile(path.join(dir, 'mempalace.yaml'), 'wing: "left open\n')
  assert.equal(await projectWing(dir), undefined)
})

test('only a regular file is read as the project config', async () => {
  const skipped = path.join(root, 'skipped')
  await mkdir(path.join(skipped, 'mempalace.yaml'), { recursive: true })
  await writeFile(path.join(skipped, 'mempal.yaml'), 'wing: legacy_wing\n')
  assert.equal(await projectWing(skipped), 'legacy_wing', 'a non-file mempalace.yaml is passed over')

  const bare = path.join(root, 'Bare-Dir')
  await mkdir(path.join(bare, 'mempalace.yaml'), { recursive: true })
  assert.equal(await projectWing(bare), 'bare_dir')
})
