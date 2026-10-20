"""rdkit.Chem.Draw.rdMolDraw2D in the browser version."""
import json


class _Options:
    def __init__(self):
        self.clearBackground = True
        self.padding = 0.05
        self.addStereoAnnotation = False
        self.bondLineWidth = 2


class MolDraw2DSVG:
    def __init__(self, width: int, height: int, *_):
        self._w, self._h, self._svg, self._opts = int(width), int(height), "", _Options()

    def drawOptions(self):
        return self._opts

    def DrawMolecule(self, mol, highlightAtoms=None, **_):
        o = self._opts
        details = {"width": self._w, "height": self._h, "clearBackground": bool(o.clearBackground),
                   "padding": float(o.padding), "addStereoAnnotation": bool(o.addStereoAnnotation)}
        if highlightAtoms:
            details["atoms"] = list(highlightAtoms)
        self._svg = str(mol._m.get_svg_with_highlights(json.dumps(details)))

    def FinishDrawing(self):
        pass

    def GetDrawingText(self) -> str:
        return self._svg


def PrepareAndDrawMolecule(drawer, mol, highlightAtoms=None, **_):
    drawer.DrawMolecule(mol, highlightAtoms=highlightAtoms)
