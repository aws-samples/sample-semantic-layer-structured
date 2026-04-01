# Ontology Download and Conversion Scripts

This directory contains scripts to download and convert ontology documentation for the Bedrock Knowledge Base.

## Overview

The scripts download ontologies from public sources and convert them to Markdown format for better retrieval-augmented generation (RAG) in Amazon Bedrock.

## Quick Start

```bash
# 1. Make download script executable
chmod +x download-ontologies.sh

# 2. Download ontologies (takes 2-5 minutes)
./download-ontologies.sh

# 3. (Optional) Add your ACORD XSD schemas
# Place ACORD schemas in ../data/ontology-sources/acord/
# See "Adding ACORD Schemas" section below

# 4. Install Python dependencies
pip install -r ontology-requirements.txt

# 5. Convert ontologies to Markdown
python3 convert-ontologies.py

# 6. Deploy CDK stack to upload to S3
cd ..
npm run cdk deploy semantic-layer-bedrock-kb
```

## Scripts

### 1. `download-ontologies.sh`

Downloads ontology documentation from public sources:

- **FIBO** (Financial Industry Business Ontology)
  - Foundation modules (FND)
  - Business Entities (BE)
  - Financial Business and Commerce (FBC)
  - Indices and Indicators (IND)

- **ACORD** (Association for Cooperative Operations Research and Development)
  - Downloads **TXLife2.36.00.xsd** (life insurance transaction standard) from public GitHub
  - No membership required for TXLife; place additional XSD files in the acord/ directory

- **Open Insurance Ontologies**
  - Insurance Knowledge Graph
  - Schema.org (insurance schemas)

- **HL7 FHIR** (Healthcare Standards)
  - Core FHIR ontology
  - Key resources: Patient, Coverage, Claim, etc.

- **Academic Ontology Design Patterns**
  - W3C ontologies (OWL-Time, PROV-O, SKOS, etc.)
  - Ontology Design Patterns repository

**Output**: `ontology-sources/` directory with RDF/OWL/XSD files

**Usage**:
```bash
./download-ontologies.sh
```

### 2. `convert-ontologies.py`

Converts RDF/OWL ontologies and XSD schemas to Markdown format for RAG.

**Features**:
- **RDF/OWL Support**: Parses RDF, Turtle, OWL, N3 formats
  - Extracts classes, properties, metadata
  - Preserves relationships and hierarchies
- **XSD Support**: Parses XML Schema Definition files
  - Extracts complex types, simple types, elements
  - Documents ACORD insurance standards
  - Preserves type hierarchies and constraints
- Generates structured Markdown documentation

**Output**: `ontology-docs/` directory with Markdown files

**Usage**:
```bash
# Basic usage (uses default directories)
python3 convert-ontologies.py

# Custom directories
python3 convert-ontologies.py ontology-sources ontology-docs

# Verbose output
python3 convert-ontologies.py -v
```

## Adding ACORD Schemas

The download script fetches **TXLife2.36.00.xsd** (ACORD life insurance transaction standard, v2.36) automatically from the public GitHub repository `jasonjanofsky/Acord60Mins`. No membership required.

To add further ACORD schemas (AL3, PC, Claims, etc.):

1. Obtain schemas from https://www.acord.org/standards/ (ACORD membership required)
2. **Place XSD files** in `../data/ontology-sources/acord/`
3. **Run the conversion script** — it processes all `.xsd` files in that directory automatically

**Resources**:
- ACORD Membership: https://www.acord.org/membership
- ACORD Standards: https://www.acord.org/standards/
- ACORD GitHub: https://github.com/ACORD

## Directory Structure

```
cdk/scripts/
├── download-ontologies.sh      # Download script
├── convert-ontologies.py       # Conversion script
├── ontology-requirements.txt   # Python dependencies
├── README.md                    # This file
├── ontology-sources/            # Downloaded RDF/OWL/XSD files (git-ignored)
│   ├── fibo/                    # FIBO ontologies
│   ├── acord/                   # ACORD XSD schemas (TXLife auto-downloaded)
│   ├── fhir/                    # HL7 FHIR resources
│   ├── w3c/                     # W3C standard ontologies
│   ├── insurance-kg/            # Insurance KG
│   └── ontology-design-patterns/
└── ontology-docs/               # Converted Markdown files (git-ignored)
    ├── fibo/
    ├── acord/
    ├── fhir/
    └── w3c/
```

## CDK Integration

The Bedrock Knowledge Base stack automatically uses these local files during deployment:

1. **Download phase**: Run `download-ontologies.sh`
2. **Convert phase**: Run `convert-ontologies.py`
3. **Deploy phase**: CDK uploads `ontology-docs/` to S3
4. **Ingestion phase**: Bedrock KB indexes the documents

If local files are not found, the stack falls back to inline patterns with a warning.

## Troubleshooting

### Download fails
```bash
# Check internet connection
# Some repositories may be temporarily unavailable
# The script will continue with available sources
```

### Conversion fails
```bash
# Install rdflib
pip install rdflib

# Check Python version (requires 3.7+)
python3 --version
```

### Files not found during CDK deploy
```bash
# Ensure scripts ran successfully
ls -la ontology-docs/

# Check for errors in previous steps
./download-ontologies.sh 2>&1 | tee download.log
python3 convert-ontologies.py -v 2>&1 | tee convert.log
```

## Maintenance

### Update ontologies
```bash
# Re-run download script to get latest versions
./download-ontologies.sh

# Re-convert
python3 convert-ontologies.py

# Re-deploy
npm run cdk deploy semantic-layer-bedrock-kb
```

### Add new sources

Edit `download-ontologies.sh` to add new ontology sources:

```bash
# Add new section
echo "Downloading Custom Ontology..."
curl -L https://example.com/ontology.ttl -o "$OUTPUT_DIR/custom/ontology.ttl"
```

## Sources and Credits

- **FIBO**: https://github.com/edmcouncil/fibo (EDM Council)
- **HL7 FHIR**: http://hl7.org/fhir/ (HL7 International)
- **W3C Ontologies**: https://www.w3.org/ (W3C)
- **Schema.org**: https://schema.org/
- **ODP**: https://github.com/cogan-shimizu-wsu/OntologyDesignPatterns

## License

The scripts are part of the Semantic Layer project. Downloaded ontologies are subject to their respective licenses.
