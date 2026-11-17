import { IAspect, RemovalPolicy, CfnResource } from 'aws-cdk-lib';
import { CfnPermissions } from 'aws-cdk-lib/aws-lakeformation';
import { IConstruct } from 'constructs';

/**
 * CDK Aspect that sets ``DeletionPolicy: Retain`` on every
 * ``AWS::LakeFormation::Permissions`` resource in the tree.
 *
 * Why this exists
 * ---------------
 * On stack teardown CloudFormation deletes an ``AWS::LakeFormation::Permissions``
 * resource by issuing a LakeFormation **Revoke**. That Revoke fails with
 * ``AccessDeniedException`` ("Resource does not exist or requester is not
 * authorized to access requested permissions") whenever the target database /
 * table / principal is already gone, or the target database's Glue authz is in
 * an orphaned state (grant/revoke/describe all denied even to an LF admin). A
 * single such failure strands the whole stack in ``DELETE_FAILED`` — and because
 * downstream stacks depend on it, it blocks the entire ``cdk destroy`` cycle.
 * (Observed 2026-07-28: the athena stack's ``AthenaLFDynamoDBTablePermissions``
 * jammed a full from-scratch destroy at 11/20 stacks; the only manual fix was
 * ``delete-stack --retain-resources``, which not everyone is permitted to run.)
 *
 * A leftover LakeFormation grant is harmless: it is re-created idempotently on
 * the next deploy (LF grants are declarative and de-duplicated). So the correct
 * teardown behaviour is to ORPHAN these permission resources rather than issue a
 * fragile Revoke. Applying this Aspect app-wide makes every LF-permission
 * resource — present and future — RETAIN on delete, so no LF grant can ever
 * strand a ``cdk destroy`` again.
 */
export class RetainLakeFormationPermissions implements IAspect {
  public visit(node: IConstruct): void {
    if (node instanceof CfnPermissions) {
      node.applyRemovalPolicy(RemovalPolicy.RETAIN);
      return;
    }
    // Belt-and-suspenders: also catch the raw L1 resource by CFN type, in case a
    // permission is created via a lower-level escape hatch rather than the L2/L1
    // ``CfnPermissions`` construct.
    if (node instanceof CfnResource && node.cfnResourceType === 'AWS::LakeFormation::Permissions') {
      node.applyRemovalPolicy(RemovalPolicy.RETAIN);
    }
  }
}
