"""rdkit.Chem.AllChem in the browser version: rdkit.Chem, without 3D (RDKit's WebAssembly build cannot embed
molecules in 3D: EmbedMolecule fails, the force fields are not there)."""
from rdkit.Chem import *  # noqa: F401,F403
from rdkit.Chem.rdDepictor import Compute2DCoords  # noqa: F401
from rdkit.Chem.rdChemReactions import ReactionFromSmarts  # noqa: F401

from .._core import NotInBrowser


class EmbedParameters:
    def __init__(self):
        self.randomSeed = -1
        self.useRandomCoords = False


def ETKDGv3():
    return EmbedParameters()


ETKDGv2 = ETKDG = ETKDGv3


def EmbedMolecule(mol, *_, **__) -> int:
    return -1  # (no 3D coordinates in the browser)


def EmbedMultipleConfs(mol, *_, **__):
    return []


def MMFFHasAllMoleculeParams(mol) -> bool:
    return False


def MMFFOptimizeMolecule(mol, *_, **__):
    raise NotInBrowser("Force fields are not available in the browser version")


UFFOptimizeMolecule = MMFFOptimizeMolecule


def GetMorganFingerprintAsBitVect(mol, radius: int = 2, nBits: int = 2048, **_):
    from .rdFingerprintGenerator import GetMorganGenerator

    return GetMorganGenerator(radius=radius, fpSize=nBits).GetFingerprint(mol)
