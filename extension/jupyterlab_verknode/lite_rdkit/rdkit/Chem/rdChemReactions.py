"""rdkit.Chem.rdChemReactions in the browser version: reactions of RDKit's WebAssembly build."""
from .._core import Mol, rdkit


def _templates(side: str) -> int:
    """The number of templates of a reaction SMARTS side (dots between them, not in brackets or groups)."""
    depth, n, bracket = 0, 1 if side.strip() else 0, False
    for ch in side:
        if ch == "[":
            bracket = True
        elif ch == "]":
            bracket = False
        elif not bracket and ch == "(":
            depth += 1
        elif not bracket and ch == ")":
            depth -= 1
        elif ch == "." and depth == 0 and not bracket:
            n += 1
    return n


class ChemicalReaction:
    def __init__(self, rxn, smarts: str):
        self._r, self._smarts = rxn, smarts
        parts = smarts.split(">")
        self._lhs, self._rhs = parts[0], parts[-1] if len(parts) >= 3 else ""

    def __del__(self):
        try:
            self._r.delete()
        except Exception:  # noqa: BLE001
            pass

    def GetNumReactantTemplates(self) -> int:
        return _templates(self._lhs)

    def GetNumProductTemplates(self) -> int:
        return _templates(self._rhs)

    def RunReactants(self, reactants, maxProducts: int = 1000):
        lst = rdkit().MolList.new()
        try:
            for m in reactants:
                lst.append(m._m)
            res = self._r.run_reactants(lst, int(maxProducts))
        finally:
            lst.delete()
        out = []
        try:
            for i in range(res.size()):
                prods = res.get(i)
                row = []
                for j in range(prods.size()):
                    row.append(Mol(prods.at(j)))
                prods.delete()
                out.append(tuple(row))
        finally:
            res.delete()
        return tuple(out)


def ReactionFromSmarts(smarts: str, replacements=None, useSmiles: bool = False):
    r = rdkit().get_rxn(str(smarts or ""))
    if r is None:
        raise ValueError(f"Not a valid reaction SMARTS: {smarts}")
    return ChemicalReaction(r, str(smarts))
