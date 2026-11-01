"""Cheminformatics nodes based on RDKit."""
from __future__ import annotations

import gzip
import os

import polars as pl
from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors, rdChemReactions, rdDepictor
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.SimDivFilters import rdSimDivPickers

from verknode.node import chem
from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, dtype_kind, require_column
from .io import PATH_HELP, _existing

CAT = "Chemistry/RDKit"
MOL_KINDS = ["molecule", "string"]
MOL_HELP = "RDKit molecule column, or a String column with SMILES / MolBlock (SDF) text."
DESCRIPTORS = dict(Descriptors.descList)
DEFAULT_DESCRIPTORS = ["MolWt", "MolLogP", "TPSA", "NumHDonors", "NumHAcceptors",
                       "NumRotatableBonds", "RingCount", "HeavyAtomCount", "FractionCSP3", "qed"]


def mol_column_field(label="Molecule column"):
    return F("column", "column", label, None, MOL_HELP, kinds=MOL_KINDS)


def output_fields(default_name):
    return [
        F("mode", "enum", "Output", "Append column", options=["Append column", "Replace input column"]),
        F("new_column", "string", "New column name", default_name, show_if={"mode": "Append column"}),
    ]


def fp_fields():
    return [
        F("fp_type", "enum", "Fingerprint type", "Morgan", options=chem.FINGERPRINTS),
        F("radius", "int", "Radius", 2, "Morgan / FeatMorgan only.", show_if={"fp_type": ["Morgan", "FeatMorgan"]}),
        F("n_bits", "int", "Number of bits", 2048, show_if={"fp_type": [t for t in chem.FINGERPRINTS if t != "MACCS"]}),
    ]


def take_rows(df: pl.DataFrame, rows: list) -> pl.DataFrame:
    return df.select(pl.all().gather(pl.Series(rows, dtype=pl.UInt32)))


def read_mols(df: pl.DataFrame, s: dict, key="column"):
    name = require_column(s, key, df.schema, "molecule column")
    return name, chem.to_mols(df[name])


def put_column(df: pl.DataFrame, s: dict, source: str, series: pl.Series) -> pl.DataFrame:
    """Append (named by new_column) or replace the source column."""
    if s.get("mode") == "Replace input column":
        return df.with_columns(series.alias(source))
    name = s.get("new_column") or series.name
    if name in df.columns:
        raise InvalidSettingsError(f'Column "{name}" already exists')
    return df.with_columns(series.alias(name))


def count_missing(ctx, mols, what="molecules"):
    n = sum(m is None for m in mols)
    if n:
        ctx.warn(f"{n} row(s) with missing or invalid {what}")


def _infer_columns(columns: dict) -> dict:
    """String property columns -> Int64 / Float64 where all values parse."""
    out = {}
    for name, values in columns.items():
        s = pl.Series(name, values, dtype=pl.String)
        for dtype in (pl.Int64, pl.Float64):
            cast = s.str.strip_chars().cast(dtype, strict=False)
            if cast.null_count() == s.null_count() and s.len() > s.null_count():
                s = cast
                break
        out[name] = s
    return out


# ----------------------------------------------------------------------
# IO
# ----------------------------------------------------------------------
class SDFReader(NodeModel):
    factory = "verknode.chem.SDFReader"
    name = "SDF Reader"
    category = f"{CAT}/IO"
    node_type = "Source"
    icon = "SDF"
    short_description = "Reads molecules and properties from an SD file."
    description = (
        "Reads an SD file (.sdf, .sdf.gz). Each record becomes a row with an RDKit molecule column, "
        "the molecule name and one column per SD property (numeric properties are detected)."
    )
    out_ports = [Port(TABLE, "Molecules", "Molecules and SD properties")]
    fields = [
        F("path", "string", "File", "", PATH_HELP, path=True, extensions=[".sdf", ".sd", ".gz"]),
        F("molecule_column", "string", "Molecule column name", "Molecule"),
        F("include_name", "bool", "Add molecule name column", True),
        F("read_properties", "bool", "Read SD properties", True),
        F("sanitize", "bool", "Sanitize molecules", True),
        F("remove_hs", "bool", "Remove hydrogens", True),
        F("limit", "int", "Maximum records (0 = all)", 0),
    ]

    def _read(self, s, ctx, limit):
        path = _existing(ctx, s["path"])
        opener = gzip.open if path.endswith(".gz") else open
        mols, names, props, failed = [], [], {}, 0
        with opener(path, "rb") as fh:
            for i, mol in enumerate(Chem.ForwardSDMolSupplier(fh, sanitize=s["sanitize"], removeHs=s["remove_hs"])):
                if limit and i >= limit:
                    break
                ctx.check_canceled()
                mols.append(mol)
                if mol is None:
                    failed += 1
                    names.append(None)
                else:
                    names.append(mol.GetProp("_Name") if mol.HasProp("_Name") else None)
                if s["read_properties"]:
                    keys = list(mol.GetPropNames()) if mol is not None else []
                    for k in keys:
                        props.setdefault(k, [None] * (len(mols) - 1))
                    for k, col in props.items():
                        col.append(mol.GetProp(k) if mol is not None and mol.HasProp(k) else None)
        if failed:
            ctx.warn(f"{failed} record(s) could not be parsed")
        mol_name = s["molecule_column"] or "Molecule"
        cols = {mol_name: chem.mol_series(mol_name, mols)}
        if s["include_name"]:
            cols["Name"] = pl.Series("Name", names, dtype=pl.String)
        for name, series in _infer_columns(props).items():
            cols[name if name not in cols else f"{name} (SD)"] = series
        return pl.DataFrame(cols)

    def configure(self, in_specs, s, ctx):
        return [self._read(s, ctx, 200).schema]

    def execute(self, inputs, s, ctx):
        df = self._read(s, ctx, max(0, s["limit"]))
        ctx.log(f"Read {df.height} molecules")
        return [df]


class MoleculeSketcher(NodeModel):
    factory = "verknode.chem.Sketcher"
    name = "Molecule Sketcher"
    category = f"{CAT}/IO"
    node_type = "Source"
    icon = "✎"
    short_description = "Draws molecules, or edits the molecules of the input table (Ketcher or the built-in sketcher)."
    description = (
        "Without an input: a table of sketches drawn or pasted (SMILES / MolBlock) in the "
        "dialog, one row per structure with an Index and a Name. "
        "With a table connected: every input row is edited - the dialog lists the input "
        "molecules (after the upstream node has run), each can be edited in the sketcher or "
        "renamed and reset to the original; the output has exactly the input rows, edited or "
        "not, and the other input columns can be kept. "
        "Structures are drawn with Ketcher (EPAM) or with the "
        "built-in sketcher: fixed bond length, 120° chains, "
        "hover hotkeys (C N O S P F l=Cl b=Br I H to set the element of the atom under the "
        "pointer, 1 2 3 for the bond order, + - for charges, Delete to remove), click a bond "
        "to cycle single → double → triple, ring templates fused onto atoms and bonds. "
        "Edited structures are stored as MolBlocks in the node settings."
    )
    in_ports = [Port(TABLE, "Molecules to edit (optional)", optional=True)]
    out_ports = [Port(TABLE, "Sketched molecules")]
    fields = [
        F("column", "column", "Input molecule column", None,
          "Molecules of the input table (first molecule or text column if not set).", kinds=MOL_KINDS),
        F("name_column", "column", "Input name column", None, "Names of the input molecules (optional).",
          kinds=["string"]),
        F("molecules", "sketcher", "Sketches", [],
          "Drawn sketches (name and MolBlock or SMILES), or with an input table the edits of its "
          "rows (\"row\": input row from 0).",
          depends={"column": "structure", "name_column": "name"}),
        F("keep_columns", "bool", "Keep the other input columns", True),
        F("smiles", "bool", "Add SMILES column", True),
        F("molblock", "bool", "Add MolBlock column", False),
    ]
    #: input molecules editable in the dialog
    DIALOG_ROWS = 2000

    def validate(self, s, in_specs):
        if not s.get("molecules") and (not in_specs or in_specs[0] is None):
            raise InvalidSettingsError("Draw at least one molecule or connect a table of molecules")

    @staticmethod
    def _input_column(df: pl.DataFrame, s: dict) -> str:
        if s.get("column"):
            return require_column(s, "column", df.schema, "molecule column")
        kinds = {name: dtype_kind(dtype) for name, dtype in df.schema.items()}
        for kind in ("molecule", "string"):
            for name, k in kinds.items():
                if k == kind:
                    return name
        raise InvalidSettingsError("The input table has no molecule or text column")

    def _input(self, df, s):
        col = self._input_column(df, s)
        mols = chem.to_mols(df[col])
        name_col = s.get("name_column")
        names = df[name_col].cast(pl.String).to_list() if name_col and name_col in df.columns else [None] * len(df)
        return col, mols, names

    def dialog_data(self, inputs, s, connected=(), context=None):
        df = inputs[0] if inputs else None
        if df is None:
            return {"connected": bool(connected and connected[0]), "rows": [], "total": 0}
        col, mols, names = self._input(df, s)
        rows = []
        for i, m in enumerate(mols[: self.DIALOG_ROWS]):
            if m is not None and (m.GetNumConformers() == 0 or m.GetConformer().Is3D()):
                m = Chem.Mol(m)
                rdDepictor.Compute2DCoords(m)
            rows.append({"row": i, "name": names[i], "molfile": Chem.MolToMolBlock(m) if m is not None else None})
        name_col = s.get("name_column") if s.get("name_column") in df.columns else None
        return {"connected": True, "column": col, "nameColumn": name_col, "rows": rows, "total": len(df)}

    def execute(self, inputs, s, ctx):
        df = inputs[0] if inputs else None
        entries = s.get("molecules") or []
        edits = {int(e["row"]): e for e in entries if e.get("row") is not None}
        extras = [e for e in entries if e.get("row") is None]

        def parse(e, label):
            mol = chem.parse(e.get("molfile") or e.get("smiles"))
            if mol is None:
                raise ValueError(f"{label} is not a valid structure")
            return mol

        index, names, mols, source = [], [], [], []
        if df is not None:
            col, in_mols, in_names = self._input(df, s)
            for i, m in enumerate(in_mols):
                e = edits.get(i)
                index.append(i + 1)
                names.append((e or {}).get("name") or in_names[i] or f"Row {i + 1}")
                # an edit without a structure only renames the row
                mols.append(parse(e, names[-1]) if e and (e.get("molfile") or e.get("smiles")) else m)
                source.append(i)
            count_missing(ctx, mols)
        start = len(index)
        # sketches of their own only without an input (with one, its rows are edited)
        for k, e in enumerate(extras if df is None else []):
            index.append(start + k + 1)
            names.append(e.get("name") or f"Molecule {start + k + 1}")
            mols.append(parse(e, names[-1]))
            source.append(None)
        cols = [pl.Series("Index", index, dtype=pl.Int64), pl.Series("Name", names, dtype=pl.String),
                chem.mol_series("Molecule", mols)]
        if s["smiles"]:
            cols.append(pl.Series("SMILES", [Chem.MolToSmiles(m) if m is not None else None for m in mols],
                                  dtype=pl.String))
        if s["molblock"]:
            cols.append(pl.Series("MolBlock", [Chem.MolToMolBlock(m) if m is not None else None for m in mols],
                                  dtype=pl.String))
        out = pl.DataFrame(cols)
        if df is not None and s["keep_columns"]:
            ours = set(out.columns) | {col, s.get("name_column")}
            others = [c for c in df.columns if c not in ours]
            if others:
                kept = df.select(others).with_row_index("__row")
                out = (out.with_columns(pl.Series("__row", source, dtype=pl.UInt32))
                       .join(kept, on="__row", how="left", maintain_order="left").drop("__row"))
        return [out]


class SDFWriter(NodeModel):
    factory = "verknode.chem.SDFWriter"
    name = "SDF Writer"
    category = f"{CAT}/IO"
    node_type = "Sink"
    icon = "SDF"
    short_description = "Writes molecules and properties to an SD file."
    description = "Writes the molecule column to an SD file; selected columns are written as SD properties."
    in_ports = [Port(TABLE, "Molecules")]
    fields = [
        F("path", "string", "File", "", PATH_HELP, path="save", extensions=[".sdf", ".sd", ".gz"]),
        mol_column_field(),
        F("name_column", "column", "Molecule name column (optional)", None),
        F("properties", "columns", "Property columns", {"include": [], "exclude": [], "enforce": "exclusion"}),
        F("compute_2d", "bool", "Compute 2D coordinates if missing", True),
        F("if_exists", "enum", "If file exists", "Overwrite", options=["Overwrite", "Fail"]),
    ]

    def configure(self, in_specs, s, ctx):
        ctx.resolve_path(s["path"])
        require_column(s, "column", in_specs[0], "molecule column")
        return []

    def execute(self, inputs, s, ctx):
        from .manipulation import selected_columns

        df = inputs[0]
        col, mols = read_mols(df, s)
        path = ctx.resolve_path(s["path"])
        if os.path.exists(path) and s["if_exists"] == "Fail":
            raise InvalidSettingsError(f"File exists and must not be overwritten: {path}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        props = [c for c in selected_columns(s["properties"], df.schema)
                 if c != col and not chem.is_mol_dtype(df.schema[c])]
        names = df[s["name_column"]].cast(pl.String).to_list() if s.get("name_column") in df.columns else None
        rows = df.select(props).iter_rows() if props else iter([()] * df.height)
        written = 0
        with Chem.SDWriter(path) as w:
            for i, (mol, values) in enumerate(zip(mols, rows)):
                if mol is None:
                    continue
                mol = Chem.Mol(mol)
                if s["compute_2d"] and mol.GetNumConformers() == 0:
                    rdDepictor.Compute2DCoords(mol)
                if names is not None:
                    mol.SetProp("_Name", names[i] or "")
                for k, v in zip(props, values):
                    if v is not None:
                        mol.SetProp(k, str(v))
                w.write(mol)
                written += 1
        count_missing(ctx, mols)
        ctx.log(f"Wrote {written} molecules to {path}")
        return []


# ----------------------------------------------------------------------
# Converters
# ----------------------------------------------------------------------
class FromMolecule(NodeModel):
    factory = "verknode.chem.FromMolecule"
    name = "RDKit From Molecule"
    category = f"{CAT}/Converters"
    icon = "→M"
    short_description = "Converts SMILES, SDF, SMARTS or InChI text into RDKit molecules."
    description = (
        "Parses a String column into an RDKit molecule column. Rows that cannot be parsed go to the "
        "second output port (or are kept with a missing molecule)."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Molecules"), Port(TABLE, "Parse errors", "Rows that could not be parsed, with error column")]
    fields = [
        F("column", "column", "Input column", None, "String column with molecule text.", kinds=["string"]),
        F("format", "enum", "Input format", "Auto", options=["Auto", "SMILES", "SDF", "SMARTS", "InChI"]),
        F("sanitize", "bool", "Sanitize molecules", True),
        F("on_error", "enum", "Rows that fail to parse", "Send to error port",
          options=["Send to error port", "Keep with missing molecule"]),
        *output_fields("Molecule"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        texts = df[col].to_list()
        mols = []
        for t in texts:
            ctx.check_canceled()
            try:
                mols.append(chem.parse(t, s["format"], s["sanitize"]))
            except Exception:  # noqa: BLE001 - invalid input becomes a missing molecule
                mols.append(None)
        failed = pl.Series([m is None and t is not None and str(t).strip() != "" for m, t in zip(mols, texts)],
                           dtype=pl.Boolean)
        out = put_column(df, s, col, chem.mol_series(s.get("new_column") or "Molecule", mols))
        errors = df.filter(failed).with_columns(pl.lit("Could not parse molecule").alias("Error"))
        if s["on_error"] == "Send to error port":
            out = out.filter(~failed)
        if errors.height:
            ctx.warn(f"{errors.height} row(s) could not be parsed")
        return [out, errors]


TO_FORMATS = ["SMILES", "SDF (MolBlock)", "SMARTS", "InChI", "InChIKey", "Molecular formula"]


class ToMolecule(NodeModel):
    factory = "verknode.chem.ToMolecule"
    name = "RDKit To Molecule"
    category = f"{CAT}/Converters"
    icon = "M→"
    short_description = "Converts molecules into SMILES, MolBlock, SMARTS, InChI or formula text."
    description = "Writes a text representation of each molecule. SMILES are canonical."
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Table with converted column")]
    fields = [
        mol_column_field(),
        F("format", "enum", "Output format", "SMILES", options=TO_FORMATS),
        F("isomeric", "bool", "Include stereochemistry", True, show_if={"format": ["SMILES", "SMARTS"]}),
        F("kekulize", "bool", "Kekulize (no aromatic flags)", False, show_if={"format": "SMILES"}),
        *output_fields("SMILES"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        fmt = s["format"]
        fn = {
            "SMILES": lambda m: Chem.MolToSmiles(m, isomericSmiles=s["isomeric"], kekuleSmiles=s["kekulize"]),
            "SDF (MolBlock)": Chem.MolToMolBlock,
            "SMARTS": lambda m: Chem.MolToSmarts(m, isomericSmiles=s["isomeric"]),
            "InChI": Chem.MolToInchi,
            "InChIKey": Chem.MolToInchiKey,
            "Molecular formula": Chem.rdMolDescriptors.CalcMolFormula,
        }[fmt]
        values = []
        for m in mols:
            try:
                values.append(None if m is None else fn(Chem.Mol(m)))
            except Exception:  # noqa: BLE001
                values.append(None)
        count_missing(ctx, mols)
        return [put_column(df, s, col, pl.Series(s.get("new_column") or fmt, values, dtype=pl.String))]


# ----------------------------------------------------------------------
# Calculators / fingerprints
# ----------------------------------------------------------------------
class DescriptorCalculation(NodeModel):
    factory = "verknode.chem.DescriptorCalculation"
    name = "RDKit Descriptor Calculation"
    category = f"{CAT}/Calculators"
    icon = "Σ⌬"
    short_description = "Calculates molecular descriptors."
    description = (
        f"Appends one column per selected RDKit descriptor ({len(DESCRIPTORS)} available, e.g. MolWt, "
        "MolLogP, TPSA, NumHDonors, qed)."
    )
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Molecules with descriptors")]
    fields = [
        mol_column_field(),
        F("descriptors", "multienum", "Descriptors",
          {"include": DEFAULT_DESCRIPTORS, "exclude": [], "enforce": "inclusion"}, options=list(DESCRIPTORS)),
        F("prefix", "string", "Column name prefix", ""),
    ]

    def execute(self, inputs, s, ctx):
        from .manipulation import selected_columns

        df = inputs[0]
        col, mols = read_mols(df, s)
        names = selected_columns(s["descriptors"], dict.fromkeys(DESCRIPTORS))
        if not names:
            raise InvalidSettingsError("No descriptors selected")
        values = {n: [] for n in names}
        for m in mols:
            ctx.check_canceled()
            for n in names:
                try:
                    values[n].append(None if m is None else float(DESCRIPTORS[n](m)))
                except Exception:  # noqa: BLE001
                    values[n].append(None)
        count_missing(ctx, mols)
        cols = []
        for n in names:
            out = f"{s['prefix']}{n}"
            if out in df.columns:
                raise InvalidSettingsError(f'Column "{out}" already exists')
            series = pl.Series(out, values[n], dtype=pl.Float64)
            if n.startswith(("Num", "fr_")) or n in ("HeavyAtomCount", "RingCount", "NHOHCount", "NOCount"):
                series = series.cast(pl.Int32)
            cols.append(series)
        return [df.with_columns(cols)]


class Fingerprint(NodeModel):
    factory = "verknode.chem.Fingerprint"
    name = "RDKit Fingerprint"
    category = f"{CAT}/Fingerprints"
    icon = "FP"
    short_description = "Calculates molecular fingerprints."
    description = (
        "Calculates Morgan (ECFP-like), feature Morgan, RDKit, atom pair, topological torsion, MACCS or "
        "pattern fingerprints, as a bit string, a list of on-bits, or one column per bit."
    )
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Molecules with fingerprints")]
    fields = [
        mol_column_field(),
        *fp_fields(),
        F("output", "enum", "Output as", "Bit string",
          options=["Bit string", "On-bit indices (list)", "Expanded bit columns"]),
        F("new_column", "string", "Column name / prefix", "FP"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        fp = chem.fingerprinter(s["fp_type"], s["radius"], s["n_bits"])
        fps = [None if m is None else fp(m) for m in mols]
        count_missing(ctx, mols)
        name = s["new_column"] or "FP"
        if s["output"] == "Bit string":
            return [df.with_columns(pl.Series(name, [None if f is None else f.ToBitString() for f in fps], dtype=pl.String))]
        if s["output"] == "On-bit indices (list)":
            return [df.with_columns(pl.Series(name, [None if f is None else list(f.GetOnBits()) for f in fps],
                                              dtype=pl.List(pl.Int32)))]
        n = next((f.GetNumBits() for f in fps if f is not None), 0)
        bits = [[None] * n if f is None else [int(c) for c in f.ToBitString()] for f in fps]
        return [df.with_columns(
            pl.Series(f"{name}_{i}", [b[i] for b in bits], dtype=pl.Int8) for i in range(n))]


# ----------------------------------------------------------------------
# Searching
# ----------------------------------------------------------------------
class SubstructureFilter(NodeModel):
    factory = "verknode.chem.SubstructureFilter"
    name = "RDKit Substructure Filter"
    category = f"{CAT}/Searching"
    icon = "⊂⌬"
    short_description = "Splits molecules by a substructure query."
    description = (
        "Molecules containing the SMARTS (or SMILES) query go to the first port, all others to the second. "
        "The query can be controlled by a flow variable."
    )
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Matching molecules"), Port(TABLE, "Non-matching molecules")]
    fields = [
        mol_column_field(),
        F("query", "string", "Query", "C(=O)[OH]", "e.g. C(=O)[OH] (carboxylic acid), c1ccccc1"),
        F("query_type", "enum", "Query type", "SMARTS", options=["SMARTS", "SMILES"]),
        F("min_matches", "int", "Minimum number of matches", 1),
        F("use_chirality", "bool", "Use chirality", False),
        F("count_column", "string", "Match count column (empty: none)", ""),
    ]

    def _query(self, s):
        q = (Chem.MolFromSmarts if s["query_type"] == "SMARTS" else Chem.MolFromSmiles)(s["query"] or "")
        if q is None or not s["query"]:
            raise InvalidSettingsError(f"Invalid {s['query_type']} query: {s['query']!r}")
        return q

    def validate(self, s, in_specs):
        self._query(s)

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        q = self._query(s)
        counts = [0 if m is None else len(m.GetSubstructMatches(q, useChirality=s["use_chirality"])) for m in mols]
        hit = pl.Series([c >= max(1, s["min_matches"]) for c in counts], dtype=pl.Boolean)
        if s["count_column"]:
            df = df.with_columns(pl.Series(s["count_column"], counts, dtype=pl.Int32))
        count_missing(ctx, mols)
        return [df.filter(hit), df.filter(~hit)]


class Similarity(NodeModel):
    factory = "verknode.chem.Similarity"
    name = "RDKit Similarity Search"
    category = f"{CAT}/Searching"
    icon = "≈⌬"
    short_description = "Calculates fingerprint similarity to query molecules."
    description = (
        "Appends the maximum similarity of each molecule to the reference molecules (second input) or to "
        "the query SMILES, plus the index of the most similar reference. Optionally filters by a threshold "
        "and sorts by similarity."
    )
    in_ports = [Port(TABLE, "Molecules"), Port(TABLE, "Reference molecules", optional=True)]
    out_ports = [Port(TABLE, "Molecules with similarity")]
    fields = [
        mol_column_field(),
        F("reference_column", "column", "Reference molecule column", None,
          "Used when the second input is connected.", kinds=MOL_KINDS, port=2),
        F("query", "string", "Query SMILES", "CC(=O)Oc1ccccc1C(=O)O", "Used when no reference table is connected."),
        *fp_fields(),
        F("metric", "enum", "Similarity metric", "Tanimoto", options=list(chem.SIMILARITY)),
        F("threshold", "float", "Minimum similarity (0 = keep all)", 0.0),
        F("sort", "bool", "Sort by similarity (descending)", True),
        F("new_column", "string", "Similarity column", "Similarity"),
    ]

    def execute(self, inputs, s, ctx):
        df, ref = inputs
        col, mols = read_mols(df, s)
        if ref is not None:
            refs = chem.to_mols(ref[require_column(s, "reference_column", ref.schema, "reference column")])
        else:
            q = chem.parse(s["query"], "SMILES")
            if q is None:
                raise InvalidSettingsError(f"Invalid query SMILES: {s['query']!r}")
            refs = [q]
        fp = chem.fingerprinter(s["fp_type"], s["radius"], s["n_bits"])
        ref_fps = [fp(m) for m in refs if m is not None]
        if not ref_fps:
            raise InvalidSettingsError("No valid reference molecules")
        bulk = chem.SIMILARITY[s["metric"]]
        sims, nearest = [], []
        for m in mols:
            ctx.check_canceled()
            if m is None:
                sims.append(None)
                nearest.append(None)
                continue
            values = bulk(fp(m), ref_fps)
            best = max(range(len(values)), key=values.__getitem__)
            sims.append(values[best])
            nearest.append(best)
        name = s["new_column"] or "Similarity"
        out = df.with_columns(pl.Series(name, sims, dtype=pl.Float64),
                              pl.Series(f"{name} nearest reference", nearest, dtype=pl.Int32))
        if s["threshold"] > 0:
            out = out.filter(pl.col(name) >= s["threshold"])
        if s["sort"]:
            out = out.sort(name, descending=True, nulls_last=True, maintain_order=True)
        count_missing(ctx, mols)
        return [out]


class DiversityPicker(NodeModel):
    factory = "verknode.chem.DiversityPicker"
    name = "RDKit Diversity Picker"
    category = f"{CAT}/Searching"
    icon = "⁂⌬"
    short_description = "Picks a diverse subset of molecules (MaxMin)."
    description = "Selects the requested number of maximally diverse molecules using the MaxMin algorithm on fingerprints."
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Diverse molecules")]
    fields = [
        mol_column_field(),
        F("count", "int", "Number of molecules to pick", 10),
        *fp_fields(),
        F("seed", "int", "Random seed (-1: none)", 42),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        fp = chem.fingerprinter(s["fp_type"], s["radius"], s["n_bits"])
        rows = [i for i, m in enumerate(mols) if m is not None]
        fps = [fp(mols[i]) for i in rows]
        n = min(max(0, s["count"]), len(fps))
        if n == 0:
            return [df.clear()]
        picks = rdSimDivPickers.MaxMinPicker().LazyBitVectorPick(fps, len(fps), n, seed=s["seed"])
        count_missing(ctx, mols)
        return [take_rows(df, [rows[i] for i in picks])]


# ----------------------------------------------------------------------
# Modifiers
# ----------------------------------------------------------------------
class StructureNormalizer(NodeModel):
    factory = "verknode.chem.StructureNormalizer"
    name = "RDKit Structure Normalizer"
    category = f"{CAT}/Modifiers"
    icon = "N⌬"
    short_description = "Standardizes molecules: cleanup, salt stripping, neutralization, tautomers."
    description = (
        "Applies RDKit MolStandardize steps in order: normalization / cleanup, keep the largest fragment "
        "(salt stripping), neutralize charges, canonical tautomer, remove explicit hydrogens."
    )
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Normalized molecules")]
    fields = [
        mol_column_field(),
        F("cleanup", "bool", "Normalize / clean up", True),
        F("largest_fragment", "bool", "Keep largest fragment (strip salts)", True),
        F("neutralize", "bool", "Neutralize charges", True),
        F("tautomer", "bool", "Canonical tautomer", False),
        F("remove_hs", "bool", "Remove explicit hydrogens", True),
        *output_fields("Normalized"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        chooser = rdMolStandardize.LargestFragmentChooser()
        uncharger = rdMolStandardize.Uncharger()
        enumerator = rdMolStandardize.TautomerEnumerator()
        out, failed = [], 0
        for m in mols:
            ctx.check_canceled()
            if m is None:
                out.append(None)
                continue
            try:
                if s["cleanup"]:
                    m = rdMolStandardize.Cleanup(m)
                if s["largest_fragment"]:
                    m = chooser.choose(m)
                if s["neutralize"]:
                    m = uncharger.uncharge(m)
                if s["tautomer"]:
                    m = enumerator.Canonicalize(m)
                if s["remove_hs"]:
                    m = Chem.RemoveHs(m)
                out.append(m)
            except Exception:  # noqa: BLE001
                failed += 1
                out.append(None)
        if failed:
            ctx.warn(f"{failed} molecule(s) could not be normalized")
        return [put_column(df, s, col, chem.mol_series(s.get("new_column") or "Normalized", out))]


class MurckoScaffolds(NodeModel):
    factory = "verknode.chem.MurckoScaffolds"
    name = "RDKit Find Murcko Scaffolds"
    category = f"{CAT}/Fragments"
    icon = "◇⌬"
    short_description = "Extracts Bemis-Murcko scaffolds."
    description = "Appends the Murcko scaffold (ring systems and linkers) of each molecule, optionally as generic framework."
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Molecules with scaffolds")]
    fields = [
        mol_column_field(),
        F("generic", "bool", "Generic framework (all atoms C, single bonds)", False),
        F("as_smiles", "bool", "Output as SMILES (String)", False),
        F("new_column", "string", "Scaffold column", "Scaffold"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        scaffolds = []
        for m in mols:
            if m is None:
                scaffolds.append(None)
                continue
            sc = MurckoScaffold.GetScaffoldForMol(m)
            if s["generic"]:
                sc = MurckoScaffold.MakeScaffoldGeneric(sc)
            scaffolds.append(sc)
        name = s["new_column"] or "Scaffold"
        if s["as_smiles"]:
            series = pl.Series(name, [None if m is None else Chem.MolToSmiles(m) for m in scaffolds], dtype=pl.String)
        else:
            series = chem.mol_series(name, scaffolds)
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        count_missing(ctx, mols)
        return [df.with_columns(series)]


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------
class Conformers(NodeModel):
    factory = "verknode.chem.Conformers"
    name = "RDKit Generate Coordinates"
    category = f"{CAT}/Geometry"
    icon = "3D"
    short_description = "Computes 2D depiction coordinates or a 3D conformer."
    description = (
        "2D: computes depiction coordinates. 3D: adds hydrogens, embeds a conformer with ETKDG and "
        "optionally optimizes it with MMFF94 or UFF."
    )
    in_ports = [Port(TABLE, "Molecules")]
    out_ports = [Port(TABLE, "Molecules with coordinates")]
    fields = [
        mol_column_field(),
        F("dimension", "enum", "Coordinates", "3D conformer", options=["2D coordinates", "3D conformer"]),
        F("optimize", "enum", "Force field optimization", "MMFF94", options=["None", "MMFF94", "UFF"],
          show_if={"dimension": "3D conformer"}),
        F("keep_hs", "bool", "Keep added hydrogens", True, show_if={"dimension": "3D conformer"}),
        F("seed", "int", "Random seed", 42, show_if={"dimension": "3D conformer"}),
        *output_fields("Conformer"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        out, failed = [], 0
        for m in mols:
            ctx.check_canceled()
            if m is None:
                out.append(None)
                continue
            m = Chem.Mol(m)
            if s["dimension"] == "2D coordinates":
                rdDepictor.Compute2DCoords(m)
                out.append(m)
                continue
            mh = Chem.AddHs(m)
            params = AllChem.ETKDGv3()
            params.randomSeed = s["seed"]
            if AllChem.EmbedMolecule(mh, params) != 0:
                failed += 1
                out.append(None)
                continue
            if s["optimize"] == "MMFF94" and AllChem.MMFFHasAllMoleculeParams(mh):
                AllChem.MMFFOptimizeMolecule(mh)
            elif s["optimize"] in ("UFF", "MMFF94"):
                AllChem.UFFOptimizeMolecule(mh)
            out.append(mh if s["keep_hs"] else Chem.RemoveHs(mh))
        if failed:
            ctx.warn(f"Embedding failed for {failed} molecule(s)")
        return [put_column(df, s, col, chem.mol_series(s.get("new_column") or "Conformer", out))]


# ----------------------------------------------------------------------
# Reactions
# ----------------------------------------------------------------------
class OneComponentReaction(NodeModel):
    factory = "verknode.chem.OneComponentReaction"
    name = "RDKit One Component Reaction"
    category = f"{CAT}/Reactions"
    icon = "⇢⌬"
    short_description = "Applies a reaction SMARTS to each molecule."
    description = (
        "Runs a single-reactant reaction (SMARTS, e.g. <code>[C:1](=O)[OH]&gt;&gt;[C:1](=O)OC</code> for "
        "methyl esterification) on each molecule. Each product becomes a row; molecules that do not react "
        "are dropped."
    )
    in_ports = [Port(TABLE, "Reactants")]
    out_ports = [Port(TABLE, "Products")]
    fields = [
        mol_column_field("Reactant column"),
        F("reaction", "string", "Reaction SMARTS", "[C:1](=O)[OH]>>[C:1](=O)OC"),
        F("unique", "bool", "Unique products per reactant", True),
        F("keep_columns", "bool", "Keep reactant columns", True),
        F("new_column", "string", "Product column", "Product"),
    ]

    def _reaction(self, s):
        try:
            rxn = rdChemReactions.ReactionFromSmarts(s["reaction"] or "")
        except Exception:  # noqa: BLE001
            rxn = None
        if rxn is None or rxn.GetNumReactantTemplates() != 1:
            raise InvalidSettingsError("Reaction SMARTS must be valid and have exactly one reactant")
        return rxn

    def validate(self, s, in_specs):
        self._reaction(s)

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col, mols = read_mols(df, s)
        rxn = self._reaction(s)
        rows, products, index = [], [], []
        for i, m in enumerate(mols):
            ctx.check_canceled()
            if m is None:
                continue
            seen = set()
            for k, prods in enumerate(rxn.RunReactants((m,))):
                p = prods[0]
                try:
                    Chem.SanitizeMol(p)
                except Exception:  # noqa: BLE001
                    continue
                smi = Chem.MolToSmiles(p)
                if s["unique"] and smi in seen:
                    continue
                seen.add(smi)
                rows.append(i)
                products.append(p)
                index.append(k)
        name = s["new_column"] or "Product"
        base = take_rows(df, rows) if s["keep_columns"] else pl.DataFrame({"Reactant row": pl.Series(rows, dtype=pl.Int64)})
        return [base.with_columns(chem.mol_series(name, products),
                                  pl.Series(f"{name} index", index, dtype=pl.Int32))]
