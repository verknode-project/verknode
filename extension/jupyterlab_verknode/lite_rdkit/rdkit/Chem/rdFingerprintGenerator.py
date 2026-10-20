"""rdkit.Chem.rdFingerprintGenerator in the browser version: the fingerprints of RDKit's WebAssembly build."""
from .._core import ExplicitBitVect, NotInBrowser, _details


class _Generator:
    def __init__(self, method: str, **details):
        self._method, self._details = method, _details(**details)

    def GetFingerprint(self, mol):
        return ExplicitBitVect(str(getattr(mol._m, self._method)(self._details)))


def GetMorganGenerator(radius: int = 3, fpSize: int = 2048, atomInvariantsGenerator=None, includeChirality: bool = False,
                       **_):
    if atomInvariantsGenerator is not None:
        raise NotInBrowser("Feature Morgan fingerprints are not available in the browser version: use Morgan")
    return _Generator("get_morgan_fp", radius=int(radius), nBits=int(fpSize), useChirality=bool(includeChirality))


def GetMorganFeatureAtomInvGen(*_):
    return "feature invariants"


def GetRDKitFPGenerator(minPath: int = 1, maxPath: int = 7, fpSize: int = 2048, **_):
    return _Generator("get_rdkit_fp", minPath=int(minPath), maxPath=int(maxPath), nBits=int(fpSize))


def GetAtomPairGenerator(minDistance: int = 1, maxDistance: int = 30, fpSize: int = 2048, **_):
    return _Generator("get_atom_pair_fp", minLength=int(minDistance), maxLength=int(maxDistance), nBits=int(fpSize))


def GetTopologicalTorsionGenerator(torsionAtomCount: int = 4, fpSize: int = 2048, **_):
    return _Generator("get_topological_torsion_fp", torsionAtomCount=int(torsionAtomCount), nBits=int(fpSize))
