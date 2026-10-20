"""rdkit.Chem.rdDepictor in the browser version."""
from .._core import rdkit

_prefer = {"coordgen": False}


def SetPreferCoordGen(prefer: bool):
    _prefer["coordgen"] = bool(prefer)
    rdkit().prefer_coordgen(bool(prefer))


def GetPreferCoordGen() -> bool:
    return _prefer["coordgen"]


def Compute2DCoords(mol, canonOrient: bool = True, clearConfs: bool = True, **_) -> int:
    mol._m.set_new_coords(_prefer["coordgen"])
    mol._changed()
    return 0


def StraightenDepiction(mol, *_):
    mol._m.straighten_depiction()
    mol._changed()
