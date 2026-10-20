"""rdkit.Chem.Scaffolds.MurckoScaffold in the browser version: Bemis-Murcko scaffolds written out on the
molecule's graph (ring atoms and the atoms linking rings; atoms double-bonded to them stay, as in RDKit)."""
from ..._core import Mol, rdkit


def _scaffold_atoms(mol) -> set:
    g = mol._graph()
    n = len(g["atoms"])
    keep = set(range(n))
    nb = {i: set(g["neighbors"][i]) for i in range(n)}
    in_ring = {i for r in g["rings"] for i in r}
    changed = True
    while changed:  # prune chain ends until only rings and linkers stay
        changed = False
        for i in list(keep):
            if i in in_ring:
                continue
            if len(nb[i] & keep) <= 1:
                keep.discard(i)
                changed = True
    if not keep:
        return keep
    bo = {frozenset(b["atoms"]): b["bo"] for b in g["bonds"]}
    for i in range(n):  # atoms double-bonded to the scaffold
        if i not in keep and any(j in keep and bo.get(frozenset((i, j)), 1) == 2 for j in nb[i]) and len(nb[i]) == 1:
            keep.add(i)
    return keep


def _subset(mol, keep: set):
    if not keep:  # (no rings: an empty molecule, as RDKit's)
        return Mol(rdkit().get_mol(""))
    out = Mol(mol)
    drop = sorted(set(range(mol.GetNumAtoms())) - keep)
    if not drop:
        return out
    index = {old: new for new, old in enumerate(sorted(keep))}

    def edit(b):
        b["atoms"] = [a for i, a in enumerate(b["atoms"]) if i in keep]
        b["bonds"] = [{**bd, "a": index[bd["a"]], "b": index[bd["b"]]} for bd in b["bonds"]
                      if bd["a"] in keep and bd["b"] in keep]
        b["props"] = [ln for ln in b["props"] if not ln.startswith(("M  CHG", "M  ISO", "M  RAD"))]

    charges = {index[i]: mol.GetAtomWithIdx(i).GetFormalCharge() for i in keep}

    def edit_all(b):
        edit(b)
        for i, a in enumerate(b["atoms"]):
            a["charge"] = charges.get(i, 0)

    out._edit(edit_all)
    return out


def GetScaffoldForMol(mol):
    return _subset(mol, _scaffold_atoms(mol))


def MakeScaffoldGeneric(mol):
    out = Mol(mol)

    def edit(b):
        for a in b["atoms"]:
            a["symbol"], a["charge"] = "C", 0
        for bd in b["bonds"]:
            bd["order"], bd["stereo"] = 1, 0

    out._edit(edit)
    return out


def MurckoScaffoldSmiles(smiles: str = None, mol=None, includeChirality: bool = False):
    from rdkit import Chem

    m = mol if mol is not None else Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(GetScaffoldForMol(m), isomericSmiles=includeChirality)
