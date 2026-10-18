// Copies the 3D structure viewers (NGL, Mol*) from node_modules into the Python package, where
// the server serves them to the structure views (jupyterlab_verknode/vendor, not in git).
import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'jupyterlab_verknode', 'vendor');
const files = {
  'ngl/ngl.js': 'node_modules/ngl/dist/ngl.js',
  'ngl/LICENSE': 'node_modules/ngl/LICENSE',
  'molstar/molstar.js': 'node_modules/molstar/build/viewer/molstar.js',
  'molstar/molstar.css': 'node_modules/molstar/build/viewer/molstar.css',
  'molstar/LICENSE': 'node_modules/molstar/LICENSE'
};
for (const [to, from] of Object.entries(files)) {
  mkdirSync(dirname(join(out, to)), { recursive: true });
  copyFileSync(join(root, from), join(out, to));
}
console.log(`vendor: ${Object.keys(files).length} files in ${out}`);
