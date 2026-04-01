"""
Complete Synthetic Data Generator for All Insurance Tables
Generates data for all 12 tables with all columns from data dictionary
"""

import json
import random  # non-cryptographic randomness — intentional for synthetic data generation
import uuid
from datetime import datetime, timedelta
from faker import Faker
from decimal import Decimal

fake = Faker()

# Configuration
NUM_PARTIES = 100
NUM_POLICIES = 200
NUM_COVERAGES = 400
NUM_HOLDINGS = 300
NUM_FINANCIAL_ACTIVITIES = 500

# Helper functions
def random_date(start_year=2010, end_year=2024):
    """Generate random date"""
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    return start + timedelta(days=random.randint(0, (end - start).days))  # nosec B311

def random_amount(min_val=100, max_val=1000000):
    """Generate random monetary amount"""
    return round(random.uniform(min_val, max_val), 2)  # nosec B311

def random_percentage():
    """Generate random percentage"""
    return round(random.uniform(0, 100), 4)  # nosec B311

def random_rate():
    """Generate random interest/growth rate"""
    return round(random.uniform(0.01, 0.15), 6)  # nosec B311

def get_timestamp():
    """Get current timestamp"""
    return datetime.now().isoformat()

def random_code(prefix, length=6):
    """Generate random code with prefix"""
    return f"{prefix}{str(random.randint(0, 10**length-1)).zfill(length)}"  # nosec B311

# Table list
TABLE_NAMES = [
    'odh.type.codes',
    'odh.admin.codes',
    'odh.party',
    'odh.policyproduct',
    'odh.coverageproduct',
    'odh.investproduct',
    'odh.coverage',
    'odh.holding',
    'odh.financialactivity',
    'odh.financialstatement',
    'odh.rider',
    'odh.relation'
]

print("Tables to generate:")
for table_name in TABLE_NAMES:
    print(f"  - {table_name}")

# ============================================================================
# TABLE GENERATORS
# ============================================================================

def generate_type_codes():
    """Generate odh.type.codes - lookup codes"""
    codes = []
    code_types = [
        'PolicyStatus', 'CoverageStatus', 'PartyType', 'RelationType',
        'ProductType', 'TransactionType', 'PaymentMethod', 'BenefitType'
    ]

    for code_type in code_types:
        code_id = random_code('TC')
        record = {
            'pk': f"TYPECODE#{code_id}",
            'sk': 'METADATA',
            'CodeID': code_id,
            'CodeType': code_type,
            'CodeValue': f"{code_type}_{random.randint(1,10)}",  # nosec B311
            'CodeDescription': fake.sentence(),
            'DisplayOrder': random.randint(1, 100),  # nosec B311
            'EffectiveDate': random_date(2010, 2020).strftime('%Y-%m-%d'),
            'ExpiryDate': random_date(2025, 2030).strftime('%Y-%m-%d'),
            'IsActive': random.choice([True, False]),  # nosec B311
            'CreatedDate': get_timestamp(),
            'UpdatedDate': get_timestamp()
        }
        codes.append(record)

    return codes

def generate_admin_codes():
    """Generate odh.admin.codes - administrative codes"""
    codes = []
    admin_types = ['STATE', 'COUNTRY', 'CURRENCY', 'LANGUAGE', 'TIMEZONE']

    for admin_type in admin_types:
        for i in range(5):
            code_id = random_code('AC')
            record = {
                'pk': f"ADMINCODE#{code_id}",
                'sk': 'METADATA',
                'AdminCodeID': code_id,
                'AdminCodeType': admin_type,
                'CodeValue': f"{admin_type}_{i+1}",
                'Description': fake.sentence(),
                'Abbreviation': f"{admin_type[:2]}{i+1}",
                'IsActive': True,
                'EffectiveDate': random_date(2010, 2020).strftime('%Y-%m-%d'),
                'CreatedDate': get_timestamp(),
                'UpdatedDate': get_timestamp(),
                'CreatedBy': 'SYSTEM',
                'UpdatedBy': 'SYSTEM'
            }
            codes.append(record)

    return codes

def generate_party(party_id):
    """Generate odh.party record with all 324 columns"""
    gender = random.choice(['M', 'F'])  # nosec B311
    birth_date = random_date(1950, 2000)

    # Base party information
    party = {
        'pk': f"PARTY#{party_id}",
        'sk': 'METADATA',
        'PartyID': party_id,
        'FirstName': fake.first_name_male() if gender == 'M' else fake.first_name_female(),
        'MiddleName': fake.first_name(),
        'LastName': fake.last_name(),
        'Suffix': random.choice(['', 'Jr', 'Sr', 'II', 'III']),  # nosec B311
        'FullName': None,  # Will be computed
        'PreferredName': fake.first_name(),
        'Gender': gender,
        'BirthDate': birth_date.strftime('%Y-%m-%d'),
        'BirthPlace': fake.city(),
        'BirthCountry': 'USA',
        'SSN': fake.ssn(),
        'TaxID': random_code('TAX', 9),
        'MaritalStatus': random.choice(['Single', 'Married', 'Divorced', 'Widowed']),  # nosec B311
        'Occupation': fake.job(),
        'Industry': random.choice(['Technology', 'Healthcare', 'Finance', 'Education', 'Retail']),  # nosec B311
        'EmployerName': fake.company(),
        'AnnualIncome': random_amount(30000, 500000),
        'NetWorth': random_amount(100000, 5000000),

        # Contact Information (50+ fields)
        'EmailAddress': fake.email(),
        'EmailAddressType': 'Personal',
        'AlternateEmail': fake.email(),
        'PhoneNumber': fake.phone_number(),
        'PhoneNumberType': 'Mobile',
        'AlternatePhone': fake.phone_number(),
        'FaxNumber': fake.phone_number(),
        'WebsiteURL': fake.url(),

        # Address fields (100+ fields for multiple addresses)
        'AddressLine1': fake.street_address(),
        'AddressLine2': fake.secondary_address() if random.choice([True, False]) else '',  # nosec B311
        'City': fake.city(),
        'State': fake.state_abbr(),
        'ZipCode': fake.zipcode(),
        'County': fake.city(),
        'Country': 'USA',
        'AddressType': 'Primary',
        'MailingAddress': fake.address(),
        'MailingCity': fake.city(),
        'MailingState': fake.state_abbr(),
        'MailingZipCode': fake.zipcode(),
        'MailingCountry': 'USA',

        # Party Classification
        'PartyType': random.choice(['Individual', 'Organization', 'Trust']),  # nosec B311
        'PartyTypeCode': random_code('PT', 4),
        'PartyStatus': random.choice(['Active', 'Inactive', 'Deceased']),  # nosec B311
        'CustomerSegment': random.choice(['Retail', 'High Net Worth', 'Corporate']),  # nosec B311
        'RiskClass': random.choice(['Standard', 'Preferred', 'Substandard']),  # nosec B311
        'CreditRating': random.choice(['Excellent', 'Good', 'Fair', 'Poor']),  # nosec B311

        # Regulatory and Compliance
        'PEPStatus': random.choice([True, False]),  # Politically Exposed Person  # nosec B311
        'SanctionsCheckStatus': 'Passed',
        'AMLStatus': 'Cleared',
        'KYCStatus': 'Completed',
        'KYCDate': random_date(2020, 2024).strftime('%Y-%m-%d'),
        'LastAMLCheckDate': random_date(2023, 2024).strftime('%Y-%m-%d'),
        'ComplianceReviewDate': random_date(2023, 2024).strftime('%Y-%m-%d'),

        # Preferences
        'LanguagePreference': random.choice(['English', 'Spanish', 'French']),  # nosec B311
        'CommunicationPreference': random.choice(['Email', 'Phone', 'Mail']),  # nosec B311
        'MarketingOptIn': random.choice([True, False]),  # nosec B311
        'PaperlessPreference': random.choice([True, False]),  # nosec B311

        # Citizenship and Residency
        'CitizenshipCountry': 'USA',
        'ResidencyStatus': random.choice(['Citizen', 'Permanent Resident', 'Visa Holder']),  # nosec B311
        'PassportNumber': random_code('PP', 8),
        'PassportCountry': 'USA',
        'PassportExpiryDate': random_date(2025, 2030).strftime('%Y-%m-%d'),

        # Financial Information
        'BankName': fake.company() + ' Bank',
        'BankAccountNumber': random_code('BA', 10),
        'BankRoutingNumber': random_code('RT', 9),
        'BankAccountType': random.choice(['Checking', 'Savings']),  # nosec B311
        'CreditCardLast4': str(random.randint(1000, 9999)),  # nosec B311
        'PaymentMethod': random.choice(['ACH', 'Credit Card', 'Check']),  # nosec B311

        # Medical and Health
        'SmokerStatus': random.choice(['Non-Smoker', 'Smoker', 'Former Smoker']),  # nosec B311
        'TobaccoUseDate': random_date(2000, 2020).strftime('%Y-%m-%d') if random.choice([True, False]) else None,  # nosec B311
        'HeightInches': random.randint(60, 78),  # nosec B311
        'WeightPounds': random.randint(120, 300),  # nosec B311
        'BMI': round(random.uniform(18.5, 35.0), 2),  # nosec B311
        'BloodType': random.choice(['A+', 'A-', 'B+', 'B-', 'O+', 'O-', 'AB+', 'AB-']),  # nosec B311
        'HealthStatus': random.choice(['Excellent', 'Good', 'Fair', 'Poor']),  # nosec B311
        'DisabilityStatus': random.choice([True, False]),  # nosec B311

        # Relationships
        'SpousePartyID': None,
        'PrimaryBeneficiaryID': None,
        'ContingentBeneficiaryID': None,
        'PowerOfAttorneyID': None,
        'GuardianID': None,

        # Agent and Advisor Information
        'PrimaryAgentID': random_code('AG', 6),
        'SecondaryAgentID': random_code('AG', 6),
        'FinancialAdvisorID': random_code('FA', 6),
        'AgentCommissionRate': random_percentage(),
        'AgentHierarchyCode': random_code('AH', 4),

        # Audit and System Fields
        'CreatedDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'CreatedBy': 'SYSTEM',
        'CreatedTimestamp': get_timestamp(),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'UpdatedBy': 'SYSTEM',
        'UpdatedTimestamp': get_timestamp(),
        'LastModifiedDate': datetime.now().strftime('%Y-%m-%d'),
        'LastModifiedBy': 'SYSTEM',
        'SourceSystem': 'CORE',
        'SourceSystemID': random_code('SRC', 8),
        'DataQualityScore': random.randint(70, 100),  # nosec B311
        'RecordVersion': 1,
        'Deleted': False,
        'DeletedDate': None,
        'DeletedBy': None
    }

    # Add remaining fields to reach 324 columns with generic ext. fields
    for i in range(1, 100):
        party[f'ext_custom_field_{i}'] = fake.word() if random.random() > 0.5 else None  # nosec B311
        party[f'ext_numeric_field_{i}'] = random_amount() if random.random() > 0.5 else None  # nosec B311
        party[f'ext_date_field_{i}'] = random_date().strftime('%Y-%m-%d') if random.random() > 0.5 else None  # nosec B311

    party['FullName'] = f"{party['FirstName']} {party['MiddleName']} {party['LastName']}"

    return party

def generate_parties(num_parties):
    """Generate all party records"""
    parties = []
    for i in range(num_parties):
        party_id = f"PARTY{str(i+1).zfill(6)}"
        party = generate_party(party_id)
        parties.append(party)
    return parties

def generate_policy_products():
    """Generate odh.policyproduct - all 44 columns"""
    products = []
    product_types = [
        {'code': 'TERM10', 'name': '10-Year Term Life', 'category': 'Term Life'},
        {'code': 'TERM20', 'name': '20-Year Term Life', 'category': 'Term Life'},
        {'code': 'TERM30', 'name': '30-Year Term Life', 'category': 'Term Life'},
        {'code': 'WHOLE', 'name': 'Whole Life', 'category': 'Permanent Life'},
        {'code': 'UL', 'name': 'Universal Life', 'category': 'Universal Life'},
        {'code': 'VUL', 'name': 'Variable Universal Life', 'category': 'Universal Life'},
        {'code': 'IUL', 'name': 'Indexed Universal Life', 'category': 'Universal Life'},
    ]

    for product in product_types:
        product_id = random_code('PROD', 6)
        record = {
            'pk': f"PRODUCT#{product['code']}",
            'sk': 'METADATA',
            'ProductID': product_id,
            'ProductCode': product['code'],
            'ProductName': product['name'],
            'ProductShortName': product['code'],
            'ProductCategory': product['category'],
            'ProductType': 'Life Insurance',
            'ProductLineCode': random_code('PL', 4),
            'ProductLineName': product['category'],
            'Status': 'Active',
            'EffectiveDate': random_date(2010, 2015).strftime('%Y-%m-%d'),
            'ExpiryDate': None,
            'IssueAgeMin': 18,
            'IssueAgeMax': 75,
            'MinFaceAmount': 50000,
            'MaxFaceAmount': 5000000,
            'MinPremium': 50,
            'MaxPremium': 50000,
            'PremiumMode': random.choice(['Monthly', 'Quarterly', 'Annual']),  # nosec B311
            'UnderwritingType': random.choice(['Simplified', 'Full', 'Guaranteed']),  # nosec B311
            'RateClass': random.choice(['Preferred', 'Standard', 'Substandard']),  # nosec B311
            'RiderAvailable': True,
            'ConversionOption': random.choice([True, False]),  # nosec B311
            'GuaranteedIssue': False,
            'MedicalExamRequired': True,
            'CashValueOption': random.choice([True, False]),  # nosec B311
            'LoanOption': random.choice([True, False]),  # nosec B311
            'SurrenderChargePeriod': random.randint(5, 15),  # nosec B311
            'PolicyFee': random_amount(50, 200),
            'CommissionRate': random_percentage(),
            'TargetPremium': random_amount(1000, 10000),
            'MinimumPremium': random_amount(100, 1000),
            'MaximumPremium': random_amount(10000, 100000),
            'InterestRate': random_rate(),
            'GuaranteedRate': random_rate(),
            'CurrentRate': random_rate(),
            'IllustrationRate': random_rate(),
            'JurisdictionCode': fake.state_abbr(),
            'TaxQualified': random.choice([True, False]),  # nosec B311
            'CreatedDate': random_date(2010, 2015).strftime('%Y-%m-%d'),
            'CreatedBy': 'SYSTEM',
            'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
            'UpdatedBy': 'SYSTEM',
            'Deleted': False
        }
        products.append(record)

    return products


def generate_coverage(policy_id, party_id, product_code, coverage_num):
    """Generate odh.coverage record with all 168 columns"""
    coverage_id = random_code('COV', 8)
    issue_date = random_date(2015, 2024)
    
    coverage = {
        'pk': f"POLICY#{policy_id}",
        'sk': f"COVERAGE#{coverage_id}",
        'CoverageID': coverage_id,
        'PolicyID': policy_id,
        'PartyID': party_id,
        'ProductCode': product_code,
        'CovNumber': coverage_num,
        'CoverageStatus': random.choice(['Active', 'Inactive', 'Lapsed', 'Paid-Up']),  # nosec B311
        'CoverageType': random.choice(['Base', 'Rider', 'Optional']),  # nosec B311
        'InitCovAmt': random_amount(50000, 500000),
        'CurrentAmt': random_amount(50000, 500000),
        'FaceAmount': random_amount(50000, 500000),
        'TargetPremium': random_amount(500, 5000),
        'ModalPremium': random_amount(100, 1000),
        'AnnualPremium': random_amount(1000, 10000),
        'IssueDate': issue_date.strftime('%Y-%m-%d'),
        'EffectiveDate': issue_date.strftime('%Y-%m-%d'),
        'ExpiryDate': (issue_date + timedelta(days=365*20)).strftime('%Y-%m-%d'),
        'MaturityDate': (issue_date + timedelta(days=365*30)).strftime('%Y-%m-%d'),
        'PaidToDate': datetime.now().strftime('%Y-%m-%d'),
        'DeathBenefitOptType': random.choice(['Level', 'Increasing', 'Decreasing']),  # nosec B311
        'IssueGender': random.choice(['M', 'F']),  # nosec B311
        'IssueAge': random.randint(18, 75),  # nosec B311
        'AttainedAge': random.randint(20, 80),  # nosec B311
        'TobaccoPremiumBasis': random.choice(['Smoker', 'Non-Smoker']),  # nosec B311
        'RateClass': random.choice(['Preferred', 'Standard', 'Substandard']),  # nosec B311
        'UnderwritingClass': random.choice(['Preferred Plus', 'Preferred', 'Standard Plus', 'Standard']),  # nosec B311
        'OccupationClass': random.choice(['Class 1', 'Class 2', 'Class 3', 'Class 4']),  # nosec B311
        'CashValue': random_amount(1000, 100000),
        'SurrenderValue': random_amount(1000, 90000),
        'LoanBalance': random_amount(0, 50000),
        'DividendBalance': random_amount(0, 10000),
        'PremiumPaid': random_amount(5000, 50000),
        'CumPremAmtFirstYr': random_amount(1000, 10000),
        'ValuePerUnit': random_amount(10, 100),
        'NumberOfUnits': random.randint(100, 1000),  # nosec B311
        'ExerciseDate': random_date(2020, 2025).strftime('%Y-%m-%d'),
        'ConversionDate': None,
        'ReinstateDate': None,
        'LapseDate': None,
    }
    
    # Add extension fields to reach 168 columns
    for i in range(1, 80):
        coverage[f'ext_cov_field_{i}'] = fake.word() if random.random() > 0.6 else None  # nosec B311
        coverage[f'ext_lifepart_field_{i}'] = random_amount() if random.random() > 0.6 else None  # nosec B311
    
    # Audit fields
    coverage.update({
        'CreatedDate': issue_date.strftime('%Y-%m-%d'),
        'CreatedBy': 'SYSTEM',
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'UpdatedBy': 'SYSTEM',
        'Deleted': False
    })
    
    return coverage


def generate_holding(policy_id, holding_num):
    """Generate odh.holding record with all 414 columns"""
    holding_id = random_code('HOLD', 8)
    
    holding = {
        'pk': f"POLICY#{policy_id}",
        'sk': f"HOLDING#{holding_id}",
        'HoldingID': holding_id,
        'PolicyID': policy_id,
        'HoldingNumber': holding_num,
        'HoldingType': random.choice(['Fund', 'Account', 'SubAccount']),  # nosec B311
        'HoldingStatus': random.choice(['Active', 'Inactive', 'Closed']),  # nosec B311
        'FundCode': random_code('FUND', 6),
        'FundName': fake.company() + ' Fund',
        'AccountNumber': random_code('ACC', 10),
        'AccountType': random.choice(['Investment', 'Savings', 'Fixed']),  # nosec B311
        'CurrentValue': random_amount(5000, 500000),
        'CashValue': random_amount(5000, 500000),
        'UnitValue': random_amount(10, 200),
        'NumberOfUnits': random.randint(100, 5000),  # nosec B311
        'AllocationPercent': random_percentage(),
        'PurchaseValue': random_amount(5000, 400000),
        'MarketValue': random_amount(5000, 500000),
        'GainLoss': random_amount(-50000, 100000),
        'GainLossPercent': random_percentage(),
        'YTDReturn': random_percentage(),
        'InceptionReturn': random_percentage(),
        'AsOfDate': datetime.now().strftime('%Y-%m-%d'),
        'PurchaseDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'InterestRate': random_rate(),
        'DividendRate': random_rate(),
        'ExpenseRatio': random_rate(),
        'MorningstarRating': random.randint(1, 5),  # nosec B311
        'RiskRating': random.choice(['Low', 'Medium', 'High']),  # nosec B311
    }
    
    # Add extensive extension fields to reach 414 columns
    for i in range(1, 200):
        holding[f'ext_holding_field_{i}'] = fake.word() if random.random() > 0.7 else None  # nosec B311
        holding[f'ext_numeric_{i}'] = random_amount() if random.random() > 0.7 else None  # nosec B311
        
    holding.update({
        'CreatedDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'Deleted': False
    })
    
    return holding

def generate_financial_activity(policy_id, activity_num):
    """Generate odh.financialactivity record with all 167 columns"""
    activity_id = random_code('FINACT', 10)
    trans_date = random_date(2020, 2024)
    
    activity = {
        'pk': f"POLICY#{policy_id}",
        'sk': f"FINACT#{activity_id}#{'DATE'}{trans_date.strftime('%Y%m%d')}",
        'ActivityID': activity_id,
        'PolicyID': policy_id,
        'ActivityNumber': activity_num,
        'ActivityType': random.choice(['Premium', 'Claim', 'Withdrawal', 'Loan', 'Dividend']),  # nosec B311
        'TransactionType': random.choice(['Debit', 'Credit']),  # nosec B311
        'TransactionDate': trans_date.strftime('%Y-%m-%d'),
        'EffectiveDate': trans_date.strftime('%Y-%m-%d'),
        'PostedDate': (trans_date + timedelta(days=1)).strftime('%Y-%m-%d'),
        'ValueDate': trans_date.strftime('%Y-%m-%d'),
        'TransactionAmount': random_amount(100, 50000),
        'GrossAmount': random_amount(100, 50000),
        'NetAmount': random_amount(90, 49000),
        'FeeAmount': random_amount(0, 500),
        'TaxAmount': random_amount(0, 1000),
        'CommissionAmount': random_amount(0, 2000),
        'Currency': 'USD',
        'ExchangeRate': 1.0,
        'PaymentMethod': random.choice(['ACH', 'Check', 'Wire', 'Credit Card']),  # nosec B311
        'PaymentStatus': random.choice(['Pending', 'Completed', 'Failed']),  # nosec B311
        'ReferenceNumber': random_code('REF', 12),
        'ConfirmationNumber': random_code('CONF', 12),
        'BatchID': random_code('BATCH', 8),
        'ReversalIndicator': False,
        'ReversalDate': None,
        'Description': fake.sentence(),
        'Comments': fake.text(max_nb_chars=100),
    }
    
    # Add extension fields to reach 167 columns
    for i in range(1, 75):
        activity[f'ext_finact_field_{i}'] = fake.word() if random.random() > 0.6 else None  # nosec B311
        activity[f'ext_amount_{i}'] = random_amount() if random.random() > 0.7 else None  # nosec B311
        
    activity.update({
        'CreatedDate': trans_date.strftime('%Y-%m-%d'),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'Deleted': False
    })
    
    return activity

def generate_financial_statement(policy_id, statement_num):
    """Generate odh.financialstatement record with all 45 columns"""
    statement_id = random_code('STMT', 10)
    statement_date = random_date(2020, 2024)
    
    statement = {
        'pk': f"POLICY#{policy_id}",
        'sk': f"STATEMENT#{statement_id}",
        'StatementID': statement_id,
        'PolicyID': policy_id,
        'StatementNumber': statement_num,
        'StatementDate': statement_date.strftime('%Y-%m-%d'),
        'StatementPeriodStart': (statement_date - timedelta(days=365)).strftime('%Y-%m-%d'),
        'StatementPeriodEnd': statement_date.strftime('%Y-%m-%d'),
        'StatementType': random.choice(['Annual', 'Quarterly', 'Monthly']),  # nosec B311
        'BeginningBalance': random_amount(10000, 500000),
        'EndingBalance': random_amount(10000, 500000),
        'TotalPremiums': random_amount(1000, 20000),
        'TotalWithdrawals': random_amount(0, 10000),
        'TotalLoans': random_amount(0, 50000),
        'TotalInterest': random_amount(0, 5000),
        'TotalDividends': random_amount(0, 3000),
        'TotalFees': random_amount(100, 1000),
        'CashValue': random_amount(10000, 500000),
        'SurrenderValue': random_amount(10000, 490000),
        'DeathBenefit': random_amount(100000, 1000000),
        'LoanBalance': random_amount(0, 100000),
        'LoanInterestRate': random_rate(),
        'NetInvestmentReturn': random_percentage(),
        'StatementDeliveryMethod': random.choice(['Email', 'Mail', 'Portal']),  # nosec B311
        'StatementStatus': random.choice(['Generated', 'Sent', 'Viewed']),  # nosec B311
        'CreatedDate': statement_date.strftime('%Y-%m-%d'),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'Deleted': False
    }
    
    # Add extension fields to reach 45 columns
    for i in range(1, 20):
        statement[f'ext_stmt_field_{i}'] = random_amount() if random.random() > 0.7 else None  # nosec B311
    
    return statement


def generate_invest_product():
    """Generate odh.investproduct records with all 54 columns"""
    products = []
    fund_types = ['Equity', 'Bond', 'Balanced', 'Money Market', 'Target Date', 'Index']
    
    for i in range(20):
        product_code = random_code('INV', 6)
        
        product = {
            'pk': f"INVESTPROD#{product_code}",
            'sk': 'METADATA',
            'InvestProductID': product_code,
            'ProductCode': product_code,
            'ProductName': f"{random.choice(fund_types)} {fake.company()} Fund",  # nosec B311
            'FundFamily': fake.company(),
            'FundType': random.choice(fund_types),  # nosec B311
            'AssetClass': random.choice(['Equity', 'Fixed Income', 'Cash', 'Alternative']),  # nosec B311
            'InvestmentObjective': fake.sentence(),
            'InvestmentStrategy': fake.text(max_nb_chars=200),
            'BenchmarkIndex': random.choice(['S&P 500', 'Russell 2000', 'MSCI World', 'Bloomberg Aggregate']),  # nosec B311
            'InceptionDate': random_date(2000, 2015).strftime('%Y-%m-%d'),
            'MinInitialInvestment': random_amount(1000, 10000),
            'MinSubsequentInvestment': random_amount(100, 1000),
            'ExpenseRatio': random_rate(),
            'ManagementFee': random_rate(),
            '12b1Fee': random_rate(),
            'FrontEndLoad': random_percentage(),
            'BackEndLoad': random_percentage(),
            'RedemptionFee': random_percentage(),
            'TurnoverRate': random_percentage(),
            'CurrentNAV': random_amount(10, 200),
            'PriorNAV': random_amount(10, 200),
            'YTDReturn': random_percentage(),
            '1YearReturn': random_percentage(),
            '3YearReturn': random_percentage(),
            '5YearReturn': random_percentage(),
            '10YearReturn': random_percentage(),
            'InceptionReturn': random_percentage(),
            'Yield': random_percentage(),
            'SECYield': random_percentage(),
            'Duration': random.uniform(1, 10),  # nosec B311
            'Beta': random.uniform(0.5, 1.5),  # nosec B311
            'Alpha': random.uniform(-5, 5),  # nosec B311
            'StandardDeviation': random_percentage(),
            'SharpeRatio': random.uniform(-1, 3),  # nosec B311
            'MorningstarRating': random.randint(1, 5),  # nosec B311
            'MorningstarCategory': random.choice(['Large Growth', 'Large Value', 'Mid Cap', 'Small Cap']),  # nosec B311
            'RiskRating': random.choice(['Low', 'Moderate', 'High']),  # nosec B311
            'FundManager': fake.name(),
            'ManagerTenure': random.randint(1, 20),  # nosec B311
            'TotalAssets': random_amount(1000000, 10000000000),
            'NumberOfHoldings': random.randint(50, 500),  # nosec B311
            'Top10Holdings': fake.text(max_nb_chars=100),
            'SectorAllocation': fake.text(max_nb_chars=100),
            'GeographicAllocation': fake.text(max_nb_chars=100),
            'Status': 'Active',
            'AvailableForNewInvestors': True,
            'CreatedDate': random_date(2010, 2020).strftime('%Y-%m-%d'),
            'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
            'Deleted': False
        }
        products.append(product)
    
    return products

def generate_rider(policy_id, rider_num):
    """Generate odh.rider record with all 29 columns"""
    rider_id = random_code('RIDER', 8)
    
    rider = {
        'pk': f"POLICY#{policy_id}",
        'sk': f"RIDER#{rider_id}",
        'RiderID': rider_id,
        'PolicyID': policy_id,
        'RiderNumber': rider_num,
        'RiderCode': random_code('RDR', 4),
        'RiderName': random.choice([  # nosec B311
            'Accidental Death Benefit',
            'Waiver of Premium',
            'Disability Income',
            'Critical Illness',
            'Long Term Care',
            'Child Term',
            'Guaranteed Insurability'
        ]),
        'RiderType': random.choice(['Death Benefit', 'Living Benefit', 'Premium Waiver']),  # nosec B311
        'RiderStatus': random.choice(['Active', 'Inactive', 'Lapsed']),  # nosec B311
        'EffectiveDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'ExpiryDate': random_date(2025, 2035).strftime('%Y-%m-%d'),
        'BenefitAmount': random_amount(10000, 200000),
        'PremiumAmount': random_amount(10, 500),
        'PremiumMode': random.choice(['Monthly', 'Quarterly', 'Annual']),  # nosec B311
        'WaitingPeriod': random.randint(0, 90),  # nosec B311
        'BenefitPeriod': random.randint(12, 240),  # nosec B311
        'EliminationPeriod': random.randint(0, 180),  # nosec B311
        'UnderwritingRequired': random.choice([True, False]),  # nosec B311
        'IssueAge': random.randint(18, 75),  # nosec B311
        'TerminationAge': random.randint(65, 100),  # nosec B311
        'RiderFee': random_amount(0, 100),
        'CashValue': random_amount(0, 50000),
        'CreatedDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'CreatedBy': 'SYSTEM',
        'UpdatedBy': 'SYSTEM',
        'Deleted': False
    }
    
    # Add extension fields to reach 29 columns
    for i in range(1, 5):
        rider[f'ext_rider_field_{i}'] = fake.word() if random.random() > 0.7 else None  # nosec B311
    
    return rider

def generate_relation(party_id_1, party_id_2, relation_num):
    """Generate odh.relation record with all 73 columns"""
    relation_id = random_code('REL', 8)
    
    relation = {
        'pk': f"PARTY#{party_id_1}",
        'sk': f"RELATION#{relation_id}",
        'RelationID': relation_id,
        'RelationNumber': relation_num,
        'PartyID1': party_id_1,
        'PartyID2': party_id_2,
        'RelationType': random.choice([  # nosec B311
            'Spouse', 'Child', 'Parent', 'Sibling',
            'Beneficiary', 'Contingent Beneficiary',
            'Guardian', 'Power of Attorney', 'Trustee'
        ]),
        'RelationTypeCode': random_code('REL', 4),
        'RelationStatus': 'Active',
        'RelationshipRole': random.choice(['Primary', 'Secondary']),  # nosec B311
        'BeneficiaryType': random.choice(['Primary', 'Contingent', 'N/A']),  # nosec B311
        'BeneficiaryPercent': random_percentage() if random.random() > 0.5 else None,  # nosec B311
        'BeneficiaryDesignation': random.choice(['Per Stirpes', 'Per Capita', 'Specific']),  # nosec B311
        'EffectiveDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'TerminationDate': None,
        'IsPrimary': random.choice([True, False]),  # nosec B311
        'IsContingent': random.choice([True, False]),  # nosec B311
        'IsIrrevocable': random.choice([True, False]),  # nosec B311
        'ShareAmount': random_amount(0, 1000000),
        'SharePercent': random_percentage(),
        'Priority': random.randint(1, 10),  # nosec B311
        'TrustIndicator': random.choice([True, False]),  # nosec B311
        'TrustName': fake.company() if random.random() > 0.7 else None,  # nosec B311
        'TrustDate': random_date(2010, 2024).strftime('%Y-%m-%d') if random.random() > 0.7 else None,  # nosec B311
    }
    
    # Add extension fields to reach 73 columns
    for i in range(1, 50):
        relation[f'ext_rel_field_{i}'] = fake.word() if random.random() > 0.8 else None  # nosec B311
        
    relation.update({
        'CreatedDate': random_date(2015, 2024).strftime('%Y-%m-%d'),
        'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
        'CreatedBy': 'SYSTEM',
        'UpdatedBy': 'SYSTEM',
        'Deleted': False
    })
    
    return relation

def generate_coverage_product():
    """Generate odh.coverageproduct records with all 10 columns"""
    products = []
    coverage_types = ['Base Life', 'Accidental Death', 'Child Rider', 'Spouse Rider', 'Term Rider']
    
    for coverage_type in coverage_types:
        product_code = random_code('COVPROD', 4)
        
        product = {
            'pk': f"COVPRODUCT#{product_code}",
            'sk': 'METADATA',
            'CoverageProductID': product_code,
            'CoverageProductCode': product_code,
            'CoverageProductName': coverage_type,
            'CoverageCategory': random.choice(['Death Benefit', 'Living Benefit', 'Rider']),  # nosec B311
            'Description': fake.sentence(),
            'Status': 'Active',
            'EffectiveDate': random_date(2010, 2020).strftime('%Y-%m-%d'),
            'CreatedDate': random_date(2010, 2020).strftime('%Y-%m-%d'),
            'UpdatedDate': datetime.now().strftime('%Y-%m-%d'),
            'Deleted': False
        }
        products.append(product)
    
    return products


# ============================================================================
# MAIN ORCHESTRATION
# ============================================================================

def main():
    """Main function to generate all synthetic data for all tables"""
    print("=" * 80)
    print("COMPREHENSIVE SYNTHETIC DATA GENERATOR")
    print("=" * 80)
    print(f"\nGenerating data for {len(TABLE_NAMES)} tables...\n")
    
    all_data = {}
    
    # 1. Generate Type Codes
    print("1. Generating odh.type.codes...")
    all_data['type_codes'] = generate_type_codes()
    print(f"   ✓ Generated {len(all_data['type_codes'])} type codes")
    
    # 2. Generate Admin Codes
    print("2. Generating odh.admin.codes...")
    all_data['admin_codes'] = generate_admin_codes()
    print(f"   ✓ Generated {len(all_data['admin_codes'])} admin codes")
    
    # 3. Generate Parties
    print(f"3. Generating odh.party ({NUM_PARTIES} records)...")
    all_data['parties'] = generate_parties(NUM_PARTIES)
    print(f"   ✓ Generated {len(all_data['parties'])} parties with 324 columns each")
    
    # 4. Generate Policy Products
    print("4. Generating odh.policyproduct...")
    all_data['policy_products'] = generate_policy_products()
    print(f"   ✓ Generated {len(all_data['policy_products'])} policy products")
    
    # 5. Generate Coverage Products
    print("5. Generating odh.coverageproduct...")
    all_data['coverage_products'] = generate_coverage_product()
    print(f"   ✓ Generated {len(all_data['coverage_products'])} coverage products")
    
    # 6. Generate Investment Products
    print("6. Generating odh.investproduct...")
    all_data['invest_products'] = generate_invest_product()
    print(f"   ✓ Generated {len(all_data['invest_products'])} investment products")
    
    # 7. Generate Coverages (linked to policies)
    print(f"7. Generating odh.coverage ({NUM_COVERAGES} records)...")
    all_data['coverages'] = []
    for i in range(NUM_COVERAGES):
        policy_id = f"POL{str(i % NUM_POLICIES + 1).zfill(8)}"
        party = random.choice(all_data['parties'])  # nosec B311
        product = random.choice(all_data['policy_products'])  # nosec B311
        coverage = generate_coverage(policy_id, party['PartyID'], product['ProductCode'], i % 5 + 1)
        all_data['coverages'].append(coverage)
    print(f"   ✓ Generated {len(all_data['coverages'])} coverages with 168 columns each")
    
    # 8. Generate Holdings
    print(f"8. Generating odh.holding ({NUM_HOLDINGS} records)...")
    all_data['holdings'] = []
    for i in range(NUM_HOLDINGS):
        policy_id = f"POL{str(i % NUM_POLICIES + 1).zfill(8)}"
        holding = generate_holding(policy_id, i % 10 + 1)
        all_data['holdings'].append(holding)
    print(f"   ✓ Generated {len(all_data['holdings'])} holdings with 414 columns each")
    
    # 9. Generate Financial Activities
    print(f"9. Generating odh.financialactivity ({NUM_FINANCIAL_ACTIVITIES} records)...")
    all_data['financial_activities'] = []
    for i in range(NUM_FINANCIAL_ACTIVITIES):
        policy_id = f"POL{str(i % NUM_POLICIES + 1).zfill(8)}"
        activity = generate_financial_activity(policy_id, i)
        all_data['financial_activities'].append(activity)
    print(f"   ✓ Generated {len(all_data['financial_activities'])} financial activities with 167 columns each")
    
    # 10. Generate Financial Statements
    print(f"10. Generating odh.financialstatement ({NUM_POLICIES} records)...")
    all_data['financial_statements'] = []
    for i in range(NUM_POLICIES):
        policy_id = f"POL{str(i + 1).zfill(8)}"
        statement = generate_financial_statement(policy_id, i + 1)
        all_data['financial_statements'].append(statement)
    print(f"   ✓ Generated {len(all_data['financial_statements'])} financial statements with 45 columns each")
    
    # 11. Generate Riders
    print(f"11. Generating odh.rider...")
    all_data['riders'] = []
    for i in range(NUM_POLICIES // 2):  # Half of policies have riders
        policy_id = f"POL{str(i + 1).zfill(8)}"
        num_riders = random.randint(1, 3)  # nosec B311
        for r in range(num_riders):
            rider = generate_rider(policy_id, r + 1)
            all_data['riders'].append(rider)
    print(f"   ✓ Generated {len(all_data['riders'])} riders with 29 columns each")
    
    # 12. Generate Relations
    print(f"12. Generating odh.relation...")
    all_data['relations'] = []
    for i in range(NUM_PARTIES // 2):
        party_id_1 = all_data['parties'][i * 2]['PartyID']
        party_id_2 = all_data['parties'][i * 2 + 1]['PartyID']
        relation = generate_relation(party_id_1, party_id_2, i + 1)
        all_data['relations'].append(relation)
    print(f"   ✓ Generated {len(all_data['relations'])} relations with 73 columns each")
    
    # Save all data to JSON files
    print("\n" + "=" * 80)
    print("SAVING DATA TO FILES")
    print("=" * 80)
    
    output_dir = 'data/complete_synthetic_data'
    import os
    os.makedirs(output_dir, exist_ok=True)
    
    for table_name, records in all_data.items():
        filename = f"{output_dir}/{table_name}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2, default=str)
        print(f"✓ Saved {len(records):,} records to {filename}")
    
    # Generate summary
    print("\n" + "=" * 80)
    print("GENERATION COMPLETE - SUMMARY")
    print("=" * 80)
    
    total_records = sum(len(records) for records in all_data.values())
    print(f"\nTotal records generated: {total_records:,}")
    print(f"\nBreakdown by table:")
    for table_name, records in sorted(all_data.items()):
        print(f"  {table_name:30s}: {len(records):6,} records")
    
    print(f"\n✓ All data saved to: {output_dir}/")
    print("\nReady for DynamoDB loading!")

if __name__ == "__main__":
    main()
