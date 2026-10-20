"""rdkit.Chem in the browser version (see rdkit/_core.py)."""
from __future__ import annotations

from .._core import (Atom, Bond, BondDir, BondType, Conformer, Mol, NotInBrowser, PropertyPickleOptions,  # noqa: F401
                     _details, mol_from, mol_to_block, rdkit as _rdkit, sd_records)


def MolFromSmiles(smiles, sanitize: bool = True):
    return mol_from(smiles, sanitize=sanitize) if smiles is not None and str(smiles).strip() else None


def MolFromMolBlock(block, sanitize: bool = True, removeHs: bool = True, strictParsing: bool = True):
    if block is None or "M  END" not in str(block) and "V3000" not in str(block):
        return None
    return mol_from(block, sanitize=sanitize, removeHs=removeHs, molblock=True)


def MolFromSmarts(smarts, mergeHs: bool = False):
    if smarts is None:
        return None
    return Mol._wrap(_rdkit().get_qmol(str(smarts)))


def MolFromInchi(inchi, sanitize: bool = True, removeHs: bool = True):
    return mol_from(inchi, sanitize=sanitize, removeHs=removeHs) if inchi else None


def MolFromMol2Block(*_, **__):
    return None  # (no Mol2 reader in the browser)


def MolToSmiles(mol, isomericSmiles: bool = True, kekuleSmiles: bool = False, canonical: bool = True, **_):
    return str(mol._m.get_smiles(_details(doIsomericSmiles=bool(isomericSmiles), doKekule=bool(kekuleSmiles),
                                          canonical=bool(canonical))))


def MolToMolBlock(mol, includeStereo: bool = True, confId: int = -1, kekulize: bool = True, forceV3000: bool = False):
    if forceV3000:
        return str(mol._m.get_v3Kmolblock())
    return mol_to_block(mol)


def MolToSmarts(mol, isomericSmiles: bool = True):
    return str(mol._m.get_smarts())


def MolToInchi(mol, options: str = ""):
    return str(mol._m.get_inchi(options) if options else mol._m.get_inchi())


def MolToInchiKey(mol, options: str = ""):
    inchi = MolToInchi(mol, options)
    return str(_rdkit().get_inchikey_for_inchi(inchi)) if inchi else ""


def MolToMolFile(mol, path: str, **_):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(MolToMolBlock(mol))


def MolFromMolFile(path: str, sanitize: bool = True, removeHs: bool = True, strictParsing: bool = True):
    with open(path, encoding="utf-8") as fh:
        return MolFromMolBlock(fh.read(), sanitize, removeHs)


# --- hydrogens, aromaticity, sanitization -----------------------------------------------------------
def AddHs(mol, explicitOnly: bool = False, addCoords: bool = False):
    out = Mol(mol)
    out._m.add_hs_in_place()
    out._changed()
    return out


def RemoveHs(mol, implicitOnly: bool = False, updateExplicitCount: bool = False, sanitize: bool = True):
    out = Mol(mol)
    out._m.remove_hs_in_place()
    out._changed()
    return out


def Kekulize(mol, clearAromaticFlags: bool = False):
    mol._m.convert_to_kekule_form()
    mol._changed()


def SetAromaticity(mol, *_):
    mol._m.convert_to_aromatic_form()
    mol._changed()


def SanitizeMol(mol, sanitizeOps=None, catchErrors: bool = False):
    """Sanitize in place (raises ValueError if the molecule is not valid)."""
    props = {k: mol.GetProp(k) for k in mol.GetPropNames(includePrivate=True)}
    m = _rdkit().get_mol(str(mol._m.get_molblock()), _details(sanitize=True, removeHs=False))
    if m is None or not m.is_valid():
        raise ValueError("Sanitization failed")
    mol._replace(m)
    for k, v in props.items():
        mol.SetProp(k, v)
    return 0


def WedgeMolBonds(mol, conf=None):
    pass  # (the MolBlock writer wedges by itself)


def AssignStereochemistry(mol, cleanIt: bool = False, force: bool = False):
    pass


def GetFormalCharge(mol) -> int:
    return sum(a.GetFormalCharge() for a in mol.GetAtoms())


def GetMolFrags(mol, asMols: bool = False, sanitizeFrags: bool = True):
    res = mol._m.get_frags()
    try:
        import json

        mapping = json.loads(res.mappings)["fragsMolAtomMapping"]
        if not asMols:
            return tuple(tuple(f) for f in mapping)
        frags = res.molList
        out = []
        for i in range(frags.size()):
            out.append(Mol._wrap(frags.at(i)))
        return tuple(out)
    finally:
        try:
            res.molList.delete()
        except Exception:  # noqa: BLE001
            pass


def PatternFingerprint(mol, fpSize: int = 2048, **_):
    from .._core import ExplicitBitVect

    return ExplicitBitVect(str(mol._m.get_pattern_fp(_details(nBits=int(fpSize)))))


def CanonSmiles(smi: str, useChiral: int = 1) -> str:
    return MolToSmiles(MolFromSmiles(smi), isomericSmiles=bool(useChiral))


# --- SD files ----------------------------------------------------------------------------------------
class ForwardSDMolSupplier:
    """Molecules of an SD file (a file object, binary or text), with their SD properties."""

    def __init__(self, fileobj, sanitize: bool = True, removeHs: bool = True, strictParsing: bool = True):
        data = fileobj.read() if hasattr(fileobj, "read") else fileobj
        self._text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
        self._sanitize, self._remove_hs = sanitize, removeHs

    def __iter__(self):
        for block, props in sd_records(self._text):
            mol = None if block is None else MolFromMolBlock(block, self._sanitize, self._remove_hs)
            if mol is not None:
                for k, v in props.items():
                    mol.SetProp(k, v)
            yield mol


class SDMolSupplier(ForwardSDMolSupplier):
    def __init__(self, path: str, sanitize: bool = True, removeHs: bool = True, strictParsing: bool = True):
        with open(path, "rb") as fh:
            super().__init__(fh, sanitize, removeHs, strictParsing)
        self._mols = list(super().__iter__())

    def __iter__(self):
        return iter(self._mols)

    def __len__(self):
        return len(self._mols)

    def __getitem__(self, i):
        return self._mols[i]


class SDWriter:
    def __init__(self, target):
        self._own = isinstance(target, str)
        self._fh = open(target, "w", encoding="utf-8") if self._own else target
        self._props = None

    def SetProps(self, names):
        self._props = list(names)

    def write(self, mol, confId: int = -1):
        text = [MolToMolBlock(mol).rstrip("\n"), ""]
        for k in self._props if self._props is not None else mol.GetPropNames():
            if mol.HasProp(k):
                text += [f">  <{k}>", mol.GetProp(k), ""]
        text.append("$$$$\n")
        self._fh.write("\n".join(text))

    def close(self):
        if self._own:
            self._fh.close()

    def flush(self):
        self._fh.flush()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SmilesWriter:
    def __init__(self, path: str, delimiter: str = " ", includeHeader: bool = False, **_):
        self._fh, self._sep = open(path, "w", encoding="utf-8"), delimiter

    def write(self, mol):
        name = mol.GetProp("_Name") if mol.HasProp("_Name") else ""
        self._fh.write(f"{MolToSmiles(mol)}{self._sep}{name}\n")

    def close(self):
        self._fh.close()


from . import rdMolDescriptors  # noqa: E402,F401 - (Chem.rdMolDescriptors.CalcMolFormula)
from . import rdDepictor  # noqa: E402,F401
from . import Descriptors  # noqa: E402,F401
from . import AllChem  # noqa: E402,F401
from . import Draw  # noqa: E402,F401
from . import rdChemReactions  # noqa: E402,F401
from . import rdFingerprintGenerator  # noqa: E402,F401
