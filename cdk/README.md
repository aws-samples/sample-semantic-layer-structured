# CDK — infrastructure for the semantic layer

TypeScript AWS CDK app. `bin/app.ts` is the entry point; stacks live under
`lib/stacks/{backend,frontend}`. See the root [`README.md`](../README.md) for what the
platform does and how the runtime pieces fit together — this file covers deploying it.

## Everyday commands

| Command                         | What it does                                                               |
| ------------------------------- | -------------------------------------------------------------------------- |
| `npm run build`                 | `tsc`                                                                      |
| `npm test`                      | Jest (`__tests__/`)                                                        |
| `npm run synth`                 | `cdk synth`                                                                |
| `npm run diff`                  | `cdk diff`                                                                 |
| `npm run deploy`                | `cdk deploy --all`                                                         |
| `npm run destroy`               | `cdk destroy --all`                                                        |
| `npm run deploy:snowflake`      | the Snowflake deploy **plus** the secret sync and the casing fix, in order |
| `npm run sync:snowflake-secret` | writes Snowflake credentials from `.env` into Secrets Manager              |
| `npm run set:snowflake-casing`  | re-applies `casing_mode=UPPER` to every deployed connector Lambda          |

Both Snowflake post-deploy steps are chained into `deploy:snowflake` deliberately: each is
required after _every_ connector deploy and each fails in a way that looks like something
else (a placeholder credential reads as "missing username"; missing casing reads as "table
not found"). Running them by hand from a checklist is how that gets forgotten — see
[§6](#6-operational-notes-and-traps).

`npm run predeploy` additionally runs the Python unit tests from the repo root.

One Snowflake step lives outside npm, because it runs against Snowflake rather than AWS:
`scripts/snowflake/bootstrap_connector.py` creates the connector's Snowflake role and user
and registers its public key — see [§3](#3-bootstrap-the-snowflake-side-cdk-cannot-do-this).

## Feature flags

Flags are CDK **context**. Their defaults live in `cdk.json`'s `context` block; a
`-c key=value` on the command line overrides that default for one invocation.

Two flags are documented here, because they are the two with a deployment trap
attached. The **full** flag table — `enableOntologyAgents`, `enableSemanticRag`,
`enableHybrid`, `enableOboPassthrough`, the replication flags and the rest — lives in the root
[`README.md`](../README.md#deployment-modes) under _Deployment Modes_.

| Flag                       | Default               | Effect                                                                                               |
| -------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------- |
| `enableSnowflakeConnector` | **`true`** (cdk.json) | deploys one Athena Snowflake connector and one `snowflake_<db>` data catalog per configured database |
| `enableAuthz`              | **`false`** (not set) | arms the Cedar policy-decision point in the REST API so its decisions actually deny                  |

The two traps are opposites, which is why both are here: `enableSnowflakeConnector`
**deletes infrastructure** when you forget it, and `enableAuthz` **disables a security
control** when you forget it. One fails a deploy loudly; the other succeeds quietly.

### `enableSnowflakeConnector` — the deletion trap

When off, no connector, secret, or catalog is created at all — the resources are absent
from the template rather than present-and-disabled.

**The connector is ON BY DEFAULT (since 2026-08-14), so a plain `cdk deploy` keeps it.**
It used to default to `false`, which made every deploy a trap: because the resources are
conditional on context rather than disabled by it, a deploy without `-c
enableSnowflakeConnector=true` did not leave an existing connector alone — it synthesized a
template in which the connector, secret, spill bucket, role, SAR app, data catalog and
Lambda permission had all **ceased to exist**, and CloudFormation dutifully deleted them.
That included deploys of a stack you thought was unrelated: `cdk deploy
semantic-layer-dev-agentcore` queued the whole connector for deletion. It usually failed
partway with `Cannot delete export
semantic-layer-dev-athena:ExportsOutputFnGetAtt…SpillBucket…Arn… as it is in use by
semantic-layer-dev-agentcore`, leaving the athena stack in `UPDATE_ROLLBACK_COMPLETE` — an
error that names an export, not the missing flag. Pinning the default in `cdk.json`
removes that footgun; `bin/__tests__/app-flags.test.ts` fails if it flips back.

Two consequences of the default being on:

- **`SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_WAREHOUSE` in `cdk/.env` are now required for every
  deploy.** Without them synth throws `enableSnowflakeConnector is on but snowflakeAccount /
snowflakeWarehouse context is not set`. Since `.env` is gitignored, that also means a
  fresh clone or a git worktree cannot synth until you create it (see `cdk/.env.example`).
  This is deliberate: a loud, self-describing error beats silently deleting live resources.
- **To deploy without the connector, pass `-c enableSnowflakeConnector=false`.** CLI context
  overrides the `cdk.json` default (verified: 0 vs 9 Snowflake resources in the synthesized
  Athena template).

`npm run deploy:snowflake` still works and still passes the flag explicitly; it is now
belt-and-braces rather than the only safe path.

### `enableAuthz` — the mirror-image trap

`enableAuthz` is the one flag deliberately **not** pinned in `cdk.json`, and for the
opposite reason: it can lock you out of the very API you would use to unlock yourself. It
does not gate whether the Cedar policy-decision point is deployed — that is installed
whenever the REST API has a `NAMESPACES_TABLE`, and it evaluates and logs a decision on
every request either way. The flag only decides whether a `Deny` is enforced. So the safe
rollout is two passes: deploy dark, read the decision logs until your own roles look right,
then deploy again with `-c enableAuthz=true`.

The trap is the return leg. Because the flag lives only on the command line, **any later
deploy that omits `-c enableAuthz=true` silently disarms enforcement** — the API keeps
serving, every request keeps being evaluated, and nothing fails, so there is no error to
notice. Unlike the Snowflake case, CloudFormation deletes nothing and the stack reports
success; the only symptom is that requests which should have been denied are not. Put the
flag in whatever wrapper you deploy with rather than typing it, and see the root README's
[Namespaces, Authorization & the SQL Firewall](../README.md#namespaces-authorization--the-sql-firewall)
for the roles and the grants table.

## Snowflake Athena connector

Federated Athena queries against Snowflake, via the AWS-published
`AthenaSnowflakeConnector` Serverless Application Repository app, running as a Lambda
**outside** the VPC on Lambda's managed egress ([§6](#6-operational-notes-and-traps)
explains why, and why moving it back is harder to undo than it looks).

**One connector per database.** A connector's `DefaultConnectionString` pins exactly one
`db=`, so exposing N databases means N Lambdas and N catalogs. They share the execution
role, the spill bucket (separate `SpillPrefix` per database) and the one credentials
secret. Catalog names are derived from the database — `FHIR_DB` → `snowflake_fhir_db` —
so a second database cannot collide with the first.

Configuration is split in two, and the split is the security boundary:

- **Non-secret identifiers** — read by `cdk synth`/`cdk deploy` from `cdk/.env`, and
  overridable with `-c key=value` (context wins).
- **Credentials** — read _only_ by `npm run sync:snowflake-secret`, which writes them
  straight to Secrets Manager. They are never read during synth, so no credential can
  reach a CloudFormation template.

That second point is the whole reason a separate sync script exists. A literal secret
handed to a CDK `Secret` construct is written verbatim into the synthesized template,
which lands in `cdk.out/` and in the CloudFormation-stored template — readable by anyone
with `cloudformation:GetTemplate`, a much wider audience than `secretsmanager:GetSecretValue`
on one ARN. So **CDK owns the secret resource; the sync script owns its value.** The
deployed template contains only a placeholder document until you run the sync.

### 1. Non-secret config

Copy the template and fill it in. `cdk/.env` is gitignored — never commit it.

```bash
cp cdk/.env.example cdk/.env
```

| Key                     | Required | Notes                                                                                       |
| ----------------------- | -------- | ------------------------------------------------------------------------------------------- |
| `SNOWFLAKE_ACCOUNT`     | yes      | e.g. `myorg-myacct`; no default, deploy fails without it                                    |
| `SNOWFLAKE_WAREHOUSE`   | yes      | virtual warehouse for federated queries                                                     |
| `SNOWFLAKE_DATABASE`    | no       | a single database; defaults to `FHIR_DB`                                                    |
| `SNOWFLAKE_DATABASES`   | no       | comma-separated list, one connector + catalog each, e.g. `FHIR_DB,CLINICAL_CSV`             |
| `SNOWFLAKE_ROLE`        | no       | defaults to `ATHENA_CONNECTOR_ROLE`; use the narrowest role that can read the target schema |
| `SNOWFLAKE_SECRET_NAME` | no       | defaults to `semantic-layer/snowflake/connector`                                            |

Setting **both** database keys is an error, not a precedence rule: silently preferring one
would deploy a catalog set nobody asked for. `SNOWFLAKE_DATABASES` supersedes the singular
key — use it for one database as easily as for several. Whitespace and a trailing comma are
tolerated, duplicates are collapsed, and an entry list that resolves to nothing fails at
synth.

Connector function names are `<project>-snowflake-<db-slug>-connector`, and the SAR
template constrains them to `^[a-z0-9-_]{1,64}$`. A name that would violate the pattern
fails at synth naming the database that produced it, rather than deep inside the nested
stack where the message names only the pattern.

### 2. Credentials

Pick **exactly one** auth mode. Setting both, or neither, is an error rather than a
guess — guessing would deploy a credential you did not intend and surface later as an
opaque auth failure in the connector's CloudWatch logs.

**Key-pair (preferred).** `SNOWFLAKE_USER` plus `SNOWFLAKE_PRIVATE_KEY`, and
`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` only if the key is encrypted.

The private key must be on **one physical line**, in double quotes, with `\n` for line
breaks. The `.env` parser is line-based, so a pasted multi-line PEM silently captures only
its first line. Generate the line from a key file rather than editing by hand:

```bash
python3 - path/to/rsa_key.p8 <<'PY'
import sys
pem = open(sys.argv[1]).read().strip()
print('SNOWFLAKE_PRIVATE_KEY="' + pem.replace('\n', '\\n') + '"')
PY
```

**Password (fallback).** `SNOWFLAKE_USER` plus `SNOWFLAKE_PASSWORD`, instead of the
private key — not alongside it.

**Never put a trailing comment on a value line.** The parser splits on the first `=` and
keeps the remainder verbatim, comment included, so

```
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=hunter2  # my passphrase
```

is a 22-character passphrase, not a 7-character one. Values are single-line only and
comments belong on their own line. This has already cost one debugging cycle: the payload
builder rejected the key as "encrypted key with wrong passphrase" — correctly, since the
passphrase really was wrong.

Then write the values to Secrets Manager:

```bash
npm run sync:snowflake-secret
```

Flags: `--env-path`, `--secret-name`, `--region`. The path flag is deliberately **not**
`--env-file` — Node 20 owns that name and consumes it before the script ever sees it, so
the script rejects it with a pointer to `--env-path`.

The builder validates the key before any AWS call and names the specific problem: a
public key pasted in place of the private half, a missing PEM header or footer, an
implausibly short body, an encrypted key with no passphrase, or a passphrase supplied for
an unencrypted key. Every one of those otherwise surfaces only as a decrypt or
JWT-signing failure in the connector's logs, far from the cause.

### 3. Bootstrap the Snowflake side — CDK cannot do this

**No CDK equivalent exists.** `CREATE USER` / `ALTER USER … SET RSA_PUBLIC_KEY` execute
inside Snowflake against Snowflake's own user store: there is no CloudFormation resource
for them, no Snowflake provider in this app, and the connector's own credentials
deliberately cannot alter users. CDK owns the AWS half (secret, connector, catalog); the
sync script owns the secret's value; **nothing in CDK owns the Snowflake user.**

It is, however, scriptable. `scripts/snowflake/bootstrap_connector.py` executes the
statements in `docs/setup_athena_connector.sql` as an ACCOUNTADMIN-capable user, then
proves the result by re-connecting as the connector's own service user over the exact
auth path the Lambda uses.

**Generate a dedicated, unencrypted service key.** Unencrypted is correct here: the
private half lives only in Secrets Manager, so a passphrase adds a second secret without
adding protection — and the connector cannot prompt for one. Do not reuse a human user's
key.

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
  -out docs/athena_connector.p8 -outform PEM
chmod 600 docs/athena_connector.p8
openssl rsa -in docs/athena_connector.p8 -pubout -out docs/athena_connector.pub
# Snowflake wants the bare base64 body — no armor, no newlines:
grep -v 'KEY-----' docs/athena_connector.pub | tr -d '\n'
# Fingerprint, to compare against RSA_PUBLIC_KEY_FP in §4:
openssl pkey -in docs/athena_connector.p8 -pubout -outform DER \
  | openssl dgst -sha256 -binary | openssl enc -base64
```

`docs/` is gitignored, so neither half is committable. Write the SQL to
`docs/setup_athena_connector.sql`:

```sql
USE ROLE ACCOUNTADMIN;
CREATE ROLE IF NOT EXISTS ATHENA_CONNECTOR_ROLE;
-- TYPE = SERVICE forbids password login outright, so key-pair auth is the only way in
-- and the account's MFA policy does not apply. Drop the line if your edition rejects it.
CREATE USER IF NOT EXISTS ATHENA_CONNECTOR
  TYPE = SERVICE
  DEFAULT_ROLE = 'ATHENA_CONNECTOR_ROLE'
  DEFAULT_WAREHOUSE = '<WAREHOUSE>';
ALTER USER ATHENA_CONNECTOR SET RSA_PUBLIC_KEY = '<bare base64 body>';
GRANT ROLE ATHENA_CONNECTOR_ROLE TO USER ATHENA_CONNECTOR;
-- Without warehouse USAGE the connector authenticates but every query fails.
GRANT USAGE ON WAREHOUSE <WAREHOUSE> TO ROLE ATHENA_CONNECTOR_ROLE;
-- Athena metadata calls list schemas and tables, so USAGE is required, not just SELECT.
GRANT USAGE ON DATABASE <DB> TO ROLE ATHENA_CONNECTOR_ROLE;
GRANT USAGE ON SCHEMA <DB>.<SCHEMA> TO ROLE ATHENA_CONNECTOR_ROLE;
GRANT SELECT ON ALL TABLES IN SCHEMA <DB>.<SCHEMA> TO ROLE ATHENA_CONNECTOR_ROLE;
-- FUTURE covers tables created later; without it, new tables are invisible.
GRANT SELECT ON FUTURE TABLES IN SCHEMA <DB>.<SCHEMA> TO ROLE ATHENA_CONNECTOR_ROLE;
GRANT SELECT ON ALL VIEWS IN SCHEMA <DB>.<SCHEMA> TO ROLE ATHENA_CONNECTOR_ROLE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA <DB>.<SCHEMA> TO ROLE ATHENA_CONNECTOR_ROLE;
```

**Why a second credential is needed, once.** Key-pair auth is chicken-and-egg: registering
the public key requires an already-authenticated session, and the only local credential
_is_ the key awaiting registration. Add an ACCOUNTADMIN-capable user and password to
`cdk/.env` to break the cycle. Both keys are read only by this script — the CDK app maps
only the five non-secret `SNOWFLAKE_*` values and excludes these from `SECRET_ENV_KEYS`,
so they reach neither a template nor the connector's secret. Never pass them as flags:
argv is visible in `ps` and lands in shell history.

```
SNOWFLAKE_ADMIN_USER=<a user holding ACCOUNTADMIN>
SNOWFLAKE_ADMIN_PASSWORD=<its password>
```

The Snowflake driver is not a project dependency. Install it into a throwaway venv:

```bash
python3 -m venv /tmp/sfvenv
/tmp/sfvenv/bin/pip install snowflake-connector-python cryptography
/tmp/sfvenv/bin/python scripts/snowflake/bootstrap_connector.py --dry-run   # list statements
/tmp/sfvenv/bin/python scripts/snowflake/bootstrap_connector.py             # run + verify
/tmp/sfvenv/bin/python scripts/snowflake/bootstrap_connector.py --verify-only
```

Success prints the service login's resolved session context, e.g.
`service login OK: ('ATHENA_CONNECTOR', 'ATHENA_CONNECTOR_ROLE', '<WAREHOUSE>', '<DB>')`,
followed by the tables the role can see. That is the real proof: it is the same auth path
the Lambda takes.

**Delete `SNOWFLAKE_ADMIN_*` from `.env` once this succeeds.** They are not needed again
except for key rotation (§5), which is two-sided anyway.

If the account enforces MFA on password login the driver will stall or be refused; in that
case run the SQL by hand in a Snowsight browser/SSO session instead. Everything after §3
is unchanged either way.

### 4. Verify the registered key

Quick boolean — the `has_rsa_public_key` column:

```sql
SHOW USERS LIKE 'ATHENA_CONNECTOR';
```

Rigorous, and worth preferring: this proves _your_ key is the one registered, not merely
that some key is. Compare Snowflake's stored fingerprint against the local public key.

```sql
DESC USER ATHENA_CONNECTOR;
SELECT "property", "value"
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "property" IN ('RSA_PUBLIC_KEY', 'RSA_PUBLIC_KEY_FP', 'RSA_PUBLIC_KEY_2_FP');
```

```bash
# Derive the fingerprint from the PRIVATE key, so a match also proves the pair agrees.
openssl pkey -in rsa_key.p8 -pubout -outform DER \
  | openssl dgst -sha256 -binary | openssl enc -base64
```

Snowflake reports `RSA_PUBLIC_KEY_FP` as `SHA256:<base64>`. A mismatch means a
_different_ key is registered, which is the common state after regenerating a key.

**`390144 (28000): JWT token is invalid` tells you nothing about which side is wrong.**
Snowflake returns byte-identical text for an unknown user, a user with no registered key,
and a user with a _different_ key. Before theorising, attempt a login with an invented
username against the same account — `bootstrap_connector.py --verify-only` with
`SNOWFLAKE_USER=NO_SUCH_USER_XYZ123` is enough. An identical error proves the message
carries no user-existence signal, so the only sound conclusion is "no registered key
matched" — suspect nothing is registered rather than that something is stale. Reaching
this error at all is good news: it proves egress, Secrets Manager retrieval, key
decryption, and JWT signing all work.

### 5. Key rollover is a two-sided operation

Rotating a key means **both**:

1. `ALTER USER … SET RSA_PUBLIC_KEY='<new body>'` in Snowflake, and
2. update `cdk/.env` and re-run `npm run sync:snowflake-secret`.

Doing only one breaks authentication. No `cdk deploy` is needed for a rollover — the
secret resource does not change, only its value.

### 6. Operational notes and traps

**The connector must stay OUT of the VPC, and putting it back is not reversible in place.**
It needs nothing inside the VPC — only outbound HTTPS to Snowflake, Secrets Manager and
STS — and Snowflake stages query results in an S3 bucket **in Snowflake's own region**
(`us-west-2` here, not the deployment region) that the JDBC driver must reach directly.
Measured on this stack, same query, same data, only placement differing:

| Operation                          | In private subnets    | Out of the VPC |
| ---------------------------------- | --------------------- | -------------- |
| Cold start                         | 150–190 s             | 12.7 s         |
| `SELECT COUNT(*)` over 10,969 rows | 894 s, then cancelled | 15.2 s         |
| Full table scan                    | never completed       | 19.2 s         |

In-VPC failures surfaced as `NoHttpResponseException … sfc-*-customer-stage.s3.*` and bare
Athena `504`s — neither of which names placement as the cause.

> **TRAP if you ever revert this.** Emptying `SubnetIds`/`SecurityGroupIds` on an
> **existing** connector does **not** detach it. The SAR template guards both parameters
> behind `Fn::If … AWS::NoValue`, and CloudFormation treats an absent `VpcConfig` on an
> _update_ as "leave the current attachment alone". Placement only changes when the
> function is **replaced** — which a new `LambdaFunctionName` forces. Confirm the real
> state from CloudFormation rather than the template: `"Vpc": null` is the answer you want.

The trade-off, for the record: a NAT gateway gives a stable egress IP that a Snowflake
network policy can allow-list, and Lambda's managed egress does not. If such a policy is in
play, allow-list differently rather than moving the connector back in.

The historical failure here was different and is now moot: attaching the connector to
`vpc.vpcDefaultSecurityGroup` left it with no outbound path at all, because CDK v2 enables
`@aws-cdk/aws-ec2:restrictDefaultSecurityGroup`, which strips every ingress _and_ egress
rule from that group while the synthesized template still looks valid. Out of the VPC there
is no security group to get wrong, and the connector creates no ENIs — so the
`ec2:*NetworkInterface` grants (which can only be scoped to `*`) are gone too. Tests assert
all three: no `SubnetIds`, no `SecurityGroupIds`, no ENI actions.

**Any SAR nested-stack update resets the connector Lambda's environment map**, dropping
`casing_mode=UPPER`. Snowflake identifiers are uppercase and Athena lowercases them before
handing them to a federated connector, so without it every lookup fails as a plain "table
not found" — a message that points at the data, not at the configuration. The SAR template
exposes no parameter for this, so it can only be set post-deploy. `npm run
set:snowflake-casing` does it for every deployed connector and `deploy:snowflake` chains
it; run it by hand after any `--exclusively` athena-stack deploy.

It merges rather than sets, because `update-function-configuration --environment`
**replaces** the whole variables map — a naive `{casing_mode: UPPER}` discards the
connection string and every query then fails on a missing default. To inspect the live map:

```bash
aws lambda get-function-configuration \
  --function-name semantic-layer-dev-snowflake-fhir-db-connector \
  --query 'Environment.Variables'
```

**A cold connector outruns Athena's metadata timeout.** Out of the VPC the cold start is
~13 s rather than the 150–190 s measured in-VPC, but Athena's metadata budget is tighter
still, so a first `aws athena list-databases` can return a bare `504` with no cause. Warm
the Lambda directly first, which also bypasses Athena entirely and shows the connector's
own reply:

```bash
aws lambda invoke --function-name semantic-layer-dev-snowflake-fhir-db-connector \
  --payload "$(base64 -w0 <<'JSON'
{"@type":"ListSchemasRequest","identity":{"id":"unknown","principal":"unknown","account":"unknown"},
 "catalogName":"snowflake_fhir_db","queryPassthroughArguments":{}}
JSON
)" /tmp/listschemas.json
```

`GetTableRequest` needs the same `queryPassthroughArguments` key; omitting it fails with
`queryPassthroughArguments is null` rather than a validation message naming the field.

**A query the connector cannot push down streams every row, against a hard 900 s ceiling.**
Athena pushes down projections and simple predicates, but **not aggregation and not
`LIMIT`**. So `SELECT COUNT(DISTINCT "resource") FROM …` becomes, in the connector's own log,
`Generated SQL : SELECT "RESOURCE" FROM "FHIR_R4"."FHIR_RESOURCES"` — every row of the JSON
document column pulled through JDBC into a Lambda capped at 900 s and 3008 MB. Wrapping the
scan in an inner `LIMIT` does **not** help: it is applied after the connector returns, so the
generated SQL is unchanged.

That ceiling is reached, not approached. The give-away in CloudWatch is
`REPORT … Duration: 900000.00 ms … Status: timeout`, one per split, while the Athena query
just sits in `RUNNING`. Keep federated queries to shapes the connector can satisfy — the
narrow projections, predicates and small result sets in [§8](#8-verified-status-read-path-works)
all return in 8–27 s — and never full-scan the `resource` column.

> This is also why the ontology build's two enrichment probes have to be bounded on a
> federated catalog: primary-key discovery (`COUNT(DISTINCT col)` over every column) and enum
> discovery (`GROUP BY col`) are both full-table aggregations, and the build fires them per
> table concurrently. Unbounded, they saturate the connector and the build dies mid-refinement.

**Cancelling an Athena query does not stop the connector invocations it already started.**
Each one keeps running to its own 900 s timeout, holding concurrency the whole time. So a
handful of abandoned scans leaves dozens of zombie invocations that congest every _later_
query for up to 15 minutes, and the symptom is not a timeout — it is unrelated queries
hanging, or failing with `TABLE_NOT_FOUND: Table 'snowflake_fhir_db.fhir_r4.medications' does
not exist`, which reads as a schema error and is really back-pressure. Two consequences:

- After cancelling anything heavy, **wait out the drain** before concluding the connector is
  broken. Watch `Duration` fall back to single-digit seconds:

  ```bash
  aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration \
    --dimensions Name=FunctionName,Value=semantic-layer-dev-snowflake-fhir-db-connector \
    --start-time "$(date -u -d '30 min ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
    --period 300 --statistics Maximum
  ```

  A `Maximum` of `900000.0` means zombies are still resident; `Throttles` stays at `0`
  throughout, so throttling metrics will not reveal this.

- Any client that gives up on a federated query should call `athena:StopQueryExecution`
  rather than just abandoning it. Not doing so is what turns one slow query into a
  self-inflicted outage of the next dozen.

### 7. Troubleshooting

Probe the connector end to end:

```bash
aws athena list-databases --catalog-name snowflake_fhir_db
```

The failure mode tells you which layer is broken — they are easy to confuse because the
caller-visible error is often three levels removed from the cause.

| Symptom                                                                                                          | Cause                                                                                                                                                                                                                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Missing required parameter: username/sfUser`                                                                    | the secret still holds the CDK placeholder; run `npm run sync:snowflake-secret`                                                                                                                                                                                                                                 |
| sync refuses before any AWS call                                                                                 | the `.env` credential is malformed; the error names the specific problem                                                                                                                                                                                                                                        |
| decrypt failure in the connector logs                                                                            | encrypted key with a wrong or missing passphrase                                                                                                                                                                                                                                                                |
| JWT / auth rejection from Snowflake                                                                              | the public key is not registered on the user, or a different one is — see §4                                                                                                                                                                                                                                    |
| table or schema not found                                                                                        | `casing_mode` missing — a SAR update reset it; run `npm run set:snowflake-casing`, see §6                                                                                                                                                                                                                       |
| `504` on the _first_ call, success on a retry                                                                    | cold start, not a fault — warm the Lambda directly first, see §6                                                                                                                                                                                                                                                |
| `CATALOG_NOT_FOUND` from an agent, not from the CLI                                                              | the calling role lacks the `athena:GetDataCatalog` family; reads as a missing catalog, is a missing grant                                                                                                                                                                                                       |
| `SELECT` hangs for minutes; logs show `NoHttpResponseException … sfc-*-customer-stage.s3.*`                      | the connector is attached to a VPC and cannot reach Snowflake's result stage — see §6                                                                                                                                                                                                                           |
| `Connect to secretsmanager…:443 failed: connect timed out`                                                       | same root cause: an in-VPC connector with no working egress. The fix is placement, not a security-group rule                                                                                                                                                                                                    |
| aggregation query sits in `RUNNING`; logs show `Duration: 900000.00 ms … Status: timeout`                        | an un-pushed-down full scan hit the Lambda ceiling — reshape the query, do not raise the client timeout, see §6                                                                                                                                                                                                 |
| _previously fine_ queries now hang or return `TABLE_NOT_FOUND`                                                   | zombie invocations from an earlier cancelled scan are still holding concurrency; wait for the drain, see §6                                                                                                                                                                                                     |
| `AccessDenied` on `lambda:InvokeFunction` for the connector, from an agent role                                  | Athena invokes the connector as the **query's own** principal, so every querying role needs invoke on it plus read/write on the spill bucket — not only the connector's own role                                                                                                                                |
| `Cannot delete export …SpillBucket…Arn… as it is in use by`                                                      | the deploy is deleting the connector because it synthesized with the flag off — check `cdk.json` still has `enableSnowflakeConnector: true` and that no `-c …=false` was passed                                                                                                                                 |
| `enableSnowflakeConnector is on but snowflakeAccount / snowflakeWarehouse context is not set`                    | `cdk/.env` is missing (fresh clone / git worktree — it is gitignored) or lacks the two required keys; see `cdk/.env.example`                                                                                                                                                                                    |
| `422 smoke execution of the compiled rule failed: … AccessDeniedException … StartQueryExecution` on rule publish | the REST API role is missing Athena _execution_ rights, or `ATHENA_WORKGROUP` does not match the workgroup the grant is scoped to. Reads as a bad rule, is a missing grant — the publish gate runs a real `LIMIT 1`, so the caller also needs the results bucket and the spill buckets. See `CDK_README.md` §12 |

The connector's own CloudWatch log group is the only place the real error appears; the
Athena API response usually does not carry it. Snowflake's side is worth checking
independently, since it distinguishes "the query never arrived" from "the query ran and the
result could not be shipped":

```sql
SELECT query_text, execution_status, total_elapsed_time
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(USER_NAME => 'ATHENA_CONNECTOR'))
ORDER BY start_time DESC;
```

### 8. Verified status — read path works

**Resolved 2026-08-12.** The read path failed for as long as the connector ran inside the
VPC: `SELECT COUNT(*)` never completed, and Snowflake's `QUERY_HISTORY_BY_USER` showed only
the `getSchemas()` metadata calls — the `SELECT` never reached Snowflake at all, dying
instead against its cross-region S3 result stage
(`NoHttpResponseException … sfc-ds1-customer-stage.s3.us-west-2.amazonaws.com`). Moving the
connector out of the VPC fixed it; [§6](#6-operational-notes-and-traps) has the measurements
and the revert trap.

Verified live against `"snowflake_fhir_db"."FHIR_R4"."PATIENTS"` (10,969 rows):

| Query                                   | Result                             | Time   |
| --------------------------------------- | ---------------------------------- | ------ |
| `SELECT COUNT(*)`                       | 10,969                             | 27.3 s |
| `GROUP BY "gender"`                     | female 5,539 + male 5,430 = 10,969 | 8.0 s  |
| `SELECT *`, all pages                   | 10,969 rows over 11 result pages   | 22.1 s |
| projection + `WHERE`/`ORDER BY`/`LIMIT` | 3 rows                             | 8.0 s  |

The first figure includes the cold start; subsequent queries are warm. Two things to know
when reproducing this:

- **Paginate `get-query-results`.** One call truncates at ~1000 rows, so a full scan looks
  small and correct rather than truncated.
- **The `resource` column holds raw FHIR JSON with literal control characters**, which the
  AWS CLI emits verbatim into its JSON output and breaks strict parsers. Read results with
  an SDK client, or parse non-strictly.
