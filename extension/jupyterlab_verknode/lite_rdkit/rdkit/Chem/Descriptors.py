"""rdkit.Chem.Descriptors in the browser version: the descriptors RDKit's WebAssembly build computes (by
RDKit's own names), and QED. Others of the server's list are not available here."""
import json

_KEYS = {
    "MolWt": "amw", "ExactMolWt": "exactmw", "MolLogP": "CrippenClogP", "MolMR": "CrippenMR", "TPSA": "tpsa",
    "LabuteASA": "labuteASA", "NumHDonors": "NumHBD", "NumHAcceptors": "NumHBA", "NHOHCount": "lipinskiHBD",
    "NOCount": "lipinskiHBA", "NumRotatableBonds": "NumRotatableBonds", "NumHeteroatoms": "NumHeteroatoms",
    "NumAmideBonds": "NumAmideBonds", "FractionCSP3": "FractionCSP3", "RingCount": "NumRings",
    "NumAromaticRings": "NumAromaticRings", "NumAliphaticRings": "NumAliphaticRings",
    "NumSaturatedRings": "NumSaturatedRings", "NumHeterocycles": "NumHeterocycles",
    "NumAromaticHeterocycles": "NumAromaticHeterocycles", "NumSaturatedHeterocycles": "NumSaturatedHeterocycles",
    "NumAliphaticHeterocycles": "NumAliphaticHeterocycles", "NumSpiroAtoms": "NumSpiroAtoms",
    "NumBridgeheadAtoms": "NumBridgeheadAtoms", "NumAtomStereoCenters": "NumAtomStereoCenters",
    "NumUnspecifiedAtomStereoCenters": "NumUnspecifiedAtomStereoCenters", "HeavyAtomCount": "NumHeavyAtoms",
    "HallKierAlpha": "hallKierAlpha", "Kappa1": "kappa1", "Kappa2": "kappa2", "Kappa3": "kappa3", "Phi": "Phi",
    **{f"Chi{i}v": f"chi{i}v" for i in range(5)}, **{f"Chi{i}n": f"chi{i}n" for i in range(5)},
}


def _descriptor(key):
    def fn(mol, *_, **__):
        return json.loads(mol._m.get_descriptors())[key]

    return fn


def qed(mol):
    from . import QED

    return QED.qed(mol)


def NumRadicalElectrons(mol):
    return sum(a.GetNumRadicalElectrons() for a in mol.GetAtoms())


def NumValenceElectrons(mol):
    outer = {1: 1, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 14: 4, 15: 5, 16: 6, 17: 7, 35: 7, 53: 7}
    return sum(outer.get(a.GetAtomicNum(), 0) - a.GetFormalCharge() + a.GetNumImplicitHs() for a in mol.GetAtoms())


descList = sorted([(name, _descriptor(key)) for name, key in _KEYS.items()]
                  + [("qed", qed), ("NumRadicalElectrons", NumRadicalElectrons),
                     ("NumValenceElectrons", NumValenceElectrons)])
_descList = descList
globals().update({name: fn for name, fn in descList})


def CalcMolDescriptors(mol, missingVal=None, silent: bool = True) -> dict:
    out = {}
    for name, fn in descList:
        try:
            out[name] = fn(mol)
        except Exception:  # noqa: BLE001
            out[name] = missingVal
    return out
