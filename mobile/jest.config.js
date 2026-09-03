// Jest configuration for the Expo SDK 57 app.
//
// The jest-expo preset supplies almost everything: the babel transform (via
// expo's internal babel preset, so no babel.config.js is required), React
// Native module resolution, asset transformers, transformIgnorePatterns, and
// the "@/" path aliases read from tsconfig.json. Only add what it cannot infer.

/** @type {import('jest').Config} */
module.exports = {
  preset: 'jest-expo',

  // The preset transforms JS/TS and binary assets, but not stylesheets, so a
  // module importing CSS (e.g. src/constants/theme.ts imports '@/global.css')
  // would fail to parse. Styles carry no behaviour under Jest; resolve them to
  // an empty module. Merged with — not replacing — the preset's own mappings.
  moduleNameMapper: {
    '\\.css$': '<rootDir>/jest/style-stub.js',
  },

  testMatch: [
    '**/__tests__/**/*.test.@(ts|tsx|js|jsx)',
    '**/*.test.@(ts|tsx|js|jsx)',
  ],

  testPathIgnorePatterns: ['/node_modules/', '/.expo/', '/dist/', '/web-build/'],
};
