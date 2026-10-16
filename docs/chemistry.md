# Chemistry (RDKit)

Molecules are a column type of their own ("RDKit Molecule", badge `M`),
stored as pickled RDKit molecules so they keep properties and conformers
through saves. Port views draw molecule columns as structures; the `⌬`
button on a String column header draws SMILES / MolBlock values too. RDKit
nodes accept a molecule column or a SMILES/MolBlock String column.

Nodes (Chemistry › RDKit): SDF Reader/Writer, RDKit From Molecule (SMILES,
SDF, SMARTS, InChI → molecule, with error port), RDKit To Molecule
(→ SMILES, MolBlock, SMARTS, InChI, InChIKey, formula), Descriptor
Calculation, Fingerprint, Substructure Filter, Similarity Search, Diversity
Picker, Structure Normalizer, Find Murcko Scaffolds, Generate Coordinates
(2D / 3D ETKDG + MMFF/UFF), One Component Reaction. CSV Writer writes
molecules as SMILES; the Python Script node has `chem` helpers
(`chem.to_mols(series)`, `chem.mol_series(name, mols)`) and `Chem`.

**Molecule Sketcher** (Chemistry › RDKit › IO) makes a table of sketches
(Index, Name, Molecule, SMILES, optional MolBlock) from structures drawn in
its dialog; they are stored as MolBlocks in the node settings (from Python:
`wf.add_node("Molecule Sketcher", molecules=[{"name": "Phenol", "smiles": "Oc1ccccc1"}])`).
With a table connected to its optional input (molecule or SMILES/MolBlock
column, optional name column) it edits that table instead: once the
upstream node has run, the dialog lists every input molecule with its
index, each can be edited in the sketcher, renamed or reset to the
original (or all at once with Reset all), and exactly the input rows come
out - edited or not - with the other input columns kept (optional). The
dialog shows which columns the structures and names come from and updates
the list as soon as another column is chosen; if that would discard edited
molecules or names, it asks first. `{"row": 2, ...}` in the settings is the
edit of input row 3.
"Draw structure…" opens the sketcher with two editors (Ketcher by default;
the dialog switches between them and remembers the last one; also in
≡ Settings):

- **Ketcher** (EPAM, standalone build with Indigo in the browser). The
  Dockerfile downloads it to `/opt/ketcher`; the server serves it at
  `<base_url>/verknode/ketcher/`.
- **Built-in sketcher** with common drawing conventions: fixed bond length, new
  bonds at 120° (zigzag), drag to draw in 15° steps or onto an atom; click
  a bond to cycle single → double → triple; double / triple / wedge / hash
  tools (wedge again flips it); chain tool (drag, bond count shown); 3–8
  rings and benzene on empty space, an atom or fused onto a bond (Kekulé
  structure kept valid); hover hotkeys on an atom `c n o s p f i h`,
  `l` = Cl, `b` = Br, `+` / `-` charge, on a bond `1 2 3`, `w` wedge,
  `h` hash, Delete removes what is under the pointer; double-click an atom
  to type a label (`NH2`, `O-`, `13C`); select tool to move / delete
  atoms; Clean (RDKit 2D layout), undo / redo, wheel zoom, middle-drag pan.
  Heteroatoms show their implicit hydrogens (`OH`, `H2N`), double bonds sit
  inside rings and are centred when terminal.

SMILES or MolBlock text can be pasted into the sketcher, and several SMILES
(one per line, optionally with a name) added at once in the node dialog.
Double-click a structure in the list to edit it; the sketcher shows the
molecule's index and name, and Previous / Next (PageUp / PageDown) go
through the list without closing it - changes are kept as you go, and
molecules you only look at stay unedited (compared by canonical SMILES).

Example: `notebooks/examples/chemistry.vkf`.
