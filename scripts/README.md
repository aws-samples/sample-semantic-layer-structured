# Semantic Layer Scripts

This directory contains scripts for managing the semantic layer project.

## Contents

### Data Generation Scripts

- **generate_complete_synthetic_data.py** - Generates synthetic insurance data for testing
- **load_to_dynamodb.py** - Loads synthetic data to DynamoDB tables
- **initial_load_to_iceberg.py** - One-time backfill: DynamoDB → Firehose → S3 Tables (Iceberg)
- **SYNTHETIC_DATA_README.md** - Detailed documentation for synthetic data scripts

### Ontology Management Scripts

- **download-ontologies.sh** - Downloads reference ontologies (FIBO, ACORD, FHIR, W3C, etc.)
- **convert-ontologies.py** - Converts RDF/OWL ontologies and XSD schemas to Markdown format
- **ontology-requirements.txt** - Python dependencies for ontology scripts
- **ONTOLOGY_SCRIPTS_README.md** - Detailed documentation for ontology scripts

### Operational Scripts

- **sync-frontend-env.sh** - Syncs the frontend `.env` file with deployed CloudFormation stack values
- **migrate_threshold_iris.py** - One-time migration for governed rules written before threshold IRIs were rule-scoped. A legacy threshold node was named `vkgr:<threshold_id>` — global, so two rules that picked the same id shared one node, and deleting either rule deleted the other's threshold and broke the whole namespace's rule list. The migration re-persists each rule from its own in-memory model, which preserves the `lifecycle` and `compiled_sparql` fields that the REST API's create/update paths deliberately reset. **Deploy the query runtimes before migrating**, and note that every rule is read before any rule is written (rewriting one legacy rule would otherwise destroy its sibling's threshold before it had been captured). Dry run by default; pass `--apply` to write. See `assets/guides/GOVERNED_RULES.md`

### Benchmark Ingestion Scripts (`benchmarks/`)

- **benchmarks/ingest_bird.py** - Ingests the BIRD mini-dev text-to-SQL benchmark into S3 Tables / Athena
- **benchmarks/ingest_cwd.py** - Ingests the data.world CWD (ACME_Insurance) benchmark
- **benchmarks/ingest_spider2.py** - Ingests the Spider 2.0-lite SQLite subset (stretch)
- **benchmarks/ingest_common.py** - Shared ingestion helpers (Parquet export, CTAS/DROP into Iceberg)
- **benchmarks/deploy_datasets.py** - Resumable deploy driver: creates the S3 Tables namespace + Lake Formation grants, then orchestrates the ingest helpers per database (no semantic-layer build). The nine agent/eval roles that need read access are discovered from live IAM rather than passed in; `--grants-only` re-applies the grants to already-ingested namespaces without ingesting anything
- **benchmarks/requirements.txt** - Pinned Python deps for the benchmark pipelines
- **benchmarks/README.md** - Ingestion runbook and per-benchmark instructions

## Directory Structure

```
scripts/
├── README.md                           # This file
├── generate_complete_synthetic_data.py # Synthetic data generator
├── load_to_dynamodb.py                 # DynamoDB loader
├── initial_load_to_iceberg.py          # DynamoDB → Firehose → S3 Tables backfill
├── SYNTHETIC_DATA_README.md            # Synthetic data documentation
├── download-ontologies.sh              # Ontology downloader
├── convert-ontologies.py               # Ontology converter
├── ontology-requirements.txt           # Python deps for ontology scripts
├── ONTOLOGY_SCRIPTS_README.md          # Ontology scripts documentation
├── sync-frontend-env.sh                # Sync frontend .env with stack outputs
├── migrate_threshold_iris.py           # Scope legacy global threshold IRIs to their rule
└── benchmarks/                         # Public text-to-SQL benchmark ingestion
    ├── ingest_bird.py                  # BIRD mini-dev
    ├── ingest_cwd.py                   # data.world CWD (ACME_Insurance)
    ├── ingest_spider2.py               # Spider 2.0-lite (stretch)
    ├── ingest_common.py                # Shared ingestion helpers
    ├── deploy_datasets.py              # Resumable deploy driver (namespace + LF grants + ingest; --grants-only re-grants)
    ├── requirements.txt                # Pinned pipeline deps
    └── README.md                       # Ingestion runbook
```

## Data Directory Structure

Scripts write to the `/data/` directory:

```
data/
├── complete_synthetic_data/            # Generated synthetic data
│   ├── parties.json
│   ├── policies.json
│   └── ...
├── ontology-sources/                   # Downloaded ontologies (git-ignored)
│   ├── fibo/
│   ├── acord/                          # ACORD XSD schemas (user-provided)
│   ├── fhir/
│   └── w3c/
└── ontology-docs/                      # Converted markdown (git-ignored)
    ├── fibo/
    ├── acord/
    ├── fhir/
    └── w3c/
```

## Quick Start

### Generate Synthetic Data

```bash
# Generate complete insurance dataset
python3 generate_complete_synthetic_data.py

# Load to DynamoDB
python3 load_to_dynamodb.py
```

### Setup Reference Ontologies

```bash
# Download ontologies (~500MB, takes 2-5 minutes)
chmod +x download-ontologies.sh
./download-ontologies.sh

# Convert to Markdown
pip install -r ontology-requirements.txt
python3 convert-ontologies.py
```

## Documentation

- **Synthetic Data**: See `SYNTHETIC_DATA_README.md` in this directory
- **Ontology Scripts**: See `ONTOLOGY_SCRIPTS_README.md` in this directory

## Notes

- All scripts should be run from this directory
- Large data files are git-ignored (see `/data/.gitignore`)
- Scripts write to `/data/` subdirectories, not to script directory
