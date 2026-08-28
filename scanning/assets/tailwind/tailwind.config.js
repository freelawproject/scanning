module.exports = {
  darkMode: 'media',
  safelist: [
    'alert-success',
    'alert-info',
    'alert-warning',
    'alert-danger',
    // Scan status badges (rendered via badge-{{ scan.status }})
    'badge-uploaded',
    'badge-queued',
    'badge-processing',
    'badge-awaiting',
    'badge-awaiting_validation',
    'badge-ready_for_page_completeness_review',
    'badge-page_completeness_review_done',
    'badge-pending_review',
    'badge-approved',
    'badge-extracted',
    'badge-error',
    'badge-error_max_retries',
    'badge-error_interrupted',
    'badge-cancelled',
    // Queue status badges (rendered via badge-{{ volume.queue_status }})
    'badge-needs_scanning',
    'badge-assigned',
    'badge-scanning',
    'badge-scanned',
    'badge-complete',
    'badge-unavailable',
    // Other dynamic badges
    'badge-s3',
    'badge-no_status',
    'badge-ok',
    'badge-gap',
    // Priority badges (rendered via badge-priority-{{ volume.priority }})
    'badge-priority-critical',
    'badge-priority-high',
    'badge-priority-medium',
    'badge-priority-low',
    'badge-priority-backlog',
  ],
  content: {
    relative: true,
    files: [
      /* Templates within the assets directory */
      '../templates/**/*.html',

      /* Templates in other django apps */
      '../../**/templates/**/*.html',

      /* JS files that could contain Tailwind CSS classes */
      '../static-global/js/**/*.js',

      /* App JS that builds markup at runtime (the process page viewer) */
      '../../static/**/*.js',
    ],
  },
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'monospace'],
      },
      colors: {
        primary: {
          50: '#F5F3FF',
          100: '#EDE9FE',
          200: '#DDD6FE',
          300: '#C4B5FD',
          400: '#A78BFA',
          500: '#7F56D9',
          600: '#6D28D9',
          700: '#5B21B6',
          800: '#4C1D95',
          900: '#180040',
        },
        gray: {
          25: '#FCFCFD',
          50: '#F9FAFB',
          100: '#F2F4F7',
          200: '#EAECF0',
          300: '#D0D5DD',
          400: '#98A2B3',
          500: '#667085',
          600: '#475467',
          700: '#344054',
          800: '#1D2939',
          900: '#101828',
        },
      },
      maxWidth: {
        content: '960px',
      },
    },
  },
  plugins: [],
};
