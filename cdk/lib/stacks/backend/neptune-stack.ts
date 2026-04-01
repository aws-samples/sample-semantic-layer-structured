import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as neptune from 'aws-cdk-lib/aws-neptune';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface NeptuneStackProps extends cdk.StackProps {
  projectName: string;
  vpc: ec2.Vpc;
}

/**
 * Neptune Stack
 * Creates Amazon Neptune cluster for RDF/OWL ontology storage
 * Supports SPARQL queries for semantic relationships
 */
export class NeptuneStack extends cdk.Stack {
  public readonly cluster: neptune.CfnDBCluster;
  public readonly clusterEndpoint: string;
  public readonly readerEndpoint: string;
  public readonly port: number = 8182;
  public readonly securityGroup: ec2.SecurityGroup;
  public readonly loadRole: iam.Role;
  public readonly neptuneLoaderRole: iam.Role; // Alias for consistency
  public readonly connectionSecret: secretsmanager.Secret;
  public readonly connectionSecretName: string;

  constructor(scope: Construct, id: string, props: NeptuneStackProps) {
    super(scope, id, props);

    // Security group for Neptune
    this.securityGroup = new ec2.SecurityGroup(this, 'NeptuneSecurityGroup', {
      vpc: props.vpc,
      description: 'Security group for Neptune cluster',
      allowAllOutbound: true,
    });

    // Allow access from within VPC
    this.securityGroup.addIngressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(this.port),
      'Allow Neptune access from VPC'
    );

    // Subnet group for Neptune
    const subnetGroup = new neptune.CfnDBSubnetGroup(this, 'NeptuneSubnetGroup', {
      dbSubnetGroupName: `${props.projectName}-neptune-subnet-group`,
      dbSubnetGroupDescription: 'Subnet group for Neptune cluster',
      subnetIds: props.vpc.isolatedSubnets.map((subnet) => subnet.subnetId),
    });

    // IAM role for Neptune to load data from S3
    this.loadRole = new iam.Role(this, 'NeptuneS3LoadRole', {
      assumedBy: new iam.ServicePrincipal('rds.amazonaws.com'),
      description: 'Role for Neptune to load RDF data from S3',
    });

    this.loadRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [`arn:aws:s3:::${props.projectName}-*`, `arn:aws:s3:::${props.projectName}-*/*`],
      })
    );

    // Parameter group for Neptune
    const parameterGroup = new neptune.CfnDBClusterParameterGroup(this, 'NeptuneParameterGroup', {
      family: 'neptune1.4', // Must match engine version 1.4.x
      description: 'Parameter group for semantic layer Neptune cluster',
      parameters: {
        neptune_enable_audit_log: '1',
        neptune_query_timeout: '120000',
      },
      name: `${props.projectName}-neptune-params`,
    });

    // Neptune cluster
    this.cluster = new neptune.CfnDBCluster(this, 'NeptuneCluster', {
      dbClusterIdentifier: `${props.projectName}-neptune-cluster`,
      engineVersion: '1.4.1.0', // Latest stable Neptune version
      dbSubnetGroupName: subnetGroup.dbSubnetGroupName,
      vpcSecurityGroupIds: [this.securityGroup.securityGroupId],
      iamAuthEnabled: true,
      storageEncrypted: true,
      backupRetentionPeriod: 7,
      preferredBackupWindow: '01:00-02:00',
      preferredMaintenanceWindow: 'sun:03:00-sun:04:00',
      dbClusterParameterGroupName: parameterGroup.ref,
      associatedRoles: [
        {
          roleArn: this.loadRole.roleArn,
        },
      ],
      enableCloudwatchLogsExports: ['audit'],
    });

    this.cluster.addDependency(subnetGroup);
    this.cluster.addDependency(parameterGroup);

    // Primary instance
    const primaryInstance = new neptune.CfnDBInstance(this, 'NeptunePrimaryInstance', {
      dbInstanceClass: 'db.r6g.xlarge',
      dbClusterIdentifier: this.cluster.ref,
      dbInstanceIdentifier: `${props.projectName}-neptune-instance-1`,
    });

    primaryInstance.addDependency(this.cluster);

    // Reader instance for high availability
    const readerInstance = new neptune.CfnDBInstance(this, 'NeptuneReaderInstance', {
      dbInstanceClass: 'db.r6g.large',
      dbClusterIdentifier: this.cluster.ref,
      dbInstanceIdentifier: `${props.projectName}-neptune-instance-2`,
    });

    readerInstance.addDependency(this.cluster);

    this.clusterEndpoint = this.cluster.attrEndpoint;
    this.readerEndpoint = this.cluster.attrReadEndpoint;
    this.neptuneLoaderRole = this.loadRole; // Alias for consistency

    // Store connection details in Secrets Manager
    this.connectionSecret = new secretsmanager.Secret(this, 'NeptuneConnectionSecret', {
      secretName: `${props.projectName}/neptune/connection`,
      description: 'Neptune cluster connection details',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          endpoint: this.clusterEndpoint,
          readerEndpoint: this.readerEndpoint,
          port: this.port,
          sparqlEndpoint: `https://${this.clusterEndpoint}:${this.port}/sparql`,
          loaderEndpoint: `https://${this.clusterEndpoint}:${this.port}/loader`,
          loaderRoleArn: this.loadRole.roleArn,
        }),
        generateStringKey: 'dummy', // Required but not used
      },
    });

    this.connectionSecretName = this.connectionSecret.secretName;

    // Outputs
    new cdk.CfnOutput(this, 'NeptuneClusterEndpoint', {
      value: this.clusterEndpoint,
      description: 'Neptune cluster endpoint',
      exportName: `${props.projectName}-neptune-endpoint`,
    });

    new cdk.CfnOutput(this, 'NeptuneReaderEndpoint', {
      value: this.readerEndpoint,
      description: 'Neptune reader endpoint',
      exportName: `${props.projectName}-neptune-reader-endpoint`,
    });

    new cdk.CfnOutput(this, 'NeptuneSparqlEndpoint', {
      value: `https://${this.clusterEndpoint}:${this.port}/sparql`,
      description: 'Neptune SPARQL query endpoint',
    });

    new cdk.CfnOutput(this, 'NeptuneLoaderEndpoint', {
      value: `https://${this.clusterEndpoint}:${this.port}/loader`,
      description: 'Neptune bulk loader endpoint',
    });

    new cdk.CfnOutput(this, 'NeptuneSecurityGroupId', {
      value: this.securityGroup.securityGroupId,
      description: 'Neptune security group ID',
    });

    // Secret name is stored in Secrets Manager — do not expose as CFN output
    // Use: const secret = secretsManager.Secret.fromSecretNameV2(stack, 'secret', secretName)
    // ref: this.connectionSecretName (programmatic access only, not CFN output)

    new cdk.CfnOutput(this, 'NeptuneLoadRoleArn', {
      value: this.loadRole.roleArn,
      description: 'IAM role for Neptune S3 bulk load',
      exportName: `${props.projectName}-neptune-load-role`,
    });
  }
}
