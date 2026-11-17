import type { SiteConfig } from '@mcptoolshop/site-theme';

export const config: SiteConfig = {
  title: 'my-package',
  description: 'A tool by mcp-tool-shop',
  logoBadge: 'MP',
  brandName: 'my-package',
  repoUrl: 'https://github.com/mcp-tool-shop-org/colab-runner',
  npmUrl: 'https://www.npmjs.com/package/my-package',
  footerText: 'MIT Licensed — built by <a href="https://github.com/mcp-tool-shop-org" style="color:var(--color-muted);text-decoration:underline">mcp-tool-shop-org</a>',

  hero: {
    badge: 'Open source',
    headline: 'my-package',
    headlineAccent: 'by mcp-tool-shop.',
    description: 'A tool by mcp-tool-shop',
    primaryCta: { href: '#usage', label: 'Get started' },
    secondaryCta: { href: 'handbook/', label: 'Read the Handbook' },
    previews: [
      { label: 'Install', code: 'npm install my-package' },
      { label: 'Import', code: "import { ... } from 'my-package'" },
      { label: 'Use', code: '// replace with a compelling one-liner' },
    ],
  },

  sections: [
    {
      kind: 'features',
      id: 'features',
      title: 'Features',
      subtitle: 'What makes my-package useful.',
      features: [
        { title: 'Fast', desc: 'Describe what makes my-package fast or efficient.' },
        { title: 'Typed', desc: 'Describe the type safety or schema guarantees.' },
        { title: 'Tested', desc: 'Describe the test coverage or reliability story.' },
      ],
    },
    {
      kind: 'code-cards',
      id: 'usage',
      title: 'Usage',
      cards: [
        { title: 'Install', code: 'npm install my-package' },
        { title: 'Basic usage', code: "import { ... } from 'my-package';\n\n// replace with real usage" },
      ],
    },
  ],
};
