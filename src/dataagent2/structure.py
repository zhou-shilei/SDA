"""Shared names for the Reader, relation index and exported evidence graph."""

VARIABLE_REGIONS = ("VH", "VL", "VHH")
CDR_REGIONS = tuple(f"CDR{chain}{i}" for chain in "HL" for i in range(1, 4))
FR_REGIONS = tuple(f"FR{chain}{i}" for chain in "HL" for i in range(1, 5))
CHAIN_REGIONS = ("VH", "VL", "H_CHAIN", "L_CHAIN", "CH", "CL", "CH1", "CH2", "CH3", "CH4", "VHH",
                 *CDR_REGIONS, *FR_REGIONS, "scFv", "Fab", "Fc", "linker", "other")
REGION_ALIASES = {"H": "H_CHAIN", "L": "L_CHAIN", "H_chain": "H_CHAIN", "L_chain": "L_CHAIN",
                  **{f"{chain}CDR{i}": f"CDR{chain}{i}" for chain in "HL" for i in range(1, 4)},
                  **{f"{chain}FR{i}": f"FR{chain}{i}" for chain in "HL" for i in range(1, 5)}}
TAG_CATEGORIES = (
    "format", "isotype", "valency", "discovery_status", "discovery_strategy", "source_organism",
    "platform", "engineering", "target", "target_type", "gene", "antigen_species",
    "cell_host_species", "purpose", "method", "system", "cell_model", "sample", "readout",
    "endpoint", "epitope_type", "epitope_evidence", "function", "competition", "other",
)

# Examples guide spelling, not extraction. Unknown supported terms remain usable.
TAG_VOCABULARY = {
    "format": ["conventional", "VHH", "bispecific", "multispecific", "Fab", "scFv", "fusion"],
    "discovery_status": ["novel", "prior_art", "lead_optimization", "unclear"],
    "discovery_strategy": ["animal_immunization", "in_vitro_display", "rational_design", "patient_derived", "synthetic_library"],
    "engineering": ["humanization", "affinity_maturation", "CDR_grafting", "FR_modification", "Fc_engineering", "liability_removal"],
    "purpose": ["binding", "blocking", "neutralization", "cross_reactivity", "epitope_mapping", "developability", "stability", "pharmacokinetics", "in_vivo_efficacy"],
    "method": ["SPR", "BLI", "KinExA", "ELISA", "flow_cytometry", "competition_assay", "HDX_MS", "mutagenesis", "X_ray", "cryo_EM"],
    "system": ["protein", "cell", "primary_cell", "whole_blood", "in_vivo"],
    "endpoint": ["KD", "KA", "kon", "koff", "IC50", "IC90", "EC50", "Tm", "expression_yield"],
    "epitope_type": ["linear", "conformational", "functional_competitive"],
    "epitope_evidence": ["structure", "mutagenesis", "peptide_mapping", "HDX", "competition", "functional_characterization"],
}


def normalize_region(region):
    return REGION_ALIASES.get(region, region)


def parent_region(region):
    region = normalize_region(region)
    if region in CDR_REGIONS + FR_REGIONS:
        return "VH" if "H" in region else "VL"
    return None
