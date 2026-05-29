import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      // ignoreRestSiblings: destructuring a prop out to keep it off the spread
      // (e.g. `node` in a ReactMarkdown renderer's `{ node, ...props }`) is a
      // deliberate exclusion, not an unused variable.
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]', ignoreRestSiblings: true }],
      // set-state-in-effect (new in react-hooks v7, a React-Compiler-era rule) is
      // too strict for the one legitimate case here: the mount effect hydrates
      // session state from localStorage, which is exactly what a mount effect is
      // for. Off rather than papered over with per-line disables.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
])
