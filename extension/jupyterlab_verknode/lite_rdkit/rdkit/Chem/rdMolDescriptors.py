"""rdkit.Chem.rdMolDescriptors in the browser version: the descriptors of RDKit's WebAssembly build."""
import json

from .._core import ExplicitBitVect


def _d(mol) -> dict:
    return json.loads(mol._m.get_descriptors())


def CalcMolFormula(mol, separateIsotopes: bool = False, abbreviateHIsotopes: bool = True) -> str:
    counts: dict = {}
    for a in mol.GetAtoms():
        counts[a.GetSymbol()] = counts.get(a.GetSymbol(), 0) + 1
        if a.GetNumImplicitHs():
            counts["H"] = counts.get("H", 0) + a.GetNumImplicitHs()
    order = (["C", "H"] if "C" in counts else []) + sorted(k for k in counts if k not in (("C", "H") if "C" in counts else ()))
    out = "".join(f"{k}{counts[k] if counts[k] > 1 else ''}" for k in order if k in counts)
    q = sum(a.GetFormalCharge() for a in mol.GetAtoms())
    if q:
        out += ("+" if q > 0 else "-") + (str(abs(q)) if abs(q) > 1 else "")
    return out


def GetMACCSKeysFingerprint(mol):
    return ExplicitBitVect(str(mol._m.get_maccs_fp()))


def _desc(key):
    return lambda mol, *_, **__: _d(mol)[key]


CalcExactMolWt = _desc("exactmw")
_CalcMolWt = _desc("amw")
CalcNumHBD = _desc("NumHBD")
CalcNumHBA = _desc("NumHBA")
CalcNumLipinskiHBD = _desc("lipinskiHBD")
CalcNumLipinskiHBA = _desc("lipinskiHBA")
CalcNumRotatableBonds = _desc("NumRotatableBonds")
CalcNumHeteroatoms = _desc("NumHeteroatoms")
CalcNumAmideBonds = _desc("NumAmideBonds")
CalcFractionCSP3 = _desc("FractionCSP3")
CalcNumRings = _desc("NumRings")
CalcNumAromaticRings = _desc("NumAromaticRings")
CalcNumAliphaticRings = _desc("NumAliphaticRings")
CalcNumSaturatedRings = _desc("NumSaturatedRings")
CalcNumHeterocycles = _desc("NumHeterocycles")
CalcNumAromaticHeterocycles = _desc("NumAromaticHeterocycles")
CalcNumSaturatedHeterocycles = _desc("NumSaturatedHeterocycles")
CalcNumAliphaticHeterocycles = _desc("NumAliphaticHeterocycles")
CalcNumSpiroAtoms = _desc("NumSpiroAtoms")
CalcNumBridgeheadAtoms = _desc("NumBridgeheadAtoms")
CalcNumAtomStereoCenters = _desc("NumAtomStereoCenters")
CalcNumUnspecifiedAtomStereoCenters = _desc("NumUnspecifiedAtomStereoCenters")
CalcNumHeavyAtoms = _desc("NumHeavyAtoms")
CalcNumAtoms = _desc("NumAtoms")
CalcLabuteASA = _desc("labuteASA")
CalcTPSA = _desc("tpsa")
CalcCrippenDescriptors = lambda mol, *_, **__: (_d(mol)["CrippenClogP"], _d(mol)["CrippenMR"])  # noqa: E731
CalcHallKierAlpha = _desc("hallKierAlpha")
CalcKappa1, CalcKappa2, CalcKappa3 = _desc("kappa1"), _desc("kappa2"), _desc("kappa3")
CalcPhi = _desc("Phi")
CalcChi0v, CalcChi1v, CalcChi2v, CalcChi3v, CalcChi4v = (_desc(f"chi{i}v") for i in range(5))
CalcChi0n, CalcChi1n, CalcChi2n, CalcChi3n, CalcChi4n = (_desc(f"chi{i}n") for i in range(5))


class NumRotatableBondsOptions:
    Default, NonStrict, Strict, StrictLinkages = -1, 0, 1, 2
