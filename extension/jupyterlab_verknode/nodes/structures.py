"""3D structure views: a protein (PDB / mmCIF) and ligands (SDF / MOL / MOL2 / PDB files, or a
molecule column) shown with NGL or with Mol*, in the node view or as a canvas widget.

The structures are embedded in the page; the viewer libraries come from the server
(verknode/vendor, see scripts/vendor.mjs) or, if they are not there, from a CDN.
"""
from __future__ import annotations

import gzip
import json
import os
import re

import numpy as np
import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, dtype_kind
from jupyterlab_verknode.engine.widgets import OutputWidget

CAT = "Chemistry/3D Structures"
# (keep in step with the versions in package.json: the CDN is the fallback for the served copies)
NGL_URLS = ["__VERKNODE_BASE__verknode/vendor/ngl/ngl.js", "https://cdn.jsdelivr.net/npm/ngl@2.5.0/dist/ngl.js"]
MOLSTAR_URLS = ["__VERKNODE_BASE__verknode/vendor/molstar/molstar.js",
                "https://cdn.jsdelivr.net/npm/molstar@5.12.0/build/viewer/molstar.js"]
MOLSTAR_CSS = ["__VERKNODE_BASE__verknode/vendor/molstar/molstar.css",
               "https://cdn.jsdelivr.net/npm/molstar@5.12.0/build/viewer/molstar.css"]

# carbon colours of the ligands, in this order (other elements keep their usual colours)
PALETTE = ["#f28e2b", "#59a14f", "#b07aa1", "#17becf", "#e15759", "#9c755f", "#edc948", "#4e79a7",
           "#ff9da7", "#76b7b2", "#bab0ac", "#8cd17d"]
PROTEIN_FORMATS = {".pdb": "pdb", ".ent": "pdb", ".cif": "mmcif", ".mmcif": "mmcif"}
LIGAND_FORMATS = {".sdf": "sdf", ".sd": "sdf", ".mol": "sdf", ".mol2": "mol2", ".pdb": "pdb", ".ent": "pdb",
                  ".xyz": "xyz"}
FILES_HELP = "Absolute paths, or relative to the folder containing the workflow (.vkf) file. Also .gz."
HELP = (
    " The protein comes from a PDB or mmCIF file; ligands from SDF / MOL / MOL2 / PDB files (every record "
    "of an SD file is a ligand) and from a molecule column of the input table (RDKit molecules, or SMILES / "
    "MolBlock / PDB text; ligands without 3D coordinates get them generated). Each ligand has a colour of its "
    "own and a line in the list on the view: show or hide it, or click its name to zoom to it. Residues near "
    "the shown ligands are drawn as sticks (PDB proteins)."
)


def structure_fields():
    return [
        F("protein", "string", "Protein (PDB / mmCIF file)", "", FILES_HELP + " Empty: ligands only.", path=True,
          extensions=[".pdb", ".ent", ".cif", ".mmcif", ".gz"]),
        F("ligand_files", "records", "Ligand files (SDF, MOL, MOL2, PDB)", [], FILES_HELP, fields=[
            F("path", "string", "File", "", path=True,
              extensions=[".sdf", ".sd", ".mol", ".mol2", ".pdb", ".ent", ".xyz", ".gz"]),
        ]),
        F("column", "column", "Ligand column (input table)", None,
          "Molecules of the input table: an RDKit molecule column, or SMILES / MolBlock / PDB text. "
          "Empty: the first molecule column.", kinds=["molecule", "string"]),
        F("name_column", "column", "Ligand name column", None, "Empty: the molecule names (or the row).",
          kinds=["string", "integer", "float"]),
        F("generate_3d", "bool", "Generate 3D coordinates for ligands without them", True),
        F("max_ligands", "int", "Maximum number of ligands", 50),
        F("protein_style", "enum", "Protein", "Cartoon",
          options=["Cartoon", "Cartoon and surface", "Ball and stick", "Hidden"]),
        F("ligand_style", "enum", "Ligands", "Ball and stick", options=["Ball and stick", "Sticks", "Space filling"]),
        F("protein_ligands", "bool", "Show the ligands and ions of the protein file", True),
        F("pocket", "bool", "Show residues near the ligands", True),
        F("pocket_radius", "float", "Within (Å)", 5.0, show_if={"pocket": True}),
        F("shown", "enum", "At the start, show", "All ligands", options=["All ligands", "First ligand"]),
        F("background", "enum", "Background", "White", options=["White", "Black"]),
    ]


# ----------------------------------------------------------------------
# gathering the structures
# ----------------------------------------------------------------------
def _read_text(ctx, path: str) -> tuple[str, str]:
    """(text, extension without .gz) of a file."""
    p = ctx.resolve_path(path)
    if not os.path.isfile(p):
        raise InvalidSettingsError(f"File does not exist: {p}")
    name = p[:-3] if p.lower().endswith(".gz") else p
    opener = gzip.open if p.lower().endswith(".gz") else open
    with opener(p, "rb") as fh:
        text = fh.read().decode("utf-8", errors="replace")
    return text, os.path.splitext(name)[1].lower()


def _split_records(text: str, fmt: str) -> list[str]:
    if fmt == "sdf":
        parts = [r.strip("\r\n") for r in re.split(r"^\$\$\$\$[ \t]*\r?$", text, flags=re.M)]
        return [p + "\n$$$$\n" for p in parts if p.strip()]
    if fmt == "mol2":
        parts = re.split(r"(?=^@<TRIPOS>MOLECULE)", text, flags=re.M)
        return [p for p in parts if p.strip().startswith("@<TRIPOS>MOLECULE")]
    return [text]


def _record_name(text: str, fmt: str, fallback: str) -> str:
    lines = text.splitlines()
    if fmt == "sdf" and lines and lines[0].strip():
        return lines[0].strip()
    if fmt == "mol2" and len(lines) > 1 and lines[1].strip():
        return lines[1].strip()
    return fallback


def _is_pdb(text: str) -> bool:
    return bool(re.search(r"^(ATOM  |HETATM)", text, flags=re.M))


def _mol_block(mol, generate: bool) -> str | None:
    """MolBlock with 3D coordinates (generated if the molecule has none and `generate`)."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    if mol.GetNumConformers() and mol.GetConformer().Is3D():
        return Chem.MolToMolBlock(mol)
    if not generate:
        return None
    m = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(m, randomSeed=0xF00D) != 0:
        if AllChem.EmbedMolecule(m, randomSeed=0xF00D, useRandomCoords=True) != 0:
            return None
    try:
        AllChem.MMFFOptimizeMolecule(m)
    except Exception:  # noqa: BLE001 - keep the embedded coordinates
        pass
    return Chem.MolToMolBlock(Chem.RemoveHs(m))


def _table_ligands(df: pl.DataFrame, s: dict, ctx) -> list[dict]:
    col = s.get("column")
    if not col:
        col = next((c for c, t in df.schema.items() if dtype_kind(t) == "molecule"), None)
        if col is None:
            return []
    if col not in df.columns:
        raise InvalidSettingsError(f'Column "{col}" is not in the input table')
    names = df[s["name_column"]].cast(pl.String).to_list() if s.get("name_column") in df.columns else None
    series = df[col]
    out, skipped = [], 0
    if dtype_kind(series.dtype) == "molecule":
        from verknode.node import chem

        values = [("mol", m) for m in chem.to_mols(series)]
    else:
        values = [("text", t) for t in series.to_list()]
    for i, (kind, v) in enumerate(values):
        ctx.check_canceled()
        if v is None:
            skipped += 1
            continue
        name = (names[i] if names and names[i] is not None else None)
        if kind == "text" and _is_pdb(v):
            out.append({"name": name or f"Row {i + 1}", "format": "pdb", "data": v})
            continue
        if kind == "text":
            from verknode.node import chem

            v = chem.parse(v)
            if v is None:
                skipped += 1
                continue
        generated = not (v.GetNumConformers() and v.GetConformer().Is3D())
        block = _mol_block(v, s.get("generate_3d", True))
        if block is None:
            skipped += 1
            continue
        if name is None:
            name = v.GetProp("_Name") if v.HasProp("_Name") and v.GetProp("_Name").strip() else f"Row {i + 1}"
        out.append({"name": name, "format": "sdf", "data": block, "generated": generated})
    if skipped:
        ctx.warn(f"{skipped} ligand(s) skipped: no structure" + ("" if s.get("generate_3d", True) else " or no 3D coordinates"))
    return out


def _spread(ligands: list):
    """Generated coordinates all sit at the origin: put those ligands side by side (no protein)."""
    from rdkit import Chem
    from rdkit.Geometry import Point3D

    mols = [(lig, Chem.MolFromMolBlock(lig["data"], sanitize=False, removeHs=False)) for lig in ligands]
    mols = [(lig, m) for lig, m in mols if m is not None and m.GetNumConformers()]
    if len(mols) < 2:
        return
    size = max(np.ptp(m.GetConformer().GetPositions(), axis=0).max() for _, m in mols) + 3.0
    cols = int(np.ceil(np.sqrt(len(mols))))
    for k, (lig, m) in enumerate(mols):
        conf = m.GetConformer()
        xyz = conf.GetPositions()
        shift = np.array([(k % cols) * size, -(k // cols) * size, 0.0]) - xyz.mean(0)
        for a in range(m.GetNumAtoms()):
            x, y, z = xyz[a] + shift
            conf.SetAtomPosition(a, Point3D(x, y, z))
        lig["data"] = Chem.MolToMolBlock(m)


def _coords(lig: dict) -> np.ndarray:
    """Atom positions of a ligand (for the residues near it)."""
    text, fmt = lig["data"], lig["format"]
    if fmt == "pdb":
        return _pdb_atoms(text, hetero=True)[0]
    try:
        from rdkit import Chem

        mol = (Chem.MolFromMolBlock(text, sanitize=False, removeHs=False) if fmt == "sdf"
               else Chem.MolFromMol2Block(text, sanitize=False, removeHs=False) if fmt == "mol2" else None)
        if mol is not None and mol.GetNumConformers():
            return np.asarray(mol.GetConformer().GetPositions(), float)
    except ImportError:
        pass
    if fmt == "sdf":  # (V2000 without RDKit)
        lines = text.splitlines()
        if len(lines) > 3 and lines[3][:3].strip().isdigit():
            n = int(lines[3][:3])
            return np.asarray([[float(lines[4 + k][p:p + 10]) for p in (0, 10, 20)] for k in range(n)], float)
    return np.zeros((0, 3))


def _pdb_atoms(text: str, hetero: bool = False) -> tuple[np.ndarray, list]:
    """(positions, residue keys "chain:number" ) of the ATOM (and HETATM) records, without water."""
    xyz, keys = [], []
    for line in text.splitlines():
        rec = line[:6]
        if rec == "ATOM  " or (hetero and rec == "HETATM"):
            if line[17:20].strip() in ("HOH", "WAT", "DOD"):
                continue
            try:
                xyz.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
            except ValueError:
                continue
            keys.append(f"{line[21].strip()}:{line[22:27].strip()}")
    return np.asarray(xyz, float).reshape(-1, 3), keys


def _pocket(protein_xyz: np.ndarray, protein_keys: list, lig_xyz: np.ndarray, radius: float) -> list:
    if not len(protein_xyz) or not len(lig_xyz):
        return []
    near = np.zeros(len(protein_xyz), bool)
    for start in range(0, len(lig_xyz), 64):
        d = ((protein_xyz[:, None, :] - lig_xyz[None, start:start + 64, :]) ** 2).sum(-1)
        near |= (d <= radius * radius).any(1)
    return sorted({protein_keys[i] for i in np.nonzero(near)[0]})


def gather(df: pl.DataFrame | None, s: dict, ctx) -> dict:
    """What the page shows: {"protein", "ligands": [{name, format, data, color, pocket}], options}."""
    protein = None
    if (s.get("protein") or "").strip():
        text, ext = _read_text(ctx, s["protein"].strip())
        fmt = PROTEIN_FORMATS.get(ext)
        if fmt is None:
            raise InvalidSettingsError(f"Protein files: {', '.join(PROTEIN_FORMATS)} (not {ext or 'no extension'})")
        protein = {"name": os.path.basename(s["protein"].strip()), "format": fmt, "data": text}
    ligands = []
    for rec in s.get("ligand_files") or []:
        path = (rec or {}).get("path", "").strip()
        if not path:
            continue
        text, ext = _read_text(ctx, path)
        fmt = LIGAND_FORMATS.get(ext)
        if fmt is None:
            raise InvalidSettingsError(f"Ligand files: {', '.join(LIGAND_FORMATS)} (not {ext or 'no extension'})")
        records = _split_records(text, fmt)
        base = os.path.basename(path)
        for k, r in enumerate(records):
            fallback = base if len(records) == 1 else f"{base} #{k + 1}"
            ligands.append({"name": _record_name(r, fmt, fallback), "format": fmt, "data": r})
    if df is not None:
        ligands += _table_ligands(df, s, ctx)
    if protein is None and not ligands:
        raise InvalidSettingsError("Nothing to show: set a protein or ligand file, or connect a table of molecules")
    limit = max(1, int(s.get("max_ligands") or 50))
    if len(ligands) > limit:
        ctx.warn(f"Showing the first {limit} of {len(ligands)} ligands (Maximum number of ligands)")
        ligands = ligands[:limit]
    if protein is None:
        _spread([lig for lig in ligands if lig.get("generated")])
    for lig in ligands:
        lig.pop("generated", None)
    pocket = bool(s.get("pocket")) and protein is not None and bool(ligands)
    if pocket and protein["format"] != "pdb":
        ctx.warn("Residues near the ligands: for PDB protein files only")
        pocket = False
    if pocket:
        pxyz, pkeys = _pdb_atoms(protein["data"])
        radius = float(s.get("pocket_radius") or 5.0)
    for i, lig in enumerate(ligands):
        lig["color"] = PALETTE[i % len(PALETTE)]
        lig["pocket"] = _pocket(pxyz, pkeys, _coords(lig), radius) if pocket else []
    return {
        "protein": protein,
        "ligands": ligands,
        "proteinStyle": s.get("protein_style") or "Cartoon",
        "ligandStyle": s.get("ligand_style") or "Ball and stick",
        "proteinLigands": bool(s.get("protein_ligands", True)),
        "pocket": pocket,
        "shown": "first" if s.get("shown") == "First ligand" else "all",
        "dark": s.get("background") == "Black",
    }


# ----------------------------------------------------------------------
# the page
# ----------------------------------------------------------------------
_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>/*TITLE*/</title>
/*CSS*/
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; font: 12px system-ui, sans-serif; }
  body { background: var(--bg); color: var(--fg); }
  :root { --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --panel: rgba(255,255,255,.88); --line: #d0d7de; }
  body.dark { --bg: #000000; --fg: #e6edf3; --muted: #9da7b3; --panel: rgba(22,27,34,.85); --line: #3d444d; }
  #viewer { position: absolute; inset: 0; }
  #panel { position: absolute; left: 8px; top: 8px; z-index: 10; background: var(--panel);
           border: 1px solid var(--line); border-radius: 6px; max-height: calc(100% - 16px); max-width: 60%;
           overflow: auto; box-shadow: 0 1px 3px rgba(0,0,0,.15); }
  #panel header { display: flex; gap: 6px; align-items: center; padding: 4px 6px; border-bottom: 1px solid var(--line);
                  position: sticky; top: 0; background: var(--panel); }
  #panel header b { flex: 1; font-weight: 600; white-space: nowrap; }
  #panel button { font: inherit; color: var(--fg); background: none; border: 1px solid var(--line); border-radius: 4px;
                  padding: 0 5px; cursor: pointer; }
  #panel.closed ul, #panel.closed .all { display: none; }
  #panel ul { list-style: none; margin: 0; padding: 3px 0; }
  #panel li { display: flex; align-items: center; gap: 5px; padding: 2px 6px; white-space: nowrap; }
  #panel li span.dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  #panel li a { color: var(--fg); cursor: pointer; overflow: hidden; text-overflow: ellipsis; }
  #panel li a:hover { text-decoration: underline; }
  #panel li.protein a { color: var(--muted); }
  #message { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
             color: var(--muted); padding: 16px; text-align: center; }
</style></head>
<body>
<div id="viewer"></div>
<div id="panel"><header><b></b><span class="all"><button data-all="1" title="Show all ligands">All</button>
<button data-all="0" title="Hide all ligands">None</button></span>
<button id="fold" title="Show or hide this list">&#9662;</button></header><ul></ul></div>
<div id="message">Loading the viewer…</div>
<script>window.STRUCTURES = /*DATA*/;</script>
<script>
(function () {
  const S = window.STRUCTURES;
  if (S.dark) document.body.classList.add('dark');
  const message = document.getElementById('message');
  const say = text => { message.textContent = text; message.style.display = text ? 'flex' : 'none'; };
  function load(urls, css) {
    return new Promise((done, fail) => {
      const next = i => {
        if (i >= urls.length) return fail(new Error('Could not load ' + urls[urls.length - 1]));
        const el = document.createElement(css ? 'link' : 'script');
        if (css) { el.rel = 'stylesheet'; el.href = urls[i]; } else { el.src = urls[i]; }
        el.onload = () => done();
        el.onerror = () => { el.remove(); next(i + 1); };
        document.head.appendChild(el);
      };
      next(0);
    });
  }
  // residues near the ligands: lines of the protein file by residue ("chain:number")
  const residues = {};
  if (S.protein && S.protein.format === 'pdb') {
    for (const line of S.protein.data.split('\n')) {
      if (line.startsWith('ATOM  ')) {
        const key = line.substr(21, 1).trim() + ':' + line.substr(22, 5).trim();
        (residues[key] = residues[key] || []).push(line);
      }
    }
  }
  const visible = S.ligands.map((l, i) => S.shown === 'all' || i === 0);
  function pocketText() {
    if (!S.pocket) return null;
    const keys = new Set();
    S.ligands.forEach((l, i) => { if (visible[i]) l.pocket.forEach(k => keys.add(k)); });
    if (!keys.size) return null;
    const lines = [];
    keys.forEach(k => (residues[k] || []).forEach(line => lines.push(line)));
    return lines.join('\n') + '\nEND\n';
  }
  async function start(viewer) {
    await viewer.init(document.getElementById('viewer'), S);
    if (S.protein) await viewer.protein(S.protein, S);
    const handles = [];
    for (let i = 0; i < S.ligands.length; i++) {
      handles.push(await viewer.ligand(S.ligands[i], S));
      if (!visible[i]) viewer.setVisible(handles[i], false);
    }
    await viewer.pocket(pocketText(), S);
    say('');
    // (with a protein: the first shown ligand in its pocket; without: all of them)
    const first = visible.indexOf(true);
    if (S.protein && first >= 0) await viewer.focus(handles[first], 0); else await viewer.reset();
    // the list
    const panel = document.getElementById('panel');
    panel.querySelector('header b').textContent = S.ligands.length + (S.ligands.length === 1 ? ' ligand' : ' ligands');
    const ul = panel.querySelector('ul');
    if (S.protein) {
      const li = document.createElement('li');
      li.className = 'protein';
      li.innerHTML = '<input type="checkbox" checked><span class="dot" style="border:1px solid var(--muted)"></span><a title="Zoom out to the whole structure"></a>';
      li.querySelector('a').textContent = S.protein.name;
      li.querySelector('a').onclick = () => viewer.reset();
      li.querySelector('input').onchange = e => viewer.setProteinVisible(e.target.checked);
      ul.appendChild(li);
    }
    const boxes = [];
    let pocketBusy = Promise.resolve();
    const update = () => { pocketBusy = pocketBusy.then(() => viewer.pocket(pocketText(), S)); };
    S.ligands.forEach((l, i) => {
      const li = document.createElement('li');
      li.innerHTML = '<input type="checkbox"><span class="dot"></span><a title="Zoom to this ligand"></a>';
      const box = li.querySelector('input');
      box.checked = visible[i];
      box.onchange = () => { visible[i] = box.checked; viewer.setVisible(handles[i], box.checked); update(); };
      li.querySelector('.dot').style.background = l.color;
      const a = li.querySelector('a');
      a.textContent = l.name;
      a.onclick = () => {
        if (!visible[i]) { box.checked = true; box.onchange(); }
        viewer.focus(handles[i], 600);
      };
      boxes.push(box);
      ul.appendChild(li);
    });
    panel.querySelectorAll('[data-all]').forEach(b => b.onclick = () => {
      boxes.forEach((box, i) => { box.checked = b.dataset.all === '1'; visible[i] = box.checked; viewer.setVisible(handles[i], box.checked); });
      update();
    });
    if (!S.ligands.length) panel.querySelector('.all').style.display = 'none';
    document.getElementById('fold').onclick = () => panel.classList.toggle('closed');
    if (window.innerWidth < 300 || window.innerHeight < 200) panel.classList.add('closed');
    // ligands only: the list next to them, not over them (with a protein it floats over the view)
    else if (!S.protein && window.innerWidth > 360) {
      document.getElementById('viewer').style.left = (panel.offsetWidth + 12) + 'px';
      setTimeout(() => viewer.reset(), 100);
    }
  }
  // unit vector from the protein's centre to a ligand's: the camera looks at the ligand from there
  window.outward = (lig, protein) => {
    if (!protein) return null;
    const d = [lig[0] - protein[0], lig[1] - protein[1], lig[2] - protein[2]];
    const n = Math.hypot(d[0], d[1], d[2]);
    return n > 1 ? d.map(v => v / n) : null;
  };
  window.startStructures = (viewer, urls) => (window.structureViewer = viewer, load(urls)).then(() => start(viewer)).catch(e => {
    console.error(e);
    say('Could not show the structures: ' + (e && e.message || e));
  });
  window.loadStyles = urls => load(urls, true).catch(() => null);
})();
</script>
<script>/*VIEWER*/</script>
</body></html>
"""

_NGL = r"""
const hex = c => parseInt(c.slice(1), 16);
const ext = f => ({ mmcif: 'cif', pdb: 'pdb', sdf: 'sdf', mol2: 'mol2', xyz: 'xyz' })[f] || f;
const blob = text => new Blob([text], { type: 'text/plain' });
const LIGAND = { 'Ball and stick': 'ball+stick', 'Sticks': 'licorice', 'Space filling': 'spacefill' };
const viewer = {
  async init(el, S) {
    this.stage = new NGL.Stage(el, { backgroundColor: S.dark ? 'black' : 'white', tooltip: true });
    // (the frame may start small or hidden on the canvas: follow its size, and draw again when shown)
    new ResizeObserver(() => this.stage.handleResize()).observe(el);
    document.addEventListener('visibilitychange', () => { this.stage.handleResize(); this.stage.viewer.requestRender(); });
    // browsers drop WebGL contexts when there are many (one per viewer): start again when it comes back
    const canvas = this.stage.viewer.renderer.domElement;
    canvas.addEventListener('webglcontextlost', e => e.preventDefault());
    canvas.addEventListener('webglcontextrestored', () => location.reload());
  },
  async protein(p, S) {
    const c = await this.stage.loadFile(blob(p.data), { ext: ext(p.format), name: p.name });
    const tint = S.dark ? '#6b7f99' : '#9fb6d3';
    if (S.proteinStyle.startsWith('Cartoon')) c.addRepresentation('cartoon', { color: tint });
    if (S.proteinStyle === 'Cartoon and surface') c.addRepresentation('surface', { sele: 'polymer', color: tint, opacity: 0.25, side: 'front' });
    if (S.proteinStyle === 'Ball and stick') c.addRepresentation('ball+stick', { sele: 'polymer' });
    // (the protein file's own ligands: thin and grey, not to be taken for the listed ones)
    if (S.proteinLigands) c.addRepresentation('licorice', { sele: 'hetero and not water', colorScheme: 'element', colorValue: 0x9a9a9a, radius: 0.12 });
    this.proteinComp = c;
  },
  async ligand(l, S) {
    const c = await this.stage.loadFile(blob(l.data), { ext: ext(l.format), name: l.name });
    c.addRepresentation(LIGAND[S.ligandStyle] || 'ball+stick', { colorScheme: 'element', colorValue: hex(l.color) });
    return c;
  },
  setVisible(c, on) { c.setVisibility(on); },
  setProteinVisible(on) { if (this.proteinComp) this.proteinComp.setVisibility(on); },
  async focus(c, ms) {
    const p = this.proteinComp && this.proteinComp.getCenter();
    const d = outward(c.getCenter().toArray(), p && p.toArray());
    if (d) {
      // the scene rotation taking d to +z (towards the camera)
      const cr = [d[1], -d[0], 0], w = 1 + d[2], n = Math.hypot(cr[0], cr[1], w);
      const q = w < 1e-6 ? [1, 0, 0, 0] : [cr[0] / n, cr[1] / n, 0, w / n];
      if (ms) this.stage.animationControls.rotate(q, ms); else this.stage.viewerControls.rotate(q);
    }
    if (ms) this.stage.animationControls.zoomMove(c.getCenter(), c.getZoom() * 1.4, ms);
    else { this.stage.viewerControls.center(c.getCenter()); this.stage.viewerControls.distance(Math.abs(c.getZoom()) * 1.4); }
    this.stage.viewer.requestRender();
  },
  async reset() { this.stage.autoView(600); },
  async pocket(text, S) {
    if (this.pocketComp) { this.stage.removeComponent(this.pocketComp); this.pocketComp = null; }
    if (!text) return;
    this.pocketComp = await this.stage.loadFile(blob(text), { ext: 'pdb', name: 'Residues near the ligands' });
    this.pocketComp.addRepresentation('licorice', { colorScheme: 'element', colorValue: S.dark ? 0xa0a0a0 : 0xb4b4b4, radius: 0.1 });
  },
};
startStructures(viewer, /*URLS*/);
"""

_MOLSTAR = r"""
const hex = c => parseInt(c.slice(1), 16);
const LIGAND = { 'Ball and stick': ['ball-and-stick', {}], 'Sticks': ['ball-and-stick', { sizeFactor: 0.18, sizeAspectRatio: 1 }],
                 'Space filling': ['spacefill', {}] };
const viewer = {
  async init(el, S) {
    this.viewer = await molstar.Viewer.create(el, {
      layoutIsExpanded: false, layoutShowControls: false, layoutShowRemoteState: false, layoutShowSequence: false,
      layoutShowLog: false, layoutShowLeftPanel: false, viewportShowExpand: false, viewportShowSelectionMode: false,
      viewportShowAnimation: false, viewportShowTrajectoryControls: false, pdbProvider: 'rcsb', emdbProvider: 'rcsb',
    });
    this.plugin = this.viewer.plugin;
    this.plugin.canvas3d.setProps({ renderer: { backgroundColor: S.dark ? 0x000000 : 0xffffff } });
  },
  async load(text, format, label) {
    const b = this.plugin.builders;
    const data = await b.data.rawData({ data: text, label });
    const trajectory = await b.structure.parseTrajectory(data, format);
    const model = await b.structure.createModel(trajectory);
    const structure = await b.structure.createStructure(model);
    return { data, structure };
  },
  async rep(target, type, color, colorParams, typeParams) {
    await this.plugin.builders.structure.representation.addRepresentation(target, { type, typeParams: typeParams || {}, color, colorParams: colorParams || {} });
  },
  async protein(p, S) {
    const loaded = await this.load(p.data, p.format, p.name);
    const structure = loaded.structure;
    const b = this.plugin.builders.structure;
    const tint = S.dark ? 0x6b7f99 : 0x9fb6d3;
    const polymer = await b.tryCreateComponentStatic(structure, 'polymer');
    if (polymer) {
      if (S.proteinStyle.startsWith('Cartoon')) await this.rep(polymer, 'cartoon', 'uniform', { value: tint });
      if (S.proteinStyle === 'Cartoon and surface') await this.rep(polymer, 'molecular-surface', 'uniform', { value: tint }, { alpha: 0.25 });
      if (S.proteinStyle === 'Ball and stick') await this.rep(polymer, 'ball-and-stick', 'element-symbol');
    }
    // (the protein file's own ligands: thin and grey, not to be taken for the listed ones)
    if (S.proteinLigands) {
      for (const kind of ['ligand', 'ion']) {
        const comp = await b.tryCreateComponentStatic(structure, kind);
        if (comp) await this.rep(comp, 'ball-and-stick', 'element-symbol', { carbonColor: { name: 'uniform', params: { value: 0x9a9a9a } } }, { sizeFactor: 0.12, sizeAspectRatio: 1 });
      }
    }
    this.proteinLoaded = loaded;
  },
  async ligand(l, S) {
    const s = await this.load(l.data, l.format === 'sdf' ? 'sdf' : l.format, l.name);
    const [type, params] = LIGAND[S.ligandStyle] || LIGAND['Ball and stick'];
    await this.rep(s.structure, type, 'element-symbol', { carbonColor: { name: 'uniform', params: { value: hex(l.color) } } }, params);
    return s;
  },
  setProteinVisible(on) { if (this.proteinLoaded) this.setVisible(this.proteinLoaded, on); },
  setVisible(s, on) {
    const state = this.plugin.state.data;
    const walk = ref => {
      if (state.cells.has(ref)) state.updateCellState(ref, { isHidden: !on });
      state.tree.children.get(ref).forEach(walk);
    };
    walk(s.data.ref);
  },
  sphere(s) {
    const cell = this.plugin.state.data.cells.get(s.structure.ref);
    return cell && cell.obj && cell.obj.data && cell.obj.data.boundary.sphere;
  },
  async focus(s, ms) {
    const sphere = this.sphere(s);
    if (!sphere) return;
    const p = this.proteinLoaded && this.sphere(this.proteinLoaded);
    const d = outward(Array.from(sphere.center), p && Array.from(p.center));
    if (!d) {
      this.plugin.managers.camera.focusSphere(sphere, { durationMs: ms, extraRadius: 1 + 0.1 * sphere.radius });
      return;
    }
    const a = Math.abs(d[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
    const k = a[0] * d[0] + a[1] * d[1] + a[2] * d[2];
    const up = [a[0] - k * d[0], a[1] - k * d[1], a[2] - k * d[2]];
    const snapshot = this.plugin.canvas3d.camera.getFocus(sphere.center, sphere.radius * 1.1 + 1, up, [-d[0], -d[1], -d[2]]);
    this.plugin.managers.camera.setSnapshot(snapshot, ms);
  },
  async reset() { this.plugin.managers.camera.reset(); },
  async pocket(text, S) {
    if (this.pocketRef) { await this.plugin.state.data.build().delete(this.pocketRef).commit(); this.pocketRef = null; }
    if (!text) return;
    const s = await this.load(text, 'pdb', 'Residues near the ligands');
    await this.rep(s.structure, 'line', 'element-symbol',
      { carbonColor: { name: 'uniform', params: { value: S.dark ? 0xa0a0a0 : 0xa8a8a8 } } }, { sizeFactor: 1.5 });
    this.pocketRef = s.data.ref;
  },
};
loadStyles(/*CSS_URLS*/).then(() => startStructures(viewer, /*URLS*/));
"""


def page(title: str, payload: dict, library: str) -> str:
    data = json.dumps(payload).replace("</", "<\\/")
    if library == "ngl":
        script = _NGL.replace("/*URLS*/", json.dumps(NGL_URLS))
        css = ""
    else:
        script = _MOLSTAR.replace("/*URLS*/", json.dumps(MOLSTAR_URLS)).replace("/*CSS_URLS*/", json.dumps(MOLSTAR_CSS))
        css = "<style>.msp-plugin { background: transparent; } .msp-viewport-controls { top: 4px; right: 4px; }</style>"
    from html import escape

    return (_PAGE.replace("/*TITLE*/", escape(title)).replace("/*CSS*/", css)
            .replace("/*DATA*/", data).replace("/*VIEWER*/", script))


# ----------------------------------------------------------------------
# nodes
# ----------------------------------------------------------------------
class _StructureView(NodeModel):
    abstract = True
    node_type = "Visualizer"
    has_view = True
    view_scripts = True
    library = "ngl"
    in_ports = [Port(TABLE, "Ligands", "Table with a molecule column (optional)", optional=True)]
    out_ports = []
    fields = [F("title", "string", "Title", "")] + structure_fields()

    def configure(self, in_specs, s, ctx):
        return []

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return []
        payload = gather(inputs[0], s, ctx)
        ctx.set_view(page(s.get("title") or self.name, payload, self.library))
        ctx.log(f"{len(payload['ligands'])} ligand(s)" + (f", protein {payload['protein']['name']}" if payload["protein"] else ""))
        return []


class NGLStructureView(_StructureView):
    factory = "verknode.structures.NGLView"
    category = CAT
    name = "NGL Structure View"
    icon = "NGL"
    library = "ngl"
    short_description = "3D view of a protein and ligands with NGL."
    description = "Shows a protein and ligands in 3D in the node view, with the NGL viewer." + HELP


class MolstarStructureView(_StructureView):
    factory = "verknode.structures.MolstarView"
    category = CAT
    name = "Mol* Structure View"
    icon = "M*"
    library = "molstar"
    short_description = "3D view of a protein and ligands with Mol*."
    description = "Shows a protein and ligands in 3D in the node view, with the Mol* viewer." + HELP


class _StructureDisplay(OutputWidget):
    abstract = True
    renderer = "html"
    width, height = 440, 340
    library = "ngl"
    in_ports = [Port(TABLE, "Ligands", "Table with a molecule column (optional)", optional=True)]
    widget_fields = structure_fields()

    def configure(self, in_specs, s, ctx):
        return [in_specs[0] if in_specs and in_specs[0] is not None else pl.Schema()]

    def render(self, obj, s, ctx):
        if ctx.dry:
            return None
        return {"html": page(s.get("title") or self.name, gather(obj, s, ctx), self.library)}

    def execute(self, inputs, s, ctx):
        obj = inputs[0]
        ctx.set_display(self.render(obj, s, ctx))
        return [obj if obj is not None else pl.DataFrame()]


class NGLStructureDisplay(_StructureDisplay):
    factory = "verknode.widgets.NGLStructureDisplay"
    name = "NGL Structure Display"
    icon = "NGL"
    library = "ngl"
    short_description = "3D protein and ligands on the canvas, with NGL."
    description = ("Shows a protein and ligands in 3D next to the node on the canvas, with the NGL viewer." + HELP
                   + " The output passes the input table on.")


class MolstarStructureDisplay(_StructureDisplay):
    factory = "verknode.widgets.MolstarStructureDisplay"
    name = "Mol* Structure Display"
    icon = "M*"
    library = "molstar"
    short_description = "3D protein and ligands on the canvas, with Mol*."
    description = ("Shows a protein and ligands in 3D next to the node on the canvas, with the Mol* viewer." + HELP
                   + " The output passes the input table on.")
