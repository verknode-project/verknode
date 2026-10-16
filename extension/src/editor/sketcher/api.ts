import { PageConfig, URLExt } from '@jupyterlab/coreutils';

import { postJSON, requestAPI } from '../../api';
import { StructureJSON } from './model';

export interface Structure extends StructureJSON {
  smiles: string;
  molfile: string;
  formula: string;
  svg: string | null;
  error?: string;
}

/** Structure of SMILES / MolBlock text (RDKit on the server); `clean` recomputes 2D coordinates. */
export function structure(text: string, clean = false, width = 200, height = 140): Promise<Structure> {
  return postJSON<Structure>('chem/structure', { text, clean, width, height });
}

let ketcher: Promise<boolean> | null = null;

/** Is the Ketcher editor installed on the server (see Dockerfile)? */
export function ketcherAvailable(): Promise<boolean> {
  ketcher ??= requestAPI<{ available: boolean }>('chem/ketcher')
    .then(r => r.available)
    .catch(() => false);
  return ketcher;
}

export const ketcherUrl = () => URLExt.join(PageConfig.getBaseUrl(), 'verknode', 'ketcher', 'index.html');

/** Is the molfile empty (no atoms)? */
export const emptyMolfile = (molfile: string) => {
  if (!molfile.trim()) {
    return true;
  }
  const v3000 = /M {2}V30 COUNTS (\d+)/.exec(molfile);
  if (v3000) {
    return v3000[1] === '0';
  }
  const counts = molfile.split('\n')[3] ?? '';
  return parseInt(counts.slice(0, 3), 10) === 0;
};
