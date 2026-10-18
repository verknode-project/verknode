"""Life-science web services: ChEMBL, RCSB PDB, UCSC Genome Browser, Ensembl, Gene Ontology (QuickGO).

Queries come from a setting (one per line) or a column of the input table; the rows of the answers
carry the query in front (nodes/web.py has the general web nodes, nodes/_http.py the HTTP).
"""
from __future__ import annotations

import json
import os
import urllib.parse

import polars as pl

from verknode.node import F, InvalidSettingsError, Port, TABLE

from ._http import get_json, queries, request, table, with_query
from .web import _WebNode, column_field

CAT = "Web Services/Life Science"


# ---------------------------------------------------------------------------------------------------
# ChEMBL
# ---------------------------------------------------------------------------------------------------
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
MOL_SCHEMA = {"query": pl.String, "chembl_id": pl.String, "name": pl.String, "smiles": pl.String,
              "formula": pl.String, "mw": pl.Float64, "alogp": pl.Float64, "hbd": pl.Int64, "hba": pl.Int64,
              "max_phase": pl.Float64, "type": pl.String}


def _mol_row(q, m):
    p = m.get("molecule_properties") or {}
    num = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
    return {"query": q, "chembl_id": m.get("molecule_chembl_id"), "name": m.get("pref_name"),
            "smiles": (m.get("molecule_structures") or {}).get("canonical_smiles"), "formula": p.get("full_molformula"),
            "mw": num(p.get("full_mwt")), "alogp": num(p.get("alogp")), "hbd": p.get("hbd"), "hba": p.get("hba"),
            "max_phase": num(m.get("max_phase")), "type": m.get("molecule_type")}


class ChEMBL(_WebNode):
    factory = "verknode.bio.ChEMBL"
    name = "ChEMBL"
    category = CAT
    icon = "ChE"
    short_description = "Molecules, targets and bioactivities from ChEMBL (EMBL-EBI)."
    description = (
        "Molecule: the molecules for ChEMBL IDs (CHEMBL25) or names (aspirin): SMILES - for RDKit To Molecule - "
        "formula, weight, ALogP, H-bond donors / acceptors, clinical phase. Similar: molecules similar to a SMILES "
        "(similarity at least, in %). Activities: the measured activities of molecules (by ChEMBL ID): target, type "
        "(IC50, Ki ...), value, units, pChEMBL. Targets: targets whose name matches (e.g. cyclooxygenase)."
    )
    fields = [
        F("mode", "enum", "What", "Molecule", options=["Molecule", "Similar", "Activities", "Targets"]),
        column_field("ID / name / SMILES column"),
        F("query", "code", "IDs / names / SMILES", "CHEMBL25\nibuprofen", "One per line."),
        F("similarity", "int", "Similarity at least (%)", 70, "Similar."),
        F("limit", "int", "Results per query", 20),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(MOL_SCHEMA) if (s.get("mode") or "Molecule") == "Molecule" else None]

    def execute(self, inputs, s, ctx):
        mode = s.get("mode") or "Molecule"
        if ctx.dry:
            return [pl.DataFrame(schema=MOL_SCHEMA)]
        limit, rows = int(s.get("limit") or 20), []
        for _, q in queries(inputs, s, "column", "query", "ChEMBL ID, name or SMILES"):
            if mode == "Molecule":
                if q.upper().startswith("CHEMBL"):
                    r = request("GET", f"{CHEMBL}/molecule/{q.upper()}.json", ctx=ctx, check=False, timeout=60)
                    mols = [r.json()] if r.status == 200 else []
                else:
                    mols = get_json(f"{CHEMBL}/molecule.json", {"pref_name__iexact": q, "limit": limit}, timeout=60, ctx=ctx)["molecules"]
                    if not mols:  # (a synonym, e.g. a brand name)
                        mols = get_json(f"{CHEMBL}/molecule.json", {"molecule_synonyms__molecule_synonym__iexact": q,
                                                                    "limit": limit}, timeout=60, ctx=ctx)["molecules"]
                rows += [_mol_row(q, m) for m in mols] or [{"query": q}]
            elif mode == "Similar":
                j = get_json(f"{CHEMBL}/similarity/{urllib.parse.quote(q, safe='')}/{int(s.get('similarity') or 70)}.json",
                             {"limit": limit}, timeout=90, ctx=ctx)
                rows += [{**_mol_row(q, m), "similarity": float(m.get("similarity") or 0)} for m in j.get("molecules", [])]
            elif mode == "Activities":
                j = get_json(f"{CHEMBL}/activity.json", {"molecule_chembl_id": q, "limit": limit,
                                                        "only": "molecule_chembl_id,target_chembl_id,target_pref_name,"
                                                                "target_organism,standard_type,standard_relation,standard_value,"
                                                                "standard_units,pchembl_value,assay_chembl_id,assay_description,document_year"},
                             timeout=90, ctx=ctx)
                rows += with_query(j.get("activities", []), q)
            else:
                j = get_json(f"{CHEMBL}/target/search.json", {"q": q, "limit": limit}, timeout=60, ctx=ctx)
                rows += with_query([{"target_chembl_id": t.get("target_chembl_id"), "name": t.get("pref_name"),
                                     "type": t.get("target_type"), "organism": t.get("organism"),
                                     "accessions": ",".join(c.get("accession") or "" for c in t.get("target_components") or [])}
                                    for t in j.get("targets", [])], q)
        if mode == "Molecule":
            return [pl.DataFrame(rows, schema=MOL_SCHEMA) if rows else pl.DataFrame(schema=MOL_SCHEMA)]
        return [table(rows)]


# ---------------------------------------------------------------------------------------------------
# RCSB PDB
# ---------------------------------------------------------------------------------------------------
PDB_SCHEMA = {"query": pl.String, "pdb_id": pl.String, "title": pl.String, "method": pl.String,
              "resolution": pl.Float64, "released": pl.String, "polymers": pl.Int64, "ligands": pl.String,
              "organisms": pl.String, "doi": pl.String, "url": pl.String}


class PDB(_WebNode):
    factory = "verknode.bio.PDB"
    name = "Protein Data Bank (RCSB)"
    category = CAT
    icon = "PDB"
    short_description = "Structures from the RCSB Protein Data Bank: entry facts, search, structure files."
    description = (
        "Entry: the facts of PDB entries (1HSG): title, method, resolution, release date, number of polymer "
        "entities, ligands, organisms, DOI. Search: the entries that match a full-text search (e.g. HIV-1 "
        "protease indinavir). Download: the structure files (PDB, mmCIF or BinaryCIF) saved into a folder next to "
        "the workflow - the path column goes to an NGL / Mol* Structure View or Display."
    )
    fields = [
        F("mode", "enum", "What", "Entry", options=["Entry", "Search", "Download"]),
        column_field("PDB ID / search column"),
        F("query", "code", "PDB IDs / searches", "1HSG\n4HHB", "One per line."),
        F("limit", "int", "Search results at most", 10),
        F("format", "enum", "File format (Download)", "pdb", options=["pdb", "cif", "bcif"]),
        F("folder", "string", "Folder (Download)", "pdb_files", "Relative to the workflow.", path=True),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(PDB_SCHEMA) if s.get("mode") != "Download" else
                pl.Schema({"query": pl.String, "pdb_id": pl.String, "path": pl.String, "bytes": pl.Int64})]

    def execute(self, inputs, s, ctx):
        mode = s.get("mode") or "Entry"
        if ctx.dry:
            return [pl.DataFrame(schema=PDB_SCHEMA)]
        ids: list[tuple[str, str]] = []
        for _, q in queries(inputs, s, "column", "query", "PDB ID or search"):
            if mode == "Search":
                body = {"query": {"type": "terminal", "service": "full_text", "parameters": {"value": q}},
                        "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": int(s.get("limit") or 10)}}}
                r = request("POST", "https://search.rcsb.org/rcsbsearch/v2/query", data=json.dumps(body),
                            headers={"Content-Type": "application/json"}, ctx=ctx, check=False)
                hits = r.json().get("result_set", []) if r.status == 200 else []
                ids += [(q, h["identifier"]) for h in hits]
            else:
                ids.append((q, q.upper()))
        if mode == "Download":
            folder = ctx.resolve_path(s.get("folder") or "pdb_files")
            os.makedirs(folder, exist_ok=True)
            rows = []
            for q, i in ids:
                ext = s.get("format") or "pdb"
                r = request("GET", f"https://files.rcsb.org/download/{i}.{ext}", ctx=ctx, timeout=120)
                path = os.path.join(folder, f"{i}.{ext}")
                with open(path, "wb") as f:
                    f.write(r.body)
                rows.append({"query": q, "pdb_id": i, "path": os.path.relpath(path, ctx.base_dir or "."), "bytes": len(r.body)})
            return [table(rows, {"query": pl.String, "pdb_id": pl.String, "path": pl.String, "bytes": pl.Int64})]
        rows = []
        for q, i in ids:
            r = request("GET", f"https://data.rcsb.org/rest/v1/core/entry/{i}", ctx=ctx, check=False)
            if r.status != 200:
                rows.append({"query": q, "pdb_id": i, "title": "(no such entry)"})
                continue
            e = r.json()
            info = e.get("rcsb_entry_info") or {}
            res = info.get("resolution_combined") or [None]
            ligands = ",".join(sorted({c.get("chem_comp_id") for c in (e.get("rcsb_binding_affinity") or [])} - {None})) or None
            nonpoly = (e.get("rcsb_entry_container_identifiers") or {}).get("non_polymer_entity_ids") or []
            if not ligands and nonpoly:
                comps = []
                for k in nonpoly:
                    j = get_json(f"https://data.rcsb.org/rest/v1/core/nonpolymer_entity/{i}/{k}", ctx=ctx)
                    comps.append((j.get("pdbx_entity_nonpoly") or {}).get("comp_id"))
                ligands = ",".join(c for c in comps if c) or None
            organisms = None
            try:
                ents = (e.get("rcsb_entry_container_identifiers") or {}).get("polymer_entity_ids") or []
                orgs = set()
                for k in ents[:4]:
                    pe = get_json(f"https://data.rcsb.org/rest/v1/core/polymer_entity/{i}/{k}", ctx=ctx)
                    orgs |= {o.get("scientific_name") for o in pe.get("rcsb_entity_source_organism") or []}
                organisms = ", ".join(sorted(o for o in orgs if o)) or None
            except InvalidSettingsError:
                pass
            rows.append({"query": q, "pdb_id": i, "title": (e.get("struct") or {}).get("title"),
                         "method": ((e.get("exptl") or [{}])[0]).get("method"), "resolution": res[0],
                         "released": ((e.get("rcsb_accession_info") or {}).get("initial_release_date") or "")[:10] or None,
                         "polymers": info.get("polymer_entity_count"), "ligands": ligands, "organisms": organisms,
                         "doi": (e.get("rcsb_primary_citation") or {}).get("pdbx_database_id_doi"),
                         "url": f"https://www.rcsb.org/structure/{i}"})
        return [pl.DataFrame(rows, schema=PDB_SCHEMA) if rows else pl.DataFrame(schema=PDB_SCHEMA)]


# ---------------------------------------------------------------------------------------------------
# UCSC Genome Browser
# ---------------------------------------------------------------------------------------------------
class UCSC(_WebNode):
    factory = "verknode.bio.UCSC"
    name = "UCSC Genome Browser"
    category = CAT
    icon = "UCSC"
    short_description = "DNA sequences and track data of genome regions (UCSC Genome Browser API)."
    description = (
        "Sequence: the DNA of regions (chr7:117,480,025-117,480,200, or the chrom / start / end columns of the "
        "input table; 0-based start, end exclusive as UCSC counts) of a genome assembly (hg38, mm39 ...). Track: "
        "the items of a track (e.g. knownGene, cpgIslandExt) in the regions, one row each."
    )
    fields = [
        F("mode", "enum", "What", "Sequence", options=["Sequence", "Track"]),
        F("genome", "string", "Genome", "hg38"),
        F("track", "string", "Track (Track)", "knownGene"),
        column_field("Region column (chr:start-end)"),
        F("chrom", "column", "Chromosome column", None, port=1),
        F("start", "column", "Start column", None, port=1),
        F("end", "column", "End column", None, port=1),
        F("regions", "code", "Regions", "chr7:117480025-117480200", "chr:start-end, one per line."),
        F("limit", "int", "Track items per region at most", 1000),
    ]

    def _regions(self, inputs, s):
        df = inputs[0] if inputs and inputs[0] is not None else None
        if df is not None and s.get("chrom") and s.get("start") and s.get("end"):
            return [(f"{r[s['chrom']]}:{r[s['start']]}-{r[s['end']]}", str(r[s["chrom"]]), int(r[s["start"]]), int(r[s["end"]]))
                    for r in df.iter_rows(named=True)]
        out = []
        for _, q in queries(inputs, s, "column", "regions", "region"):
            try:
                chrom, span = q.replace(",", "").split(":")
                start, end = [int(x) for x in span.split("-")]
            except ValueError:
                raise InvalidSettingsError(f"A region is chr:start-end, not {q!r}") from None
            out.append((q, chrom, start, end))
        return out

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame()]
        genome, rows = (s.get("genome") or "hg38").strip(), []
        for q, chrom, start, end in self._regions(inputs, s):
            if (s.get("mode") or "Sequence") == "Sequence":
                j = get_json("https://api.genome.ucsc.edu/getData/sequence",
                             {"genome": genome, "chrom": chrom, "start": start, "end": end}, ctx=ctx)
                rows.append({"query": q, "genome": genome, "chrom": chrom, "start": start, "end": end,
                             "length": len(j.get("dna") or ""), "dna": j.get("dna")})
            else:
                track = (s.get("track") or "knownGene").strip()
                j = get_json("https://api.genome.ucsc.edu/getData/track",
                             {"genome": genome, "track": track, "chrom": chrom, "start": start, "end": end,
                              "maxItemsOutput": int(s.get("limit") or 1000)}, ctx=ctx)
                items = j.get(track) or []
                if isinstance(items, dict):  # (some tracks: by chromosome)
                    items = [x for v in items.values() for x in (v if isinstance(v, list) else [])]
                rows += with_query(items, q)
        return [table(rows)]


# ---------------------------------------------------------------------------------------------------
# Ensembl
# ---------------------------------------------------------------------------------------------------
ENSEMBL = "https://rest.ensembl.org"


class Ensembl(_WebNode):
    factory = "verknode.bio.Ensembl"
    name = "Ensembl"
    category = CAT
    icon = "Ens"
    short_description = "Genes, transcripts and sequences from Ensembl (by symbol or ID)."
    description = (
        "Lookup: the genes for symbols (BRCA2, TP53) of a species, or Ensembl IDs (ENSG00000139618): ID, name, "
        "biotype, location (chromosome, start, end, strand), assembly, description and - with Transcripts - the "
        "canonical transcript. Sequence: the sequence of IDs (genomic, cDNA, CDS or protein). Cross references: "
        "the IDs of a gene in other databases (HGNC, UniProt, RefSeq, ...) - or in one, also the IDs of its "
        "transcripts and proteins (Uniprot/SWISSPROT: the reviewed UniProt accession)."
    )
    fields = [
        F("mode", "enum", "What", "Lookup", options=["Lookup", "Sequence", "Cross references"]),
        column_field("Symbol / ID column"),
        F("query", "code", "Symbols / IDs", "BRCA2\nTP53", "One per line."),
        F("species", "string", "Species", "homo_sapiens", "For symbols."),
        F("seq_type", "enum", "Sequence type", "protein", options=["genomic", "cdna", "cds", "protein"]),
        F("external_db", "string", "Database (Cross references)", "",
          "Only this one, also of the transcripts and proteins (e.g. Uniprot/SWISSPROT, RefSeq_mRNA). Empty: all of the gene."),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame()]
        mode, species, rows = s.get("mode") or "Lookup", (s.get("species") or "homo_sapiens").strip(), []
        h = {"Content-Type": "application/json", "Accept": "application/json"}

        def lookup(q):
            path = f"lookup/id/{q}" if q.upper().startswith("ENS") else f"lookup/symbol/{species}/{urllib.parse.quote(q)}"
            r = request("GET", f"{ENSEMBL}/{path}", {"expand": 0}, headers=h, ctx=ctx, check=False, timeout=60)
            return r.json() if r.status == 200 else None

        for _, q in queries(inputs, s, "column", "query", "symbol or ID"):
            if mode == "Lookup":
                g = lookup(q)
                rows.append({"query": q, **({k: g.get(k) for k in ("id", "display_name", "biotype", "seq_region_name", "start",
                                                                   "end", "strand", "assembly_name", "description",
                                                                   "canonical_transcript", "species")} if g else {"id": None})})
            elif mode == "Sequence":
                i = q if q.upper().startswith("ENS") else (lookup(q) or {}).get("id")
                if not i:
                    rows.append({"query": q, "id": None})
                    continue
                params = {"type": s.get("seq_type") or "protein"}
                if params["type"] in ("cdna", "cds", "protein"):
                    params["multiple_sequences"] = 1
                j = get_json(f"{ENSEMBL}/sequence/id/{i}", params, headers=h, timeout=90, ctx=ctx)
                for seq in (j if isinstance(j, list) else [j]):
                    rows.append({"query": q, "id": seq.get("id"), "type": params["type"], "length": len(seq.get("seq") or ""),
                                 "sequence": seq.get("seq")})
            else:
                i = q if q.upper().startswith("ENS") else (lookup(q) or {}).get("id")
                db = (s.get("external_db") or "").strip()
                xs = get_json(f"{ENSEMBL}/xrefs/id/{i}", {"external_db": db, "all_levels": 1} if db else None, headers=h,
                              timeout=60, ctx=ctx) if i else []
                if db:  # (one per ID: the proteins of the transcripts repeat it)
                    seen: set = set()
                    xs = [x for x in xs if not (x.get("primary_id") in seen or seen.add(x.get("primary_id")))]
                rows += with_query([{"id": i, "database": x.get("dbname"), "xref": x.get("primary_id"),
                                     "name": x.get("display_id"), "description": x.get("description")} for x in xs], q)
        return [table(rows)]


# ---------------------------------------------------------------------------------------------------
# Gene Ontology (QuickGO)
# ---------------------------------------------------------------------------------------------------
QUICKGO = "https://www.ebi.ac.uk/QuickGO/services"


class GeneOntology(_WebNode):
    factory = "verknode.bio.GeneOntology"
    name = "Gene Ontology"
    category = CAT
    icon = "GO"
    short_description = "Gene Ontology terms and the GO annotations of genes (QuickGO, EMBL-EBI)."
    description = (
        "Term: the GO terms for IDs (GO:0006281): name, aspect (biological process, molecular function, cellular "
        "component), definition. Search: terms whose name matches (e.g. DNA repair). Annotations: the GO "
        "annotations of gene products - UniProt accessions (P38398 for BRCA1) - with term, aspect, evidence and "
        "reference."
    )
    fields = [
        F("mode", "enum", "What", "Annotations", options=["Term", "Search", "Annotations"]),
        column_field("GO ID / search / UniProt column"),
        F("query", "code", "GO IDs / searches / UniProt accessions", "P38398\nP51587", "One per line."),
        F("limit", "int", "Results per query", 100),
    ]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame()]
        mode, limit, rows = s.get("mode") or "Annotations", int(s.get("limit") or 100), []
        qs = queries(inputs, s, "column", "query", "GO ID, search or UniProt accession")
        if mode == "Term":
            ids = ",".join(urllib.parse.quote(q, safe=":") for _, q in qs)
            j = get_json(f"{QUICKGO}/ontology/go/terms/{ids}", ctx=ctx)
            rows = [{"query": t.get("id"), "go_id": t.get("id"), "name": t.get("name"), "aspect": t.get("aspect"),
                     "definition": (t.get("definition") or {}).get("text"), "obsolete": t.get("isObsolete")}
                    for t in j.get("results", [])]
        for _, q in (qs if mode != "Term" else []):
            if mode == "Search":
                j = get_json(f"{QUICKGO}/ontology/go/search", {"query": q, "limit": min(limit, 600)}, ctx=ctx)
                rows += with_query([{"go_id": t.get("id"), "name": t.get("name"), "aspect": t.get("aspect"),
                                     "definition": (t.get("definition") or {}).get("text")} for t in j.get("results", [])], q)
            else:
                j = get_json(f"{QUICKGO}/annotation/search", {"geneProductId": q, "limit": min(limit, 200)}, ctx=ctx)
                rows += with_query([{"gene_product": a.get("geneProductId"), "symbol": a.get("symbol"), "go_id": a.get("goId"),
                                     "go_name": a.get("goName"), "aspect": a.get("goAspect"), "evidence": a.get("goEvidence"),
                                     "qualifier": a.get("qualifier"), "reference": a.get("reference"),
                                     "assigned_by": a.get("assignedBy")} for a in j.get("results", [])], q)
        return [table(rows)]
