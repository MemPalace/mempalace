import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

function normalizeBase(base?: string): string {
  if (!base || base === '/') {
    return '/'
  }

  return base.endsWith('/') ? base : `${base}/`
}

const docsBase = normalizeBase(process.env.DOCS_BASE || '/')
const editBranch = process.env.DOCS_EDIT_BRANCH || 'main'
const gaId = process.env.MEMPALACE_DOCS_GA_ID

export default withMermaid(
  defineConfig({
    title: 'MemPalace',
    description: 'Give your AI a memory. Local-first storage and retrieval for AI workflows, with benchmark results and MCP tooling.',
    base: docsBase,

    head: [
      ['link', { rel: 'icon', href: `${docsBase}mempalace_logo.png` }],
      ['link', { rel: 'preconnect', href: 'https://api.fontshare.com' }],
      ['link', { href: 'https://api.fontshare.com/v2/css?f[]=neue-machina@300,400,500,700,800&f[]=satoshi@300,400,500,700&display=swap', rel: 'stylesheet' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.googleapis.com' }],
      ['link', { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' }],
      ['link', { href: 'https://fonts.googleapis.com/css2?family=Onest:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap', rel: 'stylesheet' }],
      ['meta', { property: 'og:title', content: 'MemPalace — AI Memory System' }],
      ['meta', { property: 'og:description', content: '96.6% LongMemEval recall. Zero API calls. Local, free, open source.' }],
      ['meta', { property: 'og:image', content: `${docsBase}mempalace_logo.png` }],
      ...(gaId ? [
        ['script', { async: '', src: `https://www.googletagmanager.com/gtag/js?id=${gaId}` }],
        ['script', {}, `window.dataLayer = window.dataLayer || [];\nfunction gtag(){dataLayer.push(arguments);}\ngtag('js', new Date());\ngtag('config', '${gaId}');`],
      ] as const : []),
    ],

    themeConfig: {
      logo: '/mempalace_logo.png',
      siteTitle: 'MemPalace',

      nav: [
        { text: 'Guide', link: '/guide/getting-started' },
        { text: 'Concepts', link: '/concepts/the-palace' },
        { text: 'Reference', link: '/reference/cli' },
      ],

      sidebar: {
        '/guide/': [
          {
            text: 'Guide',
            items: [
              { text: 'Getting Started', link: '/guide/getting-started' },
              { text: 'Mining Your Data', link: '/guide/mining' },
              { text: 'Searching Memories', link: '/guide/searching' },
              { text: 'MCP Integration', link: '/guide/mcp-integration' },
              { text: 'Claude Code Plugin', link: '/guide/claude-code' },
              { text: 'Gemini CLI', link: '/guide/gemini-cli' },
              { text: 'OpenClaw Skill', link: '/guide/openclaw' },
              { text: 'Local Models', link: '/guide/local-models' },
              { text: 'Auto-Save Hooks', link: '/guide/hooks' },
              { text: 'Configuration', link: '/guide/configuration' },
            ],
          },
        ],
        '/concepts/': [
          {
            text: 'Concepts',
            items: [
              { text: 'The Palace', link: '/concepts/the-palace' },
              { text: 'Memory Stack', link: '/concepts/memory-stack' },
              { text: 'AAAK Dialect', link: '/concepts/aaak-dialect' },
              { text: 'Knowledge Graph', link: '/concepts/knowledge-graph' },
              { text: 'Specialist Agents', link: '/concepts/agents' },
              { text: 'Contradiction Detection', link: '/concepts/contradiction-detection' },
            ],
          },
        ],
        '/reference/': [
          {
            text: 'Reference',
            items: [
              { text: 'CLI Commands', link: '/reference/cli' },
              { text: 'MCP Tools', link: '/reference/mcp-tools' },
              { text: 'Python API', link: '/reference/python-api' },
              { text: 'API Reference', link: '/reference/api-reference' },
              { text: 'Module Map', link: '/reference/modules' },
              { text: 'Benchmarks', link: '/reference/benchmarks' },
              { text: 'Contributing', link: '/reference/contributing' },
            ],
          },
        ],
      },

      socialLinks: [
        { icon: 'github', link: 'https://github.com/MemPalace/mempalace' },
        { icon: 'discord', link: 'https://discord.com/invite/ycTQQCu6kn' },
      ],

      search: {
        provider: 'local',
      },

      footer: {
        message: 'Released under the MIT License.',
        copyright: 'Copyright © 2026 MemPalace contributors',
      },

      editLink: {
        pattern: `https://github.com/MemPalace/mempalace/edit/${editBranch}/website/:path`,
        text: 'Edit this page on GitHub',
      },
    },

    mermaid: {
      theme: 'dark',
    },
  })
)
