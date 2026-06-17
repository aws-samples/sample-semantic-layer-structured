/**
 * Per-group Lake Formation grants (item #4 — oauth-obo-identity-passthrough).
 *
 * Provisions two IAM roles trusted by Cognito + AgentCore Identity:
 *
 *   * DataAdminRole  — for the `Admins` Cognito group. Lake Formation
 *     `ALL` grants on every Glue database registered with the data lake.
 *     Used by stewards (admin flow: build/edit ontology, enrich
 *     metadata).
 *
 *   * DataReaderRole — for the `QueryUsers` Cognito group. Lake Formation
 *     `SELECT` + `DESCRIBE` grants on the same databases. Used by
 *     end-user query flows.
 *
 * The roles are shaped to be assumed by AgentCore Identity's OBO token
 * exchange so the user-initiated query path runs as DataReader (or
 * DataAdmin) instead of the agent service identity. The OBO middleware
 * in `lambda/rest-api/services/obo_middleware.py` already routes
 * exchanged tokens through `agentcore_service.stream_chat`.
 *
 * The Cognito group → role binding is typically set on the Identity
 * Pool's `CfnIdentityPoolRoleAttachment` `roleMappings.rules` table,
 * but doing that in the auth stack here would require a wider
 * refactor. For now we expose the role ARNs as outputs and wire them
 * into the identity pool via a small custom resource.
 */
import {
  Aws,
  CfnOutput,
  aws_cognito as cognito,
  aws_iam as iam,
  aws_lakeformation as lakeformation,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface LakeFormationGroupGrantsProps {
  readonly userPool: cognito.IUserPool;
  /** Glue databases the data-reader / data-admin roles should be granted on. */
  readonly databases: { readonly name: string; readonly catalogId?: string }[];
  /** Cognito identity pool id — required so the federated trust policy can
   *  scope ``cognito-identity.amazonaws.com:aud`` to this pool. Without
   *  this scoping any Cognito identity pool in the account could call
   *  AssumeRoleWithWebIdentity against these roles. */
  readonly identityPoolId: string;
}

export class LakeFormationGroupGrants extends Construct {
  public readonly dataAdminRole: iam.Role;
  public readonly dataReaderRole: iam.Role;
  /** Cognito groups created here (so callers can avoid creating duplicates). */
  public readonly adminsGroup: cognito.CfnUserPoolGroup;
  public readonly queryUsersGroup: cognito.CfnUserPoolGroup;

  constructor(scope: Construct, id: string, props: LakeFormationGroupGrantsProps) {
    super(scope, id);

    // Federated trust scoped to THIS identity pool. Without the
    // `cognito-identity.amazonaws.com:aud` condition any pool in the
    // account could call AssumeRoleWithWebIdentity against these roles.
    // The `amr` condition restricts to authenticated logins only —
    // unauthenticated identities can't assume DataAdmin/DataReader.
    const cognitoFederatedTrust = new iam.FederatedPrincipal(
      'cognito-identity.amazonaws.com',
      {
        StringEquals: {
          'cognito-identity.amazonaws.com:aud': props.identityPoolId,
        },
        'ForAnyValue:StringLike': {
          'cognito-identity.amazonaws.com:amr': 'authenticated',
        },
      },
      'sts:AssumeRoleWithWebIdentity'
    );

    this.dataAdminRole = new iam.Role(this, 'DataAdminRole', {
      assumedBy: cognitoFederatedTrust,
      description:
        'OBO-target role for Cognito Admins group; Lake Formation ALL on registered databases.',
    });

    this.dataReaderRole = new iam.Role(this, 'DataReaderRole', {
      assumedBy: cognitoFederatedTrust,
      description: 'OBO-target role for Cognito QueryUsers group; Lake Formation SELECT+DESCRIBE.',
    });

    // ---- Lake Formation grants -------------------------------------

    // NOTE on Lake Formation grants:
    //
    // The roles are provisioned here, but the LF database permissions
    // (ALL on databases for DataAdmin, SELECT+DESCRIBE for DataReader)
    // are NOT created by this construct. Two reasons:
    //
    //   1. CFN-resource grants require the *grantor* CFN principal to
    //      already be a Lake Formation admin. The auth stack's deploy
    //      principal (the CDK deploy role) is added to the LF admin chain
    //      via dataLakeStack.lfGrantSingletonRoleArn, but that role lives
    //      in the data-lake stack — it isn't the principal CFN uses to
    //      execute auth-stack changes, so CfnPermissions creates from this
    //      stack get rejected with "Permissions modification is invalid".
    //
    //   2. Database existence varies by deployment flag (S3 Tables /
    //      iceberg databases exist only in some flag combinations under
    //      a federated catalog).
    //
    // Resolution: provision the IAM roles + Cognito groups here. Operators
    // grant LF permissions post-deploy via the console or via a separate
    // grant-script that runs as the LF admin. Once the OBO + LF migration
    // (item #4 Phase 2) is fully wired, move grants into a dedicated
    // construct that runs through the lfGrantSingletonRole.
    //
    // The ``databases`` prop is retained on the interface for future use.
    void props.databases;

    // ---- Cognito groups --------------------------------------------

    this.adminsGroup = new cognito.CfnUserPoolGroup(this, 'AdminsGroup', {
      userPoolId: props.userPool.userPoolId,
      groupName: 'Admins',
      description: 'Stewards — full LF privileges on data lake.',
      roleArn: this.dataAdminRole.roleArn,
      precedence: 1,
    });
    this.queryUsersGroup = new cognito.CfnUserPoolGroup(this, 'QueryUsersGroup', {
      userPoolId: props.userPool.userPoolId,
      groupName: 'QueryUsers',
      description: 'End users — SELECT-only via OBO token exchange.',
      roleArn: this.dataReaderRole.roleArn,
      precedence: 10,
    });

    new CfnOutput(this, 'DataAdminRoleArn', {
      value: this.dataAdminRole.roleArn,
      description: 'IAM role assumed by Admins via OBO token exchange',
    });
    new CfnOutput(this, 'DataReaderRoleArn', {
      value: this.dataReaderRole.roleArn,
      description: 'IAM role assumed by QueryUsers via OBO token exchange',
    });
  }
}
