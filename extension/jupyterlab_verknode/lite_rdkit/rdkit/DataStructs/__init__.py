"""rdkit.DataStructs in the browser version: bit vectors and their similarities."""
from .._core import ExplicitBitVect, cosine, dice, tanimoto  # noqa: F401


def TanimotoSimilarity(a, b) -> float:
    return tanimoto(a, b)


def DiceSimilarity(a, b) -> float:
    return dice(a, b)


def CosineSimilarity(a, b) -> float:
    return cosine(a, b)


def BulkTanimotoSimilarity(fp, fps, returnDistance: bool = False):
    return [1 - tanimoto(fp, f) if returnDistance else tanimoto(fp, f) for f in fps]


def BulkDiceSimilarity(fp, fps, returnDistance: bool = False):
    return [1 - dice(fp, f) if returnDistance else dice(fp, f) for f in fps]


def BulkCosineSimilarity(fp, fps, returnDistance: bool = False):
    return [1 - cosine(fp, f) if returnDistance else cosine(fp, f) for f in fps]


def CreateFromBitString(bits: str):
    return ExplicitBitVect(bits)
