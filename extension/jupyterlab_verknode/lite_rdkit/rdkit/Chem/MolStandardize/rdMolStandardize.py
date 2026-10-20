"""rdkit.Chem.MolStandardize.rdMolStandardize in the browser version, on RDKit's WebAssembly build: the
steps verknode's Structure Normalizer uses, written out (cleanup: hydrogens and metal bonds; the largest
fragment; neutralization). Canonical tautomers are not available here."""
import json

from ..._core import Mol, NotInBrowser

METALS = {3, 4, 11, 12, 13, 19, 20, *range(21, 31), 37, 38, *range(39, 49), 55, 56, *range(57, 81), 81, 82, 83}
NONMETALS = {7, 8, 9, 15, 16, 17, 34, 35, 53}


def Cleanup(mol, params=None):
    """Removes explicit hydrogens and disconnects metals from N, O, halogens, S, P, Se (charges set)."""
    out = Mol(mol)
    out._m.remove_hs_in_place()
    out._changed()
    cut = [k for k, b in enumerate(out._graph()["bonds"])
           if {out._graph()["atoms"][i]["z"] in METALS for i in b["atoms"]} == {True, False}
           and any(out._graph()["atoms"][i]["z"] in NONMETALS for i in b["atoms"])]
    if not cut:
        return out
    atoms = out._graph()["atoms"]
    pairs = [out._graph()["bonds"][k]["atoms"] for k in cut]

    def edit(block):
        for k in sorted(cut, reverse=True):
            del block["bonds"][k]
        for i, j in pairs:
            metal, other = (i, j) if atoms[i]["z"] in METALS else (j, i)
            block["atoms"][metal]["charge"] += 1
            block["atoms"][other]["charge"] -= 1
            for k in (metal, other):  # (no valence of their own: else hydrogens fill the cut bond)
                rest = block["atoms"][k]["rest"].ljust(17)
                block["atoms"][k]["rest"] = rest[:14] + "  0" + rest[17:]

    out._edit(edit)
    return out


def _size(m) -> tuple:
    d = json.loads(m.get_descriptors())
    return (d["NumAtoms"], d["amw"])


class LargestFragmentChooser:
    """The fragment with the most atoms (hydrogens included), then the heaviest, then the first SMILES."""

    def __init__(self, preferOrganic: bool = False):
        self.preferOrganic = preferOrganic

    def choose(self, mol):
        res = mol._m.get_frags()
        frags = res.molList
        best, key = None, None
        try:
            for i in range(frags.size()):
                f = frags.at(i)
                k = (_size(f), str(f.get_smiles()))
                k = (k[0][0], k[0][1], [-ord(c) for c in k[1]])
                if key is None or k > key:
                    if best is not None:
                        best.delete()
                    best, key = f, k
                else:
                    f.delete()
        finally:
            frags.delete()
        out = Mol(best)
        for name in mol.GetPropNames():
            out.SetProp(name, mol.GetProp(name))
        return out


class Uncharger:
    """Neutralizes charged atoms by adding or removing hydrogens, keeping charge-separated groups (e.g.
    nitro) and charges that cannot be removed (quaternary nitrogen, with a counter ion if it needs one)."""

    def __init__(self, canonicalOrder: bool = True, **_):
        pass

    def uncharge(self, mol):
        out = Mol(mol)
        g = out._graph()
        atoms, nb = g["atoms"], g["neighbors"]
        fixable_pos = [i for i, a in enumerate(atoms) if a["chg"] > 0 and a["impHs"] > 0
                       and not any(atoms[j]["chg"] < 0 for j in nb[i])]
        neg = [i for i, a in enumerate(atoms) if a["chg"] < 0 and not any(atoms[j]["chg"] > 0 for j in nb[i])]
        stuck = sum(a["chg"] for i, a in enumerate(atoms) if a["chg"] > 0 and i not in fixable_pos
                    and not any(atoms[j]["chg"] < 0 for j in nb[i]))
        neg = neg[stuck:] if stuck > 0 else neg  # (keep counter ions of charges that stay)
        change = fixable_pos + neg
        if not change:
            return out
        out._edit(lambda b: [b["atoms"][i].update(charge=0) for i in change])
        return out


class TautomerEnumerator:
    def __init__(self, *_):
        pass

    def Canonicalize(self, mol):
        raise NotInBrowser("Canonical tautomers are not available in the browser version")


def ChargeParent(mol, *_):
    return Uncharger().uncharge(LargestFragmentChooser().choose(Cleanup(mol)))


def FragmentParent(mol, *_):
    return LargestFragmentChooser().choose(Cleanup(mol))

