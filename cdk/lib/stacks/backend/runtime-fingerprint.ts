import * as crypto from 'crypto';
import * as cdk from 'aws-cdk-lib';
import { IConstruct } from 'constructs';

/**
 * Computes a deterministic 16-char SHA-256 hex digest over everything that, if
 * it changes, makes CloudFormation update an AgentCore Runtime resource and
 * thereby RE-PUSH the runtime's base ``environmentVariables`` — which silently
 * strips the post-create env patch (``cloud.resource_id`` etc.) applied by the
 * CloudResourceIdHandler. The digest is the ``RuntimeFingerprint`` property on
 * that handler's CustomResource: when it flips, CFN re-invokes the handler and
 * the patch is re-applied. When it doesn't, the handler is correctly skipped.
 *
 * Two inputs are folded in, because BOTH reset the runtime env on update:
 *
 *  1. The FULL ``environmentVariables`` block (not just OTEL_* keys). Any env
 *     change — region, KB id, table name, OTEL setting — re-pushes the whole
 *     block and wipes the patch, so all of it must be in the digest.
 *  2. The container ``imageTag``. A code-only redeploy changes just the image
 *     (the env block is byte-identical), but updating the runtime's artifact
 *     still re-pushes the base env and strips the patch. OTEL-key-only digests
 *     missed this case entirely — it is the most common drift trigger, since
 *     agent code changes far more often than env vars.
 *
 * Token-bearing values (e.g. logGroup.logGroupName, imageTag source hashes) are
 * resolved through the stack's token resolver before hashing so unresolved
 * Token IDs — which are non-deterministic across synth invocations — never
 * reach the digest. Resolved values become stable CFN intrinsics (e.g.
 * {Ref: 'MetadataAgentLogGroupCA163C77'}) keyed off deterministic construct
 * logical IDs.
 *
 * A third input — the runtime's CUSTOM_JWT authorizer (allowedClients +
 * allowlistedHeaders) — is folded in because the CloudResourceId handler is now
 * the single authoritative full-replace caller that re-applies the authorizer.
 * An allowedClients/headers change must re-fire that handler, so it has to move
 * the digest. Pass an empty authorizer ({allowedClients: [], allowlistedHeaders:
 * []}) for IAM-fallback runtimes that have no authorizer.
 *
 * @param scope     Any construct in the stack (used only for the token resolver).
 * @param env       The runtime's complete environmentVariables block.
 * @param imageTag  The runtime artifact's container image tag.
 * @param authorizer The runtime's CUSTOM_JWT inputs (pre-sorted allowedClients +
 *                   allowlistedHeaders); empty arrays when no authorizer applies.
 */
export function runtimeFingerprint(
  scope: IConstruct,
  env: Record<string, string>,
  imageTag: string,
  authorizer: { allowedClients: string[]; allowlistedHeaders: string[] } = {
    allowedClients: [],
    allowlistedHeaders: [],
  }
): string {
  // Sort env keys so the digest is order-independent (object key order must not
  // change the fingerprint).
  const sortedEnv = Object.fromEntries(Object.entries(env).sort(([a], [b]) => a.localeCompare(b)));
  const resolved = cdk.Stack.of(scope).resolve({ env: sortedEnv, imageTag, authorizer });
  return crypto.createHash('sha256').update(JSON.stringify(resolved)).digest('hex').slice(0, 16);
}
