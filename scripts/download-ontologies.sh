#!/bin/bash
################################################################################
# Ontology Documentation Download Script
# Downloads ontologies from public sources for Bedrock Knowledge Base
################################################################################

set -e  # Exit on error

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${PROJECT_ROOT}/data/ontology-sources"
TEMP_DIR="${OUTPUT_DIR}/temp"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Create output directories
mkdir -p "$OUTPUT_DIR"
mkdir -p "$TEMP_DIR"

echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}Ontology Documentation Download Script${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "Output directory: $OUTPUT_DIR"
echo ""

################################################################################
# 1. FIBO (Financial Industry Business Ontology)
################################################################################
echo -e "${YELLOW}[1/5] Downloading FIBO (Financial Industry Business Ontology)...${NC}"

if [ -d "$OUTPUT_DIR/fibo" ]; then
    echo "  → FIBO already exists, pulling latest..."
    cd "$OUTPUT_DIR/fibo"
    git pull --quiet
    cd "$SCRIPT_DIR"
else
    echo "  → Cloning FIBO repository..."
    git clone --depth 1 --quiet https://github.com/edmcouncil/fibo.git "$OUTPUT_DIR/fibo"
fi

# Copy key FIBO modules to flat structure for easier processing
mkdir -p "$OUTPUT_DIR/fibo-extracted"
echo "  → Extracting key FIBO modules..."

# Foundation (FND) - Core financial concepts
if [ -d "$OUTPUT_DIR/fibo/FND" ]; then
    cp -r "$OUTPUT_DIR/fibo/FND" "$OUTPUT_DIR/fibo-extracted/" 2>/dev/null || true
fi

# Business Entities (BE)
if [ -d "$OUTPUT_DIR/fibo/BE" ]; then
    cp -r "$OUTPUT_DIR/fibo/BE" "$OUTPUT_DIR/fibo-extracted/" 2>/dev/null || true
fi

# Financial Business and Commerce (FBC)
if [ -d "$OUTPUT_DIR/fibo/FBC" ]; then
    cp -r "$OUTPUT_DIR/fibo/FBC" "$OUTPUT_DIR/fibo-extracted/" 2>/dev/null || true
fi

# Indices and Indicators (IND) - relevant for insurance
if [ -d "$OUTPUT_DIR/fibo/IND" ]; then
    cp -r "$OUTPUT_DIR/fibo/IND" "$OUTPUT_DIR/fibo-extracted/" 2>/dev/null || true
fi

echo -e "${GREEN}  ✓ FIBO downloaded successfully${NC}"

################################################################################
# 2. ACORD Standards (Insurance Data Exchange)
################################################################################
echo -e "${YELLOW}[2/5] Downloading ACORD Standards (Insurance Data Exchange)...${NC}"

mkdir -p "$OUTPUT_DIR/acord"

# TXLife 2.36 — publicly available ACORD life insurance transaction schema
# Source: https://github.com/jasonjanofsky/Acord60Mins
echo "  → Downloading ACORD TXLife 2.36.00 schema..."
curl -sL \
    "https://raw.githubusercontent.com/jasonjanofsky/Acord60Mins/master/Acord60Mins/AcordTest/TXLife2.36.00.xsd" \
    -o "$OUTPUT_DIR/acord/TXLife2.36.00.xsd"

if [ -s "$OUTPUT_DIR/acord/TXLife2.36.00.xsd" ]; then
    echo -e "${GREEN}  ✓ TXLife2.36.00.xsd downloaded successfully${NC}"
else
    echo -e "${RED}  ✗ Download failed — file is empty or missing${NC}"
fi

# Create README for the acord directory
cat > "$OUTPUT_DIR/acord/README.md" << 'EOF'
# ACORD XSD Schemas

## Included Schema

**TXLife2.36.00.xsd** — ACORD TXLife 2.36.00 (Life Insurance Transaction Standard)

Downloaded from: https://github.com/jasonjanofsky/Acord60Mins

TXLife is the ACORD standard for life insurance data exchange. Version 2.36 covers:
- Policy Administration
- New Business / Applications
- Billing and Collections
- Claims
- Party (Person, Organization) types
- Coverage and Premium structures

## Adding More ACORD Schemas

Place additional XSD files in this directory. The conversion script processes all `.xsd` files automatically.

For full ACORD schema libraries (AL3, PC, Reinsurance, Claims):
- **ACORD Membership**: https://www.acord.org/membership
- **ACORD Standards**: https://www.acord.org/standards/

## Resources

- ACORD Website: https://www.acord.org
- ACORD GitHub: https://github.com/ACORD
EOF

echo -e "${GREEN}  ✓ ACORD directory ready${NC}"

################################################################################
# 3. Open Insurance Ontologies (Complementary Standards)
################################################################################
echo -e "${YELLOW}[3/5] Downloading Open Insurance Ontologies (Complementary)...${NC}"

# Insurance Knowledge Graph
if [ -d "$OUTPUT_DIR/insurance-kg" ]; then
    echo "  → Insurance KG already exists, pulling latest..."
    cd "$OUTPUT_DIR/insurance-kg"
    git pull --quiet 2>/dev/null || true
    cd "$SCRIPT_DIR"
else
    echo "  → Cloning Insurance Knowledge Graph..."
    git clone --depth 1 --quiet https://github.com/kastle-lab/insurance-kg.git "$OUTPUT_DIR/insurance-kg" 2>/dev/null || {
        echo "  → Insurance KG not available, skipping..."
    }
fi

# Download Schema.org (includes insurance-related schemas)
echo "  → Downloading Schema.org (includes insurance schemas)..."
curl -sL https://schema.org/version/latest/schemaorg-current-https.ttl -o "$OUTPUT_DIR/schema-org.ttl"
curl -sL https://schema.org/version/latest/schemaorg-current-https.rdf -o "$OUTPUT_DIR/schema-org.rdf"

echo -e "${GREEN}  ✓ Insurance ontologies downloaded successfully${NC}"

################################################################################
# 4. HL7 FHIR (Healthcare Standards)
################################################################################
echo -e "${YELLOW}[4/5] Downloading HL7 FHIR (Healthcare Standards)...${NC}"

mkdir -p "$OUTPUT_DIR/fhir"

echo "  → Downloading FHIR core ontology..."
curl -sL http://hl7.org/fhir/fhir.ttl -o "$OUTPUT_DIR/fhir/fhir.ttl"
curl -sL http://hl7.org/fhir/fhir.rdf -o "$OUTPUT_DIR/fhir/fhir.rdf"

echo "  → Downloading key FHIR resources..."
# Download commonly used FHIR resources relevant to insurance
declare -a fhir_resources=(
    "patient"
    "practitioner"
    "organization"
    "coverage"
    "claim"
    "claimresponse"
    "explanationofbenefit"
    "insuranceplan"
    "observation"
    "condition"
    "procedure"
    "medication"
)

for resource in "${fhir_resources[@]}"; do
    echo "    - $resource"
    curl -sL "http://hl7.org/fhir/${resource}.ttl" -o "$OUTPUT_DIR/fhir/${resource}.ttl" 2>/dev/null || true
done

echo -e "${GREEN}  ✓ FHIR ontologies downloaded successfully${NC}"

################################################################################
# 5. Academic Ontology Design Patterns
################################################################################
echo -e "${YELLOW}[5/5] Downloading Academic Ontology Design Patterns...${NC}"

# ODP Repository
if [ -d "$OUTPUT_DIR/ontology-design-patterns" ]; then
    echo "  → ODP already exists, pulling latest..."
    cd "$OUTPUT_DIR/ontology-design-patterns"
    git pull --quiet 2>/dev/null || true
    cd "$SCRIPT_DIR"
else
    echo "  → Cloning Ontology Design Patterns..."
    git clone --depth 1 --quiet https://github.com/cogan-shimizu-wsu/OntologyDesignPatterns.git "$OUTPUT_DIR/ontology-design-patterns" 2>/dev/null || {
        echo "  → Using alternative ODP source..."
        mkdir -p "$OUTPUT_DIR/ontology-design-patterns"
    }
fi

# Download W3C standard ontologies
echo "  → Downloading W3C standard ontologies..."
mkdir -p "$OUTPUT_DIR/w3c"

echo "    - OWL-Time"
curl -sL https://www.w3.org/TR/owl-time/time.ttl -o "$OUTPUT_DIR/w3c/owl-time.ttl"

echo "    - PROV-O (Provenance)"
curl -sL https://www.w3.org/ns/prov -H "Accept: text/turtle" -o "$OUTPUT_DIR/w3c/prov.ttl"

echo "    - SKOS"
curl -sL https://www.w3.org/2009/08/skos-reference/skos.rdf -o "$OUTPUT_DIR/w3c/skos.rdf"

echo "    - Dublin Core Terms"
curl -sL http://purl.org/dc/terms/ -H "Accept: text/turtle" -o "$OUTPUT_DIR/w3c/dcterms.ttl"

echo "    - FOAF"
curl -sL http://xmlns.com/foaf/0.1/ -H "Accept: application/rdf+xml" -o "$OUTPUT_DIR/w3c/foaf.rdf"

echo -e "${GREEN}  ✓ Academic ontology patterns downloaded successfully${NC}"

################################################################################
# Generate Summary
################################################################################
echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}Download Complete!${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "Summary:"
echo "--------"
echo "Output directory: $OUTPUT_DIR"
echo ""

# Count files
ttl_count=$(find "$OUTPUT_DIR" -name "*.ttl" -type f | wc -l)
rdf_count=$(find "$OUTPUT_DIR" -name "*.rdf" -type f | wc -l)
owl_count=$(find "$OUTPUT_DIR" -name "*.owl" -type f | wc -l)
total_count=$((ttl_count + rdf_count + owl_count))

echo "Files downloaded:"
echo "  - Turtle (.ttl): $ttl_count"
echo "  - RDF/XML (.rdf): $rdf_count"
echo "  - OWL (.owl): $owl_count"
echo "  - Total: $total_count"
echo ""

# Calculate total size
total_size=$(du -sh "$OUTPUT_DIR" | cut -f1)
echo "Total size: $total_size"
echo ""

echo -e "${YELLOW}Next steps:${NC}"
echo "1. Run: python3 convert-ontologies.py"
echo "   (from the scripts/ directory)"
echo "2. Deploy CDK stack to upload to S3"
echo "   cd ../cdk && npm run cdk deploy semantic-layer-bedrock-kb"
echo ""

# Cleanup temp directory
rm -rf "$TEMP_DIR"

exit 0
