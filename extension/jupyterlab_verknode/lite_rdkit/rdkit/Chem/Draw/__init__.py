"""rdkit.Chem.Draw in the browser version: SVG pictures from RDKit's WebAssembly build."""
from . import rdMolDraw2D  # noqa: F401


def MolToSVG(mol, width: int = 300, height: int = 300, **_) -> str:
    d = rdMolDraw2D.MolDraw2DSVG(width, height)
    rdMolDraw2D.PrepareAndDrawMolecule(d, mol)
    return d.GetDrawingText()
