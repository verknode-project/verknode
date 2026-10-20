"""The RDKit of the browser version: the part of RDKit's Python API that verknode uses, on RDKit's
WebAssembly build (MinimalLib, @rdkit/rdkit: the same C++ toolkit, compiled for JavaScript).

Only for JupyterLite: jupyterlab_verknode/lite.py loads MinimalLib into the Pyodide worker as
``verknodeRDKit`` and puts this folder on sys.path, so ``from rdkit import Chem`` finds it. On a Jupyter
server the real RDKit is used and this package is never imported.

Molecules are MinimalLib molecules; their pickles are RDKit's own (the columns of a workflow saved on a
server read here, and the other way round). Atoms, bonds and coordinates are read from the molecule's
JSON and MolBlock; edits (charges, removed atoms, positions) go through a MolBlock.
"""
from __future__ import annotations

import json
import math
import re

ELEMENTS = ("*", "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P", "S", "Cl",
            "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se",
            "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb",
            "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er",
            "Tm", "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At",
            "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No",
            "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og")


class NotInBrowser(NotImplementedError):
    """What RDKit's WebAssembly build cannot do (e.g. 3D embedding)."""


_module = None


def rdkit():
    """The MinimalLib module (loaded by lite.py)."""
    global _module
    if _module is None:
        import js

        _module = getattr(js, "verknodeRDKit", None)
        if _module is None:
            raise RuntimeError("RDKit (WebAssembly) is not loaded")
    return _module


def _js_bytes(data: bytes):
    from pyodide.ffi import to_js

    return to_js(memoryview(bytes(data)))


def _details(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v is not None})


# ---------------------------------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------------------------------
class _Enum(int):
    _names: dict = {}

    def __new__(cls, value, name):
        obj = int.__new__(cls, value)
        obj.name = name
        return obj

    def __repr__(self):
        return f"rdkit.Chem.rdchem.{type(self).__name__}.{self.name}"


class _BondType(_Enum):
    pass


class BondType:
    UNSPECIFIED = _BondType(0, "UNSPECIFIED")
    SINGLE = _BondType(1, "SINGLE")
    DOUBLE = _BondType(2, "DOUBLE")
    TRIPLE = _BondType(3, "TRIPLE")
    AROMATIC = _BondType(12, "AROMATIC")


class _BondDir(_Enum):
    pass


class BondDir:
    NONE = _BondDir(0, "NONE")
    BEGINWEDGE = _BondDir(1, "BEGINWEDGE")
    BEGINDASH = _BondDir(2, "BEGINDASH")
    UNKNOWN = _BondDir(5, "UNKNOWN")


class PropertyPickleOptions:
    NoProps = 0
    MolProps = 1
    AtomProps = 2
    BondProps = 4
    PrivateProps = 32
    ComputedProps = 64
    AllProps = 0x7FFFFFFF


# ---------------------------------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------------------------------
class Point3D:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __repr__(self):
        return f"Point3D({self.x}, {self.y}, {self.z})"


class Conformer:
    def __init__(self, mol: "Mol"):
        self._mol = mol

    def Is3D(self) -> bool:
        return self._mol._block()["is3d"]

    def GetNumAtoms(self) -> int:
        return len(self._mol._block()["atoms"])

    def GetPositions(self):
        import numpy as np

        return np.array([[a["x"], a["y"], a["z"]] for a in self._mol._block()["atoms"]], dtype=float).reshape(-1, 3)

    def GetAtomPosition(self, i: int) -> Point3D:
        a = self._mol._block()["atoms"][int(i)]
        return Point3D(a["x"], a["y"], a["z"])

    def SetAtomPosition(self, i: int, p):
        x, y, z = (p.x, p.y, p.z) if hasattr(p, "x") else tuple(p)
        self._mol._edit(lambda b: b["atoms"][int(i)].update(x=float(x), y=float(y), z=float(z)))


# ---------------------------------------------------------------------------------------------------
# atoms and bonds (views of the molecule's JSON)
# ---------------------------------------------------------------------------------------------------
class Atom:
    def __init__(self, mol: "Mol", idx: int):
        self._mol, self._idx = mol, idx

    def _j(self) -> dict:
        return self._mol._graph()["atoms"][self._idx]

    def GetIdx(self) -> int:
        return self._idx

    def GetAtomicNum(self) -> int:
        return self._j()["z"]

    def GetSymbol(self) -> str:
        z = self.GetAtomicNum()
        return ELEMENTS[z] if 0 <= z < len(ELEMENTS) else "*"

    def GetFormalCharge(self) -> int:
        return self._j()["chg"]

    def GetIsotope(self) -> int:
        return self._j()["isotope"]

    def GetNumRadicalElectrons(self) -> int:
        return self._j()["nRad"]

    def GetIsAromatic(self) -> bool:
        return self._idx in self._mol._graph()["aromatic_atoms"]

    def IsInRing(self) -> bool:
        return any(self._idx in r for r in self._mol._graph()["rings"])

    def GetNumImplicitHs(self) -> int:
        return self._j()["impHs"]

    def GetTotalNumHs(self, includeNeighbors: bool = False) -> int:
        n = self._j()["impHs"]
        if includeNeighbors:
            n += sum(1 for a in self.GetNeighbors() if a.GetAtomicNum() == 1)
        return n

    def GetNeighbors(self):
        return tuple(Atom(self._mol, j) for j in self._mol._graph()["neighbors"][self._idx])

    def GetDegree(self) -> int:
        return len(self._mol._graph()["neighbors"][self._idx])

    def GetBonds(self):
        return tuple(b for b in self._mol.GetBonds() if self._idx in (b.GetBeginAtomIdx(), b.GetEndAtomIdx()))


class Bond:
    def __init__(self, mol: "Mol", idx: int):
        self._mol, self._idx = mol, idx

    def _j(self) -> dict:
        return self._mol._graph()["bonds"][self._idx]

    def GetIdx(self) -> int:
        return self._idx

    def GetBeginAtomIdx(self) -> int:
        return self._j()["atoms"][0]

    def GetEndAtomIdx(self) -> int:
        return self._j()["atoms"][1]

    def GetBeginAtom(self) -> Atom:
        return Atom(self._mol, self.GetBeginAtomIdx())

    def GetEndAtom(self) -> Atom:
        return Atom(self._mol, self.GetEndAtomIdx())

    def GetOtherAtomIdx(self, i: int) -> int:
        a, b = self._j()["atoms"]
        return b if i == a else a

    def GetIsAromatic(self) -> bool:
        return self._idx in self._mol._graph()["aromatic_bonds"]

    def GetBondType(self):
        if self.GetIsAromatic():
            return BondType.AROMATIC
        return {1: BondType.SINGLE, 2: BondType.DOUBLE, 3: BondType.TRIPLE}.get(self._j()["bo"], BondType.UNSPECIFIED)

    def GetBondTypeAsDouble(self) -> float:
        return 1.5 if self.GetIsAromatic() else float(self._j()["bo"])

    def IsInRing(self) -> bool:
        a, b = self._j()["atoms"]
        return any(a in r and b in r for r in self._mol._graph()["rings"])

    def GetBondDir(self):
        # (the wedges RDKit writes into a MolBlock for the molecule's stereo centres)
        bonds = self._mol._block()["bonds"]
        st = bonds[self._idx]["stereo"] if self._idx < len(bonds) else 0
        return {1: BondDir.BEGINWEDGE, 6: BondDir.BEGINDASH, 4: BondDir.UNKNOWN}.get(st, BondDir.NONE)

    def HasProp(self, key: str) -> bool:
        return key == "_MolFileBondStereo" and self._mol._from_molblock is not None

    def GetIntProp(self, key: str) -> int:
        if not self.HasProp(key):
            raise KeyError(key)
        bonds = _parse_block(self._mol._from_molblock)["bonds"]
        return bonds[self._idx]["stereo"] if self._idx < len(bonds) else 0

    def GetProp(self, key: str) -> str:
        return str(self.GetIntProp(key))


# ---------------------------------------------------------------------------------------------------
# MolBlock (V2000) text: read and written by the edits
# ---------------------------------------------------------------------------------------------------
def _parse_block(text: str) -> dict:
    lines = text.split("\n")
    counts = lines[3] if len(lines) > 3 else ""
    if "V3000" in counts:
        raise NotInBrowser("V3000 MolBlocks are not edited in the browser")
    na, nb = int(counts[0:3] or 0), int(counts[3:6] or 0)
    atoms, bonds = [], []
    for k in range(na):
        ln = lines[4 + k]
        atoms.append({"x": float(ln[0:10]), "y": float(ln[10:20]), "z": float(ln[20:30]),
                      "symbol": ln[31:34].strip(), "rest": ln[34:]})
    for k in range(nb):
        ln = lines[4 + na + k]
        bonds.append({"a": int(ln[0:3]) - 1, "b": int(ln[3:6]) - 1, "order": int(ln[6:9]),
                      "stereo": int(ln[9:12] or 0), "rest": ln[12:]})
    props, end = [], len(lines)
    for k in range(4 + na + nb, len(lines)):
        if lines[k].startswith("M  END"):
            end = k
            break
        props.append(lines[k])
    charges = {}
    for ln in props:
        if ln.startswith("M  CHG"):
            vals = ln[9:].split()
            for i in range(0, len(vals) - 1, 2):
                charges[int(vals[i]) - 1] = int(vals[i + 1])
    for i, a in enumerate(atoms):
        a["charge"] = charges.get(i, 0)
    is3d = text.split("\n")[1][20:22] == "3D" if len(lines) > 1 else False
    if not is3d and any(abs(a["z"]) > 1e-4 for a in atoms):
        is3d = True
    return {"header": lines[:3], "atoms": atoms, "bonds": bonds, "props": props, "tail": lines[end:], "is3d": is3d}


def _write_block(b: dict) -> str:
    out = list(b["header"])
    out.append(f"{len(b['atoms']):3d}{len(b['bonds']):3d}  0  0  0  0  0  0  0  0999 V2000")
    for a in b["atoms"]:
        out.append(f"{a['x']:10.4f}{a['y']:10.4f}{a['z']:10.4f} {a['symbol']:<3}{a['rest']}")
    for bd in b["bonds"]:
        out.append(f"{bd['a'] + 1:3d}{bd['b'] + 1:3d}{bd['order']:3d}{bd['stereo']:3d}{bd['rest']}")
    keep = [ln for ln in b["props"] if not ln.startswith(("M  CHG", "M  ISO", "M  RAD"))]
    out += keep
    charged = [(i + 1, a["charge"]) for i, a in enumerate(b["atoms"]) if a.get("charge")]
    for k in range(0, len(charged), 8):
        chunk = charged[k:k + 8]
        out.append(f"M  CHG{len(chunk):3d}" + "".join(f" {i:3d} {c:3d}" for i, c in chunk))
    out += b["tail"] or ["M  END"]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------
# molecules
# ---------------------------------------------------------------------------------------------------
class Mol:
    """An RDKit molecule (a MinimalLib molecule underneath)."""

    def __init__(self, arg=None, *_, **__):
        self._from_molblock = None
        self._cache: dict = {}
        if arg is None:
            self._m = rdkit().get_mol("")
        elif isinstance(arg, Mol):
            self._m = rdkit().get_mol_copy(arg._m)
            self._from_molblock = arg._from_molblock
        elif isinstance(arg, (bytes, bytearray, memoryview)):
            self._m = rdkit().get_mol_from_uint8array(_js_bytes(arg))
            if self._m is None:
                raise ValueError("Not an RDKit molecule pickle")
        else:
            self._m = arg  # (a MinimalLib molecule)

    @classmethod
    def _wrap(cls, m, molblock: str | None = None) -> "Mol | None":
        if m is None:
            return None
        try:
            if not m.is_valid():
                m.delete()
                return None
        except Exception:  # noqa: BLE001
            return None
        out = cls(m)
        out._from_molblock = molblock
        return out

    def __del__(self):
        try:
            self._m.delete()
        except Exception:  # noqa: BLE001 - (already gone)
            pass

    def _changed(self):
        self._cache = {}

    def _replace(self, m):
        old, self._m = self._m, m
        try:
            old.delete()
        except Exception:  # noqa: BLE001
            pass
        self._changed()

    # -- the molecule's data, read lazily ---------------------------------------------------------------
    def _graph(self) -> dict:
        g = self._cache.get("graph")
        if g is None:
            doc = json.loads(self._m.get_json())
            dflt = doc.get("defaults", {})
            da, db = dflt.get("atom", {}), dflt.get("bond", {})
            mol = (doc.get("molecules") or [{}])[0]
            atoms = [{**{"z": 6, "impHs": 0, "chg": 0, "nRad": 0, "isotope": 0}, **da, **a} for a in mol.get("atoms", [])]
            bonds = [{**{"bo": 1}, **db, **b} for b in mol.get("bonds", [])]
            ext = next((e for e in mol.get("extensions", []) if e.get("name") == "rdkitRepresentation"), {})
            neighbors = [[] for _ in atoms]
            for b in bonds:
                i, j = b["atoms"]
                neighbors[i].append(j)
                neighbors[j].append(i)
            g = {"atoms": atoms, "bonds": bonds, "neighbors": neighbors, "rings": [set(r) for r in ext.get("atomRings", [])],
                 "aromatic_atoms": set(ext.get("aromaticAtoms", [])), "aromatic_bonds": set(ext.get("aromaticBonds", []))}
            self._cache["graph"] = g
        return g

    def _block(self) -> dict:
        b = self._cache.get("block")
        if b is None:
            b = _parse_block(self._m.get_molblock())
            self._cache["block"] = b
        return b

    def _edit(self, fn, sanitize: bool = True):
        """Change the molecule through its MolBlock (fn edits the parsed block in place)."""
        props = {k: self.GetProp(k) for k in self.GetPropNames(includePrivate=True)}
        b = _parse_block(self._m.get_molblock(_details(kekulize=True)))
        fn(b)
        m = rdkit().get_mol(_write_block(b), _details(sanitize=sanitize, removeHs=False))
        if m is None or not m.is_valid():
            raise ValueError("The edited molecule is not valid")
        self._replace(m)
        for k, v in props.items():
            self.SetProp(k, v)

    # -- RDKit's API ---------------------------------------------------------------------------------
    def GetNumAtoms(self, onlyExplicit: bool = True) -> int:
        n = self._m.get_num_atoms()
        if not onlyExplicit:
            n += sum(a["impHs"] for a in self._graph()["atoms"])
        return n

    def GetNumHeavyAtoms(self) -> int:
        return self._m.get_num_atoms(True)

    def GetNumBonds(self) -> int:
        return self._m.get_num_bonds()

    def GetAtoms(self):
        return tuple(Atom(self, i) for i in range(len(self._graph()["atoms"])))

    def GetAtomWithIdx(self, i: int) -> Atom:
        return Atom(self, int(i))

    def GetBonds(self):
        return tuple(Bond(self, i) for i in range(len(self._graph()["bonds"])))

    def GetBondWithIdx(self, i: int) -> Bond:
        return Bond(self, int(i))

    def GetBondBetweenAtoms(self, i: int, j: int):
        for k, b in enumerate(self._graph()["bonds"]):
            if set(b["atoms"]) == {i, j}:
                return Bond(self, k)
        return None

    def GetNumConformers(self) -> int:
        return 1 if self._m.has_coords() else 0

    def GetConformer(self, _id: int = -1) -> Conformer:
        if not self._m.has_coords():
            raise ValueError("Bad Conformer Id")
        return Conformer(self)

    def GetRingInfo(self):
        return _RingInfo(self)

    # properties
    def HasProp(self, key: str) -> bool:
        return bool(self._m.has_prop(key))

    def GetProp(self, key: str) -> str:
        if not self._m.has_prop(key):
            raise KeyError(key)
        return str(self._m.get_prop(key))

    def GetIntProp(self, key: str) -> int:
        return int(self.GetProp(key))

    def GetDoubleProp(self, key: str) -> float:
        return float(self.GetProp(key))

    def SetProp(self, key: str, value, computed: bool = False):
        self._m.set_prop(str(key), str(value), bool(computed))

    SetIntProp = SetDoubleProp = SetProp

    def ClearProp(self, key: str):
        self._m.clear_prop(key)

    def GetPropNames(self, includePrivate: bool = False, includeComputed: bool = False):
        lst = self._m.get_prop_list(bool(includePrivate), bool(includeComputed))
        try:
            return [lst.get(i) for i in range(lst.size())]
        finally:
            lst.delete()

    def GetPropsAsDict(self, includePrivate: bool = False, includeComputed: bool = False) -> dict:
        out = {}
        for k in self.GetPropNames(includePrivate, includeComputed):
            v = self.GetProp(k)
            for cast in (int, float):
                try:
                    v = cast(v)
                    break
                except ValueError:
                    continue
            out[k] = v
        return out

    # pickles (RDKit's own format)
    def ToBinary(self, flags: int = 0) -> bytes:
        f = {"AllProps": True} if flags == PropertyPickleOptions.AllProps else {
            "MolProps": bool(flags & 1), "AtomProps": bool(flags & 2), "BondProps": bool(flags & 4),
            "PrivateProps": bool(flags & 32), "ComputedProps": bool(flags & 64)}
        return bytes(self._m.get_as_uint8array(json.dumps({"propertyFlags": f})).to_py())

    def __getstate__(self):
        return {"pkl": self.ToBinary(PropertyPickleOptions.AllProps), "molblock": self._from_molblock}

    def __setstate__(self, state):
        self.__init__(state["pkl"])
        self._from_molblock = state.get("molblock")

    def __copy__(self):
        return Mol(self)

    def __deepcopy__(self, memo):
        return Mol(self)

    # substructures
    def GetSubstructMatches(self, query: "Mol", useChirality: bool = False, uniquify: bool = True,
                            maxMatches: int = 1000):
        res = json.loads(self._m.get_substruct_matches(query._m, _details(useChirality=bool(useChirality),
                                                                          uniquify=bool(uniquify),
                                                                          maxMatches=int(maxMatches))) or "[]")
        if isinstance(res, dict):
            res = [res] if res.get("atoms") else []
        return tuple(tuple(r.get("atoms", [])) for r in res)

    def GetSubstructMatch(self, query: "Mol", useChirality: bool = False):
        res = json.loads(self._m.get_substruct_match(query._m, _details(useChirality=bool(useChirality))) or "{}")
        return tuple(res.get("atoms", []))

    def HasSubstructMatch(self, query: "Mol", useChirality: bool = False) -> bool:
        return bool(self.GetSubstructMatch(query, useChirality))

    def UpdatePropertyCache(self, strict: bool = True):
        pass

    def ClearComputedProps(self, includeRings: bool = True):
        pass


class _RingInfo:
    def __init__(self, mol: Mol):
        self._rings = [tuple(sorted(r)) for r in mol._graph()["rings"]]

    def NumRings(self) -> int:
        return len(self._rings)

    def AtomRings(self):
        return tuple(self._rings)

    def NumAtomRings(self, i: int) -> int:
        return sum(1 for r in self._rings if i in r)


# ---------------------------------------------------------------------------------------------------
# bit vectors (fingerprints)
# ---------------------------------------------------------------------------------------------------
class ExplicitBitVect:
    def __init__(self, n: int | str = 0):
        if isinstance(n, str):  # (MinimalLib's bit string: bit 0 first)
            self._n = len(n)
            self._v = int(n[::-1], 2) if n else 0
        else:
            self._n, self._v = int(n), 0

    def GetNumBits(self) -> int:
        return self._n

    def __len__(self) -> int:
        return self._n

    def GetNumOnBits(self) -> int:
        return self._v.bit_count()

    def GetOnBits(self):
        v, out, i = self._v, [], 0
        while v:
            if v & 1:
                out.append(i)
            v >>= 1
            i += 1
        return tuple(out)

    def GetBit(self, i: int) -> bool:
        return bool(self._v >> int(i) & 1)

    def __getitem__(self, i: int) -> int:
        return int(self.GetBit(i))

    def SetBit(self, i: int):
        self._v |= 1 << int(i)

    def ToBitString(self) -> str:
        return "".join("1" if self._v >> i & 1 else "0" for i in range(self._n))


def tanimoto(a: ExplicitBitVect, b: ExplicitBitVect) -> float:
    c = (a._v & b._v).bit_count()
    t = a._v.bit_count() + b._v.bit_count() - c
    return c / t if t else 0.0


def dice(a: ExplicitBitVect, b: ExplicitBitVect) -> float:
    c = (a._v & b._v).bit_count()
    t = a._v.bit_count() + b._v.bit_count()
    return 2 * c / t if t else 0.0


def cosine(a: ExplicitBitVect, b: ExplicitBitVect) -> float:
    c = (a._v & b._v).bit_count()
    t = math.sqrt(a._v.bit_count() * b._v.bit_count())
    return c / t if t else 0.0


# ---------------------------------------------------------------------------------------------------
# reading and writing
# ---------------------------------------------------------------------------------------------------
def mol_from(text: str, sanitize: bool = True, removeHs: bool = True, molblock: bool = False) -> Mol | None:
    if text is None:
        return None
    m = rdkit().get_mol(str(text), _details(sanitize=bool(sanitize), removeHs=bool(removeHs)))
    mol = Mol._wrap(m, str(text) if molblock else None)
    if mol is not None and molblock:
        title = str(text).split("\n", 1)[0].strip()
        if title and not mol.HasProp("_Name"):
            mol.SetProp("_Name", title)
    return mol


def mol_to_block(mol: Mol) -> str:
    text = str(mol._m.get_molblock())
    name = mol.GetProp("_Name") if mol.HasProp("_Name") else ""
    lines = text.split("\n")
    if lines and lines[0].strip() != name:
        lines[0] = name
    return "\n".join(lines)


_SD_ITEM = re.compile(r"^>.*<([^>]+)>")


def sd_records(text: str):
    """(MolBlock, {property: value}) of each record of SD text (None for a record without "M  END")."""
    text = text.replace("\r\n", "\n")
    for rec in re.split(r"^\$\$\$\$[ \t]*\n?", text, flags=re.M):
        if not rec.strip():
            continue
        end = rec.find("M  END")
        if end < 0:
            yield None, {}
            continue
        props, key, vals = {}, None, []
        for ln in rec[end + 6:].split("\n"):
            m = _SD_ITEM.match(ln)
            if m:
                key, vals = m.group(1), []
            elif key is not None:
                if ln.strip() == "":
                    props[key], key = "\n".join(vals), None
                else:
                    vals.append(ln)
        if key is not None:
            props[key] = "\n".join(vals)
        yield rec[: end + 6], props
