// Stubs out golem: and wasi: protocol imports so sbt test works in Node.js.
// The WASM runtime provides these in production; in tests we only call pure JS logic.

const stubs = {
  'wasi:logging/logging': `export function log(level, context, message) {}`,
  'wasi:cli/environment@0.2.3': `export function getEnvironment() { return []; }`,
};

const defaultStub = `export default {}`;

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('golem:') || specifier.startsWith('wasi:')) {
    return { shortCircuit: true, url: `data:text/javascript,stub:${encodeURIComponent(specifier)}` };
  }
  return nextResolve(specifier, context);
}

export async function load(url, context, nextLoad) {
  if (url.startsWith('data:text/javascript,stub:')) {
    const specifier = decodeURIComponent(url.replace('data:text/javascript,stub:', ''));
    const source = stubs[specifier] ?? defaultStub;
    return { shortCircuit: true, format: 'module', source };
  }
  return nextLoad(url, context);
}
