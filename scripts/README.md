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
└── sync-frontend-env.sh                # Sync frontend .env with stack outputs
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
