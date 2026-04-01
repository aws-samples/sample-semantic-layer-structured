#!/usr/bin/env python3
"""
Ontology to Markdown Converter

Converts RDF/OWL ontologies to Markdown format for better RAG retrieval
in Amazon Bedrock Knowledge Base.

Usage:
    python3 convert-ontologies.py [input_dir] [output_dir]
"""

import os
import sys
import glob
import argparse
from pathlib import Path
from typing import Dict, List, Set, Optional
import json

try:
    from rdflib import Graph, Namespace, URIRef, Literal
    from rdflib.namespace import RDF, RDFS, OWL, SKOS, DCTERMS
except ImportError:
    print("ERROR: rdflib not installed. Install with: pip install rdflib")
    sys.exit(1)

try:
    import defusedxml
    defusedxml.defuse_stdlib()  # patches stdlib xml modules
    from lxml import etree  # nosec B410 — lxml is required for XPath; all parse calls use a hardened XMLParser (resolve_entities=False, no_network=True, load_dtd=False)
    HAS_LXML = True
except ImportError:
    print("WARNING: lxml not installed. XSD parsing disabled. Install with: pip install lxml")
    HAS_LXML = False


class OntologyConverter:
    """Converts RDF/OWL ontologies to Markdown documentation"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.stats = {
            'files_processed': 0,
            'files_failed': 0,
            'classes_extracted': 0,
            'properties_extracted': 0,
        }

    def log(self, message: str):
        """Print log message if verbose"""
        if self.verbose:
            print(f"  {message}")

    def convert_file(self, input_path: str, output_path: str) -> bool:
        """Convert a single ontology file to Markdown"""
        try:
            self.log(f"Loading {os.path.basename(input_path)}...")

            # Load RDF graph
            g = Graph()

            # Try to parse with different formats
            for fmt in ['turtle', 'xml', 'n3', 'nt']:
                try:
                    g.parse(input_path, format=fmt)
                    break
                except Exception as _e:
                    self.log(f"    Trying format: {fmt} (parse failed, trying next format)")  # nosec B112
                    continue

            if len(g) == 0:
                self.log(f"  ⚠ Empty or invalid RDF file")
                return False

            self.log(f"  Loaded {len(g)} triples")

            # Extract ontology metadata
            ontology_info = self._extract_ontology_metadata(g)

            # Extract classes
            classes = self._extract_classes(g)

            # Extract properties
            properties = self._extract_properties(g)

            # Generate Markdown
            markdown = self._generate_markdown(ontology_info, classes, properties)

            # Write to file
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(markdown)

            self.stats['files_processed'] += 1
            self.stats['classes_extracted'] += len(classes)
            self.stats['properties_extracted'] += len(properties)

            self.log(f"  ✓ Converted: {len(classes)} classes, {len(properties)} properties")
            return True

        except Exception as e:
            self.log(f"  ✗ Error: {str(e)}")
            self.stats['files_failed'] += 1
            return False

    def _extract_ontology_metadata(self, g: Graph) -> Dict:
        """Extract ontology-level metadata"""
        info = {
            'title': None,
            'description': None,
            'version': None,
            'namespace': None,
        }

        # Find ontology resource
        for ontology in g.subjects(RDF.type, OWL.Ontology):
            # Get title
            for label in [RDFS.label, DCTERMS.title]:
                title = g.value(ontology, label)
                if title:
                    info['title'] = str(title)
                    break

            # Get description
            for desc in [RDFS.comment, DCTERMS.description]:
                description = g.value(ontology, desc)
                if description:
                    info['description'] = str(description)
                    break

            # Get version
            version = g.value(ontology, OWL.versionInfo)
            if version:
                info['version'] = str(version)

            info['namespace'] = str(ontology)
            break

        return info

    def _extract_classes(self, g: Graph) -> List[Dict]:
        """Extract OWL classes"""
        classes = []

        for cls in g.subjects(RDF.type, OWL.Class):
            if isinstance(cls, URIRef):
                class_info = {
                    'uri': str(cls),
                    'label': self._get_label(g, cls),
                    'comment': self._get_comment(g, cls),
                    'subclass_of': [],
                    'properties': [],
                }

                # Get superclasses
                for parent in g.objects(cls, RDFS.subClassOf):
                    if isinstance(parent, URIRef):
                        class_info['subclass_of'].append(self._format_uri(parent))

                # Get properties with this class as domain
                for prop in g.subjects(RDFS.domain, cls):
                    class_info['properties'].append(self._get_label(g, prop))

                classes.append(class_info)

        return sorted(classes, key=lambda x: x['label'])

    def _extract_properties(self, g: Graph) -> List[Dict]:
        """Extract OWL properties"""
        properties = []

        # Object properties
        for prop in g.subjects(RDF.type, OWL.ObjectProperty):
            properties.append(self._extract_property(g, prop, 'ObjectProperty'))

        # Datatype properties
        for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
            properties.append(self._extract_property(g, prop, 'DatatypeProperty'))

        # Annotation properties
        for prop in g.subjects(RDF.type, OWL.AnnotationProperty):
            properties.append(self._extract_property(g, prop, 'AnnotationProperty'))

        return sorted(properties, key=lambda x: x['label'])

    def _extract_property(self, g: Graph, prop: URIRef, prop_type: str) -> Dict:
        """Extract information about a single property"""
        info = {
            'uri': str(prop),
            'label': self._get_label(g, prop),
            'comment': self._get_comment(g, prop),
            'type': prop_type,
            'domain': [],
            'range': [],
        }

        # Get domain
        for domain in g.objects(prop, RDFS.domain):
            if isinstance(domain, URIRef):
                info['domain'].append(self._format_uri(domain))

        # Get range
        for range_cls in g.objects(prop, RDFS.range):
            if isinstance(range_cls, URIRef):
                info['range'].append(self._format_uri(range_cls))

        return info

    def _get_label(self, g: Graph, resource: URIRef) -> str:
        """Get human-readable label for a resource"""
        # Try different label properties
        for label_prop in [RDFS.label, SKOS.prefLabel, DCTERMS.title]:
            label = g.value(resource, label_prop)
            if label:
                return str(label)

        # Fallback to local name from URI
        return self._format_uri(resource)

    def _get_comment(self, g: Graph, resource: URIRef) -> str:
        """Get comment/description for a resource"""
        for comment_prop in [RDFS.comment, SKOS.definition, DCTERMS.description]:
            comment = g.value(resource, comment_prop)
            if comment:
                return str(comment)
        return ""

    def _format_uri(self, uri: URIRef) -> str:
        """Format URI to readable name"""
        uri_str = str(uri)
        # Extract local name
        if '#' in uri_str:
            return uri_str.split('#')[-1]
        elif '/' in uri_str:
            return uri_str.split('/')[-1]
        return uri_str

    def _generate_markdown(self, info: Dict, classes: List[Dict], properties: List[Dict]) -> str:
        """Generate Markdown documentation"""
        md = []

        # Title
        title = info['title'] or "Ontology Documentation"
        md.append(f"# {title}\n")

        # Metadata
        if info['description']:
            md.append(f"{info['description']}\n")

        if info['namespace']:
            md.append(f"**Namespace**: `{info['namespace']}`\n")

        if info['version']:
            md.append(f"**Version**: {info['version']}\n")

        md.append("\n---\n")

        # Classes section
        if classes:
            md.append("\n## Classes\n")
            md.append(f"This ontology defines {len(classes)} classes.\n")

            for cls in classes:
                md.append(f"\n### {cls['label']}\n")

                if cls['comment']:
                    md.append(f"{cls['comment']}\n")

                md.append(f"\n- **URI**: `{cls['uri']}`")

                if cls['subclass_of']:
                    md.append(f"\n- **Subclass of**: {', '.join(cls['subclass_of'])}")

                if cls['properties']:
                    md.append(f"\n- **Properties**: {', '.join(cls['properties'][:10])}")
                    if len(cls['properties']) > 10:
                        md.append(f" (and {len(cls['properties']) - 10} more)")

                md.append("\n")

        # Properties section
        if properties:
            md.append("\n## Properties\n")
            md.append(f"This ontology defines {len(properties)} properties.\n")

            for prop in properties:
                md.append(f"\n### {prop['label']}\n")

                if prop['comment']:
                    md.append(f"{prop['comment']}\n")

                md.append(f"\n- **URI**: `{prop['uri']}`")
                md.append(f"\n- **Type**: {prop['type']}")

                if prop['domain']:
                    md.append(f"\n- **Domain**: {', '.join(prop['domain'])}")

                if prop['range']:
                    md.append(f"\n- **Range**: {', '.join(prop['range'])}")

                md.append("\n")

        return '\n'.join(md)


class XSDConverter:
    """Converts XML Schema (XSD) files to Markdown documentation"""

    # XML Schema namespace
    XS_NS = "http://www.w3.org/2001/XMLSchema"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.stats = {
            'files_processed': 0,
            'files_failed': 0,
            'complex_types_extracted': 0,
            'simple_types_extracted': 0,
            'elements_extracted': 0,
        }

    def log(self, message: str):
        """Print log message if verbose"""
        if self.verbose:
            print(f"  {message}")

    def convert_file(self, input_path: str, output_path: str) -> bool:
        """Convert a single XSD file to Markdown"""
        if not HAS_LXML:
            self.log("  ⚠ XSD parsing requires lxml (skipped)")
            self.stats['files_failed'] += 1
            return False

        try:
            self.log(f"Loading {os.path.basename(input_path)}...")

            # Parse XSD using a hardened parser (resolves B320: disable entity resolution and network access)
            _parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
            tree = etree.parse(input_path, _parser)  # nosec B320 — hardened XMLParser passed: resolve_entities=False, no_network=True, load_dtd=False
            root = tree.getroot()

            # Extract schema metadata
            schema_info = self._extract_schema_metadata(root)

            # Extract complex types
            complex_types = self._extract_complex_types(root)

            # Extract simple types
            simple_types = self._extract_simple_types(root)

            # Extract global elements
            elements = self._extract_elements(root)

            # Generate Markdown
            markdown = self._generate_markdown(schema_info, complex_types, simple_types, elements)

            # Write to file
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(markdown)

            self.stats['files_processed'] += 1
            self.stats['complex_types_extracted'] += len(complex_types)
            self.stats['simple_types_extracted'] += len(simple_types)
            self.stats['elements_extracted'] += len(elements)

            self.log(f"  ✓ Converted: {len(complex_types)} complex types, {len(simple_types)} simple types, {len(elements)} elements")
            return True

        except Exception as e:
            self.log(f"  ✗ Error: {str(e)}")
            self.stats['files_failed'] += 1
            return False

    def _extract_schema_metadata(self, root) -> Dict:
        """Extract schema-level metadata"""
        info = {
            'target_namespace': root.get('targetNamespace', 'Not specified'),
            'version': root.get('version', 'Not specified'),
            'element_form_default': root.get('elementFormDefault', 'unqualified'),
        }

        # Try to extract documentation
        doc_elem = root.find(f"{{{self.XS_NS}}}annotation/{{{self.XS_NS}}}documentation")
        if doc_elem is not None and doc_elem.text:
            info['description'] = doc_elem.text.strip()
        else:
            info['description'] = None

        return info

    def _extract_complex_types(self, root) -> List[Dict]:
        """Extract complex type definitions"""
        complex_types = []

        for ct in root.findall(f".//{{{self.XS_NS}}}complexType"):
            type_name = ct.get('name')
            if not type_name:
                continue

            type_info = {
                'name': type_name,
                'documentation': self._get_documentation(ct),
                'elements': [],
                'attributes': [],
                'base_type': None,
            }

            # Check for extension/restriction
            extension = ct.find(f".//{{{self.XS_NS}}}extension")
            restriction = ct.find(f".//{{{self.XS_NS}}}restriction")

            if extension is not None:
                type_info['base_type'] = self._format_type(extension.get('base'))
            elif restriction is not None:
                type_info['base_type'] = self._format_type(restriction.get('base'))

            # Extract elements
            for elem in ct.findall(f".//{{{self.XS_NS}}}element"):
                elem_name = elem.get('name') or elem.get('ref')
                if elem_name:
                    type_info['elements'].append({
                        'name': self._format_type(elem_name),
                        'type': self._format_type(elem.get('type', 'any')),
                        'min_occurs': elem.get('minOccurs', '1'),
                        'max_occurs': elem.get('maxOccurs', '1'),
                        'documentation': self._get_documentation(elem),
                    })

            # Extract attributes
            for attr in ct.findall(f".//{{{self.XS_NS}}}attribute"):
                attr_name = attr.get('name') or attr.get('ref')
                if attr_name:
                    type_info['attributes'].append({
                        'name': self._format_type(attr_name),
                        'type': self._format_type(attr.get('type', 'string')),
                        'use': attr.get('use', 'optional'),
                        'documentation': self._get_documentation(attr),
                    })

            complex_types.append(type_info)

        return sorted(complex_types, key=lambda x: x['name'])

    def _extract_simple_types(self, root) -> List[Dict]:
        """Extract simple type definitions"""
        simple_types = []

        for st in root.findall(f".//{{{self.XS_NS}}}simpleType"):
            type_name = st.get('name')
            if not type_name:
                continue

            type_info = {
                'name': type_name,
                'documentation': self._get_documentation(st),
                'base_type': None,
                'restrictions': [],
                'enumerations': [],
            }

            # Check for restriction
            restriction = st.find(f".//{{{self.XS_NS}}}restriction")
            if restriction is not None:
                type_info['base_type'] = self._format_type(restriction.get('base'))

                # Extract enumerations
                for enum in restriction.findall(f".//{{{self.XS_NS}}}enumeration"):
                    value = enum.get('value')
                    doc = self._get_documentation(enum)
                    type_info['enumerations'].append({
                        'value': value,
                        'documentation': doc,
                    })

                # Extract other restrictions (length, pattern, etc.)
                for facet in restriction:
                    tag = etree.QName(facet).localname
                    if tag not in ['annotation', 'enumeration']:
                        type_info['restrictions'].append({
                            'type': tag,
                            'value': facet.get('value'),
                        })

            simple_types.append(type_info)

        return sorted(simple_types, key=lambda x: x['name'])

    def _extract_elements(self, root) -> List[Dict]:
        """Extract global element definitions"""
        elements = []

        for elem in root.findall(f"./{{{self.XS_NS}}}element"):
            elem_name = elem.get('name')
            if not elem_name:
                continue

            elem_info = {
                'name': elem_name,
                'type': self._format_type(elem.get('type', 'any')),
                'documentation': self._get_documentation(elem),
            }

            elements.append(elem_info)

        return sorted(elements, key=lambda x: x['name'])

    def _get_documentation(self, element) -> str:
        """Extract documentation from xs:annotation/xs:documentation"""
        doc_elem = element.find(f"{{{self.XS_NS}}}annotation/{{{self.XS_NS}}}documentation")
        if doc_elem is not None and doc_elem.text:
            return doc_elem.text.strip()
        return ""

    def _format_type(self, type_str: str) -> str:
        """Format XSD type name by removing namespace prefix"""
        if not type_str:
            return "any"

        # Remove namespace prefix
        if ':' in type_str:
            return type_str.split(':')[-1]

        return type_str

    def _generate_markdown(self, info: Dict, complex_types: List[Dict],
                          simple_types: List[Dict], elements: List[Dict]) -> str:
        """Generate Markdown documentation for XSD"""
        md = []

        # Title
        md.append(f"# XML Schema Documentation\n")

        # Metadata
        if info['description']:
            md.append(f"{info['description']}\n")

        md.append(f"**Target Namespace**: `{info['target_namespace']}`\n")
        md.append(f"**Version**: {info['version']}\n")
        md.append(f"**Element Form Default**: {info['element_form_default']}\n")

        md.append("\n---\n")

        # Global Elements
        if elements:
            md.append("\n## Global Elements\n")
            md.append(f"This schema defines {len(elements)} global elements.\n")

            for elem in elements:
                md.append(f"\n### {elem['name']}\n")

                if elem['documentation']:
                    md.append(f"{elem['documentation']}\n")

                md.append(f"\n- **Type**: `{elem['type']}`\n")

        # Complex Types
        if complex_types:
            md.append("\n## Complex Types\n")
            md.append(f"This schema defines {len(complex_types)} complex types.\n")

            for ct in complex_types:
                md.append(f"\n### {ct['name']}\n")

                if ct['documentation']:
                    md.append(f"{ct['documentation']}\n")

                if ct['base_type']:
                    md.append(f"\n**Extends**: `{ct['base_type']}`\n")

                # Elements
                if ct['elements']:
                    md.append(f"\n**Elements**:\n")
                    for elem in ct['elements'][:20]:  # Limit to 20 for readability
                        cardinality = f"[{elem['min_occurs']}..{elem['max_occurs']}]"
                        md.append(f"- `{elem['name']}` ({elem['type']}) {cardinality}")
                        if elem['documentation']:
                            md.append(f" - {elem['documentation'][:100]}")
                        md.append("\n")

                    if len(ct['elements']) > 20:
                        md.append(f"\n  _(and {len(ct['elements']) - 20} more elements)_\n")

                # Attributes
                if ct['attributes']:
                    md.append(f"\n**Attributes**:\n")
                    for attr in ct['attributes']:
                        md.append(f"- `{attr['name']}` ({attr['type']}) - {attr['use']}")
                        if attr['documentation']:
                            md.append(f" - {attr['documentation'][:100]}")
                        md.append("\n")

        # Simple Types
        if simple_types:
            md.append("\n## Simple Types\n")
            md.append(f"This schema defines {len(simple_types)} simple types.\n")

            for st in simple_types:
                md.append(f"\n### {st['name']}\n")

                if st['documentation']:
                    md.append(f"{st['documentation']}\n")

                if st['base_type']:
                    md.append(f"\n**Base Type**: `{st['base_type']}`\n")

                # Enumerations
                if st['enumerations']:
                    md.append(f"\n**Allowed Values**:\n")
                    for enum in st['enumerations'][:50]:  # Limit to 50
                        md.append(f"- `{enum['value']}`")
                        if enum['documentation']:
                            md.append(f" - {enum['documentation'][:100]}")
                        md.append("\n")

                    if len(st['enumerations']) > 50:
                        md.append(f"\n  _(and {len(st['enumerations']) - 50} more values)_\n")

                # Other restrictions
                if st['restrictions']:
                    md.append(f"\n**Restrictions**:\n")
                    for restriction in st['restrictions']:
                        md.append(f"- {restriction['type']}: `{restriction['value']}`\n")

        return '\n'.join(md)


def main():
    """Main conversion script"""
    parser = argparse.ArgumentParser(description='Convert ontologies to Markdown')
    parser.add_argument('input_dir', nargs='?', default='../data/ontology-sources',
                        help='Input directory containing ontology files (default: ../data/ontology-sources)')
    parser.add_argument('output_dir', nargs='?', default='../data/ontology-docs',
                        help='Output directory for Markdown files (default: ../data/ontology-docs)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()

    # Get absolute paths relative to script directory
    script_dir = Path(__file__).parent

    # Resolve input/output directories (handle both absolute and relative paths)
    if Path(args.input_dir).is_absolute():
        input_dir = Path(args.input_dir)
    else:
        input_dir = (script_dir / args.input_dir).resolve()

    if Path(args.output_dir).is_absolute():
        output_dir = Path(args.output_dir)
    else:
        output_dir = (script_dir / args.output_dir).resolve()

    print("=" * 60)
    print("Ontology to Markdown Converter")
    print("=" * 60)
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    print("")

    if not input_dir.exists():
        print(f"ERROR: Input directory not found: {input_dir}")
        print("Run download-ontologies.sh first!")
        sys.exit(1)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find all ontology files (RDF/OWL and XSD)
    rdf_patterns = ['**/*.ttl', '**/*.rdf', '**/*.owl', '**/*.n3', '**/*.nt']
    xsd_patterns = ['**/*.xsd']

    rdf_files = []
    for pattern in rdf_patterns:
        rdf_files.extend(input_dir.glob(pattern))

    xsd_files = []
    for pattern in xsd_patterns:
        xsd_files.extend(input_dir.glob(pattern))

    total_files = len(rdf_files) + len(xsd_files)
    print(f"Found {len(rdf_files)} RDF/OWL files and {len(xsd_files)} XSD files ({total_files} total)\n")

    # Convert files
    rdf_converter = OntologyConverter(verbose=args.verbose)
    xsd_converter = XSDConverter(verbose=args.verbose)

    file_num = 1

    # Convert RDF/OWL files
    if rdf_files:
        print("Converting RDF/OWL ontologies...\n")
        for file_path in rdf_files:
            # Generate output path
            relative_path = file_path.relative_to(input_dir)
            output_path = output_dir / relative_path.with_suffix('.md')

            print(f"[{file_num}/{total_files}] {relative_path}")
            rdf_converter.convert_file(str(file_path), str(output_path))
            file_num += 1

    # Convert XSD files
    if xsd_files:
        print("\nConverting XSD schemas...\n")
        for file_path in xsd_files:
            # Generate output path
            relative_path = file_path.relative_to(input_dir)
            output_path = output_dir / relative_path.with_suffix('.md')

            print(f"[{file_num}/{total_files}] {relative_path}")
            xsd_converter.convert_file(str(file_path), str(output_path))
            file_num += 1

    # Print summary
    print("\n" + "=" * 60)
    print("Conversion Complete!")
    print("=" * 60)

    # RDF/OWL Statistics
    print("\nRDF/OWL Ontologies:")
    print(f"  Files processed: {rdf_converter.stats['files_processed']}")
    print(f"  Files failed: {rdf_converter.stats['files_failed']}")
    print(f"  Classes extracted: {rdf_converter.stats['classes_extracted']}")
    print(f"  Properties extracted: {rdf_converter.stats['properties_extracted']}")

    # XSD Statistics
    print("\nXSD Schemas:")
    print(f"  Files processed: {xsd_converter.stats['files_processed']}")
    print(f"  Files failed: {xsd_converter.stats['files_failed']}")
    print(f"  Complex types extracted: {xsd_converter.stats['complex_types_extracted']}")
    print(f"  Simple types extracted: {xsd_converter.stats['simple_types_extracted']}")
    print(f"  Elements extracted: {xsd_converter.stats['elements_extracted']}")

    # Total
    total_processed = rdf_converter.stats['files_processed'] + xsd_converter.stats['files_processed']
    total_failed = rdf_converter.stats['files_failed'] + xsd_converter.stats['files_failed']
    print(f"\nTotal:")
    print(f"  Files processed: {total_processed}")
    print(f"  Files failed: {total_failed}")

    print(f"\nOutput directory: {output_dir}")
    print("\nNext step: Deploy CDK stack to upload to S3")


if __name__ == '__main__':
    main()
