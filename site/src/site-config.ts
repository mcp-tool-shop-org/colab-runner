import type { SiteConfig } from '@mcptoolshop/site-theme';

export const config: SiteConfig = {
  title: 'Colab Runner',
  description:
    'The studio’s shared runner for paid Google Colab windows: a detached job, a spending cap, token scrubbing, and a session that always gets stopped.',
  logoBadge: 'CR',
  brandName: 'colab-runner',
  repoUrl: 'https://github.com/mcp-tool-shop-org/colab-runner',
  footerText:
    'MIT Licensed — built by <a href="https://mcp-tool-shop.github.io/" style="color:var(--color-muted);text-decoration:underline">MCP Tool Shop</a>',

  hero: {
    badge: '0.1.0 · Python 3.11+',
    headline: 'Colab Runner',
    headlineAccent: 'paid windows that always stop.',
    description:
      'Start a job on a Colab VM that outlives the rig, watch it inside a spending cap, scrub every token from what comes back, and stop the session on every exit. One shared runner instead of a copy in every lane.',
    primaryCta: { href: 'handbook/getting-started/', label: 'Get started' },
    secondaryCta: { href: 'handbook/', label: 'Read the Handbook' },
    previews: [
      { label: 'Install', code: 'pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"' },
      {
        label: 'Run',
        code: 'python -m colab_runner run --session S --out-dir D --script /content/job.sh --wall-s 3600 --max-rate R --cap C --cleanup-bound-s 600',
      },
      { label: 'Recover', code: 'python -m colab_runner stop --out-dir D' },
    ],
  },

  sections: [
    {
      kind: 'features',
      id: 'features',
      title: 'What it does',
      subtitle: 'The code every Colab lane used to carry its own copy of.',
      features: [
        {
          title: 'A detached job',
          desc: 'The job runs on the VM in its own process, with a status file and a log, so a lost rig-side process never loses the work.',
        },
        {
          title: 'A spending cap',
          desc: 'The window is priced from the card’s rate, and the run ends at the cap with room reserved for its own cleanup.',
        },
        {
          title: 'Stall guards',
          desc: 'A setup that stops growing, progress that stops moving, a dead job or a poll blackout each end the run instead of billing on.',
        },
        {
          title: 'A stop on every exit',
          desc: 'Once it owns the session it stops it, and a marker stays until the stop is confirmed, so `stop` can always finish the job.',
        },
        {
          title: 'Token scrubbing',
          desc: 'Every line and downloaded text file is scrubbed. A binary holding a token is quarantined unchanged and the download is refused.',
        },
        {
          title: 'Structured refusals',
          desc: 'Every refusal and error has a code, a message and a hint, and the exit codes follow the org convention.',
        },
      ],
    },
    {
      kind: 'data-table',
      id: 'exits',
      title: 'Exit codes',
      subtitle: 'For `run` and `stop`.',
      columns: ['Exit', 'Meaning'],
      rows: [
        ['0', 'Done, and the session’s stop confirmed'],
        ['1', 'A refusal or a usage error, before the session marker was written'],
        ['2', 'Any other end, or an error; stop the session if its marker is still there'],
        ['3', 'Done, but the stop was not confirmed: run `stop --out-dir D`'],
      ],
    },
    {
      kind: 'code-cards',
      id: 'usage',
      title: 'Usage',
      cards: [
        {
          title: 'A window',
          code: 'colab new -s my-window\npython -m colab_runner run --session my-window --out-dir runs/w1 \\\n  --script /content/train.sh --wall-s 3600 \\\n  --max-rate 13.5 --cap 15 --cleanup-bound-s 600',
        },
        {
          title: 'After a lost rig',
          code: '# session_open.json is still in the out dir\npython -m colab_runner stop --out-dir runs/w1',
        },
      ],
    },
  ],
};
