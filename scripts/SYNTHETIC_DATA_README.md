# Synthetic Data & DynamoDB Setup Guide

## Overview

This guide covers the complete process of generating synthetic insurance data and loading it into DynamoDB for the semantic layer project. The synthetic dataset includes **12 tables** with **1,349 columns**, designed for a single-table DynamoDB architecture.

## Quick Start

### 1. Generate Synthetic Data (if needed)

```bash
cd scripts
python3 generate_complete_synthetic_data.py
```

### 2. Deploy DynamoDB with Automatic Data Loading

```bash
cd cdk
npm run cdk deploy semantic-layer-dynamodb
```

### 3. Verify Data

```bash
aws dynamodb describe-table \
  --table-name semantic-layer-insurance-data \
  --query 'Table.ItemCount'
```

## Synthetic Data Details

### Generated Tables (12 Total)

| # | Table Name | Records | Columns | File Size | Status |
|---|------------|---------|---------|-----------|--------|
| 1 | odh.type.codes | 40 | 10 | ~3 KB | ✅ Complete |
| 2 | odh.admin.codes | 100 | 11 | ~11 KB | ✅ Complete |
| 3 | **odh.party** | 5,000 | **324** | ~1.3 MB | ✅ Complete |
| 4 | odh.policyproduct | 20 | 44 | ~9 KB | ✅ Complete |
| 5 | odh.coverageproduct | 15 | 10 | ~2 KB | ✅ Complete |
| 6 | odh.investproduct | 80 | 54 | ~40 KB | ✅ Complete |
| 7 | **odh.coverage** | 10,000 | **168** | ~2.6 MB | ✅ Complete |
| 8 | **odh.holding** | 15,000 | **414** | ~4.0 MB | ✅ Complete |
| 9 | **odh.financialactivity** | 12,000 | **167** | ~2.7 MB | ✅ Complete |
| 10 | odh.financialstatement | 1,200 | 45 | ~297 KB | ✅ Complete |
| 11 | odh.rider | 800 | 29 | ~176 KB | ✅ Complete |
| 12 | odh.relation | 500 | 73 | ~116 KB | ✅ Complete |
| | **TOTAL** | **~45,000** | **1,349** | **~11 MB** | ✅ Complete |

### File Structure

```
semantic-layer/
├── data/
│   └── complete_synthetic_data/                 # Generated data
│       ├── type_codes.json                      # 40 records
│       ├── admin_codes.json                     # 100 records
│       ├── parties.json                         # 5,000 records (324 cols)
│       ├── policy_products.json                 # 20 records
│       ├── coverage_products.json               # 15 records
│       ├── invest_products.json                 # 80 records
│       ├── coverages.json                       # 10,000 records (168 cols)
│       ├── holdings.json                        # 15,000 records (414 cols)
│       ├── financial_activities.json            # 12,000 records (167 cols)
│       ├── financial_statements.json            # 1,200 records
│       ├── riders.json                          # 800 records
│       └── relations.json                       # 500 records
├── scripts/
│   ├── generate_complete_synthetic_data.py      # Data generator
│   └── load_to_dynamodb.py                      # Manual loader
└── cdk/
    └── lib/
        ├── constructs/
        │   └── dynamodb-data-loader.ts          # CDK data loader construct
        └── stacks/backend/
            └── dynamodb-stack.ts                # DynamoDB infrastructure
```

### Key Features of Synthetic Data

#### Complete Column Coverage
- **odh.party** (324 columns): Personal info, contact details, financial data, health records, compliance
- **odh.holding** (414 columns): Investment accounts, performance metrics, risk analytics, returns
- **odh.coverage** (168 columns): Coverage amounts, underwriting details, cash values, death benefits
- **odh.financialactivity** (167 columns): Transactions, payments, batch processing

#### Realistic Data Characteristics
- Proper relationships between entities (referential integrity)
- Valid date ranges (2010-2024)
- Realistic monetary amounts and distributions
- Audit trail fields (created/updated timestamps)
- Extension fields for future schema evolution

#### DynamoDB Single-Table Design
- Optimized partition keys (pk) and sort keys (sk)
- Efficient access patterns for all entity types
- Support for hierarchical data (policy → coverages, holdings, etc.)

### Key Pattern Structure

```
pk (Partition Key): {EntityType}#{ID}
sk (Sort Key): #METADATA or {RelatedEntity}#{ID}
```

**Examples**:
- Party: `pk=PARTY#P001`, `sk=#METADATA`
- Policy with coverage: `pk=POLICY#POL001`, `sk=COVERAGE#C001`
- Party relationship: `pk=PARTY#P001`, `sk=RELATION#P002`

## DynamoDB Setup

### Infrastructure Created

**Global Secondary Indexes**:
- GSI1: Alternative access patterns
- GSI2: Secondary entity lookups
- GSI3: Additional query patterns

### CDK Components

#### 1. Data Loader Construct
**File**: `/cdk/lib/constructs/dynamodb-data-loader.ts`

Features:
- Lambda function for batch loading JSON data
- Reads data files during CDK synthesis
- Handles float-to-Decimal conversion automatically
- CloudFormation custom resource pattern
- Efficient batch write operations

#### 2. DynamoDB Stack
**File**: `/cdk/lib/stacks/backend/dynamodb-stack.ts`

Configuration:
- Optional `loadSyntheticData` parameter (default: false)
- Integrates DynamoDBDataLoader construct
- Loads all 12 synthetic data files
- Configured billing mode: PAY_PER_REQUEST

#### 3. App Configuration
**File**: `/cdk/bin/app.ts`

```typescript
const dynamodbStack = new DynamoDBStack(app, `${projectName}-dynamodb`, {
  env,
  projectName,
  loadSyntheticData: true, // Enable automatic data loading
});
```

## Data Loading Options

### Option 1: Automatic CDK Deployment

**Pros**:
- Fully automated, no manual steps
- Consistent with Infrastructure as Code
- Runs during stack creation
- Ideal for CI/CD pipelines

**Cons**:
- Slower initial deployment (5-10 minutes)
- Redeployments may reload data

**When to use**: Initial setup, production deployments, CI/CD

**Command**:
```bash
cd cdk
npm run cdk deploy semantic-layer-dynamodb
```

### Option 2: Manual Python Script

**Pros**:
- Faster (2-3 minutes)
- Can reload data anytime without redeploying
- More granular control
- Useful for development

**Cons**:
- Requires manual execution
- Separate from CDK deployment workflow

**When to use**: Data updates, development, troubleshooting

**Command**:
```bash
cd scripts
python3 load_to_dynamodb.py [table_name] [region]

# Example
python3 load_to_dynamodb.py semantic-layer-insurance-data us-east-1
```

## Data Loading Process

### Automatic (CDK)

1. Deploy stack with `loadSyntheticData: true`
2. CDK synthesizes and reads JSON files
3. Custom resource Lambda function created
4. Lambda loads data during stack creation
5. CloudFormation signals completion

### Manual (Python Script)

1. Deploy stack with `loadSyntheticData: false`
2. Run `load_to_dynamodb.py` script
3. Script reads JSON files from `data/complete_synthetic_data/`
4. Batch writes to DynamoDB table
5. Reports progress and completion

## Access Patterns Supported

1. Get party by ID
2. Get all coverages for a policy
3. Get all holdings for a policy
4. Get financial activities by policy
5. Get financial statements by policy
6. Get riders for a policy
7. Get relations for a party
8. Get product by code
9. Lookup type codes
10. Lookup admin codes

### Example Queries

```python
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('semantic-layer-insurance-data')

# Get a specific party
response = table.get_item(
    Key={'pk': 'PARTY#PARTY000001', 'sk': '#METADATA'}
)
print(response['Item'])

# Get all coverages for a policy
response = table.query(
    KeyConditionExpression=Key('pk').eq('POLICY#POL00000001') &
                          Key('sk').begins_with('COVERAGE#')
)
for item in response['Items']:
    print(f"Coverage: {item['CoverageID']} - ${item['CurrentAmt']}")

# Get all holdings for a policy
response = table.query(
    KeyConditionExpression=Key('pk').eq('POLICY#POL00000001') &
                          Key('sk').begins_with('HOLDING#')
)
```

## Regenerating Data

To regenerate with different parameters:

```bash
# Edit configuration
nano scripts/generate_complete_synthetic_data.py

# Modify these values:
NUM_PARTIES = 5000              # Number of customers
NUM_POLICIES = 10000            # Number of policies
NUM_COVERAGES = 10000           # Number of coverages
NUM_HOLDINGS = 15000            # Number of holdings
NUM_FINANCIAL_ACTIVITIES = 12000 # Number of transactions

# Run generator
python3 scripts/generate_complete_synthetic_data.py
```

## Verification

### Check Table Status

```bash
# Get table description
aws dynamodb describe-table \
  --table-name semantic-layer-insurance-data

# Get item count (may have delay)
aws dynamodb describe-table \
  --table-name semantic-layer-insurance-data \
  --query 'Table.ItemCount'
```

### Sample Data

```bash
# Scan sample records
aws dynamodb scan \
  --table-name semantic-layer-insurance-data \
  --limit 5 \
  --output json | jq '.Items'

# Query specific party
aws dynamodb get-item \
  --table-name semantic-layer-insurance-data \
  --key '{"pk":{"S":"PARTY#PARTY000001"},"sk":{"S":"#METADATA"}}'
```

### Check CDK Deployment

```bash
# View stack outputs
aws cloudformation describe-stacks \
  --stack-name semantic-layer-dynamodb \
  --query 'Stacks[0].Outputs'

# Check custom resource status
aws cloudformation describe-stack-events \
  --stack-name semantic-layer-dynamodb \
  --max-items 10
```

### Check Lambda Logs

```bash
# View data loader function logs
aws logs tail /aws/lambda/semantic-layer-dynamodb-SyntheticDataLoader* --follow
```

## Troubleshooting

### Issue: Data files not found

**Error**: Custom resource fails with "File not found"

**Solution**: Ensure data exists at correct location

```bash
ls -la data/complete_synthetic_data/
# Should show 12 JSON files
```

### Issue: Lambda timeout

**Error**: Custom resource times out after 15 minutes

**Solution**: Check Lambda logs for errors

```bash
aws logs tail /aws/lambda/semantic-layer-dynamodb-SyntheticDataLoader* --follow
```

### Issue: Table name mismatch

**Error**: Script cannot find table

**Solution**: Use correct table name from stack output

```bash
# Get table name
aws cloudformation describe-stacks \
  --stack-name semantic-layer-dynamodb \
  --query 'Stacks[0].Outputs[?OutputKey==`InsuranceTableName`].OutputValue' \
  --output text

# Use in script
python3 load_to_dynamodb.py semantic-layer-insurance-data us-east-1
```

### Issue: Permission errors

**Error**: Access denied during data loading

**Solution**: Verify IAM permissions for DynamoDB

```bash
# Check current identity
aws sts get-caller-identity

# Verify DynamoDB permissions
aws iam get-user-policy --user-name YOUR_USER --policy-name DynamoDBAccess
```

## Data Quality Metrics

- **Completeness**: All 1,349 columns from data dictionary
- **Referential Integrity**: Proper ID relationships maintained
- **Realistic Values**: Valid ranges for all fields
- **Date Consistency**: Proper temporal ordering
- **Audit Trail**: Created/updated timestamps on all records
- **DynamoDB Ready**: Proper pk/sk key structure

## Next Steps

After successful deployment and data loading:

1. Test access patterns with sample queries
2. Configure Athena federated queries for analytics
3. Set up Glue crawler for schema discovery
4. Build semantic layer abstraction for business queries
5. Connect to QuickSight or other BI tools
6. Implement additional GSIs for new access patterns
7. Set up monitoring and alarms for table metrics

## Performance Considerations

- **Initial Load Time**: 5-10 minutes via CDK, 2-3 minutes via script
- **Table Size**: ~11 MB of data, ~45,000 items
- **Batch Operations**: 25 items per batch write
- **Billing Mode**: PAY_PER_REQUEST (no provisioned capacity needed)
- **GSI Overhead**: Additional write costs for 3 GSIs

## Summary

This comprehensive setup provides:
- **12 insurance tables** with complete schema coverage
- **All 1,349 columns** from data dictionary
- **Two loading methods** (automatic CDK vs manual script)
- **Production-ready infrastructure** for semantic layer development

The synthetic data and DynamoDB infrastructure are now ready for:
- Application development and testing
- Performance and load testing
- Analytics and reporting workloads
- Semantic layer query abstraction
- Demo and training environments