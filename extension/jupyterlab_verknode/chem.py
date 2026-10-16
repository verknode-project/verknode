"""RDKit helpers: molecule columns, parsing, fingerprints and depiction.

Molecule columns are polars structs ``{"rdkit_mol": Binary}`` holding the
pickled molecule (all properties and conformers included). Use these helpers
from the Python Script node as ``chem``::

    mols = chem.to_mols(input_table_1["Molecule"])
    output_table_1 = input_table_1.with_columns(chem.mol_series("Copy", mols))
"""
from __future__ import annotations

import polars as pl
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import rdDepictor, rdFingerprintGenerator, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D

from .engine.types import MOL_DTYPE, MOL_FIELD, dtype_kind

RDLogger.DisableLog("rdApp.*")

_PICKLE = Chem.PropertyPickleOptions.AllProps


def is_mol_dtype(dtype) -> bool:
    return dtype == MOL_DTYPE


def mol_to_cell(mol):
    return None if mol is None else {MOL_FIELD: mol.ToBinary(_PICKLE)}


def mol_series(name: str, mols) -> pl.Series:
    """Build a molecule column from RDKit molecules (None = missing)."""
    return pl.Series(name, [mol_to_cell(m) for m in mols], dtype=MOL_DTYPE)


def parse(text, fmt: str = "Auto", sanitize: bool = True):
    """Parse SMILES / MolBlock (SDF record) / SMARTS / InChI text into a molecule."""
    if text is None:
        return None
    text = str(text)
    if not text.strip():
        return None
    if fmt == "Auto":
        stripped = text.strip()
        if "M  END" in text or "\n" in stripped:
            fmt = "SDF"
        elif stripped.startswith("InChI="):
            fmt = "InChI"
        else:
            fmt = "SMILES"
    if fmt == "SMILES":
        return Chem.MolFromSmiles(text.strip(), sanitize=sanitize)
    if fmt == "SDF":
        return Chem.MolFromMolBlock(text, sanitize=sanitize, removeHs=False)
    if fmt == "SMARTS":
        return Chem.MolFromSmarts(text.strip())
    if fmt == "InChI":
        return Chem.MolFromInchi(text.strip(), sanitize=sanitize)
    raise ValueError(f"Unknown molecule format: {fmt}")


def to_mols(series: pl.Series) -> list:
    """Molecules of a molecule column, or parsed from a SMILES/MolBlock string column."""
    if is_mol_dtype(series.dtype):
        return [None if v is None or v.get(MOL_FIELD) is None else Chem.Mol(v[MOL_FIELD]) for v in series.to_list()]
    if dtype_kind(series.dtype) == "string":
        return [parse(v) for v in series.to_list()]
    raise ValueError(f'Column "{series.name}" does not contain molecules (type {series.dtype})')


def molecules_to_smiles(df: pl.DataFrame) -> pl.DataFrame:
    """Replace molecule columns by SMILES strings (for text formats like CSV)."""
    cols = [c for c, t in df.schema.items() if is_mol_dtype(t)]
    if not cols:
        return df
    return df.with_columns(
        pl.Series(c, [None if m is None else Chem.MolToSmiles(m) for m in to_mols(df[c])], dtype=pl.String)
        for c in cols
    )


# ----------------------------------------------------------------------
# fingerprints
# ----------------------------------------------------------------------
FINGERPRINTS = ["Morgan", "FeatMorgan", "RDKit", "AtomPair", "TopologicalTorsion", "MACCS", "Pattern"]


def fingerprinter(fp_type: str, radius: int = 2, n_bits: int = 2048):
    """Return a function mol -> ExplicitBitVect."""
    if fp_type in ("Morgan", "FeatMorgan"):
        kw = {}
        if fp_type == "FeatMorgan":
            kw["atomInvariantsGenerator"] = rdFingerprintGenerator.GetMorganFeatureAtomInvGen()
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits, **kw)
        return gen.GetFingerprint
    if fp_type == "RDKit":
        return rdFingerprintGenerator.GetRDKitFPGenerator(fpSize=n_bits).GetFingerprint
    if fp_type == "AtomPair":
        return rdFingerprintGenerator.GetAtomPairGenerator(fpSize=n_bits).GetFingerprint
    if fp_type == "TopologicalTorsion":
        return rdFingerprintGenerator.GetTopologicalTorsionGenerator(fpSize=n_bits).GetFingerprint
    if fp_type == "MACCS":
        return rdMolDescriptors.GetMACCSKeysFingerprint
    if fp_type == "Pattern":
        return lambda m: Chem.PatternFingerprint(m, fpSize=n_bits)
    raise ValueError(f"Unknown fingerprint type: {fp_type}")


SIMILARITY = {
    "Tanimoto": DataStructs.BulkTanimotoSimilarity,
    "Dice": DataStructs.BulkDiceSimilarity,
    "Cosine": DataStructs.BulkCosineSimilarity,
}


# ----------------------------------------------------------------------
# depiction (port views)
# ----------------------------------------------------------------------
def depict(mol, width: int = 180, height: int = 120) -> str | None:
    if mol is None:
        return None
    try:
        d = rdMolDraw2D.MolDraw2DSVG(width, height)
        opts = d.drawOptions()
        opts.clearBackground = False
        opts.padding = 0.08
        if mol.GetNumConformers() == 0 or mol.GetConformer().Is3D():
            mol = Chem.Mol(mol)
            rdDepictor.Compute2DCoords(mol)
        rdMolDraw2D.PrepareAndDrawMolecule(d, mol)
        d.FinishDrawing()
        svg = d.GetDrawingText()
        return svg[svg.find("<svg"):]
    except Exception:  # noqa: BLE001 - depiction must never break the table view
        return None


def view_cells(series: pl.Series) -> list:
    """JSON cells for a port view: {"svg", "smiles"} per molecule."""
    out = []
    for m in to_mols(series):
        if m is None:
            out.append(None)
        else:
            out.append({"__molecule__": True, "svg": depict(m), "smiles": Chem.MolToSmiles(m)})
    return out


# ----------------------------------------------------------------------
# Molecule Sketcher support
# ----------------------------------------------------------------------
_WEDGE = {1: "wedge", 6: "hash", 4: "either"}


def structure(text, clean: bool = False, width: int = 200, height: int = 140) -> dict:
    """Everything a sketcher needs about a structure given as SMILES or MolBlock.

    Returns {"smiles", "molfile", "formula", "svg", "atoms", "bonds"} with 2D
    coordinates (computed when missing, or when `clean` is set), Kekulé bond
    orders and wedge/hash stereo bonds; {"error"} when the text cannot be
    read. Coordinates are in Å (bond length ~1.5) with y pointing up.
    """
    if text is None or not str(text).strip():
        return {"smiles": "", "molfile": "", "formula": "", "svg": None, "atoms": [], "bonds": []}
    mol = parse(text)
    if mol is None:
        return {"error": "Not a valid structure (check valences and charges)"}
    has_coords = mol.GetNumConformers() > 0 and not mol.GetConformer().Is3D() and any(
        x or y for x, y, _ in mol.GetConformer().GetPositions().tolist())
    if clean or not has_coords:
        rdDepictor.SetPreferCoordGen(True)
        rdDepictor.Compute2DCoords(mol)
        Chem.WedgeMolBonds(mol, mol.GetConformer())
        stereo = {b.GetIdx(): {Chem.BondDir.BEGINWEDGE: 1, Chem.BondDir.BEGINDASH: 6}.get(b.GetBondDir(), 0)
                  for b in mol.GetBonds()}
    else:
        stereo = {b.GetIdx(): b.GetIntProp("_MolFileBondStereo") if b.HasProp("_MolFileBondStereo") else 0
                  for b in mol.GetBonds()}
    kek = Chem.Mol(mol)
    try:
        Chem.Kekulize(kek, clearAromaticFlags=True)
    except Exception:  # noqa: BLE001 - keep aromatic orders if kekulization fails
        pass
    conf = kek.GetConformer()
    atoms = [
        {
            "element": a.GetSymbol(),
            "x": round(conf.GetAtomPosition(a.GetIdx()).x, 4),
            "y": round(conf.GetAtomPosition(a.GetIdx()).y, 4),
            "charge": a.GetFormalCharge(),
            "isotope": a.GetIsotope() or None,
        }
        for a in kek.GetAtoms()
    ]
    bonds = [
        {
            "a": b.GetBeginAtomIdx(),
            "b": b.GetEndAtomIdx(),
            "order": 1.5 if b.GetBondType() == Chem.BondType.AROMATIC else int(b.GetBondTypeAsDouble()),
            "stereo": _WEDGE.get(stereo.get(b.GetIdx(), 0)),
        }
        for b in kek.GetBonds()
    ]
    return {
        "smiles": Chem.MolToSmiles(mol),
        "molfile": Chem.MolToMolBlock(mol),
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "svg": depict(mol, width, height),
        "atoms": atoms,
        "bonds": bonds,
    }
