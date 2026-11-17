import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export interface LfAdminRegistrationProps {
  /** IAM role/user ARNs to register as Lake Formation administrators. */
  adminArns: string[];
}

/**
 * Additively registers Lake Formation administrators.
 *
 * ``AWS::LakeFormation::DataLakeSettings`` is a per-account SINGLETON — its
 * physical id is the bare account id. When more than one CloudFormation stack
 * declares that resource type, the second stack's CREATE fails with
 * "<account> already exists in stack <other-stack>". Several stacks in this app
 * each need their own role registered as an LF admin during their own deploy
 * (for CfnPermissions grants and glue:CreateCatalog under LF authorization), so
 * a single declarative owner does not fit.
 *
 * Instead, this construct does a read-modify-write via a custom resource:
 * get-data-lake-settings → union in the requested admin ARNs → put. Because it
 * is an ``AwsCustomResource``/Lambda (not the singleton CFN type), every stack
 * gets its own physical id and admins ACCUMULATE across stacks rather than one
 * stack clobbering another. On Delete it removes only the ARNs it added.
 */
export class LfAdminRegistration extends Construct {
  public readonly customResource: cdk.CustomResource;

  constructor(scope: Construct, id: string, props: LfAdminRegistrationProps) {
    super(scope, id);

    const fn = new lambda.Function(this, 'Fn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(2),
      code: lambda.Code.fromInline(
        [
          'import boto3',
          '',
          'lf = boto3.client("lakeformation")',
          '',
          'def _admins():',
          '    s = lf.get_data_lake_settings()["DataLakeSettings"]',
          '    return s, [a["DataLakePrincipalIdentifier"] for a in s.get("DataLakeAdmins", [])]',
          '',
          'def _put(settings, arns):',
          '    settings["DataLakeAdmins"] = [{"DataLakePrincipalIdentifier": a} for a in arns]',
          '    lf.put_data_lake_settings(DataLakeSettings=settings)',
          '',
          'def handler(event, context):',
          '    req = event["RequestType"]',
          '    want = event["ResourceProperties"].get("AdminArns", [])',
          '    settings, current = _admins()',
          '    if req == "Delete":',
          '        keep = [a for a in current if a not in want]',
          '        if keep != current:',
          '            _put(settings, keep)',
          '        return {"PhysicalResourceId": event.get("PhysicalResourceId", "lf-admin-registration")}',
          '    merged = list(current)',
          '    for a in want:',
          '        if a not in merged:',
          '            merged.append(a)',
          '    if merged != current:',
          '        _put(settings, merged)',
          '    return {"PhysicalResourceId": event.get("PhysicalResourceId", "lf-admin-registration"),',
          '            "Data": {"adminCount": str(len(merged))}}',
        ].join('\n')
      ),
    });

    // PutDataLakeSettings requires the caller be an existing LF admin (the CDK
    // bootstrap cfn-exec role is seeded as one on first touch). These are
    // account-scoped LF settings APIs with no resource ARN.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'lakeformation:GetDataLakeSettings',
          'lakeformation:PutDataLakeSettings',
        ],
        resources: ['*'],
      })
    );

    const provider = new cr.Provider(this, 'Provider', {
      onEventHandler: fn,
    });

    this.customResource = new cdk.CustomResource(this, 'Resource', {
      serviceToken: provider.serviceToken,
      properties: {
        // CloudFormation triggers an Update whenever this list changes, which
        // re-runs the additive merge. No nonce needed — the merge is idempotent.
        AdminArns: props.adminArns,
      },
    });
  }
}
