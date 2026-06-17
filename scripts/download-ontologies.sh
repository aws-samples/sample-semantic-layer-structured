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

# curl wrapper: fail on HTTP errors, follow redirects, retry transient failures,
# require a non-empty body, and reject responses that are clearly HTML when an
# RDF/turtle/XSD file was expected (servers like hl7.org reply with HTML
# "300 Multiple Choices" pages for unknown extensions, which curl --fail
# does NOT treat as an error).
fetch() {
    local url="$1"
    local out="$2"
    local label="${3:-$(basename "$out")}"
    if curl --silent --show-error --location --fail \
            --retry 3 --retry-delay 2 --retry-connrefused \
            --max-time 60 \
            "$url" -o "$out"; then
        if [ ! -s "$out" ]; then
            echo -e "${RED}    ✗ ${label}: empty response${NC}"
            rm -f "$out"
            return 1
        fi
        # Reject HTML payloads when we expected RDF/TTL/XSD/OWL.
        case "$out" in
            *.ttl|*.rdf|*.owl|*.n3|*.nt|*.xsd)
                local first
                first="$(head -c 256 "$out" | tr -d '\n' | tr '[:upper:]' '[:lower:]')"
                case "$first" in
                    *'<!doctype html'*|*'<html'*)
                        echo -e "${RED}    ✗ ${label}: server returned HTML (likely 'multiple choices' / 404 page)${NC}"
                        rm -f "$out"
                        return 1
                        ;;
                esac
                ;;
        esac
        return 0
    fi
    echo -e "${RED}    ✗ ${label}: download failed (${url})${NC}"
    rm -f "$out"
    return 1
}

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

# NOTE: previously this step copied FND/BE/FBC/IND into a sibling
# `fibo-extracted/` directory. The converter then walked both trees and
# produced duplicate markdowns. The converter now recurses through
# `$OUTPUT_DIR/fibo/` directly, so the copy is no longer needed.
# Clean up any stale `fibo-extracted/` left over from earlier runs:
if [ -d "$OUTPUT_DIR/fibo-extracted" ]; then
    echo "  → Removing stale fibo-extracted/ (duplicates fibo/)..."
    rm -rf "$OUTPUT_DIR/fibo-extracted"
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
if fetch \
    "https://raw.githubusercontent.com/jasonjanofsky/Acord60Mins/master/Acord60Mins/AcordTest/TXLife2.36.00.xsd" \
    "$OUTPUT_DIR/acord/TXLife2.36.00.xsd" \
    "TXLife2.36.00.xsd"; then
    echo -e "${GREEN}  ✓ TXLife2.36.00.xsd downloaded successfully${NC}"
fi

# Remove any stray top-level copy from earlier runs of this script —
# the canonical location is acord/.
if [ -f "$OUTPUT_DIR/TXLife2.36.00.xsd" ]; then
    rm -f "$OUTPUT_DIR/TXLife2.36.00.xsd"
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
fetch "https://schema.org/version/latest/schemaorg-current-https.ttl" \
      "$OUTPUT_DIR/schema-org.ttl" "schema-org.ttl" || true
fetch "https://schema.org/version/latest/schemaorg-current-https.rdf" \
      "$OUTPUT_DIR/schema-org.rdf" "schema-org.rdf" || true

echo -e "${GREEN}  ✓ Insurance ontologies downloaded successfully${NC}"

################################################################################
# 4. HL7 FHIR (Healthcare Standards)
################################################################################
echo -e "${YELLOW}[4/5] Downloading HL7 FHIR (Healthcare Standards)...${NC}"

mkdir -p "$OUTPUT_DIR/fhir"

echo "  → Downloading FHIR core ontology..."
# fhir.ttl is the comprehensive FHIR ontology — every resource (Patient,
# Claim, Coverage, Observation, …) is already a class inside it. Earlier
# versions of this script also fetched per-resource <resource>.ttl URLs,
# but hl7.org returns 300 Multiple Choices HTML for those (no per-resource
# turtle file exists). The single fhir.ttl is sufficient.
fetch "http://hl7.org/fhir/fhir.ttl" "$OUTPUT_DIR/fhir/fhir.ttl" "fhir.ttl" || true
fetch "http://hl7.org/fhir/fhir.rdf" "$OUTPUT_DIR/fhir/fhir.rdf" "fhir.rdf" || true

# Clean up per-resource HTML stubs from earlier runs of this script
for stale in patient practitioner organization coverage claim claimresponse \
             explanationofbenefit insuranceplan observation condition \
             procedure medication; do
    rm -f "$OUTPUT_DIR/fhir/${stale}.ttl"
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
fetch "https://www.w3.org/2006/time" "$OUTPUT_DIR/w3c/owl-time.ttl" "owl-time" \
    || fetch "https://www.w3.org/TR/owl-time/time.ttl" "$OUTPUT_DIR/w3c/owl-time.ttl" "owl-time (fallback)" \
    || true

echo "    - PROV-O (Provenance)"
# Content negotiation can produce HTML when servers don't honour Accept; fall
# back to the canonical RDF/XML URL if the turtle fetch returns HTML or fails.
if curl --silent --show-error --location --fail --max-time 60 \
        -H "Accept: text/turtle" \
        "https://www.w3.org/ns/prov" -o "$OUTPUT_DIR/w3c/prov.ttl" \
        && [ -s "$OUTPUT_DIR/w3c/prov.ttl" ] \
        && ! head -c 64 "$OUTPUT_DIR/w3c/prov.ttl" | grep -qi '<html'; then
    :
else
    fetch "https://www.w3.org/ns/prov-o" "$OUTPUT_DIR/w3c/prov.ttl" "prov.ttl" \
        || fetch "https://www.w3.org/ns/prov.ttl" "$OUTPUT_DIR/w3c/prov.ttl" "prov.ttl (fallback)" \
        || true
fi

echo "    - SKOS"
fetch "https://www.w3.org/2009/08/skos-reference/skos.rdf" \
      "$OUTPUT_DIR/w3c/skos.rdf" "skos" || true

echo "    - Dublin Core Terms"
fetch "https://www.dublincore.org/specifications/dublin-core/dcmi-terms/dublin_core_terms.ttl" \
      "$OUTPUT_DIR/w3c/dcterms.ttl" "dcterms" || true

echo "    - FOAF"
fetch "http://xmlns.com/foaf/spec/index.rdf" \
      "$OUTPUT_DIR/w3c/foaf.rdf" "foaf" || true

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
xsd_count=$(find "$OUTPUT_DIR" -name "*.xsd" -type f | wc -l)
total_count=$((ttl_count + rdf_count + owl_count + xsd_count))

echo "Files downloaded:"
echo "  - Turtle (.ttl): $ttl_count"
echo "  - RDF/XML (.rdf): $rdf_count"
echo "  - OWL (.owl): $owl_count"
echo "  - XSD (.xsd): $xsd_count"
echo "  - Total: $total_count"
echo ""

# Calculate total size
total_size=$(du -sh "$OUTPUT_DIR" | cut -f1)
echo "Total size: $total_size"
echo ""

echo -e "${YELLOW}Next steps:${NC}"
echo "1. Run: python3 convert-ontologies.py"
echo "   (from the scripts/ directory)"
echo "   • Aggregator/Metadata RDF files with no classes are skipped by"
echo "     default; pass --keep-empty to retain them."
echo "   • Companion <name>-imports.md files capture the owl:imports graph;"
echo "     pass --no-imports to disable."
echo "2. Deploy CDK stack to upload to S3"
echo "   cd ../cdk && npm run cdk deploy semantic-layer-bedrock-kb"
echo ""

# Cleanup temp directory
rm -rf "$TEMP_DIR"

exit 0
